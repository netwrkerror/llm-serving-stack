import json
from typing import AsyncIterator

import httpx

from .config import settings
from .schemas import ChatCompletionRequest

_NS_PER_MS = 1_000_000

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
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(settings.request_timeout),
        )

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get("/api/tags")
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list:
        resp = await self.client.get("/api/tags")
        resp.raise_for_status()
        return resp.json().get("models", [])

    def _to_ollama_payload(self, req: ChatCompletionRequest) -> dict:
        system_prompt = None
        turns: list[str] = []
        for msg in req.messages:
            if msg.role == "system":
                system_prompt = msg.content
            elif msg.role == "user":
                turns.append(f"User: {msg.content}")
            elif msg.role == "assistant":
                turns.append(f"Assistant: {msg.content}")
        prompt = "\n".join(turns) + "\nAssistant:"

        payload: dict = {
            "model": req.model,
            "prompt": prompt,
            "options": {
                "temperature": req.temperature,
                "num_predict": req.max_tokens or 512,
                "top_p": req.top_p,
            },
        }
        if system_prompt:
            payload["system"] = system_prompt
        return payload

    async def generate(self, req: ChatCompletionRequest) -> tuple[str, int, int, dict]:
        """Non-streaming. Returns (text, prompt_tokens, completion_tokens, timings)."""
        payload = self._to_ollama_payload(req)
        payload["stream"] = False
        resp = await self.client.post("/api/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return (
            data["response"],
            data.get("prompt_eval_count", 0),
            data.get("eval_count", 0),
            _extract_timings(data),
        )

    async def generate_stream(self, req: ChatCompletionRequest) -> AsyncIterator[tuple[str, bool, int, dict]]:
        """Streaming. Yields (token, is_done, cumulative_token_count, meta).

        meta is empty except on the final chunk, where it carries {"timings", "usage"}
        from what the backend reported. Either key may be absent.
        """
        payload = self._to_ollama_payload(req)
        payload["stream"] = True
        token_count = 0
        async with self.client.stream("POST", "/api/generate", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    token = data.get("response", "")
                    done = data.get("done", False)
                    if token:
                        token_count += 1
                    meta = {"timings": _extract_timings(data), "usage": _extract_usage(data)} if done else {}
                    yield token, done, token_count, meta
                    if done:
                        break
                except json.JSONDecodeError:
                    continue

    async def close(self) -> None:
        await self.client.aclose()


ollama_backend = OllamaBackend()
