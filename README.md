# llm-serving-stack

A production-grade LLM inference stack built layer by layer — OpenAI-compatible gateway, Prometheus observability, Grafana dashboards, and benchmarking.

Backed locally by [Ollama](https://ollama.com) (Metal-accelerated on Apple Silicon). Swappable with vLLM or TGI in production by changing one env var.

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
│  • Async request queue     │  ← models vLLM's continuous batching scheduler
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
| Request queue | PagedAttention scheduler | `asyncio.Semaphore` |
| TTFT metric | Internal | Prometheus histogram |
| Tokens per second | Internal | Prometheus histogram |
| Health endpoint | ✓ | `/health` |
| Prometheus metrics | ✓ | `/metrics` |

---

## Stack

| Layer | What it does |
|-------|-------------|
| `gateway/` | FastAPI app — request queuing, sync + streaming inference, Prometheus metrics |
| `observability/` | Prometheus scrape config + auto-provisioned Grafana dashboard |
| `benchmark/` | 4-scenario load test (sequential vs concurrent, sync vs streaming) |
| Routing _(coming)_ | Multi-model routing based on prompt complexity |
| Auth _(coming)_ | API key middleware |
| Rate limiting _(coming)_ | Per-key token budget + requests/min cap |
| Tracing _(coming)_ | OpenTelemetry spans → Jaeger / Tempo |

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
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

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

### Benchmark

```bash
python3 -m pip install httpx
python3 benchmark/bench.py
```

Measured results, with the conditions they were taken under, are in
[`benchmark/RESULTS.md`](benchmark/RESULTS.md).

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

Open **http://localhost:3000** → Dashboards → **LLM Serving Stack**.

| Metric | Description |
|--------|-------------|
| `llm_requests_total` | Request rate by status |
| `llm_active_requests` | Live queue depth |
| `llm_request_duration_seconds` | End-to-end latency histogram (p50 / p95) |
| `llm_time_to_first_token_seconds` | TTFT — key UX metric for streaming |
| `llm_tokens_per_second` | Generation throughput |
| `llm_tokens_generated_total` | Cumulative token count |

---

## Swapping to a real GPU backend

Change one env var in `docker-compose.yml`:

```yaml
# vLLM
GATEWAY_OLLAMA_BASE_URL: "http://your-vllm-host:8000"

# TGI
GATEWAY_OLLAMA_BASE_URL: "http://your-tgi-host:80"
```

Everything else — API, metrics, dashboard — stays identical.

---

## Project layout

```
llm-serving-stack/
├── gateway/
│   ├── main.py         FastAPI app — routes, lifespan, semaphore queue
│   ├── backend.py      Ollama HTTP client (sync + streaming)
│   ├── metrics.py      Prometheus metric definitions
│   ├── schemas.py      OpenAI-compatible Pydantic models
│   ├── config.py       Settings via env vars
│   ├── Dockerfile
│   └── requirements.txt
├── benchmark/
│   └── bench.py        4-scenario benchmark
├── observability/
│   ├── prometheus.yml
│   └── grafana/        Auto-provisioned datasource + dashboard
├── docker-compose.yml
```

---

## License

MIT
