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

## Bedrock backend: model availability, and what is not measured (2026-11-15)

The `BedrockBackend` is implemented and unit-tested. **No Bedrock call has been made
from this repo.** There are no AWS credentials on this machine — no `~/.aws`, no
`AWS_*` environment variables, no AWS CLI — so every Bedrock latency, cost and
cancellation number below reads **untested**, not zero and not estimated.

What *is* established: which Llama models Bedrock still offers, which is the headline
finding, because it invalidates the comparison this stage was designed around.

### Llama 3.2 1B is gone from Bedrock

The plan was to run the same model locally and on Bedrock so quality was controlled and
only the backend differed. That is no longer possible.

| Model | Bedrock model id | Lifecycle | EOL date | Usable today |
|-------|------------------|-----------|----------|--------------|
| Llama 3.2 1B Instruct | `meta.llama3-2-1b-instruct-v1:0` | EOL | **2026-07-07** | **no** |
| Llama 3.2 3B Instruct | `meta.llama3-2-3b-instruct-v1:0` | EOL | **2026-07-07** | **no** |
| Llama 3.1 8B Instruct | `meta.llama3-1-8b-instruct-v1:0` | Legacy | 2027-03-30 | yes |
| Llama 3.3 70B Instruct | `meta.llama3-3-70b-instruct-v1:0` | Legacy | 2027-03-30 | yes |

Verified 2026-11-15 from the AWS model cards, which state for both 3.2 models:
"reached end of life on July 7, 2026 and is no longer available. Requests to this model
fail with `ResourceNotFoundException: This model version has reached the end of its
life.`"

- [Llama 3.2 1B Instruct](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-meta-llama-3-2-1b-instruct.html)
- [Llama 3.2 3B Instruct](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-meta-llama-3-2-3b-instruct.html)
- [Llama 3.1 8B Instruct](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-meta-llama-3-1-8b-instruct.html)
- [Llama 3.3 70B Instruct](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-meta-llama-3-3-70b-instruct.html)

Bedrock's own "Models at a glance" index still links Llama 3.2 1B and 3B; the index
includes legacy and EOL cards, so it is not an availability signal. The per-model card is.

**Consequence: the smallest Llama available on Bedrock is 8B, against `llama3.2:1b`
locally — an 8× parameter difference. Any local-vs-Bedrock comparison using these is a
comparison of *different models*, and must be labelled that way wherever it appears.**
See `NOTES.md` for the cheaper alternative (pull `llama3.1:8b` locally instead), which
would restore a controlled comparison and has not been done.

Both candidate models are in **Legacy** lifecycle with EOL 2027-03-30. A cost table built
on them has roughly four months of shelf life.

**Configuration detail that will bite:** Llama 3.1 8B is not available *in-region* in
`us-east-1` — only `us-west-2` is. Elsewhere the geo inference id is required:
`us.meta.llama3-1-8b-instruct-v1:0`. Llama 3.3 70B is in-region only in `us-east-2`.
Using the bare id in the wrong region fails rather than silently routing.

### Untested: everything requiring an AWS call

| Measurement | Status |
|-------------|--------|
| TTFT p50 / e2e p50, Bedrock direct via aioboto3 (cell a) | **untested** — no credentials |
| TTFT p50 / e2e p50, Bedrock via the gateway (cell b) | **untested** — no credentials |
| Gateway overhead on Bedrock, (b) − (a) | **untested** |
| Bedrock on-demand prices for the models used | **untested** — see below |
| Cost per 1k requests from reported usage | **untested** |
| **Cancellation cost: tokens billed after client disconnect** | **untested** |
| Converse event mapping against the live API | **untested** — unit-tested against synthetic events only |

**Prices are not recorded.** <https://aws.amazon.com/bedrock/pricing/> was fetched on
2026-11-15; its Meta section lists Llama 3.2 and 3.3 in the headings but renders token
prices only for Llama 2, so no per-1k-token figure for the models above could be read
off it. Rather than copy a price from a third-party aggregator, this row stays untested.

**Cancellation cost is the row that decides a design question.** Stage 1 measured that a
disconnected client on Ollama wastes 1–3% of generated tokens, and that the cleanup path
is not guaranteed (1 full generation in 90). On a laptop that costs watts. On Bedrock it
costs money, and the gateway cannot see Ollama's log trick to count it. When credentials
exist, the source of truth is Bedrock model invocation logging (per-invocation output
token count) or, coarser, the CloudWatch `OutputTokenCount` metric for the model id over
the run window, compared against tokens the client actually received — reported as
dollars per 1000 disconnects.

### The one measured cell: local baseline

Runnable without AWS, so it was run. This is the (c) cell of
`benchmark/bedrock_compare.py` and the baseline the Bedrock cells will be compared against.

**Conditions**

| | |
|---|---|
| Date | 2026-11-15, 21:00 MST |
| Hardware | Apple M5 Pro (`Mac17,9`), arm64 |
| Ollama | 0.32.5, `library=Metal` verified from the server log, `OLLAMA_NUM_PARALLEL:4` |
| Model | `llama3.2:1b` |
| Gateway | compose image built from `377bf00` (Stage 1), 4 queue slots, `GATEWAY_BACKEND=ollama` |
| Load | streaming, n=10, c=1, `max_tokens=120`, 10 fixed prompts (see `bedrock_compare.py`) |
| Run dir | `benchmark/out/run-bedrock-compare-20261115-210022/` |

| cell | backend | model | n | TTFT p50 | e2e p50 | tokens p50 | usage reported |
|------|---------|-------|---|----------|---------|------------|----------------|
| local-gateway | ollama | `llama3.2:1b` | 10 | 0.202 s | 0.663 s | 58 | yes |

Reported usage over the 10 requests: 346 prompt tokens, 584 completion tokens.
Marginal cost of these 10 requests locally: **not measured** — the laptop has not been
metered. Not "$0".

The same run exercised cells (a) and (b), which declined cleanly: (a) reported "no AWS
credentials resolved from the standard chain", (b) was not given a model id and refused
rather than assuming one. Those refusals are in the run directory's `report.txt`.

---

## Client disconnect: what happens upstream (2026-11-15)

The hypothesis under test was that when a streaming client hangs up, the gateway releases
its semaphore slot but never closes the backend generator, so the httpx stream to Ollama
stays open and **Ollama keeps generating**.

**That is wrong in the common case.** In 89 of 90 measured disconnects Ollama cancelled
the task 30–50 ms (median) after the client went away, wasting 1–3% of the tokens it had
generated. The upstream was being closed all along — by async-generator finalization
rather than by anything the gateway did deliberately.

What *was* broken is that a disconnect left no trace anywhere: no counter, no duration,
no span outcome. 60 cancelled requests produced no streaming row in `llm_requests_total`
at all.

**Conditions**

| | |
|---|---|
| Date | 2026-11-15, 17:19–17:37 MST |
| Hardware | Apple M5 Pro (`Mac17,9`), arm64 |
| Ollama | 0.32.5, llama.cpp / GGML **Metal** (`library=Metal`, verified per run from the server log) |
| Model | `llama3.2:1b`, warmed directly and through the gateway before each run |
| Gateway | compose, 4 queue slots, `GATEWAY_BACKEND=ollama` (`/api/chat`) |
| Setting verified | `OLLAMA_NUM_PARALLEL:4` read back from the server's own log |
| Load | `benchmark/disconnect.py`: streaming, `max_tokens=400`, client reads for 1.0 s then drops the socket |
| Trials | n=10 per cell. c=1: one cell per arm. c=4: 5 cells before, 4 cells after |
| Run dirs | `benchmark/out/run-disconnect-before-metal-20261115-171955/`, `run-disconnect-before-metal-reps-20261115-173451/`, `run-disconnect-after-metal-20261115-172242/`, `run-disconnect-after-metal-reps-20261115-173207/` |

"before" is the code at `6360a39`; "after" adds `agen.aclose()` in `event_stream`'s
`finally`, the `llm_requests_cancelled_total` counter, and an `llm.outcome` span
attribute. Token counts come from Ollama's own log: `eval_count` where the task completed
normally, total context minus prompt where it was cancelled (llama.cpp prints no timing
block for a cancelled task). Request-to-task matching is by unique prompt length — see
the header of `benchmark/disconnect.py`.

### Tokens generated after the client left

| arm | c | requests | tokens to client | tokens generated | after close | wasted | cancelled |
|-----|---|----------|------------------|------------------|-------------|--------|-----------|
| before | 1 | 10 | 1182 | 1220 | 38 | 3.1% | 10/10 |
| before | 4 | 50 | 3895 | 3957 | 62 | 1.6% | 50/50 |
| after | 1 | 10 | 1191 | 1202 | 11 | 0.9% | 10/10 |
| after | 4 | 40 | 3154 | 3490 | 336 | 9.6% | 39/40 |

The `after` c=4 row is dominated by a single request; excluding it, that arm is 37 wasted
tokens over 39 requests (**1.2%**) — the same as `before` within run-to-run spread. The
fix does not reduce wasted tokens, because there were almost none to reduce.

### The one that got away

In `run-disconnect-after-metal-20261115-172242/c4`, request 8 (Ollama task 1431):

| | |
|---|---|
| tokens the client received | 101 |
| tokens Ollama generated | **400** — its full `max_tokens` |
| source | `eval_count`, i.e. llama.cpp printed a normal completion timing block |
| `cancel task` in the log | **absent** |
| client close → Ollama done | 2.738 s |

This is precisely the behaviour the hypothesis predicted, and it happened *with* the fix
in place. It is 1 occurrence in 90 disconnects: 0 of 50 in `before` at c=4, 1 of 40 in
`after`. With one event the arms cannot be separated, so **this is not attributable to
the fix** — and it is not evidence the fix caused it. What it does show is that cleanup is
not guaranteed in either arm.

The reason is structural. `agen.aclose()` lives in `event_stream`'s `finally`, and
reaching that `finally` depends on the same async-generator finalization that was already
closing the upstream. When finalization is late, the explicit close is late too. Making
cancellation deterministic requires detecting the disconnect somewhere that is not inside
the generator being cleaned up — an `http.disconnect` listener, or a pump task owned by
the request. **Untested; not implemented.**

### What the fix actually changed: disconnects became visible

| | before | after |
|---|--------|-------|
| disconnects measured | 60 | 40 |
| rows in `llm_requests_total` | **none** — no `stream="true"` series exists | unchanged by design |
| `llm_requests_cancelled_total` | metric did not exist | **20 of 20** in the main run |
| `llm_tokens_generated_total` | 8 — the warm-up's only | unchanged |
| `llm.outcome` on `llm.request` | absent | `cancelled` / `completed` / `error` |

Before the fix a cancelled request incremented nothing: the `done` branch never runs, and
the `except Exception` branch does not catch the unwinding. 60 disconnects that cost real
GPU time were invisible on every dashboard. `llm_requests_cancelled_total` is deliberately
separate from `llm_requests_total` rather than a third `status` value, so existing
success/error ratios keep their meaning.

The tokens Ollama generated for a cancelled request are still **not** counted in
`llm_tokens_generated_total` — the gateway only learns the count from the final chunk,
which by definition never arrives. Known gap, untested as to whether the log-derived count
could be recovered another way.

### Timing

| arm | c | close → `cancel task`, median | max |
|-----|---|-------------------------------|-----|
| before | 1 | 0.031 s | 0.319 s |
| before | 4 | 0.030 s | 0.053 s |
| after | 1 | 0.030 s | 0.056 s |
| after | 4 | 0.034 s | 0.049 s |

Unchanged by the fix, as expected: the close happens at finalization either way.

### The slot is released promptly

`llm_active_requests` returns to 0 within one 1 s query step of its last non-zero sample
in every cell, before and after — comfortably inside the 5 s scrape interval, and it never
stays elevated. Peak matches the concurrency (1 and 4).
`llm_queue_waiting_requests` stayed at 0 throughout: with 4 slots and c≤4 nothing ever
queued at the gateway. **The semaphore release was never the problem.**

### Two CPU-only runs, kept but not comparable

`run-disconnect-before-20261115-163332/` and `run-disconnect-after-20261115-171317/` were
taken before the Metal check existed and are **invalid for comparison with anything else
in this file**. Both were launched through `/usr/local/bin/timeout`, an x86_64 binary, so
the whole process tree ran under Rosetta; Rosetta started the x86_64 half of Ollama's
universal binary, which has no Metal support, and the server logged
`inference compute id=cpu library=cpu`. Throughput was ~33 tok/s against ~120–140 tok/s on
Metal. They are retained because runs are not destroyed, and because the before/after
comparison *within* that pair is internally consistent. `run_disconnect.sh` now aborts if
`uname -m` disagrees with `hw.optional.arm64`, or if Ollama reports `library=cpu`.
`run-disconnect-smoke-20261115-163121/` is a QUICK-mode harness smoke test at n=3, also
CPU-only, also not data.

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
