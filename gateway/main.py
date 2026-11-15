import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import trace as otel_trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .auth import APIKeyMiddleware
from .backend import backend
from .config import settings
from .limiter import rate_limiter
from .logger import log
from .metrics import (
    IN_FLIGHT,
    QUEUE_WAITING,
    REQUEST_COUNT,
    REQUEST_DURATION,
    THROUGHPUT,
    TOKENS_GENERATED,
    TTFT,
)
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
        "backend_kind": settings.backend,
        "backend": settings.backend_url,
        "queue_slots": settings.max_concurrent_requests,
        "tracing": settings.tracing_enabled,
    })
    yield
    await backend.close()
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
    ok = await backend.health_check()
    return {
        "status": "ok" if ok else "degraded",
        "backend": settings.backend_url,
        "backend_kind": settings.backend,
        "queue_slots": settings.max_concurrent_requests,
    }


@app.get("/v1/models")
async def list_models():
    try:
        models = await backend.list_models()
        return {
            "object": "list",
            "data": [{"id": m, "object": "model", "owned_by": settings.backend} for m in models],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    arrival = time.perf_counter()
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

    if req.stream:
        resp: Response = await _stream_response(req, tier, arrival)
    else:
        # JSONResponse rather than the Pydantic model directly, so the headers below
        # apply to both paths.
        try:
            resp = JSONResponse(content=(await _sync_response(req, tier, arrival)).model_dump())
        except _UpstreamFailure as fail:
            resp = _openai_error(fail.status, fail.message, fail.err_type)

    resp.headers["X-Model-Tier"] = tier
    resp.headers["X-Routed-Model"] = routed_model
    return resp


async def _acquire_slot(model: str) -> None:
    """Wait for a queue slot, counting this request as queued while it blocks."""
    QUEUE_WAITING.labels(model=model).inc()
    try:
        await _queue.acquire()
    finally:
        QUEUE_WAITING.labels(model=model).dec()


def _set_span_attrs(span, req: ChatCompletionRequest, tier: str) -> None:
    span.set_attribute("llm.model", req.model)
    span.set_attribute("llm.tier", tier)
    span.set_attribute("llm.stream", req.stream)
    span.set_attribute("llm.max_tokens", req.max_tokens or 512)


def _record_backend_timings(span, timings: dict) -> None:
    """Attach the backend's self-reported durations to the inference span.

    Only Ollama reports these; OpenAI-compatible backends send nothing, so skip quietly.

    unattributed_ms is total minus the phases Ollama attributes to this request. Measured
    against OLLAMA_NUM_PARALLEL=1 vs 4, this is the term that tracks backend contention:
    it goes from ~1ms on the first request to ~400ms on the last of four concurrent ones,
    while each request's own eval time stays flat. Note total_duration already includes
    that wait, so wall-clock-minus-total measures only HTTP overhead, not queueing.
    """
    if not timings:
        return
    for name, value in timings.items():
        span.set_attribute(f"ollama.{name}", round(value, 2))
    if "total_ms" in timings:
        phases = sum(timings.get(k, 0.0) for k in ("load_ms", "prompt_eval_ms", "eval_ms"))
        span.set_attribute("ollama.unattributed_ms", round(timings["total_ms"] - phases, 2))


class _UpstreamFailure(Exception):
    """A backend failure that happened before any bytes were sent to the client,
    so it can still be reported as a real HTTP status."""

    def __init__(self, status: int, message: str, err_type: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.err_type = err_type


def _classify_upstream(exc: Exception) -> tuple[int, str]:
    """Map a backend exception onto the status the caller should see."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 404:
            return 404, "model_not_found"
        if code in (400, 401, 403, 422, 429):
            return code, "upstream_error"
        return 502, "upstream_error"
    if isinstance(exc, httpx.TimeoutException):
        return 504, "upstream_timeout"
    if isinstance(exc, httpx.RequestError):
        return 502, "upstream_unavailable"
    return 500, "internal_error"


def _openai_error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": None}},
    )


async def _prepend(first, rest: AsyncIterator) -> AsyncIterator:
    yield first
    async for item in rest:
        yield item


async def _stream_response(req: ChatCompletionRequest, tier: str, arrival: float) -> Response:
    """Open the backend stream and read its first chunk before returning a response.

    Once a StreamingResponse is returned, the 200 and the SSE content-type are already
    committed, so a failure after that can only be an error chunk. Pulling the first
    chunk here means a dead backend or an unknown model still gets a real status code.
    Failures after this point stay 200 + an error chunk, as the protocol requires.
    """
    model = req.model
    # Absent stream_options means send usage; an explicit value wins either way.
    include_usage = req.stream_options.include_usage if req.stream_options else True
    t = get_tracer()

    IN_FLIGHT.labels(model=model).inc()
    span = t.start_span("llm.request")
    _set_span_attrs(span, req, tier)

    acquired = False
    agen = None
    inf_span = None
    try:
        with otel_trace.use_span(span, end_on_exit=False):
            with t.start_as_current_span("llm.queue.wait"):
                await _acquire_slot(model)
            acquired = True
            inf_span = t.start_span("llm.inference")
            with otel_trace.use_span(inf_span, end_on_exit=False):
                gen_start = time.perf_counter()
                agen = backend.generate_stream(req)
                first = await agen.__anext__()
    except BaseException as exc:
        if agen is not None:
            await agen.aclose()
        if acquired:
            _queue.release()
        IN_FLIGHT.labels(model=model).dec()
        for s in (inf_span, span):
            if s is not None:
                if isinstance(exc, Exception):
                    s.record_exception(exc)
                s.end()
        if not isinstance(exc, Exception):
            raise

        if isinstance(exc, StopAsyncIteration):
            status, err_type, message = 502, "upstream_error", "backend produced no output"
        else:
            status, err_type = _classify_upstream(exc)
            message = str(exc)

        REQUEST_COUNT.labels(model=model, status="error", stream="true").inc()
        REQUEST_DURATION.labels(model=model, stream="true", status="error").observe(
            time.perf_counter() - arrival
        )
        log.error("request_failed", extra={"model": model, "error": message, "http_status": status})
        return _openai_error(status, message, err_type)

    async def event_stream() -> AsyncIterator[str]:
        first_token_recorded = False
        try:
            with otel_trace.use_span(span, end_on_exit=False):
                with otel_trace.use_span(inf_span, end_on_exit=False):
                    try:
                        async for token, done, token_count, meta in _prepend(first, agen):
                            if token and not first_token_recorded:
                                # From arrival, not from generation start: queue wait is
                                # latency the caller actually feels.
                                ttft = time.perf_counter() - arrival
                                TTFT.labels(model=model).observe(ttft)
                                inf_span.add_event("first_token", {
                                    "ttft_ms": round(ttft * 1000, 2),
                                    "since_generation_start_ms": round((time.perf_counter() - gen_start) * 1000, 2),
                                })
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
                                # generation time drives throughput; end-to-end drives latency
                                duration = time.perf_counter() - gen_start
                                e2e = time.perf_counter() - arrival
                                _record_backend_timings(inf_span, meta.get("timings", {}))
                                REQUEST_COUNT.labels(model=model, status="success", stream="true").inc()
                                REQUEST_DURATION.labels(model=model, stream="true", status="success").observe(e2e)
                                TOKENS_GENERATED.labels(model=model).inc(token_count)
                                THROUGHPUT.labels(model=model).observe(token_count / duration if duration > 0 else 0)
                                log.info("request_completed", extra={
                                    "model": model, "stream": True,
                                    "duration_s": round(e2e, 3),
                                    "generation_s": round(duration, 3),
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

                                # Separate usage chunk with empty choices, the shape the
                                # OpenAI API uses for stream_options.include_usage. Chunk
                                # counting is approximate; the backend's own count is not.
                                usage = meta.get("usage", {})
                                if usage and include_usage:
                                    yield "data: " + json.dumps({
                                        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                                        "object": "chat.completion.chunk",
                                        "model": model,
                                        "choices": [],
                                        "usage": usage,
                                    }) + "\n\n"
                                yield "data: [DONE]\n\n"
                    except Exception as exc:
                        # Past the first chunk the status is already sent, so a failure
                        # can only be reported in-band. OpenAI's nested error shape: the
                        # SDK reads .message off it and raises APIError.
                        REQUEST_COUNT.labels(model=model, status="error", stream="true").inc()
                        REQUEST_DURATION.labels(model=model, stream="true", status="error").observe(
                            time.perf_counter() - arrival
                        )
                        log.error("request_failed", extra={"model": model, "error": str(exc)})
                        inf_span.record_exception(exc)
                        yield "data: " + json.dumps({
                            "error": {
                                "message": str(exc),
                                "type": "upstream_error",
                                "param": None,
                                "code": None,
                            }
                        }) + "\n\n"
                        yield "data: [DONE]\n\n"
        finally:
            _queue.release()
            IN_FLIGHT.labels(model=model).dec()
            inf_span.end()
            span.end()

    return StreamingResponse(event_stream(), media_type="text/event-stream")

async def _sync_response(req: ChatCompletionRequest, tier: str, arrival: float) -> ChatCompletionResponse:
    model = req.model
    t = get_tracer()
    IN_FLIGHT.labels(model=model).inc()
    try:
        with t.start_as_current_span("llm.request") as span:
            _set_span_attrs(span, req, tier)

            with t.start_as_current_span("llm.queue.wait"):
                await _acquire_slot(model)
            try:
                with t.start_as_current_span("llm.inference") as inf_span:
                    start = time.perf_counter()
                    try:
                        text, prompt_tok, completion_tok, meta = await backend.generate(req)
                    except Exception as exc:
                        REQUEST_COUNT.labels(model=model, status="error", stream="false").inc()
                        REQUEST_DURATION.labels(model=model, stream="false", status="error").observe(
                            time.perf_counter() - arrival
                        )
                        status, err_type = _classify_upstream(exc)
                        log.error("request_failed", extra={
                            "model": model, "error": str(exc), "http_status": status,
                        })
                        inf_span.record_exception(exc)
                        raise _UpstreamFailure(status, str(exc), err_type)

                    # generation time drives throughput; end-to-end drives latency
                    duration = time.perf_counter() - start
                    e2e = time.perf_counter() - arrival
                    _record_backend_timings(inf_span, meta.get("timings", {}))
                    REQUEST_COUNT.labels(model=model, status="success", stream="false").inc()
                    REQUEST_DURATION.labels(model=model, stream="false", status="success").observe(e2e)
                    TOKENS_GENERATED.labels(model=model).inc(completion_tok)
                    THROUGHPUT.labels(model=model).observe(completion_tok / duration if duration > 0 else 0)
                    log.info("request_completed", extra={
                        "model": model, "stream": False,
                        "duration_s": round(e2e, 3),
                        "generation_s": round(duration, 3),
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
    finally:
        IN_FLIGHT.labels(model=model).dec()
