"""Per-key routing policy, explicit tier requests, and fallback chains.

The precedence these pin, highest first:

    1. a named model      — honoured as-is, never re-routed
    2. routing.tier       — beats the key's policy
    3. the key's policy   — when it names a tier
    4. classify()         — the keyword heuristic

`local_only` is a constraint applied after that, not a fifth source: it removes
non-local backends, refusing an explicit request with 403 and downgrading an automatic
one. Driven with asyncio.run, matching the other async tests here.
"""
import asyncio

import pytest
from fastapi import Request
from prometheus_client import REGISTRY

from gateway import main
from gateway.auth import api_key_from, routing_policy_for
from gateway.config import TierConfig, settings
from gateway.router import Route, RouteRefused, resolve_route
from gateway.schemas import ChatCompletionRequest, ChatMessage

SHORT = "hello there"
COMPLEX = "Analyze the trade-offs here in detail"


def req(prompt=SHORT, **kw) -> ChatCompletionRequest:
    return ChatCompletionRequest(messages=[ChatMessage(role="user", content=prompt)], **kw)


def fake_request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                    "headers": raw})


# --- key config -----------------------------------------------------------------------

def test_plain_key_list_still_works_and_defaults_to_auto():
    """Part 1's format: no "=" anywhere."""
    settings.api_keys = "k1,k2"
    assert settings.api_key_set == {"k1", "k2"}
    assert settings.policy_for("k1") == "auto"
    assert settings.invalid_key_policies == {}


def test_keys_can_carry_a_policy():
    settings.api_keys = "k1,k2=local_only,k3=fast"
    assert settings.api_key_set == {"k1", "k2", "k3"}
    assert settings.policy_for("k1") == "auto"
    assert settings.policy_for("k2") == "local_only"
    assert settings.policy_for("k3") == "fast"


def test_unknown_key_is_auto():
    settings.api_keys = "k1=local_only"
    assert settings.policy_for("nobody") == "auto"


def test_a_misspelt_policy_is_reported_not_corrected():
    """The failure mode that matters: a typo'd local_only must not become auto.

    Startup refuses on this rather than handing the key the paid backends it was
    configured not to reach.
    """
    settings.api_keys = "k1=localonly"
    assert settings.invalid_key_policies == {"k1": "localonly"}


def test_policy_is_auto_when_auth_is_disabled():
    """With auth off there is no key, so a policy would be unenforceable anyway:
    any caller could choose their own by omitting the header."""
    settings.auth_enabled = False
    settings.api_keys = "k1=local_only"
    assert routing_policy_for(fake_request({"Authorization": "Bearer k1"})) == "auto"


def test_policy_comes_from_the_bearer_token_when_auth_is_on():
    settings.auth_enabled = True
    settings.api_keys = "k1=local_only"
    assert routing_policy_for(fake_request({"Authorization": "Bearer k1"})) == "local_only"
    assert routing_policy_for(fake_request()) == "auto"


def test_api_key_from_handles_a_missing_or_odd_header():
    assert api_key_from(fake_request()) == "anonymous"
    assert api_key_from(fake_request({"Authorization": "Basic xyz"})) == "anonymous"
    assert api_key_from(fake_request({"Authorization": "Bearer  k1 "})) == "k1"


# --- precedence -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mixed_tiers():
    """fast and balanced local, powerful paid — the configuration policy exists for."""
    settings.tier_fast = "ollama:llama3.2:1b"
    settings.tier_balanced = "ollama:llama3.1:8b"
    settings.tier_powerful = "bedrock:some.big-model-v1:0"


def test_classify_decides_when_nothing_else_does():
    route = resolve_route(req(SHORT), "auto")
    assert (route.source, route.tier, route.kind) == ("classify", "fast", "ollama")
    assert route.explicit is False


def test_policy_naming_a_tier_beats_classify():
    route = resolve_route(req(SHORT), "balanced")
    assert (route.source, route.tier) == ("policy", "balanced")
    assert route.explicit is True, "a pinned tier is an explicit choice, not a guess"


def test_explicit_tier_beats_the_policy():
    """Documented precedence: a tier policy is a default, not a cap. local_only is the
    cap, and it is not expressible as a tier."""
    route = resolve_route(req(SHORT, routing={"tier": "powerful"}), "fast")
    assert (route.source, route.tier) == ("explicit_tier", "powerful")


def test_named_model_beats_an_explicit_tier():
    route = resolve_route(req(COMPLEX, model="llama3.1:8b", routing={"tier": "powerful"}), "auto")
    assert route.source == "named_model"
    assert route.model == "llama3.1:8b"
    assert route.tier == "balanced"
    assert route.kind == "ollama", "a named local model was routed to a paid backend"


def test_named_model_beats_a_tier_policy():
    route = resolve_route(req(SHORT, model="llama3.1:8b"), "powerful")
    assert (route.source, route.model, route.kind) == ("named_model", "llama3.1:8b", "ollama")


def test_unknown_tier_is_a_400_not_a_403():
    """Asking for a tier that does not exist is a malformed request, not a denied one."""
    with pytest.raises(RouteRefused) as caught:
        resolve_route(req(SHORT, routing={"tier": "enormous"}), "auto")
    assert caught.value.status == 400
    assert caught.value.err_type == "invalid_request_error"
    assert "enormous" in caught.value.message


# --- local_only -----------------------------------------------------------------------

def test_local_only_downgrades_automatic_routing():
    """Allowed to rewrite the tier, because automatic routing promised nothing."""
    assert resolve_route(req(COMPLEX), "auto").tier == "powerful"

    route = resolve_route(req(COMPLEX), "local_only")
    assert route.tier == "balanced"
    assert route.downgraded_from == "powerful"
    assert route.kind == "ollama"


def test_local_only_downgrade_goes_down_not_up():
    """A downgrade should cost capability, never add it."""
    settings.tier_balanced = "bedrock:also-paid"
    route = resolve_route(req(COMPLEX), "local_only")
    assert route.tier == "fast", "skipped past the only remaining local tier"


def test_local_only_refuses_an_explicit_tier():
    with pytest.raises(RouteRefused) as caught:
        resolve_route(req(SHORT, routing={"tier": "powerful"}), "local_only")
    assert caught.value.status == 403
    assert caught.value.err_type == "policy_error"
    # The error names both halves, so the caller knows what to change.
    assert "powerful" in caught.value.message
    assert "local_only" in caught.value.message


def test_local_only_refuses_a_named_paid_model():
    """The invariant says a named model is never re-routed. Under local_only it is
    therefore refused rather than quietly served somewhere cheaper."""
    with pytest.raises(RouteRefused) as caught:
        resolve_route(req(SHORT, model="bedrock:some.big-model-v1:0"), "local_only")
    assert caught.value.status == 403
    assert "local_only" in caught.value.message


def test_local_only_refuses_when_no_tier_is_local():
    settings.tier_fast = "bedrock:a"
    settings.tier_balanced = "bedrock:b"
    settings.tier_powerful = "bedrock:c"
    with pytest.raises(RouteRefused) as caught:
        resolve_route(req(SHORT), "local_only")
    assert caught.value.status == 403


def test_local_only_leaves_a_local_route_alone():
    route = resolve_route(req(SHORT), "local_only")
    assert (route.tier, route.kind, route.downgraded_from) == ("fast", "ollama", None)


def test_local_only_strips_a_paid_fallback_but_keeps_serving():
    """The primary is local, so the request proceeds — it just cannot fall back to paid."""
    settings.tier_fast = "ollama:llama3.2:1b,bedrock:some.big-model-v1:0"
    assert len(resolve_route(req(SHORT), "auto").chain) == 2

    route = resolve_route(req(SHORT), "local_only")
    assert [cfg.kind for cfg in route.chain] == ["ollama"]
    assert route.downgraded_from is None, "not a downgrade: the tier is unchanged"


# --- chains ---------------------------------------------------------------------------

def test_tier_chain_parses_in_order():
    settings.tier_fast = "ollama:llama3.2:1b,openai:llama3.2:1b"
    chain = settings.resolve_chain("fast")
    assert [(c.kind, c.model) for c in chain] == [
        ("ollama", "llama3.2:1b"), ("openai", "llama3.2:1b")]
    assert settings.resolve_tier("fast") == chain[0], "primary is the chain's first entry"


def test_a_single_entry_tier_has_nothing_to_fall_back_to():
    settings.tier_fast = "ollama:llama3.2:1b"
    assert len(settings.resolve_chain("fast")) == 1


def test_unconfigured_tier_is_a_single_entry_chain():
    settings.tier_balanced = ""
    settings.model_balanced = ""
    assert settings.resolve_chain("balanced") == [TierConfig("ollama", settings.default_model)]


def test_a_chain_with_a_paid_fallback_counts_as_a_remote_tier():
    """Startup must warn: a primary failure is enough to reach the paid entry."""
    settings.tier_fast = "ollama:llama3.2:1b,bedrock:some.big-model-v1:0"
    settings.tier_powerful = "ollama:llama3.2:1b"
    assert "fast" in settings.remote_tiers


def test_chain_kinds_are_all_built_at_startup():
    settings.tier_fast = "ollama:llama3.2:1b,openai:llama3.2:1b"
    settings.tier_balanced = "ollama:llama3.1:8b"
    settings.tier_powerful = "ollama:llama3.1:8b"
    assert settings.active_kinds == ["ollama", "openai"]


def test_named_model_falls_back_only_between_backends_serving_that_model():
    """"Never re-routed" has to keep holding: the model is identical down the chain."""
    settings.tier_fast = "ollama:llama3.2:1b,openai:llama3.2:1b"
    route = resolve_route(req(SHORT, model="llama3.2:1b"), "auto")
    assert [(c.kind, c.model) for c in route.chain] == [
        ("ollama", "llama3.2:1b"), ("openai", "llama3.2:1b")]


def test_naming_the_backend_too_pins_it_with_no_fallback():
    settings.tier_fast = "ollama:llama3.2:1b,openai:llama3.2:1b"
    route = resolve_route(req(SHORT, model="ollama:llama3.2:1b"), "auto")
    assert [c.kind for c in route.chain] == ["ollama"]


# --- fallback behaviour ---------------------------------------------------------------

def fallback_count(tier, from_kind, to_kind, reason) -> float:
    return REGISTRY.get_sample_value("llm_fallbacks_total", {
        "tier": tier, "from_kind": from_kind, "to_kind": to_kind, "reason": reason}) or 0.0


class Boom:
    """Fails on the first chunk with a given exception."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def generate_stream(self, req):
        self.calls += 1
        raise self.exc
        yield  # pragma: no cover - makes this an async generator

    async def generate(self, req):
        self.calls += 1
        raise self.exc


class Works:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, req):
        self.calls += 1
        yield "ok", False, 1, {}
        yield "", True, 1, {"timings": {}, "usage": {"completion_tokens": 1}}

    async def generate(self, req):
        self.calls += 1
        return "ok", 1, 1, {"timings": {}, "usage": {}}


def chain_route(tier="fast", entries=(("a", "m1"), ("b", "m2"))) -> Route:
    cfgs = tuple(TierConfig(k, m) for k, m in entries)
    return Route(tier=tier, model=cfgs[0].model, kind=cfgs[0].kind, explicit=False,
                 chain=cfgs)


@pytest.fixture
def registry():
    saved = dict(main._backends)
    main._backends.clear()
    yield main._backends
    main._backends.clear()
    main._backends.update(saved)


def test_fallback_on_a_connection_error(registry):
    import httpx

    primary = Boom(httpx.ConnectError("refused"))
    secondary = Works()
    registry.update({"a": primary, "b": secondary})
    before = fallback_count("fast", "a", "b", "upstream_unavailable")

    resp, used = asyncio.run(
        main._stream_response(req(SHORT, stream=True), "fast", 0.0, chain_route()))
    chunks = asyncio.run(drain(resp))

    assert primary.calls == 1 and secondary.calls == 1
    assert used.kind == "b", "response must report the backend that answered"
    assert any("ok" in c for c in chunks)
    assert fallback_count("fast", "a", "b", "upstream_unavailable") == before + 1


async def drain(resp):
    return [chunk async for chunk in resp.body_iterator]


def test_no_fallback_on_a_bad_model_id(registry):
    """A 404 means the request is wrong; the next backend would say the same thing
    and charge for the privilege."""
    import httpx

    request = httpx.Request("POST", "http://x/api/chat")
    response = httpx.Response(404, request=request, text="model not found")
    primary = Boom(httpx.HTTPStatusError("404", request=request, response=response))
    secondary = Works()
    registry.update({"a": primary, "b": secondary})

    resp, _used = asyncio.run(
        main._stream_response(req(SHORT, stream=True), "fast", 0.0, chain_route()))

    assert primary.calls == 1
    assert secondary.calls == 0, "fell back on an error the next backend cannot fix"
    assert resp.status_code == 404


def test_no_fallback_on_a_malformed_request(registry):
    import httpx

    request = httpx.Request("POST", "http://x/api/chat")
    response = httpx.Response(422, request=request, text="context too long")
    primary = Boom(httpx.HTTPStatusError("422", request=request, response=response))
    secondary = Works()
    registry.update({"a": primary, "b": secondary})

    resp, _used = asyncio.run(
        main._stream_response(req(SHORT, stream=True), "fast", 0.0, chain_route()))

    assert secondary.calls == 0
    assert resp.status_code == 422


def test_fallback_on_a_throttle(registry):
    """429 is the backend's problem, not the request's — another backend may well serve it."""
    import httpx

    request = httpx.Request("POST", "http://x/api/chat")
    response = httpx.Response(429, request=request, text="slow down")
    primary = Boom(httpx.HTTPStatusError("429", request=request, response=response))
    secondary = Works()
    registry.update({"a": primary, "b": secondary})

    _resp, used = asyncio.run(
        main._stream_response(req(SHORT, stream=True), "fast", 0.0, chain_route()))
    assert used.kind == "b"


def test_last_backend_failing_is_reported_not_retried(registry):
    import httpx

    primary = Boom(httpx.ConnectError("refused"))
    secondary = Boom(httpx.ConnectError("also refused"))
    registry.update({"a": primary, "b": secondary})

    resp, _used = asyncio.run(
        main._stream_response(req(SHORT, stream=True), "fast", 0.0, chain_route()))

    assert primary.calls == 1 and secondary.calls == 1
    assert resp.status_code == 502


def test_sync_path_falls_back_too(registry):
    import httpx

    primary = Boom(httpx.ConnectError("refused"))
    secondary = Works()
    registry.update({"a": primary, "b": secondary})

    body, used = asyncio.run(main._sync_response(req(SHORT), "fast", 0.0, chain_route()))
    assert used.kind == "b"
    assert body.choices[0].message.content == "ok"


def test_fallback_skipped_when_the_chain_has_one_entry(registry):
    import httpx

    primary = Boom(httpx.ConnectError("refused"))
    registry.update({"a": primary})
    resp, _used = asyncio.run(main._stream_response(
        req(SHORT, stream=True), "fast", 0.0, chain_route(entries=(("a", "m1"),))))
    assert resp.status_code == 502


def test_startup_refuses_an_unrecognised_policy():
    """Fail fast, loudly: the alternative is a key silently gaining paid access."""
    from fastapi.testclient import TestClient

    settings.api_keys = "k1=localonly"
    with pytest.raises(RuntimeError, match="unrecognised routing policies"):
        with TestClient(main.app):
            pass


def test_startup_accepts_valid_policies():
    from fastapi.testclient import TestClient

    settings.api_keys = "k1,k2=local_only,k3=powerful"
    settings.tier_powerful = "ollama:llama3.1:8b"
    with TestClient(main.app):
        pass


# --- downgrade observability ----------------------------------------------------------

def downgrade_count(from_tier, to_tier) -> float:
    return REGISTRY.get_sample_value("llm_policy_downgrades_total", {
        "from_tier": from_tier, "to_tier": to_tier}) or 0.0


def test_downgrade_is_counted_and_the_served_tier_is_reported(registry):
    """A downgrade must leave a trace a dashboard can see, not just a log line."""
    from fastapi.testclient import TestClient

    class Fake:
        async def health_check(self): return True
        async def list_models(self): return []
        async def generate(self, req): return "hi", 1, 1, {"timings": {}, "usage": {}}
        async def generate_stream(self, req):
            yield "hi", False, 1, {}
            yield "", True, 1, {"timings": {}, "usage": {}}
        async def close(self): pass

    settings.auth_enabled = True
    settings.api_keys = "k-local=local_only"
    settings.tier_fast = "ollama:llama3.2:1b"
    settings.tier_balanced = "ollama:llama3.1:8b"
    settings.tier_powerful = "bedrock:some.big-model-v1:0"
    before = downgrade_count("powerful", "balanced")

    with TestClient(main.app) as client:
        main._backends["ollama"] = Fake()
        resp = client.post("/v1/chat/completions",
                           headers={"Authorization": "Bearer k-local"},
                           json={"model": "auto", "max_tokens": 4,
                                 "messages": [{"role": "user", "content": COMPLEX}]})

    assert resp.status_code == 200
    # The header reports the tier that served the request, not the one asked for.
    assert resp.headers["X-Model-Tier"] == "balanced"
    assert resp.headers["X-Routing-Policy"] == "local_only"
    assert resp.headers["X-Routed-Backend"] == "ollama"
    assert downgrade_count("powerful", "balanced") == before + 1


def test_no_downgrade_counted_when_nothing_is_rewritten(registry):
    before = downgrade_count("fast", "fast")
    route = resolve_route(req(SHORT), "local_only")
    assert route.downgraded_from is None
    assert downgrade_count("fast", "fast") == before


def test_auth_off_plus_a_paid_tier_is_warned_about(caplog):
    """Both halves are legal alone; together an open port can spend money."""
    import logging
    from fastapi.testclient import TestClient

    settings.auth_enabled = False
    settings.api_keys = ""
    settings.tier_powerful = "bedrock:some.big-model-v1:0"
    settings.aws_region = "us-west-2"

    with caplog.at_level(logging.WARNING):
        with TestClient(main.app):
            pass

    messages = [r.message for r in caplog.records]
    assert "remote_tiers_configured" in messages
    assert "remote_tiers_configured_without_auth" in messages


def test_no_open_port_warning_when_auth_is_on(caplog):
    import logging
    from fastapi.testclient import TestClient

    settings.auth_enabled = True
    settings.api_keys = "k1"
    settings.tier_powerful = "bedrock:some.big-model-v1:0"
    settings.aws_region = "us-west-2"

    with caplog.at_level(logging.WARNING):
        with TestClient(main.app):
            pass

    messages = [r.message for r in caplog.records]
    assert "remote_tiers_configured" in messages
    assert "remote_tiers_configured_without_auth" not in messages


def test_no_open_port_warning_when_every_tier_is_local(caplog):
    import logging
    from fastapi.testclient import TestClient

    settings.auth_enabled = False
    settings.tier_powerful = "ollama:llama3.2:1b"

    with caplog.at_level(logging.WARNING):
        with TestClient(main.app):
            pass

    assert "remote_tiers_configured_without_auth" not in [r.message for r in caplog.records]


def test_auth_failure_falls_back_to_the_next_backend(registry):
    """Credentials are per backend: a 401 says nothing about the next one."""
    import httpx

    request = httpx.Request("POST", "http://x/api/chat")
    response = httpx.Response(401, request=request, text="bad credentials")
    primary = Boom(httpx.HTTPStatusError("401", request=request, response=response))
    secondary = Works()
    registry.update({"a": primary, "b": secondary})

    _resp, used = asyncio.run(
        main._stream_response(req(SHORT, stream=True), "fast", 0.0, chain_route()))
    assert used.kind == "b", "a per-backend auth failure took down a tier with a working fallback"
