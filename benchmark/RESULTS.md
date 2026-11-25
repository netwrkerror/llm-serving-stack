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

## The disconnect fix, and what it is bounded to (2026-11-22)

Stage 1b measured a disconnected request holding the gateway's only queue slot for
101–155 seconds in 5 of 40 pairs. This is the cause, the fix, and the bound.

### The cause, in three measured parts

**1. Starlette never closes the body iterator.** Verified against the installed
`starlette 0.38.6`: the string `aclose` appears **nowhere in the package**.
`StreamingResponse.__call__` runs a task group where `listen_for_disconnect` returns on
`http.disconnect` and cancels the group's scope, which cancels the task sitting in
`async for chunk in self.body_iterator`. **A cancelled `async for` does not close its
iterator.** So `event_stream` is left suspended and its `finally` — which releases the
slot — is unreachable except by finalization.

A nuance measured rather than assumed: cancelling a task group's *own* scope is absorbed
by anyio, so `__call__` returns **normally** on a disconnect. That path is not itself
inside a cancelled scope.

**2. The generator is caught in a reference cycle, so finalization waits for the
collector.** `gc.DEBUG_SAVEALL` found 4 `event_stream` generators in cycles; the direct
holder of every one is `starlette.responses.StreamingResponse`. The cycle's type census
is the mechanism: **1453 traceback, 1375 frame, 158 CancelledError** objects. The
disconnect's `CancelledError` carries a traceback, the traceback holds frames, a frame
holds the response, the response holds `body_iterator`. Refcount never reaches zero.

The holder is **framework-created, not gateway-created**, so there was no gateway
reference to remove.

**3. The delay is the collector's schedule.** Forcing `gc.collect()` every second left
the fault occurring at its usual rate but bounded it to **628, 637, 724 and 1110 ms** —
one collection interval — against 101–155 s unforced.

### Instrumentation shortens the delay, not the cycle

| run | instrument | result |
|-----|-----------|--------|
| baseline + nodiag | none | **5 of 40 late** (101–155 s) |
| gc-loop | `gc.collect()` every 1 s | **4 of 59 bounded** to ≤1.11 s |
| saveall | `gc.DEBUG_SAVEALL` | **4 of 40 found in cycles**, all already closed |
| diag + referrers | per-request registry | **0 of 60 late** |

The last row is an observer effect, not a result: one-sided Fisher exact on 0/60 against
5/40 gives **p = 0.0087**. The first version of the stuck-stream diagnostic allocated
four objects per request, and allocation pressure is what drives CPython's generational
collector. It suppressed the fault it was built to watch. Rebuilt to touch the request
path zero times.

CPython runs finalizers *before* `DEBUG_SAVEALL` retains an object — verified directly:
a suspended generator in a cycle had its `finally` run during `gc.collect()` with the
flag on. That is why all four caught generators show `ag_frame is None`: they were stuck
behind the collector and then closed **by** it.

**Unverified hypothesis** for why `DEBUG_SAVEALL` also showed 0 late pairs when it should
not change collection timing: retaining garbage changes how deallocations count against
the gen-0 threshold. No evidence either way.

### Which disconnect path this actually is

Two independent signals, and they agree — but not in the way the version numbers alone
would suggest.

| signal | says |
|--------|------|
| uvicorn 0.32.0 | reports **`spec_version: "2.4"`** in both `h11_impl` and `httptools_impl` |
| starlette 0.38.6 | **does not branch on it** — `spec_version` appears nowhere in the package |
| the `DEBUG_SAVEALL` census | **158 `CancelledError` objects**, which is the task-group path's signature |

At ASGI 2.4 and above there is no `listen_for_disconnect`: a disconnect arrives as an
`OSError` from `send()` (uvicorn raises `ClientDisconnected(OSError)`), which unwinds by
exception rather than by scope cancellation and would leave no `CancelledError` behind.
The census is full of them, so the live path is the **task-group cancel**, below-2.4
behaviour — because the installed Starlette ignores what uvicorn reports.

So the n=59 validation ran against the real server and therefore covers the path that
actually fires here. The agreement matters because it means **the risk is version
coupling, not a mis-measurement**: upgrading Starlette to a release that honours
`spec_version` 2.4 would silently move this stack onto the other path. Hence the pin in
`gateway/requirements.txt`, and a unit test for the `OSError`-from-`send()` path even
though nothing here exercises it today.

### The fix

`ClosingStreamingResponse` closes `body_iterator` in a `finally`, under
`anyio.CancelScope(shield=True)`. `_StreamCleanup` owns the generator, the spans and the
slot, and runs **exactly once** whichever of the three paths reaches it first: the normal
end of the stream, an in-band error, or the response being torn down.

It also closes the pre-first-`__anext__` window previously recorded as untested in
NOTES. Closing a generator that never started runs no `finally`, so the response falls
back to calling the cleanup object directly.

### Validation

**Conditions**

| | |
|---|---|
| Date | 2026-11-22, 21:59–22:04 MST |
| Hardware | Apple M5 Pro (`Mac17,9`), arm64 |
| Ollama | 0.32.5, `library=Metal` verified, `OLLAMA_NUM_PARALLEL:1` |
| Gateway | compose, **1 queue slot** |
| Load | 60 disconnect pairs + 20 control, A `max_tokens=400` closing after its first chunk |
| Ollama log poll | **2 ms** (was 10 ms) |
| Instruments | **none** — no gc loop, no stuck scanner, no `DEBUG_SAVEALL` |
| Run dir | `benchmark/out/run-queued-disconnect-validate-20261122-215941/` |

| | before | after |
|---|--------|-------|
| late pairs (slot held >10 s) | **5 / 40 = 12.5%** | **0 / 59** |
| max slot held | **154,600 ms** | **63.6 ms** |
| `finally` → B admitted | 2.35 ms p50 | 2.7 ms p50, max 4.3 ms |
| `aclose()` duration | 0.002 ms p50 | 0.001 ms p50, max 0.362 ms |

**Stated as a bound, not as "fixed".** Zero late pairs in 59 gives a one-sided 95% upper
bound on the true rate of **4.95%**. `P(0 in 59 | 12.5%) = 3.8 × 10⁻⁴`. This rules out a
rate of 5% or more. It does not rule out something rarer.

### Tokens generated after the client left

Both runs recomputed from the raw Ollama logs under **one counting rule**: Ollama's
`eval_count` where llama.cpp printed a timing block, otherwise its context at release
minus its prompt tokens. A cancelled task prints no timing block, so the fallback is the
usual case and the source is recorded per pair.

| | baseline, 20 pairs | validation, 59 pairs |
|---|---|---|
| normal pairs: after-close **per pair, mean** | **11.6** | **11.1** |
| normal pairs: p50 / max | 12 / 12 | 11 / 12 |
| normal pairs: total | 186 | 655 |
| late pairs: total | **1596** | **none** |
| late pairs: max per pair | **399** | — |
| whole arm total | 1782 | 655 |

**The steady-state waste is unchanged, and that is the correct result.** ~11 tokens per
pair before and after. It is the harness's own doing: A's client reads one chunk and then
waits 60 ms so that B is certainly queued before the close, and Ollama generates through
that settle at ~117 tok/s. The fix was never going to change that, and it did not.

What the fix removed is the **late-pair** waste: the worst pair went from **399 tokens to
12**, and the whole-arm total fell even though the arm is three times larger.

**There is no 0.25-vs-11 discrepancy to explain; the 0.25 was an arithmetic error of
mine.** An earlier draft said "4 wasted in the other 16 pairs", which implied ~0.25
tokens per normal pair and made the 655 look like a regression. That figure was never
computed — it was asserted. The baseline's 16 normal pairs wasted **186**, a mean of
**11.62**, against the validation's **11.10**. The two agree, and the apparent gap
existed only in the draft.

What produced it: the 1596 and the 186 had been obtained by different methods, one from
`eval_count` and one by eye, and nothing forced them onto the same rule.
`benchmark/after_close.py` now applies one rule to any run directory and writes
`after-close-by-pair.txt` beside the run, recording per pair the task id, tokens
received, tokens generated, the difference, **and which source the count came from**. The
two files behind this table are:

- `run-queued-disconnect-20261122-202845/after-close-by-pair.txt`
- `run-queued-disconnect-validate-20261122-215941/after-close-by-pair.txt`

Source split: baseline 4 `eval_count` / 16 `ctx-prompt`, validation 1 / 58. The
`eval_count` rows are precisely the pairs where Ollama ran to completion — a timing block
means a normal finish — so in the baseline the four `eval_count` rows **are** the four
late pairs, each at exactly 399 wasted. That is a cross-check rather than a coincidence:
a pair that was never cancelled is a pair whose timings Ollama printed.

### Ordering, now resolved

At a 2 ms poll the effect finally exceeds the instrument:

| | |
|---|---|
| gap `B_admit − A_cancel` | p50 **−3.3 ms**, min −11.3, max +1.1 |
| B admitted before Ollama's cancel | 53 of 59 |
| gaps exceeding the ±1.646 ms clock uncertainty | 40 of 59 |

The poll still biases the cancel stamp late by up to 2 ms, which flatters this direction,
so the honest reading is **B is admitted roughly 1.3–3.3 ms before Ollama logs the
cancel**. It costs B nothing: `ollama.unattributed_ms` is **1.4 ms p50 on the disconnect
arm against 3.1 ms on control** — still lower, so the slot release is not handing B to a
backend that is still busy.

### A denominator correction, retroactive

The `a_completed` exclusion — a pair where A's answer ends on its own before the client
can cut it off — was applied back over every preserved run directory:

- **Stage 1b pre-fix runs: 0 exclusions.** The 5/40 rate is unchanged. Applying this can
  only ever raise a pre-fix rate, and here it raised nothing.
- The two 60-pair runs each had **1**, so the gc test is **4 of 59** and the validation
  **0 of 59**.
- **Stage 1 was wrong, and inconsistent with its own table.** Its lead said "89 of 90"
  while the table beneath it listed 110 requests. The preserved records hold **110 genuine
  disconnects, 0 excluded, 109 cancelled promptly, 1 full-generation leak**. Corrected
  throughout: the leak was **1 in 110**, not 1 in 90.

### Not measured

| | |
|---|---|
| Residual rate below 4.95% | untested — would need several hundred pairs |
| The ASGI `spec_version` 2.4+ disconnect path (`ClientDisconnect` from `send()`) | untested at time of writing |
| Why `DEBUG_SAVEALL` suppressed the fault | unverified hypothesis only |
| The same experiment against a remote backend | **untested, Bedrock** |

---

## Disconnect with a request queued behind it (2026-11-22)

Stage 1 measured what a client disconnect does to the upstream. It did not measure what
it does to the **next** request. This does.

> The explicit `agen.aclose()` before the slot release was added **after Part 1 was
> published** — it is the Stage 1 fix, not the code Part 1 described. What follows tests
> whether it is enough.

With one gateway slot and one Ollama slot, request B is admitted the instant A's slot is
released, and Stage 1 releases that slot as soon as `aclose()` returns. The question is
whether that instant is also when Ollama stops.

**Headline: 16 of 20 pairs behave exactly as designed. The other 4 held the gateway's
only slot for 101–155 seconds while Ollama generated 400 tokens for a client that had
already gone.**

**Conditions**

| | |
|---|---|
| Date | 2026-11-22, 20:28–20:40 MST |
| Hardware | Apple M5 Pro (`Mac17,9`), arm64 |
| Ollama | 0.32.5, `library=Metal` verified from the server log, **`OLLAMA_NUM_PARALLEL:1`** |
| Gateway | compose, **1 queue slot**, `GATEWAY_BACKEND=ollama` |
| Load | 20 pairs per arm. A: streaming, `max_tokens=400`, client closes after its first content chunk. B: sent while A is in flight, so it queues. Control arm: identical, but A completes |
| Clock offset | gateway − host = **+0.193 ms, ±1.538 ms** (NTP-style bracket via `/health` `now_ms`, 25 probes, best of) |
| Ollama log poll | **10 ms** — the cancel stamp trails Ollama's own write by up to one interval |
| Run dir | `benchmark/out/run-queued-disconnect-20261122-202845/` |

### The four pairs that went wrong

| pair | A's task | client received | Ollama generated | slot held | Ollama `cancel task` |
|------|----------|-----------------|------------------|-----------|----------------------|
| 0 | 18 | 1 chunk | **400 tokens** | **101.0 s** | never logged |
| 1 | 452 | 1 chunk | **400 tokens** | **122.4 s** | never logged |
| 5 | 1032 | 1 chunk | **400 tokens** | **154.6 s** | never logged |
| 18 | 2049 | 1 chunk | **400 tokens** | **136.2 s** | never logged |

From Ollama's own log for task 18: `eval time = 3411.75 ms / 400 tokens`, then
`stop processing: n_tokens = 444`. A normal completion, no cancellation — because by the
time the gateway closed the stream, Ollama had finished the whole generation seconds
earlier and there was nothing left to cancel.

The gateway's `request_cancelled` for those requests reports `duration_s = 101.125`,
`122.601`, `154.6`, `136.2` against **0.228** for a normal pair. So the `finally` did
run, and ran correctly — about 100 to 155 seconds late.

**Across the disconnect arm: 1596 tokens generated for nobody** (4 × 400 produced, 4
received), against 4 wasted in the other 16 pairs combined.

**This is the Stage 1 "cleanup is not guaranteed" case, reproduced at 4 in 20 rather than
1 in 110**, and with a consequence Stage 1 could not see: the slot. Stage 1 ran at 4 slots
with nothing queued, so a late finalization cost only wasted tokens. At one slot with a
request waiting, the same fault blocks the queue for two minutes.

**Where B waited: the gateway queue, not Ollama.** B's `ollama.unattributed_ms` in those
pairs is 3.1–4.0 ms, indistinguishable from the rest. The two minutes were spent entirely
on the gateway's semaphore, waiting for a generator that was not being collected.

**Cause, stated as far as the evidence goes.** The `finally` runs when the suspended
async generator is finalized. With a plain reference drop CPython finalizes it at once,
which is the 16-pair case at ~2.4 ms. When something holds a reference — a traceback, a
cycle — finalization waits for the cyclic collector, and its timing has nothing to do
with the request. The 101/122/136/155 s spread fits a collector running on its own
schedule and **does not fit** the 120 s `request_timeout`, which would cluster. **Not
proven:** no reference-cycle dump was taken, so what holds the generator is untested.

### The 16 normal pairs

| | |
|---|---|
| `aclose()` duration | p50 **0.002 ms**, max 2.473 ms |
| A's `finally` → B admitted | p50 **2.35 ms**, max 2.88 ms (same clock, reliable) |
| B's gateway queue wait | p50 60.9 ms — dominated by the harness's own 60 ms settle before A closes |
| B's `ollama.unattributed_ms` | p50 **1.3 ms** |

`aclose()` costs nothing measurable: closing the httpx stream is a socket teardown, not a
round trip. When finalization happens on time, the whole release path is ~2.4 ms.

### Ordering of A's cancel against B's admission: not resolved

| | |
|---|---|
| gap `B_admit − A_cancel`, 16 pairs with both stamps | p50 **−3.6 ms**, min −13.9, max +9.2 |
| B admitted before Ollama cancelled | 9 of 16 |

**These numbers do not support a conclusion and are reported only to show why.** The
cancel stamp comes from a 10 ms log poll, which biases it *late* — exactly the direction
that makes B look as though it were admitted first. A p50 gap of 3.6 ms sits well inside
that 10 ms, so the sign of the median is an artifact of the method as easily as a
property of the system. The clock offset (±1.5 ms) is not the limiting term; the poll is.

Resolving it needs the poll below the effect: `queued_disconnect.py --log-poll-ms 2`.
Not run, so **untested**.

**The question does not depend on it.** Whether B is admitted a few milliseconds before
or after Ollama's cancel, the clock-independent measure says it costs B nothing:

| arm | B's `ollama.unattributed_ms`, Ollama's own account of time B waited inside Ollama |
|-----|------------------------------------------------------------------------------|
| disconnect | n=20, p50 **1.3 ms**, min 1.1, max 4.0 |
| control | n=20, p50 **2.8 ms**, min 1.2, max 3.7 |

B waits **less** inside Ollama after a disconnect than in the control, not more. If the
slot release were handing B to a backend still busy with A, this is the number that would
rise, and it does not. The −1.4 ms difference of medians is small and in the harmless
direction; the control is marginally higher because A has just finished a 400-token
generation there, so the slot is handed over warm and busy rather than idle.

### What this changes

Stage 1 concluded that the semaphore release was never the problem. **At four slots with
nothing queued, that held. At one slot with a request waiting, it does not** — not
because the release is wrong, but because it cannot happen until the generator is
finalized, and in 4 of 20 cases that took two minutes.

The fix named in Stage 1 is the same one: detect the disconnect somewhere that is not
inside the generator being cleaned up — an `http.disconnect` listener, or a pump task
owned by the request — so neither the upstream close nor the slot release waits on the
collector. **Still not implemented, still untested.**

### Not measured

| | |
|---|---|
| What holds the reference in the late pairs | untested — no cycle dump taken |
| Ordering of B's admission against Ollama's cancel | **not resolved** at a 10 ms poll; needs ≤2 ms |
| Whether the rate changes with slot count, model, or `max_tokens` | untested — one configuration |
| The same experiment against a remote backend | **untested, Bedrock** |

---

## Quality per tier, local tiers (2026-11-16)

The cost table's quality column, for the two tiers that exist without credentials. The
Bedrock cell is a rerun of one tier away — see `eval/README.md`.

**Conditions**

| | |
|---|---|
| Date | 2026-11-16, 17:42 MST |
| Hardware | Apple M5 Pro (`Mac17,9`), arm64, 24 GiB |
| Ollama | 0.32.5, `library=Metal`, `OLLAMA_NUM_PARALLEL:4` |
| Gateway | compose at `1bad7ca`, `fast`=`ollama:llama3.2:1b`, `balanced`=`ollama:llama3.1:8b` |
| Eval set | `eval/prompts.jsonl` @ **`1bad7ca`**, committed before this run, clean (not `-DIRTY`) |
| Load | 30 prompts × 2 tiers, sequential, `temperature=0`, `max_tokens=256` |
| Grading | two graders; **`author` is the reported column**, `claude-session` reported alongside |
| Agreement | **55 / 56** items where both graders have a verdict |
| Run dir | `benchmark/out/run-eval-1bad7ca-20261116-174250/` |

Sequential on purpose: concurrency would add queue wait to the latency column, and this
eval is about answers rather than throughput.

### Pass counts per category per tier

**Read this before the numbers: the mechanical column is wrong in four places, in both
directions.** Three of its verdicts reject correct answers and one accepts a broken one,
so the regex/json pass counts are not a measurement of capability — and for one tier they
land on the right total only because two errors cancel. The table below is the hand
grades, not the checks.

Reasoning is **10 wide**: the 8 rubric prompts are hand-graded for both tiers. Grading is
**by the author**, with an independent pass by Claude in the working session reported
alongside and every disagreement listed. The session's pass is not independent of what is
being measured — a model grading a model's output — which is why the author's column is
the result. Every grade is in `eval/grades.jsonl` with the sha256 of the completion it was
made against and a one-sentence reason; `eval/report.py` verifies those hashes and refuses
to count a grade whose completion has changed.

The two graders **agreed on 55 of 56 items**. The single disagreement is listed below.

| tier | model | factual | reasoning | code: content | code: format |
|------|-------|---------|-----------|---------------|--------------|
| `fast` | `llama3.2:1b` | 6/10 (60%) | **0/10 (0%)** | 7/10 | 9/10 |
| `balanced` | `llama3.1:8b` | 9/10 (90%) | **5/10 (50%)** | **10/10** | 8/10 |

Reasoning breaks down as mechanical 0/2 + rubric 0/8 for `fast`, and mechanical 1/2 +
rubric 4/8 for `balanced`. The 1B scored **zero** on reasoning across all ten — not a
narrow miss: it got the p50/p99 question backwards, invented $10,000-vs-$1,000,000
arithmetic for two equal token totals, and answered the prompt-token question with "the
8B generates more nuanced prompts".

**No blended score.** The categories are not commensurable.

#### Where the mechanical checks and the graders disagree

An earlier version of this section reported code from the mechanical checks alone —
"fast 7/10, balanced 6/10" — and concluded **"the 8B is better on facts and worse on
code"**. Hand-grading does not support that. On content the 8B is **10/10 against the
1B's 7/10**: strictly better, and perfect. It loses only on *following the output
instruction*, 8/10 against 9/10.

The mechanical column disagrees with the graders on four cells, and **not all in the
same direction**:

| prompt | tier | check | graders | what actually happened |
|--------|------|-------|---------|------------------------|
| `code-08` | fast | FAIL | pass | `git log --format=%s` prints one line per commit. The regex demanded the literal `--oneline`. **Check too narrow.** |
| `code-08` | balanced | FAIL | pass | `git log --format=oneline` likewise. **Check too narrow.** |
| `code-04` | balanced | FAIL | pass | Answered `` `-L` ``, correct. The regex required whitespace before the flag and a backtick is neither. **Check artifact.** |
| `code-02` | fast | **pass** | **FAIL** | Emitted `from typing import Add`, which does not exist, so the code does not import. The regex only looked for the signature and never saw the broken line. **Check too generous.** |

**For `fast` the two errors cancel.** Mechanical says 7/10 and content+format-both-pass is
also 7/10 — the same number reached by one false failure and one false pass. A check that
is wrong twice and right in total is the worst case for anyone reading the number alone.
For `balanced` the checks understate by 2 (`code-04`, `code-08`), with no compensating
error.

The checks are left exactly as committed at `1bad7ca` rather than loosened after seeing
results; `grades.jsonl` is the record of where they are wrong, and a future set should fix
`code-02`, `code-04` and `code-08` before measuring anything else with them.

#### The one disagreement between graders

`code-02` [fast], content — **author: FAIL, session: pass.**

- author: *"from typing import Add doesn't exist, so the code doesn't import"*
- session: *"The requested signature is correct, body omitted."*

The author's reading is the recorded result, and is the better one: the prompt asked for
code, and code that raises `ImportError` on its first line is not correct code however
right the signature below it is. The session graded the artifact asked for and ignored
the rest of what was emitted — a narrower question than the one the prompt poses. This is
also the cell where the mechanical check is too generous, for exactly the same reason, so
the narrow reading was wrong twice independently.

### Prompt tokens per cell

| tier | model | prompt tokens, total | mean | completion tokens, total |
|------|-------|----------------------|------|--------------------------|
| `fast` | `llama3.2:1b` | 1426 | 47.5 | 811 |
| `balanced` | `llama3.1:8b` | 976 | 32.5 | 948 |

Mean difference is **exactly 15.0 tokens per prompt**, which independently reproduces the
template finding in `NOTES.md` (measured there as 32 vs 17 on a single prompt). The
`llama3.2` template emits a system block with a knowledge-cutoff line unconditionally;
the `llama3.1` template emits none when there is no system message and no tools. Same
tokenizer, same prompts, 450 tokens of difference across 30 requests.

This is why prompt tokens are recorded per cell. Any cost-per-request comparison between
these tiers is comparing different input token counts for identical prompts, and a
per-token price would not notice.

### What `classify()` would have done under `auto`

| category | → `fast` | → `balanced` | → `powerful` |
|----------|----------|--------------|--------------|
| factual | 9 | 1 | 0 |
| reasoning | 4 | 6 | 0 |
| code | 9 | 1 | 0 |
| **total** | **22** | **8** | **0** |

**`powerful` is unreachable for this workload.** Not under-used — unreachable. The
heuristic sends a prompt to `powerful` only on a complexity keyword or ≥120 words, and
**no prompt in the set contains any of the 25 keywords**, while the longest is 33 words.
So every routing decision here is pure word count against the 20-word boundary, and 73%
of the workload lands on the cheapest tier.

Two honest consequences:

- For a workload shaped like this, the keyword half of the heuristic contributes nothing
  at all, and the expensive tier would never be billed. That is the cheap outcome, but it
  is cheap by accident rather than by judgment: `reason-03` ("explain in one sentence why
  a semaphore…") goes to `fast`, which scored 0/2 on graded reasoning.
- The substring false positives pinned in `tests/test_router.py` ("analyzer" matching
  "analyze") **do not fire here**, because no prompt contains those substrings. The risk
  is real and tested, but this prompt set does not exercise it. That is a gap in the eval
  set, not evidence the heuristic is safe.

**Cost of that routing: untested, Bedrock.** The local tiers have no measured marginal
cost — the laptop is not metered — so there is no price to multiply these counts by.

#### The adversarial set — the heuristic's known failure mode, on purpose

`eval/prompts-adversarial.jsonl`, five short prompts built from the substring-containment
false positives pinned in `tests/test_router.py`. **A separate file and a separate table,
never merged into the 30-prompt results** — the committed set at `1bad7ca` stays exactly
as it was measured.

| id | prompt | words | keyword matched | routed to |
|----|--------|-------|-----------------|-----------|
| `adv-01` | "build me an analyzer" | 4 | `analyze` in "analyz**er**" | **powerful** |
| `adv-02` | "the designer left early" | 4 | `design` in "design**er**" | **powerful** |
| `adv-03` | "the signal analyzer is cheap" | 5 | `analyze` in "analyz**er**" | **powerful** |
| `adv-04` | "who implemented the lift" | 4 | `implement` in "implement**ed**" | **powerful** |
| `adv-05` | "is the debugger free" | 4 | `debug` in "debug**ger**" | **powerful** |

**5 of 5 four-word questions route to the most expensive tier.** None of them asks for
analysis, design, implementation or debugging. Put next to the 30-prompt set, where the
same heuristic sends **0 of 30** genuine reasoning and code prompts to `powerful`, this is
the whole problem in two tables: the keyword list decides what the expensive tier costs,
and it is not reading intent — it is matching substrings.

Cost of these five: **untested, Bedrock**. On a paid `powerful` tier they would be the
most expensive requests in the workload.

### Memory, and which models were resident

Nothing was resident when the run started. The runner does all of `fast` and then all of
`balanced`, and Ollama **evicted the 1B when the 8B loaded**: `api/ps` showed only
`llama3.1:8b` afterwards. So unlike the latency cell in the 8B section above, this 8B
cell ran with the 8B alone resident, which is the cleaner configuration of the two.

Swap used still grew 14.67 GB → 18.22 GB over the run on a machine that was already
swapping. Latency is not the measurement here, so this does not qualify the pass counts;
it is recorded because it qualifies anything anyone later tries to read out of the
latency figures in `results.json`.

### Not measured

| | |
|---|---|
| Bedrock tier quality | **untested, Bedrock** — one env var and a single-tier rerun |
| Cost per correct answer | **untested, Bedrock** — needs both a price and a measured marginal cost |
| The 8 rubric prompts | ungraded by design; recorded with completions for grading |
| Whether the code-category gap survives a stricter prompt or JSON mode | untested |
| Run-to-run variance | single run per tier at `temperature=0`; no repeats |

---

## Policy routing and fallback (2026-11-16)

Fallback measured with two local Ollama servers, so this is the **mechanism** — does
fallback fire, and does it fire only before the first byte — not the latency cost of
failing over to a remote provider. That column is **untested, Bedrock**.

**Conditions**

| | |
|---|---|
| Date | 2026-11-16, 17:33 MST |
| Hardware | Apple M5 Pro (`Mac17,9`), arm64 |
| Ollama | 0.32.5, `library=Metal` verified in both servers' logs, `OLLAMA_NUM_PARALLEL:4` each |
| Backends | primary `ollama` @ 127.0.0.1:11434, secondary `openai` @ 127.0.0.1:11435/v1 (Ollama's own `/v1`) |
| Tier | `fast` = `ollama:llama3.2:1b,openai:llama3.2:1b` |
| Load | streaming, n=24, c=4, `max_tokens=400`, 2 repeats |
| Kill | `SIGTERM` to the process listening on 11434, 4 s into each run |
| Run dirs | `benchmark/out/run-fallback-20261116-173336/` (the measurement), `benchmark/out/run-fallback-20261116-172519/` (the 10 s kill, see below) |

### The specified 10 s kill measures nothing on this machine

The first run used the stage's 10 s and reported **zero fallbacks in both repeats** —
not because fallback is broken, but because the scenario was already over. At
`max_tokens=400` these prompts still finish in ~1.4 s p50 (the model stops well short of
the cap), so n=24 at c=4 is a **10.2 s** run. The kill landed at +10.17 s against a
10.18 s wall: after the last request had been served. That run is kept as
`run-fallback-20261116-172519/` because a reader could easily repeat the mistake and
conclude the mechanism does not work. The kill was moved to 4 s, which is mid-run.

### Fallback under load, kill at 4 s

| | repeat 1 | repeat 2 |
|---|---|---|
| wall | 7.17 s | 8.96 s |
| kill landed | +4.16 s | +4.13 s |
| requests | 24 | 24 |
| served by primary | 12 | 10 |
| **fell back** (to `openai`) | **8** | **12** |
| failed | 4 | 2 |
| p50 e2e, normal | 0.953 s | 1.364 s |
| p50 e2e, fell back | **1.474 s** | **1.550 s** |
| p50 TTFT, normal | 0.239 s | 0.232 s |
| p50 TTFT, fell back | 0.304 s | 0.239 s |

Falling back cost **+0.52 s** and **+0.19 s** on p50 end-to-end across the two repeats —
one refused connection plus a second backend's prefill, on the same machine. The spread
between repeats is larger than the effect, so read this as "roughly a prefill's worth",
not as a figure to quote to three decimals.

**Only the within-repeat comparison is valid.** The "normal" p50 moves from 0.953 s in
repeat 1 to 1.364 s in repeat 2 — a 43% shift in the baseline, with nothing about the
request path changed. Both Ollama servers share one Metal device, so they contend for the
same GPU, and how much depends on how many requests each is holding at the moment, which
the kill changes. Compare normal against fell-back *inside* a repeat; do not compare
either column across repeats, and do not treat 0.953 s as this machine's uncontended
baseline — the single-server figure for the same model is 0.678 s p50 e2e (see the local
8B section).

`llm_fallbacks_total{tier="fast",from_kind="ollama",to_kind="openai",reason="upstream_unavailable"}`
= **20**, exactly 8 + 12. Every fallback had the same reason: the killed server refuses
connections, which `_classify_upstream` maps to `upstream_unavailable`.

### Zero fallbacks after the first byte

The 6 failures across both repeats are the post-first-byte case, and they are the
evidence for this rather than a separate experiment:

- All 6 had **HTTP 200 and a recorded TTFT** (0.19–0.34 s) before failing — they were
  already streaming when the primary died.
- All 6 ended as **in-band error chunks**: `peer closed connection without sending
  complete message body (incomplete chunked read)`.
- **None of them fell back.** `llm_fallbacks_total` moved by exactly the number of
  requests that had no TTFT yet.
- Conversely, "fallen-back requests with no first token: none" in both repeats — no
  fallen-back request promised bytes it then failed to deliver.

That is the designed behaviour: once the 200 and the SSE content-type are committed a
failure can only be reported in band, so there is nothing to retry into. The guarantee is
structural as well as measured — the only two call sites that can fall back are
`_open_first_chunk` and `_generate_with_fallback`, both of which run before any yield to
the client.

### Fallback is not attempted when the request itself is wrong

A 4xx meaning the request is malformed is not retried: an unknown model id is unknown at
the next backend too, and an over-long prompt is over-long there too, so retrying buys
the same error at a second backend's price. `_NO_FALLBACK_STATUSES` is 400, 404, 413 and
422. Everything else — connection failures, timeouts, 5xx, throttling, and auth errors
that may simply be this backend's credentials — is worth another backend's attempt.
Unit-tested in both directions (404 and 422 do not fall back, 429 and connect errors do);
**the only one observed in a live run is `upstream_unavailable`.**

### What is not measured

| | |
|---|---|
| Latency cost of falling back to a **remote** backend | **untested, Bedrock** |
| Cost of a fallback that reaches a paid backend | **untested, Bedrock** |
| Fallback across a real network partition (both servers are on one host here) | untested |
| Fallback when the primary is slow rather than dead — there is no timeout-triggered case | **not implemented**, see below |
| Policy routing under load (the policy tests are unit-level; no load run exercises `local_only`) | untested |

### Not done: timeout-triggered fallback

Fallback today fires on a backend that **fails**. A backend that is merely **slow** holds
the request until `request_timeout` (120 s) and only then moves on, by which point
falling back is useless.

The intended shape, for the record, since this is the first thing to build after the
Bedrock cells: a **first-byte budget per attempt**, separate from and much shorter than
the whole-request timeout. When an attempt misses its budget, open the next backend and
cancel the slow attempt as soon as the new one yields a first chunk — hedging, but only
ever before the first byte, never after. The existing structure already enforces the
"never after" half: the only two call sites that can fall back run before any yield to
the client.

**Untested and not implemented.** A slow region is exactly the case it exists for, which
is why it waits for a remote backend to measure against rather than being guessed at
locally — two Ollama servers on one Metal device cannot produce a realistic slow-but-alive
backend.

---

## Metric definitions

**`llm_tokens_generated_total` and `llm_tokens_per_second` count the backend's reported
completion tokens, falling back to the streamed delta count when a backend reports no
usage** (changed 2026-11-15). Previously both counted deltas. On Ollama one delta is one
token, so the two are identical and **every figure in this file taken against Ollama is
unaffected** — verified against a running server: for one 60-token request the metric
delta, the reported `usage.completion_tokens` and the content-chunk count were all 60.
The distinction starts to matter on Bedrock, where one Converse `contentBlockDelta` can
carry several tokens: counting deltas would understate throughput on the dashboards while
the cost table, which reads `usage`, stayed right. A cancelled request still falls back to
the delta count, because its final chunk never arrives.

The `request_completed` log line now carries both: `tokens` (billed) and `tokens_sent`
(what the client received).

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
locally — an 8× parameter difference.** Resolved by adding a local 8B cell rather than by
moving the baseline: `llama3.2:1b` stays the series baseline, and `llama3.1:8b` runs
locally as a second cell so the same weights can be compared local-vs-Bedrock. The tier
mapping for the rest of this stage is:

| Tier | Backend | Model | Role |
|------|---------|-------|------|
| `fast` | ollama | `llama3.2:1b` | series baseline, unchanged from Part 1 |
| `balanced` | ollama | `llama3.1:8b` | local half of the controlled comparison |
| `powerful` | bedrock | an Active model, chosen at run time (below) | paid tier |

`balanced` vs `powerful` is only a controlled comparison when `powerful` is the same 8B
weights on Bedrock. Against a 70B it is not, and is labelled as different models.

Both Llama 3.x candidates are in **Legacy** lifecycle with EOL 2027-03-30, so a cost table
built on them has about four months of shelf life. At least one row needs a model that
survives that date. `meta.llama3-1-8b-instruct-v1:0` is kept as a configured model for the
like-for-like cell rather than as a tier default; whether Llama 3.3 70B is Active or
Legacy **in the configured region** is checked at run time by the startup lifecycle check
below, and is **untested** here because that check needs credentials.

### Lifecycle check at startup

The gateway now reports each Bedrock tier's lifecycle status when it starts, because
nothing warned that the model this stack was built around was being retired — calls
simply begin failing with `ResourceNotFoundException`. For every tier whose kind is
`bedrock` it calls the control-plane `GetFoundationModel` (not an inference call, does not
bill) in the region the tier resolves to, and logs one line per tier:

- `bedrock_model_lifecycle` with `tier`, `model`, `lifecycle`, `region` when ACTIVE
- `bedrock_model_legacy` as a **warning** when LEGACY, naming the EOL consequence
- `bedrock_lifecycle_unknown` as a warning when the call failed, with the error

It never blocks startup: the runtime role may not hold `bedrock:GetFoundationModel`, and a
missing label is not a reason to refuse to serve. Unit-tested with an injected client;
**untested against the live API.** Lifecycle state is per region, so the report is for
whichever region the tier resolved to.

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
The page is JS-rendered, so reading it needs a browser with the Meta tab selected.

**Convention for when they are recorded:** every cost row carries the date its price was
read and the URL it was read from, alongside the usual trial count, concurrency, model,
backend and run directory. A price without a read-date is not usable six weeks later —
these two Llama models are already Legacy, and prices move independently of lifecycle.

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

### Local 8B: does it fit, and what does it cost in latency

The local half of the controlled comparison. Run before anything else, to establish
whether an 8B is usable on this machine at all.

**Conditions**

| | |
|---|---|
| Date | 2026-11-15, 22:15 MST |
| Hardware | Apple M5 Pro (`Mac17,9`), arm64, 24 GiB |
| Ollama | 0.32.5, `library=Metal`, `OLLAMA_NUM_PARALLEL:4` |
| Models | `llama3.2:1b`; `llama3.1:8b` (Q4_K_M, 6.87 GB resident) |
| Gateway | compose image at `2be1df5` + the three review fixes, 4 queue slots |
| Load | streaming, n=10, c=1, `max_tokens=120`, same 10 prompts as the cell above |
| Run dir | `benchmark/out/run-local-8b-20261115-221501/` (`conditions.txt` holds the raw memory and offload lines) |

| cell | backend | model | n | TTFT p50 | e2e p50 | tokens p50 | usage reported |
|------|---------|-------|---|----------|---------|------------|----------------|
| local | ollama | `llama3.2:1b` | 10 | 0.204 s | 0.678 s | 53 | yes |
| local | ollama | `llama3.1:8b` | 10 | **0.314 s** | **1.899 s** | 48 | yes |

Reported usage over 10 requests: 1B — 346 prompt, 596 completion. 8B — 196 prompt, 700
completion. Marginal cost locally for either: **not measured**, the laptop is not metered.

**It fits, on the GPU, and the machine swaps for it.** All three are results:

- `library=Metal` and `offloaded 33/33 layers to GPU` — full offload, no CPU fallback.
  6.87 GB resident in VRAM against 17.3 GiB available, so VRAM is not the constraint.
- **System memory is.** Swap used went from 17.10 GB to 17.71 GB across the run and the
  swap file itself grew 17.4 → 18.4 GB; free memory fell from 30% to 20%. The machine was
  already swapping ~12.8 GB before the 8B was pulled, so this is added pressure on a
  machine that had none to spare, not a clean baseline.
- The 8B is **2.8× the end-to-end latency of the 1B** (1.899 s vs 0.678 s) and 1.5× the
  TTFT, at a slightly *lower* token count. Treat these as an upper bound on what this
  machine does with an 8B rather than as the model's latency: a host under memory
  pressure is not a quiet host, and run-to-run spread has not been characterised at n=10.

The 1B cell reproduces the 21:00 run (0.204/0.678 against 0.202/0.663) on a gateway build
that includes the token-accounting change, which is the independent check that the change
did not move Ollama's numbers.

**Prompt tokens differ between the two local models for identical prompts** — 346 vs 196
for the same ten strings. Different chat templates and tokenizers across llama3.1 and
llama3.2, not a measurement error. It matters for any per-token cost comparison: the same
prompt set is not the same number of billable input tokens on different models.

---

## Client disconnect: what happens upstream (2026-11-15)

The hypothesis under test was that when a streaming client hangs up, the gateway releases
its semaphore slot but never closes the backend generator, so the httpx stream to Ollama
stays open and **Ollama keeps generating**.

**That is wrong in the common case.** In 109 of 110 measured disconnects Ollama cancelled
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
in place. It is 1 occurrence in 110 disconnects: 0 of 50 in `before` at c=4, 1 of 40 in
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
