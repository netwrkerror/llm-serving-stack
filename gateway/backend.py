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


def _make_backend() -> Backend:
    if settings.backend == "openai":
        return OpenAICompatBackend()
    return OllamaBackend()


backend: Backend = _make_backend()
