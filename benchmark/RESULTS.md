# Measured results

Numbers recorded from actual runs, with the conditions they were taken under. Anything
not measured is not listed here.

> Tables below dated before the `/api/chat` switch were taken with the native backend
> posting a hand-built prompt string to `/api/generate`, which tokenized differently
> (32 prompt tokens vs 27 for the same message). Their token counts are not comparable
> with later runs; the latency and contention figures are unaffected.

---

## Does OLLAMA_NUM_PARALLEL change anything?

**Conditions**

| | |
|---|---|
| Date | 2026-11-13 |
| Hardware | Apple M5 Pro |
| Ollama | 0.32.5 |
| Runner | llama.cpp / GGML Metal |
| Model | `llama3.2:1b`, warmed before each run |
| Gateway | 4 queue slots (`GATEWAY_MAX_CONCURRENT_REQUESTS=4`) |
| Load | 4 concurrent **streaming** requests, `max_tokens=60` |
| Why 4 | Matches the gateway's slot count, so nothing queues at the gateway and all 4 reach Ollama at once. This isolates Ollama's own scheduling. |

Timings are read off the `llm.inference` span in Jaeger.
`unattributed_ms = ollama.total_ms − (load_ms + prompt_eval_ms + eval_ms)`.

### OLLAMA_NUM_PARALLEL=1

| # | span_ms | total_ms | eval_ms | unattributed_ms |
|---|---------|----------|---------|-----------------|
| 0 | 348.2   | 338.0    | 118.6   | 1.3             |
| 1 | 754.2   | 746.2    | 128.9   | 419.0           |
| 2 | 614.6   | 608.9    | 129.7   | 282.3           |
| 3 | 476.4   | 471.1    | 126.5   | 148.3           |

**wall = 0.79s**, contention spread = 418ms

### OLLAMA_NUM_PARALLEL=4

| # | span_ms | total_ms | eval_ms | unattributed_ms |
|---|---------|----------|---------|-----------------|
| 0 | 593.0   | 589.3    | 357.9   | 1.9             |
| 1 | 584.3   | 576.1    | 244.0   | 33.3            |
| 2 | 544.0   | 537.2    | 205.6   | 32.6            |
| 3 | 535.1   | 525.4    | 196.4   | 32.1            |

**wall = 0.64s**, contention spread = 31ms

### What this shows

Requests genuinely run concurrently at `=4`:

- Contention time collapses from up to 419ms down to a flat ~32ms.
- Per-request durations go from staggered (348–754ms) to uniform (535–593ms).
- Wall time drops 19%.
- `prompt_eval_ms` rises to ~106ms on requests 1–3 at `=4` — the batched prefill, i.e.
  the requests sharing a forward pass.

### Two corrections worth keeping

**Ollama's `total_duration` already includes its own queue wait.** An earlier attempt
measured `inference span duration − ollama.total_ms` on the assumption that the gap was
backend queue time. It isn't: that gap is flat at 4–9ms in every configuration, because
it only contains HTTP round-trip overhead between the gateway container and Ollama on the
host. The term that actually tracks contention is `total_ms` minus the phases Ollama
attributes to the request, which is what `ollama.unattributed_ms` reports.

**The first version of this experiment was invalid.** Both runs were secretly at `=1` —
see the launch note in the README. An earlier draft concluded "Ollama serializes
regardless of the setting." That was an artifact of the broken setup and is not true.
