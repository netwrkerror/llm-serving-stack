"""Bedrock request/response mapping, and tier-to-backend resolution.

These prove the *mapping*, not the contract. No AWS call has been made from this
repo — there were no credentials available — so the Converse event shapes below are
written from its documented contract and could be wrong in ways a unit test cannot
see. RESULTS.md records the Bedrock measurements as untested for the same reason.

Driven with asyncio.run rather than pytest-asyncio, matching tests/test_cancel.py.
"""
import asyncio

import pytest

from gateway import main
from gateway.backend import BedrockBackend, _converse_usage, _to_converse, make_backend
from gateway.config import TierConfig, parse_tier_spec, settings
from gateway.router import resolve_route
from gateway.schemas import ChatCompletionRequest, ChatMessage


def req(*, model="m", messages=None, max_tokens=120, **kw) -> ChatCompletionRequest:
    if messages is None:
        messages = [ChatMessage(role="user", content="hello")]
    return ChatCompletionRequest(model=model, messages=messages, max_tokens=max_tokens, **kw)


# --- request mapping ------------------------------------------------------------------

def test_system_messages_go_to_system_not_messages():
    """Converse rejects a "system" role inside messages."""
    args = _to_converse(req(messages=[
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]))

    assert args["system"] == [{"text": "be terse"}]
    assert [m["role"] for m in args["messages"]] == ["user", "assistant"]
    assert args["messages"][0]["content"] == [{"text": "hi"}]


def test_system_key_absent_when_there_is_no_system_message():
    """An empty system list is not the same as omitting it."""
    assert "system" not in _to_converse(req())


def test_inference_config_carries_the_sampling_params():
    args = _to_converse(req(max_tokens=99, temperature=0.3, top_p=0.9))
    assert args["inferenceConfig"] == {"maxTokens": 99, "temperature": 0.3, "topP": 0.9}


def test_max_tokens_none_falls_back_to_the_same_default_as_other_backends():
    args = _to_converse(req(max_tokens=None))
    assert args["inferenceConfig"]["maxTokens"] == 512


def test_model_id_is_passed_through_untouched():
    """Bedrock ids contain dots and colons; nothing may rewrite them."""
    model = "us.meta.llama3-2-1b-instruct-v1:0"
    assert _to_converse(req(model=model))["modelId"] == model


# --- usage mapping --------------------------------------------------------------------

def test_converse_usage_is_translated_to_openai_keys():
    assert _converse_usage({"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}) == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
    }


def test_converse_usage_totals_itself_when_total_is_absent():
    assert _converse_usage({"inputTokens": 10, "outputTokens": 5})["total_tokens"] == 15


def test_converse_usage_empty_when_counts_are_missing():
    """Callers must tolerate an absent usage block, as with the other backends."""
    assert _converse_usage({}) == {}
    assert _converse_usage({"inputTokens": 10}) == {}


# --- streaming ------------------------------------------------------------------------

class FakeEventStream:
    """The async iterator Bedrock's converse_stream puts under response["stream"]."""

    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def close(self):
        self.closed = True


class FakeBedrockClient:
    def __init__(self, events=None, response=None):
        self._events = events or []
        self._response = response or {}
        self.stream = None
        self.calls = []

    async def converse(self, **kwargs):
        self.calls.append(("converse", kwargs))
        return self._response

    async def converse_stream(self, **kwargs):
        self.calls.append(("converse_stream", kwargs))
        self.stream = FakeEventStream(self._events)
        return {"stream": self.stream}


TYPICAL_EVENTS = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockDelta": {"delta": {"text": "Hel"}, "contentBlockIndex": 0}},
    {"contentBlockDelta": {"delta": {"text": "lo"}, "contentBlockIndex": 0}},
    {"contentBlockStop": {"contentBlockIndex": 0}},
    {"messageStop": {"stopReason": "end_turn"}},
    {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 2, "totalTokens": 9},
                  "metrics": {"latencyMs": 123}}},
]


def collect(events):
    async def run():
        client = FakeBedrockClient(events=events)
        backend = BedrockBackend(client=client)
        out = [t async for t in backend.generate_stream(req())]
        return out, client
    return asyncio.run(run())


def test_stream_yields_tokens_then_a_final_tuple():
    tuples, _ = collect(TYPICAL_EVENTS)

    tokens = [(tok, done, count) for tok, done, count, _ in tuples if not done]
    assert tokens == [("Hel", False, 1), ("lo", False, 2)]

    token, done, count, meta = tuples[-1]
    assert (token, done, count) == ("", True, 2)
    assert meta["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    assert meta["timings"] == {}, "Converse reports no phase breakdown"


def test_usage_arriving_after_message_stop_still_reaches_the_final_tuple():
    """The reason messageStop is not treated as the end of the stream."""
    _, _, _, meta = collect(TYPICAL_EVENTS)[0][-1]
    assert meta["usage"]["completion_tokens"] == 2


def test_stream_without_metadata_still_terminates_with_empty_usage():
    tuples, _ = collect([
        {"contentBlockDelta": {"delta": {"text": "x"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ])
    assert tuples[-1][1] is True
    assert tuples[-1][3]["usage"] == {}


def test_empty_deltas_do_not_count_as_tokens():
    tuples, _ = collect([
        {"contentBlockDelta": {"delta": {"text": ""}}},
        {"contentBlockDelta": {"delta": {}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ])
    assert tuples[-1][2] == 0


def test_event_stream_is_closed_when_the_consumer_stops_early():
    """The point of the async client: closing the stream closes the connection.

    A disconnected client must not leave Bedrock generating billable tokens.
    """
    async def run():
        client = FakeBedrockClient(events=TYPICAL_EVENTS)
        backend = BedrockBackend(client=client)
        agen = backend.generate_stream(req())
        assert (await agen.__anext__())[0] == "Hel"
        await agen.aclose()
        return client

    client = asyncio.run(run())
    assert client.stream.closed, "event stream left open after early exit"


def test_non_streaming_concatenates_content_blocks_and_reports_usage():
    async def run():
        client = FakeBedrockClient(response={
            "output": {"message": {"content": [{"text": "a"}, {"text": "b"}]}},
            "usage": {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
        })
        return await BedrockBackend(client=client).generate(req())

    text, prompt_tok, completion_tok, meta = asyncio.run(run())
    assert text == "ab"
    assert (prompt_tok, completion_tok) == (3, 2)
    assert meta["usage"]["total_tokens"] == 5


# --- tier specs -----------------------------------------------------------------------

@pytest.mark.parametrize("spec, expected", [
    ("ollama:llama3.2:1b", ("ollama", "llama3.2:1b")),
    ("bedrock:meta.llama3-2-1b-instruct-v1:0", ("bedrock", "meta.llama3-2-1b-instruct-v1:0")),
    ("openai:some-model", ("openai", "some-model")),
    # No recognised kind prefix: the whole string is the model. This is the case that
    # keeps Part 1's "llama3.2:1b" working as a bare model name.
    ("llama3.2:1b", (None, "llama3.2:1b")),
    ("mistral:7b", (None, "mistral:7b")),
    ("plain-model", (None, "plain-model")),
])
def test_parse_tier_spec(spec, expected):
    assert parse_tier_spec(spec) == expected


def test_tier_defaults_are_local_and_use_the_default_model():
    """Shipped configuration must not reach a paid backend."""
    assert settings.tiers == {
        "fast": TierConfig("ollama", "llama3.2:1b"),
        "balanced": TierConfig("ollama", "llama3.2:1b"),
        "powerful": TierConfig("ollama", "llama3.2:1b"),
    }
    assert settings.remote_tiers == {}
    assert settings.active_kinds == ["ollama"]


def test_tier_spec_overrides_kind_and_model():
    settings.tier_powerful = "bedrock:some.model-v1:0"
    cfg = settings.resolve_tier("powerful")
    assert cfg == TierConfig("bedrock", "some.model-v1:0")
    assert cfg.is_local is False
    assert settings.remote_tiers == {"powerful": cfg}
    assert settings.active_kinds == ["bedrock", "ollama"]


def test_bare_tier_spec_uses_the_default_backend_kind():
    settings.tier_fast = "some-other-model"
    assert settings.resolve_tier("fast") == TierConfig("ollama", "some-other-model")


def test_gateway_backend_still_sets_the_kind_for_every_tier():
    """Part 1's single-env-var configuration keeps working."""
    settings.backend = "openai"
    assert [cfg.kind for cfg in settings.tiers.values()] == ["openai"] * 3


# --- the explicit-model invariant, across backends (Stage 2 step 3a) ------------------

def test_named_local_model_stays_local_when_powerful_is_bedrock():
    """A client naming the local model must not be routed to a paid backend.

    The routing half of this is pinned in test_router.py; this adds the half that
    matters once tiers have different *backends*.
    """
    settings.tier_fast = "ollama:llama3.2:1b"
    settings.tier_powerful = "bedrock:some.model-v1:0"

    # 200 words with no keyword: auto would class this as powerful, i.e. Bedrock.
    long_prompt = " ".join(["word"] * 200)
    auto = resolve_route(req(model="auto", messages=[ChatMessage(role="user", content=long_prompt)]))
    assert (auto.tier, auto.kind) == ("powerful", "bedrock")

    named = resolve_route(req(model="llama3.2:1b",
                              messages=[ChatMessage(role="user", content=long_prompt)]))
    assert named.model == "llama3.2:1b"
    assert named.tier == "fast"
    assert named.kind == "ollama", "a named local model was routed to a paid backend"


def test_client_can_name_a_backend_explicitly():
    settings.tier_fast = "ollama:llama3.2:1b"
    route = resolve_route(req(model="bedrock:some.model-v1:0"))
    assert (route.kind, route.model, route.explicit) == ("bedrock", "some.model-v1:0", True)


def test_named_kind_wins_over_a_tier_configured_for_a_different_backend():
    """Same model id on two backends: the tier is only reported if it agrees."""
    settings.tier_fast = "ollama:shared-name"
    route = resolve_route(req(model="bedrock:shared-name"))
    assert route.kind == "bedrock"
    assert route.tier is None, "reported a tier whose backend is not the one used"


def test_unconfigured_named_model_goes_to_the_default_kind():
    route = resolve_route(req(model="mistral:7b"))
    assert (route.tier, route.kind) == (None, "ollama")


# --- registry -------------------------------------------------------------------------

def test_make_backend_builds_each_kind():
    assert type(make_backend("bedrock")).__name__ == "BedrockBackend"
    assert type(make_backend("ollama")).__name__ == "OllamaBackend"
    assert type(make_backend("openai")).__name__ == "OpenAICompatBackend"


def test_make_backend_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="unknown backend kind"):
        make_backend("not-a-backend")


def test_building_bedrock_makes_no_client_and_needs_no_credentials():
    """Construction must stay cheap and credential-free: the registry builds every
    configured kind at startup, including on a machine with no AWS access."""
    backend = make_backend("bedrock")
    assert backend._client is None
    assert backend._stack is None


def test_backend_for_unknown_kind_is_a_503_not_a_crash():
    from fastapi import HTTPException
    main._backends.clear()
    main._backends["ollama"] = object()
    with pytest.raises(HTTPException) as caught:
        main.backend_for("bedrock")
    assert caught.value.status_code == 503
    main._backends.clear()


def test_registry_is_built_in_lifespan_and_closed_after(monkeypatch):
    """Consecutive requests through one TestClient, which the old singleton broke.

    backend = _make_backend() used to run at import, so httpx's connection pool was
    created outside any event loop; TestClient drives each request through its own
    short-lived loop, and a pooled connection from the first request belonged to a
    loop that was gone by the second — an intermittent "Event loop is closed" 500.
    Building the registry inside lifespan creates the clients on the running loop,
    and `with TestClient(app)` is what runs lifespan.
    """
    from fastapi.testclient import TestClient

    built: list[str] = []
    closed: list[str] = []

    class FakeBackend:
        def __init__(self, kind):
            self.kind = kind

        async def health_check(self):
            return True

        async def list_models(self):
            return ["fake-model"]

        async def generate(self, req):
            return "hi", 1, 1, {"timings": {}, "usage": {}}

        async def generate_stream(self, req):
            yield "hi", False, 1, {}
            yield "", True, 1, {"timings": {}, "usage": {}}

        async def close(self):
            closed.append(self.kind)

    def fake_make(kind):
        built.append(kind)
        return FakeBackend(kind)

    monkeypatch.setattr(main, "make_backend", fake_make)

    with TestClient(main.app) as client:
        assert built == ["ollama"], "registry not built from active_kinds"
        for i in range(4):
            resp = client.post("/v1/chat/completions", json={
                "model": "llama3.2:1b",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4,
                "stream": bool(i % 2),
            })
            assert resp.status_code == 200, f"request {i} failed: {resp.text}"
            assert resp.headers["X-Routed-Backend"] == "ollama"

    assert closed == ["ollama"], "backends not closed in lifespan"
    assert main._backends == {}, "registry not cleared"


def test_setting_a_region_makes_bedrock_reachable_by_name():
    """Opting into Bedrock without pointing a tier at it.

    This is what lets a client use "bedrock:<model-id>" explicitly while every tier
    stays local — the Stage 2 rule that Bedrock is reachable when configured, but
    never through automatic routing.
    """
    assert "bedrock" not in settings.active_kinds
    settings.aws_region = "us-east-1"
    assert "bedrock" in settings.active_kinds
    assert settings.remote_tiers == {}, "no tier should have become remote"


def test_no_region_and_no_tier_means_no_bedrock_client_is_built():
    settings.aws_region = ""
    assert settings.active_kinds == ["ollama"]
