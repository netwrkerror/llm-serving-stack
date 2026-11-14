import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .auth import APIKeyMiddleware
from .backend import ollama_backend
from .config import settings
from .limiter import rate_limiter
from .logger import log
from .metrics import ACTIVE_REQUESTS, REQUEST_COUNT, REQUEST_DURATION, THROUGHPUT, TOKENS_GENERATED, TTFT
from .router import classify, select_model
from .tracing import get_tracer, setup_tracing
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)

_queue = asyncio.Semaphore(settings.max_concurrent_requests)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing()
    log.info("gateway_starting", extra={
        "backend": settings.ollama_base_url,
        "queue_slots": settings.max_concurrent_requests,
        "tracing": settings.tracing_enabled,
    })
    yield
    await ollama_backend.close()
    log.info("gateway_stopped")


app = FastAPI(
    title="LLM Serving Stack",
    description="OpenAI-compatible inference gateway backed by Ollama.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(APIKeyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    ok = await ollama_backend.health_check()
    return {"status": "ok" if ok else "degraded", "backend": settings.ollama_base_url}


@app.get("/v1/models")
async def list_models():
    try:
        models = await ollama_backend.list_models()
        return {
            "object": "list",
            "data": [{"id": m["name"], "object": "model", "owned_by": "ollama"} for m in models],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    api_key = request.headers.get("Authorization", "")[len("Bearer "):].strip() or "anonymous"

    allowed, reason = rate_limiter.check_request(api_key)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    allowed, reason = rate_limiter.check_tokens(api_key, req.max_tokens or 512)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    routed_model = select_model(req)
    tier = classify(req)
    req = req.model_copy(update={"model": routed_model})

    log.info("request_received", extra={"model": routed_model, "tier": tier, "stream": req.stream})
    ACTIVE_REQUESTS.labels(model=routed_model).inc()
    try:
        if req.stream:
            resp = _stream_response(req, tier)
        else:
            resp = await _sync_response(req, tier)

        if hasattr(resp, "headers"):
            resp.headers["X-Model-Tier"] = tier
            resp.headers["X-Routed-Model"] = routed_model
        return resp
    finally:
        ACTIVE_REQUESTS.labels(model=routed_model).dec()


def _set_span_attrs(span, req: ChatCompletionRequest, tier: str) -> None:
    span.set_attribute("llm.model", req.model)
    span.set_attribute("llm.tier", tier)
    span.set_attribute("llm.stream", req.stream)
    span.set_attribute("llm.max_tokens", req.max_tokens or 512)


def _stream_response(req: ChatCompletionRequest, tier: str) -> StreamingResponse:
    model = req.model

    # The handler returns this StreamingResponse before the generator body runs, so the
    # queue slot has to be acquired here and held until the final chunk is yielded.
    async def event_stream() -> AsyncIterator[str]:
        t = get_tracer()
        with t.start_as_current_span("llm.request") as span:
            _set_span_attrs(span, req, tier)

            with t.start_as_current_span("llm.queue.wait"):
                await _queue.acquire()
            try:
                with t.start_as_current_span("llm.inference"):
                    start = time.perf_counter()
                    first_token_recorded = False
                    try:
                        async for token, done, token_count in ollama_backend.generate_stream(req):
                            if token and not first_token_recorded:
                                TTFT.labels(model=model).observe(time.perf_counter() - start)
                                first_token_recorded = True

                            if token:
                                chunk = {
                                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion.chunk",
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": token}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"

                            if done:
                                duration = time.perf_counter() - start
                                REQUEST_COUNT.labels(model=model, status="success", stream="true").inc()
                                REQUEST_DURATION.labels(model=model, stream="true").observe(duration)
                                TOKENS_GENERATED.labels(model=model).inc(token_count)
                                THROUGHPUT.labels(model=model).observe(token_count / duration if duration > 0 else 0)
                                log.info("request_completed", extra={
                                    "model": model, "stream": True,
                                    "duration_s": round(duration, 3),
                                    "tokens": token_count,
                                    "tps": round(token_count / duration if duration > 0 else 0, 1),
                                })
                                final = {
                                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion.chunk",
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                }
                                yield f"data: {json.dumps(final)}\n\n"
                                yield "data: [DONE]\n\n"
                    except Exception as exc:
                        REQUEST_COUNT.labels(model=model, status="error", stream="true").inc()
                        log.error("request_failed", extra={"model": model, "error": str(exc)})
                        yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            finally:
                _queue.release()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _sync_response(req: ChatCompletionRequest, tier: str) -> ChatCompletionResponse:
    model = req.model
    t = get_tracer()
    with t.start_as_current_span("llm.request") as span:
        _set_span_attrs(span, req, tier)

        with t.start_as_current_span("llm.queue.wait"):
            await _queue.acquire()
        try:
            with t.start_as_current_span("llm.inference"):
                start = time.perf_counter()
                try:
                    text, prompt_tok, completion_tok = await ollama_backend.generate(req)
                except Exception as exc:
                    REQUEST_COUNT.labels(model=model, status="error", stream="false").inc()
                    log.error("request_failed", extra={"model": model, "error": str(exc)})
                    raise HTTPException(status_code=500, detail=str(exc))

                duration = time.perf_counter() - start
                REQUEST_COUNT.labels(model=model, status="success", stream="false").inc()
                REQUEST_DURATION.labels(model=model, stream="false").observe(duration)
                TOKENS_GENERATED.labels(model=model).inc(completion_tok)
                THROUGHPUT.labels(model=model).observe(completion_tok / duration if duration > 0 else 0)
                log.info("request_completed", extra={
                    "model": model, "stream": False,
                    "duration_s": round(duration, 3),
                    "tokens": completion_tok,
                    "tps": round(completion_tok / duration if duration > 0 else 0, 1),
                })
                return ChatCompletionResponse(
                    model=model,
                    choices=[ChatCompletionChoice(message=ChatMessage(role="assistant", content=text))],
                    usage=Usage(
                        prompt_tokens=prompt_tok,
                        completion_tokens=completion_tok,
                        total_tokens=prompt_tok + completion_tok,
                    ),
                )
        finally:
            _queue.release()
