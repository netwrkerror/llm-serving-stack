# Benchmark notes

Append-only working notes for the measurement work: what surprised us, what didn't work,
and what we are not sure is right. Measured numbers and their conditions live in
`RESULTS.md`; this file is the honest margin around them. Newest stage at the bottom.

## Stage 0 — paying down what Part 1 exposed (2026-11-15)

No measurements in this stage; corrections and one small refactor only.

**The `~400ms` docstring figure was wrong twice over, not just uncited.** It traced to the
2026-11-13 probe in `RESULTS.md`, which is itself marked superseded. In that run the
419 ms `unattributed_ms` belonged to request **#1 of four at `OLLAMA_NUM_PARALLEL=1`** —
the docstring called it "the last of four concurrent", and the actual last request (#3)
was 148 ms. The docstring also framed it as a `=1` vs `=4` result, but ~400 ms only ever
appears at `=1`; at `=4` the same term is 3–14 ms. Replaced with a pointer to
`RESULTS.md` rather than a qualified number, so the figures live in exactly one place.

**The duplicate `classify()` call was load-bearing in a way worth writing down.** The
obvious "compute the tier once and index the tier→model map" would have changed behavior:
when a client names a non-default model, the old code returned that model from
`select_model` but *still* reported the heuristic's tier from the second `classify()`
call. So the tier header has always described what the heuristic thought, not what was
used. `resolve_route()` preserves that deliberately, and says so. Verified the four
cases (short/no-pref, complex/no-pref, explicit-other, explicit-equals-default) return
the same (tier, model) pairs as before.

**Renamed `select_model` → `resolve_route`** since it now returns `(tier, model)`. Nothing
referenced it outside `main.py` — no tests covered it, which is its own small finding:
the routing heuristic is currently untested. Stage 4 exercises it over 30 fixed prompts,
which will be the first real coverage it gets.

**Unsure / flagged:** the explicit-model gap is only a comment, as Stage 0 asked. Until
Stage 3 lands, a client that deliberately asks for `llama3.2:1b` (the default) still gets
re-routed by prompt content. With all three tiers currently pointing at `llama3.2:1b` this
is invisible; it starts to matter the moment Stage 2 puts Bedrock on a tier, because a
client asking for the local model by name could be silently sent to a paid backend. Worth
ordering Stage 3 carefully, or shipping Stage 2 with `powerful` left on Ollama by default.

**Gotcha for anyone adding tests:** `TestClient` runs each request in a fresh event loop,
but the backend singleton's `httpx.AsyncClient` binds to the first one. A streaming
request issued *after* a non-streaming one in the same process fails with
`Event loop is closed` and a 500. Pre-existing, unrelated to this stage's changes — the
same stream succeeds as the first request in a fresh process. Stage 2's backend registry
built in `lifespan` should make this go away; if it doesn't, tests need one client per loop.

**Found one more copy of the retracted claim.** `CODE_WALKTHROUGH.md` said "This models
vLLM's request scheduler" about the same semaphore — the exact claim Part 1 retracted.
Corrected it rather than leave it standing. Two caveats: it is beyond the five items
Stage 0 listed, and `.gitignore` excludes `CODE*.md`, so that file is a local-only doc
and the fix is **not** part of the PR. Nothing public was carrying this copy.

**Doc drift not fixed (needs its own pass):** the same walkthrough section still describes
`ACTIVE_REQUESTS.inc()` and `async with _queue`, but the code now uses `IN_FLIGHT` and
`_acquire_slot()` (which exists so queue-wait time is counted separately). The
non-streaming lifecycle diagram is also pre-tracing. Left alone deliberately — it is a
doc-sync job, not a Part 1 correction, and it would have buried this PR's five real fixes.

**`.gitignore` change, worth a look in review.** The root `NOTES.md` is ignored on
purpose (`NOTES.md`, no slash — so it matches at every level, including this file). The
hand-back asks for notes under `benchmark/`, and the write-up is built from `RESULTS.md`
+ `NOTES.md`, so this file needs to be tracked to be reviewable alongside the numbers it
annotates. Added a `!benchmark/NOTES.md` negation rather than loosening the root rule —
the root file stays private. `benchmark/out/` stays ignored, so "runs are never
destroyed" means on disk, not in git; run directories are referenced by name here and in
`RESULTS.md` but their contents are local.
