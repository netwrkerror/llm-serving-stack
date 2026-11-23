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

from .auth import APIKeyMiddleware, api_key_from, routing_policy_for
from .backend import Backend, make_backend
from .config import POLICIES, TierConfig, settings
from .limiter import rate_limiter
from .logger import log
from .metrics import (
    FALLBACKS,
    IN_FLIGHT,
    POLICY_DOWNGRADES,
    QUEUE_WAITING,
    REQUEST_COUNT,
    REQUESTS_CANCELLED,
    REQUEST_DURATION,
    THROUGHPUT,
    TOKENS_GENERATED,
    TTFT,
)
from .router import Route, RouteRefused, resolve_route
from .tracing import get_tracer, setup_tracing
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)

_queue = asyncio.Semaphore(settings.max_concurrent_requests)

# Backends by kind, built at startup and closed in lifespan. Module-level so the
# request path can reach it without a singleton per backend; empty until lifespan
# runs, which is also what makes it swappable in tests.
_backends: dict[str, Backend] = {}


def backend_for(kind: str) -> Backend:
    """The backend serving a kind, built at startup.

    A missing kind is a configuration error rather than a request error, but it
    surfaces here because that is where it first matters.
    """
    try:
        return _backends[kind]
    except KeyError:
        raise HTTPException(
            status_code=503,
            detail=f"no backend configured for kind {kind!r}; "
                   f"built kinds are {sorted(_backends) or 'none'}",
        )


async def _log_bedrock_lifecycles() -> None:
    """Report each Bedrock tier's model lifecycle status at startup.

    This exists because the model this stack was first built around — Llama 3.2 1B —
    reached end of life on Bedrock while the work was in progress, and nothing in the
    gateway would have said so: calls simply start failing with ResourceNotFoundException.
    A LEGACY warning at startup is the warning you want months before that.

    GetFoundationModel is a control-plane call, not inference, so it does not bill.
    It never blocks startup: the runtime role may not hold bedrock:GetFoundationModel,
    and a missing label is not a reason to refuse to serve.
    """
    tiers = {t: cfg for t, cfg in settings.tiers.items() if cfg.kind == "bedrock"}
    if not tiers:
        return
    impl = _backends.get("bedrock")
    if impl is None or not hasattr(impl, "model_lifecycle"):
        return

    for tier, cfg in tiers.items():
        status, error = await impl.model_lifecycle(cfg.model)
        extra = {
            "tier": tier,
            "model": cfg.model,
            "lifecycle": status,
            "region": settings.aws_region or "(credential chain)",
        }
        if error:
            log.warning("bedrock_lifecycle_unknown", extra={**extra, "error": error})
        elif status.upper() == "LEGACY":
            log.warning("bedrock_model_legacy", extra={
                **extra,
                "note": "model is in its legacy period and will reach end of life; "
                        "calls start failing with ResourceNotFoundException at EOL",
            })
        else:
            log.info("bedrock_model_lifecycle", extra=extra)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing()

    # Refuse to start on an unrecognised routing policy. Defaulting a misspelt
    # "local_only" to "auto" would hand that key exactly the paid backends it was
    # configured not to reach, and nothing downstream would look wrong.
    invalid = settings.invalid_key_policies
    if invalid:
        log.error("invalid_key_policies", extra={
            "invalid": {k[:4] + "…": p for k, p in invalid.items()},
            "valid": list(POLICIES),
        })
        raise RuntimeError(
            f"unrecognised routing policies in GATEWAY_API_KEYS: "
            f"{sorted(set(invalid.values()))}; valid policies are {', '.join(POLICIES)}"
        )

    for kind in settings.active_kinds:
        _backends[kind] = make_backend(kind)

    remote = settings.remote_tiers
    log.info("gateway_starting", extra={
        "backend_kind": settings.backend,
        "backend": settings.backend_url,
        "queue_slots": settings.max_concurrent_requests,
        "tracing": settings.tracing_enabled,
        "tiers": {t: str(cfg) for t, cfg in settings.tiers.items()},
        "backend_kinds": sorted(_backends),
    })
    # Stated explicitly, every start: a tier on a paid backend means automatic
    # routing can spend money, and that should never be something you discover
    # from a bill. Shipped defaults put all three tiers on the local backend.
    if remote:
        log.warning("remote_tiers_configured", extra={
            "remote_tiers": {t: str(cfg) for t, cfg in remote.items()},
            "note": "automatic routing can send requests to a paid backend",
        })
        # Each half of this is legal on its own: auth off is the shipped default for
        # local development, and a paid tier is a deliberate choice. Together they mean
        # anyone who can reach the port can spend money, and no key is needed to do it —
        # so there is also no routing policy available to stop them, because a policy
        # attaches to a key. Logged as its own event rather than folded into the line
        # above, so it can be alerted on.
        if not settings.auth_enabled:
            log.warning("remote_tiers_configured_without_auth", extra={
                "remote_tiers": {t: str(cfg) for t, cfg in remote.items()},
                "auth_enabled": False,
                "note": "a paid backend is reachable by any caller that can reach this "
                        "port, and with no API key there is no routing policy that can "
                        "restrict it; set GATEWAY_AUTH_ENABLED and GATEWAY_API_KEYS",
            })
    else:
        log.info("all_tiers_local", extra={
            "tiers": {t: str(cfg) for t, cfg in settings.tiers.items()},
        })

    await _log_bedrock_lifecycles()

    yield

    for kind, impl in _backends.items():
        try:
            await impl.close()
        except Exception as exc:  # one bad backend must not block the others
            log.warning("backend_close_failed", extra={"kind": kind, "error": str(exc)})
    _backends.clear()
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
    # Every built backend is checked, and "status" stays ok/degraded so the
    # benchmark scripts' grep for '"status":"ok"' keeps working.
    checks = {}
    for kind, impl in _backends.items():
        try:
            checks[kind] = bool(await impl.health_check())
        except Exception:
            checks[kind] = False
    return {
        "status": "ok" if checks and all(checks.values()) else "degraded",
        # The server's own wall clock. Lets a caller measure the offset between its
        # clock and this process's — needed whenever gateway timestamps are lined up
        # against events observed elsewhere, and the gateway may be in a VM whose
        # clock is not the host's.
        "now_ms": _now_ms(),
        "backend": settings.backend_url,
        "backend_kind": settings.backend,
        "backends": checks,
        "tiers": {t: str(cfg) for t, cfg in settings.tiers.items()},
        "queue_slots": settings.max_concurrent_requests,
    }


@app.get("/v1/models")
async def list_models():
    """Models each backend reports, plus every configured tier model.

    The tier models are added because a backend need not be able to enumerate
    itself — Bedrock cannot, from the runtime client — and a configured model
    should still be discoverable. A backend that fails to list is skipped rather
    than failing the whole response; it would otherwise take the others down
    with it.
    """
    seen: dict[str, str] = {}
    errors: dict[str, str] = {}

    for tier_cfg in settings.tiers.values():
        seen.setdefault(tier_cfg.model, tier_cfg.kind)

    for kind, impl in _backends.items():
        try:
            for model in await impl.list_models():
                seen.setdefault(model, kind)
        except Exception as exc:
            errors[kind] = str(exc)

    if not seen and errors:
        raise HTTPException(status_code=503, detail="; ".join(
            f"{k}: {v}" for k, v in errors.items()))

    body: dict = {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "owned_by": kind}
            for model, kind in sorted(seen.items())
        ],
    }
    if errors:
        # Surfaced rather than swallowed: a partial list should say so.
        body["errors"] = errors
    return body


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    arrival = time.perf_counter()
    # Correlates every log line for one request. Without it, two concurrent requests
    # for the same model are indistinguishable in the log, which is exactly the case
    # the queued-disconnect experiment has to take apart.
    req_id = uuid.uuid4().hex[:8]
    api_key = api_key_from(request)

    allowed, reason = rate_limiter.check_request(api_key)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    allowed, reason = rate_limiter.check_tokens(api_key, req.max_tokens or 512)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    policy = routing_policy_for(request)
    try:
        route = resolve_route(req, policy)
    except RouteRefused as refused:
        # A refusal is reported, not worked around. The alternative — quietly serving a
        # cheaper tier than was asked for — is the same class of bug as quietly serving
        # a paid one, just in the direction nobody notices until the answers are worse.
        log.warning("route_refused", extra={
            "policy": policy, "http_status": refused.status, "error": refused.message,
        })
        resp = _openai_error(refused.status, refused.message, refused.err_type)
        resp.headers["X-Routing-Policy"] = policy
        return resp

    routed_model = route.model
    # A named model that matches no tier has no tier; the header still has to say
    # something, and "none" is the honest answer.
    tier = route.tier or "none"
    req = req.model_copy(update={"model": routed_model})

    log.info("request_received", extra={
        "req_id": req_id,
        "arrived_at_ms": _now_ms(),
        "model": routed_model,
        "tier": tier,
        "backend_kind": route.kind,
        "explicit_model": route.explicit,
        "policy": policy,
        "route_source": route.source,
        "chain": [str(cfg) for cfg in route.chain],
        "stream": req.stream,
    })
    if route.downgraded_from:
        # Allowed — automatic routing was never promised a tier — but never silent, and
        # counted rather than only logged: X-Model-Tier below already reports the tier
        # that served the request, so without this counter the fact that a *different*
        # tier was asked for leaves no trace a dashboard can see.
        POLICY_DOWNGRADES.labels(from_tier=route.downgraded_from, to_tier=tier).inc()
        log.info("tier_downgraded", extra={
            "policy": policy, "from_tier": route.downgraded_from, "to_tier": tier,
            "reason": "policy forbids the original tier's backend",
        })

    if req.stream:
        resp, used = await _stream_response(req, tier, arrival, route, req_id)
    else:
        # JSONResponse rather than the Pydantic model directly, so the headers below
        # apply to both paths.
        try:
            body, used = await _sync_response(req, tier, arrival, route, req_id)
            resp = JSONResponse(content=body.model_dump())
        except _UpstreamFailure as fail:
            resp = _openai_error(fail.status, fail.message, fail.err_type)
            used = TierConfig(route.kind, route.model)

    # Report what answered, not what was planned: with a fallback chain those differ.
    resp.headers["X-Model-Tier"] = tier
    resp.headers["X-Routed-Model"] = used.model
    resp.headers["X-Routed-Backend"] = used.kind
    resp.headers["X-Routing-Policy"] = policy
    resp.headers["X-Request-Id"] = req_id
    return resp


def _now_ms() -> float:
    """Wall-clock milliseconds, for timestamps that are compared across processes.

    Deliberately not perf_counter: a monotonic clock is only meaningful inside one
    process, and these events are lined up against a client on the host and an Ollama
    log stamped by the harness. Wall clock is the only shared reference, and the
    harness measures the offset rather than assuming it is zero.
    """
    return time.time() * 1000.0


async def _acquire_slot(model: str, req_id: str = "") -> None:
    """Wait for a queue slot, counting this request as queued while it blocks.

    Logs the moment the slot is granted. That instant is the end of the llm.queue.wait
    span, so a request queued behind another is admitted exactly here — which is the
    event the queued-disconnect experiment measures against Ollama's own cancellation.
    """
    QUEUE_WAITING.labels(model=model).inc()
    waited_from = time.perf_counter()
    try:
        await _queue.acquire()
    finally:
        QUEUE_WAITING.labels(model=model).dec()
    log.info("slot_acquired", extra={
        "req_id": req_id,
        "model": model,
        "waited_ms": round((time.perf_counter() - waited_from) * 1000, 3),
        "at_ms": _now_ms(),
    })


def _set_span_attrs(span, req: ChatCompletionRequest, tier: str, kind: str = "") -> None:
    span.set_attribute("llm.model", req.model)
    span.set_attribute("llm.tier", tier)
    if kind:
        span.set_attribute("llm.backend", kind)
    span.set_attribute("llm.stream", req.stream)
    span.set_attribute("llm.max_tokens", req.max_tokens or 512)


def _record_backend_timings(span, timings: dict) -> dict:
    """Attach the backend's self-reported durations to the inference span.

    Only Ollama reports these; OpenAI-compatible backends send nothing, so skip quietly.

    unattributed_ms is total minus the phases Ollama attributes to this request. It is the
    term that tracks backend contention: it grows when requests wait on each other inside
    Ollama, while each request's own eval time stays flat. Note total_duration already
    includes that wait, so wall-clock-minus-total measures only HTTP overhead, not queueing.

    For the measured values, and the scenario and run directory behind each, see the
    OLLAMA_NUM_PARALLEL sweeps in benchmark/RESULTS.md. Figures are not repeated here so
    there is one place to keep them honest.
    """
    if not timings:
        return {}
    recorded = {}
    for name, value in timings.items():
        recorded[name] = round(value, 2)
        span.set_attribute(f"ollama.{name}", recorded[name])
    if "total_ms" in timings:
        phases = sum(timings.get(k, 0.0) for k in ("load_ms", "prompt_eval_ms", "eval_ms"))
        recorded["unattributed_ms"] = round(timings["total_ms"] - phases, 2)
        span.set_attribute("ollama.unattributed_ms", recorded["unattributed_ms"])
    # Returned as well as set on the span so it can be logged: reading one number back
    # out of a trace is a lot of machinery for a value the request already has.
    return recorded


def _completion_tokens(meta: dict, delta_count: int) -> int:
    """Generated tokens for the token and throughput metrics.

    The backend's own count when it reports one, the streamed delta count otherwise.
    These are not the same thing: for Ollama one delta is one token, so the two agree,
    but a Converse `contentBlockDelta` can carry several tokens at once. Counting
    deltas there would understate Bedrock throughput on the dashboards while the cost
    table — which reads `usage` — was right, and the two would quietly disagree.

    The fallback matters for more than safety: a cancelled request never receives the
    final chunk, so usage never arrives and the delta count is all there is.
    """
    usage = meta.get("usage") or {}
    reported = usage.get("completion_tokens")
    if isinstance(reported, int) and reported >= 0:
        return reported
    return delta_count


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


# A 4xx that means the *request* is wrong, not the backend. Retrying these against
# another backend buys the same error at a second backend's price: an unknown model id
# is unknown there too, and a prompt over the context limit is over it there too.
#
# 401 and 403 DO fall back, which is the one entry here that could reasonably go the
# other way. The reasoning: credentials are per backend, not per request. A 401 from
# Bedrock says this gateway's AWS credentials are wrong or expired; it says nothing
# about whether the next backend in the chain can serve the request, and that backend
# very often has entirely separate credentials (or none, for a local one). Treating it
# as non-retryable would mean one misconfigured backend takes down a tier that has a
# working fallback sitting behind it.
#
# The cost of being wrong in this direction is bounded and visible: a second attempt
# that also fails, and an llm_fallbacks_total{reason="upstream_error"} series that makes
# a chain-wide auth problem obvious on a dashboard. The cost of the other direction is a
# tier that is down while a healthy backend goes unused. Untested against a real 401 —
# no credentialed backend has been configured yet.
#
# Everything else — connection failures, timeouts, 5xx and throttling — is worth another
# backend's attempt.
_NO_FALLBACK_STATUSES = frozenset({400, 404, 413, 422})


def _should_fallback(exc: BaseException) -> tuple[bool, str]:
    """(may we try the next backend, a short reason for the metric)."""
    if isinstance(exc, StopAsyncIteration):
        return True, "no_output"
    if not isinstance(exc, Exception):
        return False, "cancelled"
    status, err_type = _classify_upstream(exc)
    if status in _NO_FALLBACK_STATUSES:
        return False, err_type
    return True, err_type


def _openai_error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": None}},
    )


async def _prepend(first, rest: AsyncIterator) -> AsyncIterator:
    yield first
    async for item in rest:
        yield item


async def _open_first_chunk(route: Route, req: ChatCompletionRequest, span):
    """Walk the tier's chain until one backend yields its first chunk.

    Returns (agen, first, cfg) for whichever backend answered. Raises the last
    exception if none did.

    This is the only place fallback can happen, and that is a protocol constraint
    rather than a choice: once a StreamingResponse is returned the 200 and the SSE
    content-type are committed, so a later failure can only be an in-band error chunk.
    Pulling the first chunk eagerly is what buys the chance to try another backend
    while a real status code is still possible.
    """
    last_exc: BaseException | None = None
    for index, cfg in enumerate(route.chain):
        impl = backend_for(cfg.kind)
        attempt = req.model_copy(update={"model": cfg.model}) if cfg.model != req.model else req
        agen = impl.generate_stream(attempt)
        try:
            first = await agen.__anext__()
            if index:
                span.set_attribute("llm.fallback", f"{route.chain[0].kind}->{cfg.kind}")
                span.set_attribute("llm.fallback.attempts", index + 1)
            return agen, first, cfg
        except BaseException as exc:
            await agen.aclose()
            last_exc = exc
            may_retry, reason = _should_fallback(exc)
            is_last = index == len(route.chain) - 1
            if not may_retry or is_last:
                raise
            nxt = route.chain[index + 1]
            FALLBACKS.labels(tier=route.tier or "none", from_kind=cfg.kind,
                             to_kind=nxt.kind, reason=reason).inc()
            log.warning("backend_fallback", extra={
                "tier": route.tier or "none",
                "from_kind": cfg.kind, "from_model": cfg.model,
                "to_kind": nxt.kind, "to_model": nxt.model,
                "reason": reason, "error": str(exc),
            })
    raise last_exc if last_exc else RuntimeError("empty backend chain")


async def _stream_response(req: ChatCompletionRequest, tier: str, arrival: float,
                           route: Route, req_id: str = "") -> tuple[Response, TierConfig]:
    """Open the backend stream, falling back while that is still possible, and return
    a streaming response plus the backend that actually answered.

    Failures after the first chunk stay 200 + an error chunk, as the protocol requires,
    and are never retried.
    """
    model = route.model
    kind = route.kind
    # Absent stream_options means send usage; an explicit value wins either way.
    include_usage = req.stream_options.include_usage if req.stream_options else True
    t = get_tracer()

    IN_FLIGHT.labels(model=model).inc()
    span = t.start_span("llm.request")
    _set_span_attrs(span, req, tier, kind)

    acquired = False
    inf_span = None
    try:
        with otel_trace.use_span(span, end_on_exit=False):
            with t.start_as_current_span("llm.queue.wait"):
                await _acquire_slot(model, req_id)
            acquired = True
            inf_span = t.start_span("llm.inference")
            with otel_trace.use_span(inf_span, end_on_exit=False):
                gen_start = time.perf_counter()
                agen, first, used = await _open_first_chunk(route, req, span)
                # The chain entry that answered may not be the primary.
                model, kind = used.model, used.kind
                inf_span.set_attribute("llm.backend", kind)
                inf_span.set_attribute("llm.model", model)
    except BaseException as exc:
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
        log.error("request_failed", extra={
            "model": model, "backend_kind": kind,
            "error": message, "http_status": status,
        })
        return _openai_error(status, message, err_type), TierConfig(kind, model)

    async def event_stream() -> AsyncIterator[str]:
        first_token_recorded = False
        # Stays "cancelled" unless we reach the end of the stream or fail in-band.
        # Nothing else can distinguish a client that hung up from one that waited:
        # a disconnect unwinds this generator without raising anything catchable
        # here, so the absence of a better outcome *is* the signal.
        outcome = "cancelled"
        # Counted here rather than taken from the backend's cumulative count, so a
        # cancelled request can still report what the client actually received.
        sent_tokens = 0
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
                                sent_tokens += 1

                            if done:
                                # generation time drives throughput; end-to-end drives latency
                                duration = time.perf_counter() - gen_start
                                e2e = time.perf_counter() - arrival
                                ollama_timings = _record_backend_timings(
                                    inf_span, meta.get("timings", {}))
                                billed = _completion_tokens(meta, token_count)
                                REQUEST_COUNT.labels(model=model, status="success", stream="true").inc()
                                REQUEST_DURATION.labels(model=model, stream="true", status="success").observe(e2e)
                                TOKENS_GENERATED.labels(model=model).inc(billed)
                                THROUGHPUT.labels(model=model).observe(billed / duration if duration > 0 else 0)
                                log.info("request_completed", extra={
                                    "req_id": req_id,
                                    "at_ms": _now_ms(),
                                    "ollama_unattributed_ms": ollama_timings.get("unattributed_ms"),
                                    "ollama_total_ms": ollama_timings.get("total_ms"),
                                    "ollama_eval_ms": ollama_timings.get("eval_ms"),
                                    "ollama_prompt_eval_ms": ollama_timings.get("prompt_eval_ms"),
                                    "model": model, "stream": True,
                                    "duration_s": round(e2e, 3),
                                    "generation_s": round(duration, 3),
                                    "tokens": billed,
                                    # What the client actually received, which is a delta
                                    # count and so not always the token count.
                                    "tokens_sent": sent_tokens,
                                    "tps": round(billed / duration if duration > 0 else 0, 1),
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
                                outcome = "completed"
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
                        outcome = "error"
        finally:
            # Close the backend generator here, not in _prepend, and before the slot
            # is handed back.
            #
            # Here rather than _prepend: this closure owns the resources — agen, the
            # slot, the spans — while _prepend is a generic adapter that owns nothing.
            # Putting it there would also not help, because nothing holds a reference
            # to the _prepend generator either (it is a temporary of the `async for`),
            # so it has the identical finalization problem one level further down.
            #
            # Explicitly rather than implicitly: on a disconnect this generator is
            # left suspended, and what closed the upstream even before this call
            # existed was async-generator finalization — CPython dropping the last
            # reference and asyncio's finalizer hook scheduling aclose(). Closing the
            # backend by name says what we mean instead of relying on that cascade.
            #
            # What this does NOT do is make cleanup guaranteed, and the measurements
            # say so. Reaching this `finally` depends on the same finalization, so
            # when finalization is late the close is late too: in one disconnect out
            # of ~90 measured, Ollama ran the request to its full max_tokens anyway
            # (benchmark/RESULTS.md, "Client disconnect"). Typical behaviour is a
            # cancel ~30-50ms after the client leaves, before and after this change.
            # Making it deterministic needs disconnect detection that does not run
            # inside the generator being cleaned up — an http.disconnect listener or
            # a pump task owned by the request. Not done here; see RESULTS.md.
            #
            # Before the release because the ordering is cheap to get right: the slot
            # used to be freed while the upstream request was still open, so briefly
            # the gateway believed it had capacity Ollama was still using.
            #
            # Unconditional because the normal path has already exhausted agen, which
            # makes this a no-op there.
            # Stamped before aclose so the ordering of "the finally began" against
            # "the upstream was closed" is visible rather than inferred.
            finally_at = _now_ms()
            aclose_from = time.perf_counter()
            try:
                await agen.aclose()
            except Exception as exc:  # never let cleanup mask the response
                log.warning("backend_close_failed", extra={"model": model, "error": str(exc)})
            aclose_ms = round((time.perf_counter() - aclose_from) * 1000, 3)
            if outcome == "cancelled":
                log.info("backend_stream_closed", extra={
                    "req_id": req_id,
                    "model": model,
                    "finally_at_ms": finally_at,
                    "at_ms": _now_ms(),
                    "aclose_ms": aclose_ms,
                })

            if outcome == "cancelled":
                REQUESTS_CANCELLED.labels(model=model).inc()
                log.info("request_cancelled", extra={
                    "req_id": req_id,
                    "model": model,
                    "stream": True,
                    "duration_s": round(time.perf_counter() - arrival, 3),
                    "finally_at_ms": finally_at,
                    "at_ms": _now_ms(),
                    "tokens_sent": sent_tokens,
                })
            span.set_attribute("llm.outcome", outcome)
            inf_span.set_attribute("llm.outcome", outcome)

            _queue.release()
            IN_FLIGHT.labels(model=model).dec()
            inf_span.end()
            span.end()

    return (StreamingResponse(event_stream(), media_type="text/event-stream"),
            TierConfig(kind, model))

async def _generate_with_fallback(route: Route, req: ChatCompletionRequest, span):
    """Walk the tier's chain for a non-streaming request.

    Fallback is unconditionally safe here: nothing is sent to the client until the whole
    completion exists, so there is no first-byte deadline to respect. The same
    non-retryable statuses still apply — a bad model id is bad everywhere.
    """
    last_exc: BaseException | None = None
    for index, cfg in enumerate(route.chain):
        impl = backend_for(cfg.kind)
        attempt = req.model_copy(update={"model": cfg.model}) if cfg.model != req.model else req
        try:
            result = await impl.generate(attempt)
            if index:
                span.set_attribute("llm.fallback", f"{route.chain[0].kind}->{cfg.kind}")
                span.set_attribute("llm.fallback.attempts", index + 1)
            return result, cfg
        except BaseException as exc:
            last_exc = exc
            may_retry, reason = _should_fallback(exc)
            is_last = index == len(route.chain) - 1
            if not may_retry or is_last:
                raise
            nxt = route.chain[index + 1]
            FALLBACKS.labels(tier=route.tier or "none", from_kind=cfg.kind,
                             to_kind=nxt.kind, reason=reason).inc()
            log.warning("backend_fallback", extra={
                "tier": route.tier or "none",
                "from_kind": cfg.kind, "from_model": cfg.model,
                "to_kind": nxt.kind, "to_model": nxt.model,
                "reason": reason, "error": str(exc), "stream": False,
            })
    raise last_exc if last_exc else RuntimeError("empty backend chain")


async def _sync_response(req: ChatCompletionRequest, tier: str, arrival: float,
                         route: Route, req_id: str = "") -> tuple[ChatCompletionResponse, TierConfig]:
    model = route.model
    kind = route.kind
    used = TierConfig(route.kind, route.model)
    t = get_tracer()
    IN_FLIGHT.labels(model=model).inc()
    try:
        with t.start_as_current_span("llm.request") as span:
            _set_span_attrs(span, req, tier, kind)

            with t.start_as_current_span("llm.queue.wait"):
                await _acquire_slot(model, req_id)
            try:
                with t.start_as_current_span("llm.inference") as inf_span:
                    start = time.perf_counter()
                    try:
                        (text, prompt_tok, completion_tok, meta), used = \
                            await _generate_with_fallback(route, req, span)
                        model, kind = used.model, used.kind
                        inf_span.set_attribute("llm.backend", kind)
                        inf_span.set_attribute("llm.model", model)
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
                        # Same attribute as the streaming path, so a dashboard can
                        # group by outcome without caring which path served it. There
                        # is no "cancelled" here: a non-streaming client that hangs up
                        # is not observable from inside this function — the response is
                        # built before anything is written back.
                        span.set_attribute("llm.outcome", "error")
                        inf_span.set_attribute("llm.outcome", "error")
                        raise _UpstreamFailure(status, str(exc), err_type)

                    # generation time drives throughput; end-to-end drives latency
                    duration = time.perf_counter() - start
                    e2e = time.perf_counter() - arrival
                    ollama_timings = _record_backend_timings(inf_span, meta.get("timings", {}))
                    span.set_attribute("llm.outcome", "completed")
                    inf_span.set_attribute("llm.outcome", "completed")
                    REQUEST_COUNT.labels(model=model, status="success", stream="false").inc()
                    REQUEST_DURATION.labels(model=model, stream="false", status="success").observe(e2e)
                    TOKENS_GENERATED.labels(model=model).inc(completion_tok)
                    THROUGHPUT.labels(model=model).observe(completion_tok / duration if duration > 0 else 0)
                    log.info("request_completed", extra={
                        "req_id": req_id,
                        "at_ms": _now_ms(),
                        "ollama_unattributed_ms": ollama_timings.get("unattributed_ms"),
                        "ollama_total_ms": ollama_timings.get("total_ms"),
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
                    ), used
            finally:
                _queue.release()
    finally:
        IN_FLIGHT.labels(model=model).dec()
