from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "llm_requests_total",
    "Total LLM requests",
    ["model", "status", "stream"],
)

REQUEST_DURATION = Histogram(
    "llm_request_duration_seconds",
    "End-to-end request duration: arrival to last byte, including queue wait. "
    "Per-phase breakdown lives on the trace spans",
    ["model", "stream"],
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

THROUGHPUT = Histogram(
    "llm_tokens_per_second",
    "Tokens generated per second",
    ["model"],
    buckets=[1, 5, 10, 20, 50, 100, 200],
)
