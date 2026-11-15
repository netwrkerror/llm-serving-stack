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

## Backend concurrency under load (2026-11-15)

The run the write-up cites. Supersedes the 2026-11-14 sweep below, which measured the
same effect but could not retain its traces or metrics — `run_suite.sh` used
`docker compose down -v` at the time, and Jaeger was on in-memory storage, so starting
the second run destroyed the first one's observability data. Both are now preserved.

**Conditions**

| | |
|---|---|
| Date | 2026-11-15 |
| Hardware | Apple M5 Pro |
| Ollama | 0.32.5, llama.cpp / GGML Metal |
| Model | `llama3.2:1b`, warmed directly and through the gateway before each run |
| Gateway | 4 queue slots, `GATEWAY_BACKEND=ollama` (`/api/chat`) |
| Load | streaming, `max_tokens=120`, each scenario run twice |
| Setting verified | `OLLAMA_NUM_PARALLEL:1` / `:4` read back from each server's own log |
| Run dirs | `benchmark/out/run-1-20261115-100640/`, `benchmark/out/run-4-20261115-100823/` |

192/192 requests succeeded across both runs.

### The twelve cells

Both repetitions shown, with the spread between them. The write-up quotes the means.

**`OLLAMA_NUM_PARALLEL=1`**

| scenario | metric | rep 1 | rep 2 | mean | spread |
|----------|--------|-------|-------|------|--------|
| sequential, c=1 | TTFT p50 | 0.175 | 0.175 | **0.175 s** | 0.0% |
| | TTFT p95 | 0.330 | 0.193 | **0.262 s** | 52.4% |
| | latency p50 | 0.740 | 0.830 | **0.785 s** | 11.5% |
| | tokens/sec | 102.3 | 99.3 | **100.8** | 3.0% |
| at capacity, c=4 | TTFT p50 | 2.274 | 2.251 | **2.263 s** | 1.0% |
| | TTFT p95 | 2.435 | 2.502 | **2.468 s** | 2.7% |
| | latency p50 | 3.010 | 2.980 | **2.995 s** | 1.0% |
| | tokens/sec | 104.4 | 105.9 | **105.2** | 1.4% |
| oversubscribed, c=8 | TTFT p50 | 5.324 | 5.347 | **5.335 s** | 0.4% |
| | TTFT p95 | 5.796 | 6.118 | **5.957 s** | 5.4% |
| | latency p50 | 6.220 | 6.080 | **6.150 s** | 2.3% |
| | tokens/sec | 100.7 | 97.9 | **99.3** | 2.8% |

**`OLLAMA_NUM_PARALLEL=4`**

| scenario | metric | rep 1 | rep 2 | mean | spread |
|----------|--------|-------|-------|------|--------|
| sequential, c=1 | TTFT p50 | 0.186 | 0.182 | **0.184 s** | 2.2% |
| | TTFT p95 | 0.256 | 0.229 | **0.242 s** | 11.1% |
| | latency p50 | 0.910 | 0.870 | **0.890 s** | 4.5% |
| | tokens/sec | 93.7 | 96.4 | **95.1** | 2.8% |
| at capacity, c=4 | TTFT p50 | 0.270 | 0.346 | **0.308 s** | 24.7% |
| | TTFT p95 | 0.378 | 0.571 | **0.474 s** | 40.7% |
| | latency p50 | 1.390 | 1.430 | **1.410 s** | 2.8% |
| | tokens/sec | 229.3 | 206.2 | **217.8** | 10.6% |
| oversubscribed, c=8 | TTFT p50 | 1.659 | 1.585 | **1.622 s** | 4.6% |
| | TTFT p95 | 2.238 | 1.763 | **2.000 s** | 23.7% |
| | latency p50 | 2.700 | 2.610 | **2.655 s** | 3.4% |
| | tokens/sec | 209.3 | 242.2 | **225.8** | 14.6% |

Two cells are too noisy to quote on their own: `=1` sequential TTFT p95 (52.4% spread,
the 7th of 8 samples) and `=4` at-capacity TTFT p95 (40.7%). The load-bearing figures
are tight — at-capacity TTFT p50 has 1.0% spread at `=1` and 24.7% at `=4`, against an
**7.3× difference between the settings**, and aggregate throughput agrees to within 2%
across repetitions.

### Headline

| | `=1` | `=4` | |
|---|---|---|---|
| sequential, c=1 — TTFT p50 | 0.175 s | 0.184 s | same |
| sequential, c=1 — tokens/sec | 100.8 | 95.1 | same |
| at capacity, c=4 — TTFT p50 | 2.263 s | 0.308 s | **7.3× lower** |
| at capacity, c=4 — tokens/sec | 105.2 | 217.8 | **2.07× higher** |
| oversubscribed, c=8 — TTFT p50 | 5.335 s | 1.622 s | 3.3× lower |
| oversubscribed, c=8 — tokens/sec | 99.3 | 225.8 | 2.27× higher |

At `=1` aggregate throughput is pinned near 100 tok/s at every concurrency level —
100.8, 105.2, 99.3. Concurrency buys no extra work, only queueing. The `c=1` row is the
control: identical under both settings, because with one request in flight there is
nothing to batch.

### Per-request vs aggregate throughput

Measured over the at-capacity window, where gateway queue wait is effectively zero
(max 0.3 ms), so this is purely backend behaviour.

| setting | per-request p50 | per-request p95 | aggregate | ratio |
|---------|-----------------|-----------------|-----------|-------|
| `=1` | 37.3 | 133.7 | 105.2 | 2.8× |
| `=4` | 72.2 | 103.7 | 217.8 | 3.0× |

Two different quantities. `llm_tokens_per_second` is a histogram of **per-request**
rates — one observation per request, its own tokens over its own generation time.
bench.py's `tokens/sec` is **aggregate**: all tokens over wall clock. Do not mix them.

At `=1`, four concurrent requests time-slicing one runner each generate at ~37 tok/s.
At `=4` they run together at ~72 each, so both the per-request rate and the aggregate
roughly double.

### load_ms is not model-load time

Oversubscribed scenario, n=32 per setting, split by whether the request waited at the
gateway (>50 ms) or went straight through.

**`OLLAMA_NUM_PARALLEL=1`**

| metric | group | min | median | max |
|--------|-------|-----|--------|-----|
| `load_ms` | no queue wait | 157.15 | **162.10** | 166.43 |
| | queued >50 ms | 178.96 | **264.73** | 529.99 |
| `unattributed_ms` | no queue wait | 1.80 | **855.38** | 1990.75 |
| | queued >50 ms | 1861.59 | **2094.02** | 2634.30 |
| `eval_ms` | no queue wait | 471.26 | 700.49 | 790.20 |
| | queued >50 ms | 651.96 | 820.00 | 1059.19 |
| `queue_wait_ms` | queued | 649.25 | 3060.91 | 3421.83 |

**`OLLAMA_NUM_PARALLEL=4`**

| metric | group | min | median | max |
|--------|-------|-----|--------|-----|
| `load_ms` | no queue wait | 163.35 | **170.04** | 174.08 |
| | queued >50 ms | 187.49 | **286.93** | 485.92 |
| `unattributed_ms` | no queue wait | 3.13 | **11.30** | 14.19 |
| | queued >50 ms | 3.47 | **6.96** | 27.42 |
| `eval_ms` | no queue wait | 829.76 | 956.86 | 1126.34 |
| | queued >50 ms | 878.94 | 1067.72 | 1464.42 |
| `queue_wait_ms` | queued | 1040.65 | 1312.57 | 1858.49 |

The model is warm before every run, and uncontended requests load in a tight
157–174 ms under both settings. Requests released from the gateway queue report
1.6–1.7× that — and they do so at `=4` as well, where `unattributed_ms` shows the
backend is not contended at all. So `load_duration` is not purely model load: it
inflates when requests arrive at the backend in a burst. A stronger mechanistic claim
than that is not supported by this data.

The two attributes track different queues:

- **`load_ms` follows the gateway queue** — raised for queued requests under both settings.
- **`unattributed_ms` follows backend contention** — 855–2094 ms at `=1`, 7–11 ms at `=4`,
  and at `=4` it does not rise with gateway queueing.

`eval_ms` is *higher* at `=4` (957 vs 700 median): an individual request generates more
slowly when sharing the GPU, which is the same fact as the per-request rate above.

### Two traces

Both contended, one from each run. Still queryable in Jaeger — badger storage persists
across runs.

| | `=1` — `8a1179c799bdd20856afcbd05e811f7f` | `=4` — `157f8dcd8e9e6ee98ed4c787b3676e94` |
|---|---|---|
| `llm.request` | 7253.03 ms | 3221.74 ms |
| `llm.queue.wait` | 3241.96 ms | 1858.49 ms |
| `llm.inference` | 4008.65 ms | 1358.80 ms |
| `ollama.total_ms` | 3998.78 | 1339.73 |
| `ollama.load_ms` | 380.82 | 424.27 |
| `ollama.prompt_eval_ms` | 14.80 | 17.36 |
| `ollama.eval_ms` | 968.86 | 891.21 |
| **`ollama.unattributed_ms`** | **2634.30** | **6.89** |
| `first_token` → `ttft_ms` | 6282.98 | 2329.97 |
| `first_token` → `since_generation_start_ms` | 3039.65 | 467.30 |

`load_ms` and `eval_ms` are near-identical between the two; `unattributed_ms` differs by
382×. `load_ms` is actually *higher* in the `=4` trace (424 vs 381) despite that request
facing no backend contention — consistent with the burst finding above.

### A counter caveat for screenshots

`llm_tokens_generated_total` lives in the gateway process, so clearing Prometheus series
between runs does not reset it. If the gateway container is not restarted, the "Total
Tokens Generated" panel shows the sum of every run since it started — 18,465 after these
two, against 7,674 and 7,717 actually generated. `run_suite.sh` now restarts the gateway
at the start of each run.

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
