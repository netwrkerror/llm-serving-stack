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
