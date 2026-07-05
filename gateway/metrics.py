from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "llm_requests_total",
    "Total LLM requests",
    ["model", "status", "stream"],
)

REQUEST_DURATION = Histogram(
    "llm_request_duration_seconds",
    "End-to-end request duration",
    ["model", "stream"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

TTFT = Histogram(
    "llm_time_to_first_token_seconds",
    "Time to first token (streaming only)",
    ["model"],
    buckets=[0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0],
)

TOKENS_GENERATED = Counter(
    "llm_tokens_generated_total",
    "Total tokens generated",
    ["model"],
)

ACTIVE_REQUESTS = Gauge(
    "llm_active_requests",
    "Currently in-flight requests",
    ["model"],
)

THROUGHPUT = Histogram(
    "llm_tokens_per_second",
    "Tokens generated per second",
    ["model"],
    buckets=[1, 5, 10, 20, 50, 100, 200],
)
