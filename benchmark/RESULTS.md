# Measured results

Numbers recorded from actual runs, with the conditions they were taken under. Anything
not measured is not listed here.

> Tables below dated before the `/api/chat` switch were taken with the native backend
> posting a hand-built prompt string to `/api/generate`, which tokenized differently
> (32 prompt tokens vs 27 for the same message). Their token counts are not comparable
> with later runs; the latency and contention figures are unaffected.
>
> Earlier runs also predate the mid-stream error fix, when `bench.py` skipped error
> chunks and counted a failed stream as a short success. Any table from a run that hit
> upstream errors would have overstated its success rate and understated latency.

---

## Backend concurrency under load (2026-11-14)

The headline run. Full sweep at `OLLAMA_NUM_PARALLEL=1` and `=4`, via
`benchmark/run_suite.sh`, which refuses to proceed unless the setting actually reached
the server.

**Conditions**

| | |
|---|---|
| Date | 2026-11-14 |
| Hardware | Apple M5 Pro |
| Ollama | 0.32.5, llama.cpp / GGML Metal |
| Model | `llama3.2:1b`, warmed directly and through the gateway before each run |
| Gateway | 4 queue slots, `GATEWAY_BACKEND=ollama` (`/api/chat`) |
| Load | streaming, `max_tokens=120`, each scenario run twice |
| Prometheus | fresh volume per run (`docker compose down -v`) |
| Transcripts | `run-1-20261114-174843.txt`, `run-4-20261114-175727.txt` |

`tokens/sec` here is **aggregate**: all tokens ÷ wall clock. It is not the same as the
Grafana `llm_tokens_per_second` panel, which is a histogram of per-request rates — at
`=4` the median individual request ran at ~77 tok/s while the system did ~215.

### OLLAMA_NUM_PARALLEL=1

| scenario | lat p50 | lat p95 | TTFT p50 | TTFT p95 | tok/s | ok |
|----------|---------|---------|----------|----------|-------|-----|
| sequential, c=1 | 0.71 / 0.83 s | 0.94 / 0.96 s | 0.177 / 0.169 s | 0.223 / 0.202 s | 105 / 95 | 8/8 |
| at capacity, c=4 | 3.27 / 3.20 s | 3.49 / 3.44 s | 2.452 / 2.417 s | 2.684 / 2.656 s | 100 / 102 | 24/24 |
| oversubscribed, c=8 | 6.01 / 6.13 s | 6.23 / 6.32 s | 5.193 / 5.414 s | 5.492 / 5.561 s | 103 / 102 | 16/16 |

### OLLAMA_NUM_PARALLEL=4

| scenario | lat p50 | lat p95 | TTFT p50 | TTFT p95 | tok/s | ok |
|----------|---------|---------|----------|----------|-------|-----|
| sequential, c=1 | 0.76 / 0.85 s | 0.95 / 0.98 s | 0.179 / 0.184 s | 0.243 / 0.229 s | 106 / 92 | 8/8 |
| at capacity, c=4 | 1.51 / 1.40 s | 2.01 / 1.57 s | 0.204 / 0.228 s | 0.455 / 0.427 s | 202 / 228 | 24/24 |
| oversubscribed, c=8 | 2.84 / 2.46 s | 3.45 / 2.76 s | 1.767 / 1.513 s | 2.167 / 1.751 s | 214 / 234 | 16/16 |

Both repetitions are shown. Each run is 96 requests across its six scenario-runs;
192/192 succeeded over the two runs, with no errors.

### Side by side

| | `=1` | `=4` | |
|---|---|---|---|
| sequential, c=1 — TTFT p50 | 0.173 s | 0.181 s | same |
| sequential, c=1 — tok/s | 100 | 99 | same |
| at capacity, c=4 — TTFT p50 | 2.43 s | 0.216 s | **11× lower** |
| at capacity, c=4 — tok/s | 101 | 215 | **2.1× higher** |
| oversubscribed, c=8 — TTFT p50 | 5.30 s | 1.64 s | 3.2× lower |
| oversubscribed, c=8 — tok/s | 102 | 224 | 2.2× higher |

### What this shows

**At `=1`, throughput is pinned at ~100 tok/s whether 1, 4, or 8 requests are in
flight.** Concurrency buys nothing; it only makes callers wait, and TTFT climbs
0.17 s → 2.43 s → 5.30 s. At `=4` throughput roughly doubles as soon as there is
anything to batch, and TTFT at capacity is an order of magnitude lower.

The `c=1` row is the control: identical under both settings, because with one request
in flight there is nothing to batch.

The oversubscribed row isolates the gateway's own queue. At `=4` the backend is no
longer the constraint, yet TTFT still rises to 1.64 s at `c=8` — that is 8 requests
waiting on 4 slots, which is admission control working as designed, not backend
contention.

### The same effect in a single trace

Two `llm.inference` spans from these runs:

| | `=1` (trace `3f8a624`) | `=4` (trace `93e0053`) |
|---|---|---|
| `llm.queue.wait` | 3.21 s | 1.52 s |
| `ollama.total_ms` | 2957.51 | 1120.43 |
| `ollama.eval_ms` | 690.06 | 913.49 |
| `ollama.prompt_eval_ms` | 11.62 | 16.57 |
| `ollama.load_ms` | 399.34 | 181.38 |
| **`ollama.unattributed_ms`** | **1856.50** | **9.00** |
| `first_token` → `ttft_ms` | 5488.74 | 1735.98 |
| `first_token` → `since_generation_start_ms` | 2276.24 | 213.41 |

`unattributed_ms` is the term that moves: 1856 ms of the `=1` request's time inside
Ollama is attributable to neither load, prompt eval, nor its own token generation —
it is waiting behind other requests. At `=4` that collapses to 9 ms. Both traces also
show a large `queue.wait`, which is the gateway's slots, a separate queue from the
backend's.

---

## Does OLLAMA_NUM_PARALLEL change anything? (earlier probe)

Superseded by the sweep above, which uses larger samples and repetitions. Kept because
it is where the measurement method was worked out.

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
