from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "llm_requests_total",
    "Total LLM requests",
    ["model", "status", "stream"],
)

REQUEST_DURATION = Histogram(
    "llm_request_duration_seconds",
    "End-to-end request duration: arrival to last byte, including queue wait. "
    "Failures are recorded too — filter on status so a burst of fast errors "
    "doesn't drag the success quantiles down. Per-phase breakdown is on the trace spans",
    ["model", "stream", "status"],
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 10.0, 30.0, 60.0],
)

TTFT = Histogram(
    "llm_time_to_first_token_seconds",
    "Time from request arrival to the first streamed token, including queue wait",
    ["model"],
    buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.5, 10.0],
)

TOKENS_GENERATED = Counter(
    "llm_tokens_generated_total",
    "Total tokens generated",
    ["model"],
)

IN_FLIGHT = Gauge(
    "llm_active_requests",
    "Requests accepted and not yet fully returned, streaming counted until the last chunk. "
    "Includes those still waiting for a slot: in-flight = waiting + running",
    ["model"],
)

QUEUE_WAITING = Gauge(
    "llm_queue_waiting_requests",
    "Requests blocked waiting to acquire a queue slot. "
    "Subtract from llm_active_requests to get the number actually generating",
    ["model"],
)

REQUESTS_CANCELLED = Counter(
    "llm_requests_cancelled_total",
    "Streaming requests whose client went away before the stream finished. "
    "Deliberately a separate counter rather than a status on llm_requests_total: "
    "a cancelled request has no meaningful latency or token count, so folding it "
    "into that counter would change what existing success/error ratios mean",
    ["model"],
)

FALLBACKS = Counter(
    "llm_fallbacks_total",
    "Times a tier's next backend was tried after the previous one failed. Only ever "
    "incremented before the first chunk reaches the client; after that a failure is an "
    "in-band error chunk and cannot be retried",
    ["tier", "from_kind", "to_kind", "reason"],
)

THROUGHPUT = Histogram(
    "llm_tokens_per_second",
    "Tokens generated per second",
    ["model"],
    buckets=[1, 5, 10, 20, 50, 100, 200],
)
