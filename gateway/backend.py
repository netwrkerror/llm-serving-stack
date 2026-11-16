import asyncio
import inspect
import json
from typing import AsyncIterator, Protocol

import httpx

from .config import settings
from .schemas import ChatCompletionRequest

_NS_PER_MS = 1_000_000


class Backend(Protocol):
    """What the gateway needs from a backend, regardless of its wire protocol.

    generate and generate_stream both return a meta dict that may carry "timings"
    and "usage". Only Ollama reports timings; callers must tolerate either being absent.
    """

    async def health_check(self) -> bool: ...

    async def list_models(self) -> list[str]: ...

    async def generate(self, req: ChatCompletionRequest) -> tuple[str, int, int, dict]: ...

    def generate_stream(self, req: ChatCompletionRequest) -> AsyncIterator[tuple[str, bool, int, dict]]: ...

    async def close(self) -> None: ...

# Ollama reports these on its final response chunk, in nanoseconds. Other backends
# (vLLM, TGI, anything OpenAI-compatible) don't, so callers must treat them as optional.
_TIMING_FIELDS = {
    "total_duration": "total_ms",
    "load_duration": "load_ms",
    "prompt_eval_duration": "prompt_eval_ms",
    "eval_duration": "eval_ms",
}


def _extract_timings(data: dict) -> dict[str, float]:
    return {
        name: data[field] / _NS_PER_MS
        for field, name in _TIMING_FIELDS.items()
        if isinstance(data.get(field), (int, float))
    }


def _extract_usage(data: dict) -> dict[str, int]:
    """Token counts from Ollama's final chunk. Empty if the backend omits them."""
    prompt = data.get("prompt_eval_count")
    completion = data.get("eval_count")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return {}
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


class OllamaBackend:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=settings.backend_url,
            timeout=httpx.Timeout(settings.request_timeout),
        )

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get("/api/tags")
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        resp = await self.client.get("/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", []) if m.get("name")]

    def _to_ollama_payload(self, req: ChatCompletionRequest) -> dict:
        # /api/chat, not /api/generate: it takes roles directly, so the model's chat
        # template is applied exactly once. Building a "User:/Assistant:" string for
        # /api/generate double-wrapped it — the template was applied on top, leaving
        # those markers as literal text inside one user turn (32 prompt tokens vs 27
        # for the same message, and multi-turn collapsed into a single turn).
        return {
            "model": req.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "options": {
                "temperature": req.temperature,
                "num_predict": req.max_tokens or 512,
                "top_p": req.top_p,
            },
        }

    async def generate(self, req: ChatCompletionRequest) -> tuple[str, int, int, dict]:
        """Non-streaming. Returns (text, prompt_tokens, completion_tokens, meta)."""
        payload = self._to_ollama_payload(req)
        payload["stream"] = False
        resp = await self.client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return (
            (data.get("message") or {}).get("content", ""),
            data.get("prompt_eval_count", 0),
            data.get("eval_count", 0),
            {"timings": _extract_timings(data), "usage": _extract_usage(data)},
        )

    async def generate_stream(self, req: ChatCompletionRequest) -> AsyncIterator[tuple[str, bool, int, dict]]:
        """Streaming. Yields (token, is_done, cumulative_token_count, meta).

        meta is empty except on the final chunk, where it carries {"timings", "usage"}
        from what the backend reported. Either key may be absent.
        """
        payload = self._to_ollama_payload(req)
        payload["stream"] = True
        token_count = 0
        async with self.client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = (data.get("message") or {}).get("content", "")
                done = data.get("done", False)
                if token:
                    token_count += 1
                meta = {"timings": _extract_timings(data), "usage": _extract_usage(data)} if done else {}
                yield token, done, token_count, meta
                if done:
                    break

    async def close(self) -> None:
        await self.client.aclose()


class OpenAICompatBackend:
    """Talks the OpenAI chat-completions protocol: vLLM, TGI, Ollama's own /v1, etc.

    base_url should include the version prefix, e.g. http://localhost:11434/v1.
    No timings are available — those are an Ollama-specific extension.
    """

    def __init__(self) -> None:
        headers = {"Authorization": f"Bearer {settings.backend_api_key}"} if settings.backend_api_key else {}
        self.client = httpx.AsyncClient(
            base_url=settings.backend_url,
            timeout=httpx.Timeout(settings.request_timeout),
            headers=headers,
        )

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get("/models")
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        resp = await self.client.get("/models")
        resp.raise_for_status()
        return [m["id"] for m in resp.json().get("data", []) if m.get("id")]

    def _payload(self, req: ChatCompletionRequest) -> dict:
        return {
            "model": req.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
            "top_p": req.top_p,
            "max_tokens": req.max_tokens or 512,
        }

    async def generate(self, req: ChatCompletionRequest) -> tuple[str, int, int, dict]:
        payload = self._payload(req)
        payload["stream"] = False
        resp = await self.client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
        prompt_tok = usage.get("prompt_tokens", 0)
        completion_tok = usage.get("completion_tokens", 0)
        return text, prompt_tok, completion_tok, {"timings": {}, "usage": usage}

    async def generate_stream(self, req: ChatCompletionRequest) -> AsyncIterator[tuple[str, bool, int, dict]]:
        payload = self._payload(req)
        payload["stream"] = True
        # Ask for usage where the server supports it; servers that don't simply ignore it.
        payload["stream_options"] = {"include_usage": True}

        token_count = 0
        usage: dict = {}
        async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:"):].strip()
                if raw == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if data.get("usage"):
                    usage = data["usage"]

                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                token = delta.get("content") or ""
                if token:
                    token_count += 1
                    yield token, False, token_count, {}
                if choices[0].get("finish_reason"):
                    break

        yield "", True, token_count, {"timings": {}, "usage": usage}

    async def close(self) -> None:
        await self.client.aclose()


def _to_converse(req: ChatCompletionRequest) -> dict:
    """ChatCompletionRequest -> Converse arguments.

    System messages go in `system`, not the message list — Converse rejects a
    "system" role inside `messages`. Everything else becomes a one-text-block
    message. Consecutive same-role messages are *not* merged; Converse wants
    strict alternation and will complain, which is a clearer failure than this
    function silently rewriting the conversation.
    """
    system = [{"text": m.content} for m in req.messages if m.role == "system"]
    messages = [
        {"role": m.role, "content": [{"text": m.content}]}
        for m in req.messages
        if m.role != "system"
    ]
    args: dict = {
        "modelId": req.model,
        "messages": messages,
        "inferenceConfig": {
            "maxTokens": req.max_tokens or 512,
            "temperature": req.temperature,
            "topP": req.top_p,
        },
    }
    if system:
        args["system"] = system
    return args


def _converse_usage(usage: dict) -> dict[str, int]:
    """Converse usage -> the same keys the other backends report.

    Converse says inputTokens/outputTokens; the gateway's usage chunk and the
    OpenAI protocol say prompt_tokens/completion_tokens. Translated here so one
    shape reaches the client regardless of backend.
    """
    prompt = usage.get("inputTokens")
    completion = usage.get("outputTokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return {}
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": usage.get("totalTokens", prompt + completion),
    }


class BedrockBackend:
    """Amazon Bedrock via the Converse API.

    Async on purpose. A synchronous boto3 call pulled from a worker thread cannot
    be cancelled: on client disconnect the thread goes on draining the event
    stream to the end, and Bedrock bills for every token it produces. With an
    async client, closing the response stream closes the HTTP connection, which
    gives at least the same finalization-driven cancellation measured against
    Ollama in benchmark/RESULTS.md.

    Credentials come from the standard chain only — never from settings.

    UNVERIFIED against the live API: written from the Converse event contract, but
    at the time of writing this repo had no AWS credentials available, so no call
    has been made. The event mapping is covered by unit tests with synthetic
    events, which proves the mapping and not the contract.
    """

    def __init__(self, client=None, control_client=None) -> None:
        # Injected clients are how the tests avoid needing AWS at all.
        self._client = client
        self._injected = client is not None
        self._control_client = control_client
        self._control_injected = control_client is not None
        self._stack = None
        self._lock = asyncio.Lock()

    async def _get_control_client(self):
        """The `bedrock` control-plane client, separate from `bedrock-runtime`.

        Only used for GetFoundationModel, which reports lifecycle status and is not
        an inference call, so it does not bill.
        """
        if self._control_client is not None:
            return self._control_client
        async with self._lock:
            if self._control_client is not None:
                return self._control_client
            import aioboto3
            from contextlib import AsyncExitStack

            if self._stack is None:
                self._stack = AsyncExitStack()
            kwargs = {}
            if settings.aws_region:
                kwargs["region_name"] = settings.aws_region
            self._control_client = await self._stack.enter_async_context(
                aioboto3.Session().client("bedrock", **kwargs)
            )
            return self._control_client

    async def model_lifecycle(self, model_id: str) -> tuple[str, str | None]:
        """(status, error) for a model id, via GetFoundationModel.

        status is Bedrock's modelLifecycle.status — ACTIVE or LEGACY — or "unknown"
        when the call could not be made, in which case error says why. Never raises:
        the runtime role may not hold bedrock:GetFoundationModel, and a gateway must
        not refuse to start because it could not look up a label.

        Lifecycle state is per region, so this reports it for whichever region the
        client resolved to.
        """
        try:
            client = await self._get_control_client()
            resp = await client.get_foundation_model(modelIdentifier=model_id)
            details = resp.get("modelDetails", {}) or {}
            lifecycle = details.get("modelLifecycle", {}) or {}
            return lifecycle.get("status") or "unknown", None
        except Exception as exc:
            return "unknown", f"{type(exc).__name__}: {exc}"

    async def _get_client(self):
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            try:
                import aioboto3
            except ImportError as exc:  # pragma: no cover - depends on the image
                raise RuntimeError(
                    "the bedrock backend needs aioboto3 (see gateway/requirements.txt). "
                    "Synchronous boto3 is deliberately not used: a converse_stream call "
                    "in a worker thread cannot be cancelled on client disconnect."
                ) from exc

            from contextlib import AsyncExitStack

            session = aioboto3.Session()
            kwargs = {"config": _bedrock_config()}
            if settings.aws_region:
                kwargs["region_name"] = settings.aws_region
            self._stack = AsyncExitStack()
            self._client = await self._stack.enter_async_context(
                session.client("bedrock-runtime", **kwargs)
            )
            return self._client

    async def health_check(self) -> bool:
        """Region resolves, credentials resolve, and a client can be built.

        Deliberately NOT an inference call — that would bill for a health check —
        so this does not prove the configured model exists or that the caller is
        allowed to invoke it. A green check here means "configured", not
        "reachable". Credentials are checked explicitly because building a client
        succeeds without any, which would otherwise report a healthy backend on a
        machine that cannot call Bedrock at all.
        """
        try:
            client = await self._get_client()
            if client is None:
                return False
            if self._injected:
                return True
            import aioboto3

            creds = aioboto3.Session().get_credentials()
            if inspect.isawaitable(creds):
                creds = await creds
            return creds is not None
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """Empty: enumerating Bedrock models needs the `bedrock` control-plane
        client and ListFoundationModels, which this backend does not hold. The
        gateway adds configured tier models to /v1/models itself, so a configured
        Bedrock model is still discoverable. Untested."""
        return []

    async def generate(self, req: ChatCompletionRequest) -> tuple[str, int, int, dict]:
        client = await self._get_client()
        resp = await client.converse(**_to_converse(req))
        blocks = (resp.get("output", {}).get("message", {}) or {}).get("content", []) or []
        text = "".join(b.get("text", "") for b in blocks)
        usage = _converse_usage(resp.get("usage", {}) or {})
        return (
            text,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            # No timings: Converse reports metrics.latencyMs for the whole call, which
            # is not a phase breakdown, so there is nothing to put in the shape
            # _record_backend_timings expects.
            {"timings": {}, "usage": usage},
        )

    async def generate_stream(self, req: ChatCompletionRequest) -> AsyncIterator[tuple[str, bool, int, dict]]:
        """Yields (token, is_done, cumulative_token_count, meta), as the others do.

        Converse's usage arrives in a `metadata` event, which may come after
        `messageStop`. The final tuple is therefore emitted once the event stream
        ends, so usage is whatever the stream actually reported by then.
        """
        client = await self._get_client()
        resp = await client.converse_stream(**_to_converse(req))

        token_count = 0
        usage: dict = {}
        stream = resp["stream"]
        try:
            async for event in stream:
                if "contentBlockDelta" in event:
                    token = (event["contentBlockDelta"].get("delta", {}) or {}).get("text", "")
                    if token:
                        token_count += 1
                        yield token, False, token_count, {}
                elif "metadata" in event:
                    reported = event["metadata"].get("usage", {}) or {}
                    if reported:
                        usage = _converse_usage(reported)
                # messageStart / contentBlockStop / messageStop carry no text and no
                # usage. messageStop is not treated as the end: metadata follows it.
        finally:
            # Closing the event stream closes the underlying connection, which is the
            # whole reason this backend is async. Without it, a disconnect would leave
            # Bedrock generating billable tokens nobody reads.
            close = getattr(stream, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

        yield "", True, token_count, {"timings": {}, "usage": usage}

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        if not self._injected:
            self._client = None
        if not self._control_injected:
            self._control_client = None


def _bedrock_config():
    """botocore Config for the runtime client.

    botocore's standard mode, which is three attempts. That does not risk paying
    twice for one request: it retries throttles and connection or 5xx failures that
    happen before a response begins, and once converse_stream has started returning
    events a failure mid-stream is surfaced rather than retried. So a retry means the
    first attempt generated nothing to bill for.
    """
    from botocore.config import Config

    return Config(
        read_timeout=settings.bedrock_timeout,
        connect_timeout=10,
        retries={"mode": "standard"},
    )


def make_backend(kind: str) -> Backend:
    """Build one backend by kind. The registry in main.py owns the instances."""
    if kind == "openai":
        return OpenAICompatBackend()
    if kind == "bedrock":
        return BedrockBackend()
    if kind == "ollama":
        return OllamaBackend()
    raise ValueError(f"unknown backend kind: {kind!r}")
