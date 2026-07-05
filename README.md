# llm-serving-stack

A production-grade LLM inference stack built layer by layer — OpenAI-compatible gateway, observability, benchmarking, and multi-model routing.

## Stack

| Layer | What it does |
|-------|-------------|
| Gateway | OpenAI-compatible `/v1/chat/completions` API with request queuing and SSE streaming |
| Backend | Ollama (local, Metal-accelerated) — swappable with vLLM or TGI |
| Observability | Prometheus metrics + Grafana dashboard (TTFT, latency p95, TPS, queue depth) |
| Benchmark | 4-scenario load test — sequential vs concurrent, sync vs streaming |
| Routing _(coming)_ | Multi-model routing based on prompt complexity |
| Auth _(coming)_ | API key middleware |
| Rate limiting _(coming)_ | Per-key token budget + requests/min cap |

## Quickstart

_Setup instructions added as each layer lands._

## Architecture

_Diagram added after core layers are complete._

## License

MIT
