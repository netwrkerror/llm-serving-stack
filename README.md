# llm-serving-stack

A production-grade LLM inference stack built layer by layer — OpenAI-compatible gateway, Prometheus observability, Grafana dashboards, and benchmarking.

Backed locally by [Ollama](https://ollama.com) (Metal-accelerated on Apple Silicon). Speaks the OpenAI protocol too, so vLLM or TGI can sit behind it; only tested against Ollama's own /v1 so far.

---

## Architecture

```
Client (curl / Python / OpenAI SDK)
         │
         ▼
┌────────────────────────────┐
│     FastAPI Gateway        │  :8000
│  ─────────────────────     │
│  • OpenAI-compatible API   │
│  • Async request queue     │  ← admission control: caps in-flight requests; batching is the backend's
│  • SSE streaming           │  ← same wire format as vLLM / TGI
│  • Prometheus metrics      │  ← TTFT, latency p95, TPS, queue depth
└────────────┬───────────────┘
             │ http
             ▼
    ┌─────────────────┐
    │  Ollama Backend │  (host machine, Metal-accelerated)
    │  llama3.2:1b    │  swappable → vLLM / TGI
    └─────────────────┘

┌──────────────┐     ┌────────────┐
│  Prometheus  │◀────│  /metrics  │  scraped every 5s
│  :9090       │     └────────────┘
└──────┬───────┘
       │ PromQL
       ▼
┌────────────┐
│  Grafana   │  :3000  auto-provisioned dashboard
└────────────┘
```

### How it maps to production stacks

| Concept | vLLM / TGI | This project |
|---------|-----------|--------------|
| OpenAI-compatible API | ✓ | ✓ |
| SSE streaming | ✓ | ✓ |
| Admission control | continuous-batching scheduler | `asyncio.Semaphore`, fixed slot count |
| Batching | several sequences share a forward pass | none — the backend decides |
| KV-cache memory | PagedAttention | not managed here |
| TTFT metric | internal | Prometheus histogram |
| Tokens per second | internal | Prometheus histogram |
| Health endpoint | ✓ | `/health` |
| Prometheus metrics | ✓ | `/metrics` |

The semaphore is admission control, not batching. It caps how many requests reach the
backend at once; it cannot make them share a forward pass. PagedAttention is a separate
idea again — it is how vLLM manages KV-cache memory in pages so more sequences fit in
VRAM, which is what makes aggressive batching practical.

---

## Stack

| Layer | What it does |
|-------|-------------|
| `gateway/` | FastAPI app — admission control, sync + streaming inference, routing, auth, rate limiting |
| `observability/` | Prometheus + Grafana, Jaeger traces, Loki/Promtail logs |
| `benchmark/` | 5-scenario load test, ending oversubscribed so gateway queueing shows up |
| Routing | `model` absent or `"auto"` picks a tier from prompt complexity; naming a model is honoured as-is. `X-Model-Tier` / `X-Routed-Model` on the response |
| Auth | API key middleware — **off by default**, set `GATEWAY_AUTH_ENABLED` and `GATEWAY_API_KEYS` |
| Rate limiting | Per-key requests/min and token budget — **off by default**, set `GATEWAY_RATE_LIMIT_ENABLED` |
| Tracing | OpenTelemetry spans — **tested with Jaeger only**; any OTLP collector should work, untested |

---

## Quickstart

### Prerequisites

```bash
# Ollama (Mac)
brew install ollama
ollama serve &
ollama pull llama3.2:1b

# Docker Desktop — https://www.docker.com/products/docker-desktop/
```

### Run the full stack

```bash
docker compose up --build
```

| Service | URL |
|---------|-----|
| Gateway API docs | http://localhost:8000/docs |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Jaeger | http://localhost:16686 |
| Loki | http://localhost:3100 (query via Grafana) |

Stop and resume:
```bash
docker compose stop   # pause, data preserved
docker compose start  # resume
```

---

## Usage

### curl

```bash
# Non-streaming
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.2:1b",
    "messages": [{"role": "user", "content": "What is PagedAttention?"}]
  }' | python3 -m json.tool

# Streaming
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.2:1b",
    "messages": [{"role": "user", "content": "Explain vLLM in 3 sentences."}],
    "stream": true
  }'

# Let the gateway choose the tier: omit "model", or send "auto".
# Naming a model instead — as the two calls above do — is always honoured as-is.
curl -i http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Compare paged and contiguous KV cache."}]
  }' | grep -i "x-model-tier\|x-routed-model"
```

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="local")

# Streaming
for chunk in client.chat.completions.create(
    model="llama3.2:1b",
    messages=[{"role": "user", "content": "Explain GPU MIG in 2 sentences."}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### Tests

```bash
make test
```

Creates `.venv` on first run. Covers the rate limiter (per-key windows, both budgets,
reset) and the auth middleware (missing/bad/valid keys, open paths). It does **not**
cover the gateway's request path, backends, or metrics — those were verified by running
the stack, not by tests.

### Benchmark

```bash
python3 -m pip install httpx
python3 benchmark/bench.py
```

For a full comparison run, use the suite script instead — it launches Ollama itself and
aborts if `OLLAMA_NUM_PARALLEL` did not reach the server:

```bash
./benchmark/run_suite.sh 1    # then take screenshots
./benchmark/run_suite.sh 4
```

Measured results, with the conditions they were taken under, are in
[`benchmark/RESULTS.md`](benchmark/RESULTS.md).

#### What a run leaves behind

Runs do not destroy each other. Each writes a directory:

```
benchmark/out/run-<n>-<timestamp>/
    transcript.txt              everything the script printed
    prometheus-snapshot/        that run's TSDB, exported at the end
    traces-oversubscribed.txt   trace IDs for the c=8 scenario, with queue wait
                                and the ollama.* timings, so they can be found later
```

Jaeger keeps every run's spans — it is configured with badger on a named volume
rather than the all-in-one default of in-memory storage, which loses everything when
the container is replaced. Look a trace up by ID from `traces-oversubscribed.txt`.

Prometheus is a single instance, so `llm_*` series are cleared at the start of each
run to keep the live dashboard showing only the current one. The previous run's data
is snapshotted out first, so nothing is lost.

#### Reading an earlier run

The dashboard has a **Data source** variable at the top. It defaults to the live
Prometheus, and pointing it at an archived run needs no edits to any panel.

Serve the snapshot on a spare port:

```bash
cp -R benchmark/out/run-1-20261115-100640/prometheus-snapshot /tmp/old-run
docker run --rm -d --name prom-old -p 9091:9090 \
  -v /tmp/old-run:/prometheus \
  -v "$PWD/observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  prom/prometheus:v2.55.0 \
  --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus
```

Copy first — Prometheus writes a lock file, so it will not open a read-only mount.

In Grafana, add a Prometheus data source pointing at
`http://host.docker.internal:9091`, then open the dashboard and pick it from the
**Data source** dropdown. Every panel repopulates. Set an **absolute** time range
covering the run — the run directory's name is its start timestamp, and the
transcript records how long it took. Two runs compared side by side should use
windows of equal length.

To compare two archived runs, serve both on different ports, add both as data
sources, and switch the variable between them without touching the time range.

`docker rm -f prom-old` when finished.

#### Setting OLLAMA_NUM_PARALLEL

Ollama's concurrency is set on the **server process**, not the client. On macOS the
menu-bar app runs its own server and will respawn it without your shell's environment,
so `OLLAMA_NUM_PARALLEL=4 ollama serve` silently has no effect while the app is running —
and `pkill ollama` just hands control back to the app.

Quit the menu-bar app first, then launch the server yourself:

```bash
osascript -e 'quit app "Ollama"'
pkill ollama
OLLAMA_NUM_PARALLEL=4 ollama serve > /tmp/ollama.log 2>&1 &
```

Confirm it took effect — this must show the value you set:

```bash
grep -o "OLLAMA_NUM_PARALLEL:[0-9]*" /tmp/ollama.log | tail -1
# OLLAMA_NUM_PARALLEL:4
```

Check the log of the server you started. `~/.ollama/logs/server.log` is the menu-bar
app's server log, so it only ever reflects that server; a server launched from the shell
logs to stdout, wherever you redirect it.

---

## Observability

All three signals, correlated: a log line carries the `trace_id` of its trace.

| Signal | Where | What |
|--------|-------|------|
| Metrics | Grafana, http://localhost:3000 → **LLM Serving Stack** | rates, latency, TTFT, queue depth |
| Traces | Jaeger, http://localhost:16686 → service `llm-serving-stack` | per-request span waterfall |
| Logs | Grafana → Explore → Loki, `{container=~".*gateway.*"}` | structured JSON with `trace_id` |

### Metrics

| Metric | Description |
|--------|-------------|
| `llm_requests_total` | Request count by model, status, stream |
| `llm_active_requests` | In-flight: accepted and not yet fully returned, **including those waiting for a slot** |
| `llm_queue_waiting_requests` | Blocked waiting for a slot. In-flight minus this is the number actually generating |
| `llm_request_duration_seconds` | End-to-end, arrival to last byte, including queue wait. Labelled by `status` so failures are separable |
| `llm_time_to_first_token_seconds` | Arrival to first streamed token, **including queue wait** |
| `llm_tokens_per_second` | Generation throughput, excluding queue wait |
| `llm_tokens_generated_total` | Cumulative token count |

### Trace spans

Each request produces three spans: `llm.request` wraps `llm.queue.wait` (time to acquire
a slot) and `llm.inference` (generation). `llm.inference` carries a `first_token` event
and, on the Ollama backend, the backend's own phase timings:

| Attribute | Meaning |
|-----------|---------|
| `ollama.total_ms` | What Ollama reports for the whole call — **includes its own queue wait** |
| `ollama.load_ms` / `prompt_eval_ms` / `eval_ms` | Per-phase breakdown |
| `ollama.unattributed_ms` | `total` minus those phases. Tracks backend contention: ~1 ms for an uncontended request, ~400 ms for the last of four when `OLLAMA_NUM_PARALLEL=1` |

---

## Configuration

Everything is `GATEWAY_`-prefixed. Security features are off by default so the stack runs
with no configuration; turn them on explicitly.

| Variable | Default | Notes |
|----------|---------|-------|
| `GATEWAY_BACKEND` | `ollama` | `ollama` or `openai` |
| `GATEWAY_BACKEND_URL` | `http://localhost:11434` | must match the protocol — add `/v1` for `openai` |
| `GATEWAY_BACKEND_API_KEY` | _(empty)_ | sent as a bearer token when set |
| `GATEWAY_MAX_CONCURRENT_REQUESTS` | `4` | slot count; requests beyond this wait |
| `GATEWAY_REQUEST_TIMEOUT` | `120.0` | seconds, applied to the backend HTTP client |
| `GATEWAY_DEFAULT_MODEL` | `llama3.2:1b` | fallback only — the model used for a tier that has not been configured |
| `GATEWAY_MODEL_FAST` / `_BALANCED` / `_POWERFUL` | _(empty → `GATEWAY_DEFAULT_MODEL`)_ | routing tiers, all the same model until you pull more |
| `GATEWAY_AUTH_ENABLED` | `false` | **off by default** |
| `GATEWAY_API_KEYS` | _(empty)_ | comma-separated; `/health`, `/metrics`, `/v1/models` stay open |
| `GATEWAY_RATE_LIMIT_ENABLED` | `false` | **off by default** |
| `GATEWAY_MAX_REQUESTS_PER_MINUTE` | `60` | per key, fixed window |
| `GATEWAY_MAX_TOKENS_PER_MINUTE` | `10000` | charged upfront from `max_tokens` |
| `GATEWAY_TRACING_ENABLED` | `false` (`true` in compose) | |
| `GATEWAY_OTLP_ENDPOINT` | `http://localhost:4317` | **tested with Jaeger only** |

---

## Choosing a backend

`GATEWAY_BACKEND` selects the wire protocol, and `GATEWAY_BACKEND_URL` must match it:

| | `ollama` (default) | `openai` |
|---|---|---|
| Endpoint | `/api/chat` | `/v1/chat/completions` |
| URL form | `http://host:11434` | `http://host:11434/v1` |
| Sends | the `messages` list | the `messages` list |
| Backend timings on spans | yes | no |

```yaml
# docker-compose.yml — Ollama's own OpenAI-compatible endpoint
GATEWAY_BACKEND: "openai"
GATEWAY_BACKEND_URL: "http://host.docker.internal:11434/v1"
GATEWAY_BACKEND_API_KEY: ""   # if the backend requires one
```

Everything else — the gateway API, metrics, dashboard, traces — stays identical.

Both send roles to an endpoint that applies the model's chat template once, so prompts
tokenize identically — 27 prompt tokens for the same message on `llama3.2:1b` either way.
The native path is worth keeping because it reports per-phase timings (`ollama.load_ms`,
`prompt_eval_ms`, `eval_ms`) that the OpenAI protocol has no field for; those show up as
span attributes in Jaeger.

**Tested against Ollama's `/v1` only.** vLLM and TGI both speak this protocol, but
neither has been run here — that is Part 3, on a rented GPU. Until then, treat vLLM
and TGI support as untested.

---

## Project layout

```
llm-serving-stack/
├── gateway/
│   ├── main.py         FastAPI app — routes, admission control, SSE, spans
│   ├── backend.py      Backend protocol + Ollama and OpenAI-compatible clients
│   ├── router.py       Prompt-complexity tier selection
│   ├── auth.py         API key middleware
│   ├── limiter.py      Fixed-window request and token budget
│   ├── metrics.py      Prometheus metric definitions
│   ├── tracing.py      OpenTelemetry setup
│   ├── logger.py       JSON logging with trace_id injection
│   ├── schemas.py      OpenAI-compatible Pydantic models
│   ├── config.py       Settings via env vars
│   ├── Dockerfile
│   └── requirements.txt
├── benchmark/
│   ├── bench.py        5-scenario benchmark
│   ├── run_suite.sh    Full comparison run, aborts on a bad Ollama setting
│   └── RESULTS.md      Measured numbers, with conditions
├── tests/              Rate limiter and auth middleware (make test)
├── observability/
│   ├── prometheus.yml
│   ├── loki/           Log store config
│   ├── promtail/       Ships container logs to Loki
│   └── grafana/        Auto-provisioned datasources + dashboard
├── docker-compose.yml
```

---

## License

MIT
