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

**`.gitignore` change, worth a look in review.** The root `NOTES.md` is ignored on
purpose (`NOTES.md`, no slash — so it matches at every level, including this file). The
hand-back asks for notes under `benchmark/`, and the write-up is built from `RESULTS.md`
+ `NOTES.md`, so this file needs to be tracked to be reviewable alongside the numbers it
annotates. Added a `!benchmark/NOTES.md` negation rather than loosening the root rule —
the root file stays private. `benchmark/out/` stays ignored, so "runs are never
destroyed" means on disk, not in git; run directories are referenced by name here and in
`RESULTS.md` but their contents are local.

## Pre-Stage 1 — the explicit-model invariant, and the heuristic's first tests (2026-11-15)

Still no measurements. This closes the gap Stage 0 only commented on, before any tier can
point at a paid backend.

**Supersedes the "Unsure / flagged" note above.** That note said the gap would wait for
Stage 3 and suggested shipping Stage 2 with `powerful` left local. The gap is now fixed
outright instead, which is the better order: Stage 2 can point a tier at Bedrock without
a client's named model being able to drift onto it.

**The invariant.** A request that names a model goes to that model and is never
re-routed; only `model` absent or `"auto"` goes through `classify()`. `model` is now
optional in `schemas.py` with `"auto"` as the sentinel, so absent and `"auto"` collapse
to one path and nothing downstream sees `None`.

**What changed in behaviour, and it is worth being clear about it.** Before, `X-Model-Tier`
reported what the heuristic *thought* even when the client's own model was used — Stage 0
preserved that deliberately. It cannot survive this invariant, because the tier now picks
the **backend**, not just the model. A tier is only meaningful if it says where the
request actually went. So for a named model the tier is now the tier *serving that model*,
found by reverse lookup over `settings.tier_models`. Verified against a real uvicorn
server: naming `llama3.2:1b` on a prompt `auto` would have sent to `powerful` now reports
`tier: fast` and stays there.

**Two edges that needed a decision, not a default:**

- *A named model matching no configured tier* (`mistral:7b`) is still honoured — the
  backend decides whether it exists — but it has no tier, so `Route.tier` is `None` and
  the header reads `none`. Confirmed end-to-end: the gateway passes it through and Ollama
  returns a clean 404 with `X-Model-Tier: none`. The alternative, inventing a tier for it,
  would have meant guessing a backend for a model nobody configured.
- *Several tiers on the same model* is the shipped default (all three are `llama3.2:1b`),
  so the reverse lookup needs a tie-break. It takes the first match in
  `fast → balanced → powerful` order, i.e. the cheapest tier wins. Documented in
  `Settings.tier_models` because the dict's insertion order is now load-bearing.

**`default_model` is now only a fallback,** as asked: the three per-tier models default to
empty and resolve to `default_model` when unset. Shipped behaviour is unchanged (all
tiers → `llama3.2:1b`), but the fallback is real rather than three copies of the same
literal. `schemas.py` no longer hardcodes `llama3.2:1b` either.

**The heuristic had zero tests; now it has 62.** Pinning it first turned up two sharp
edges that are pinned as-is rather than fixed, because Stage 4 is where they get judged:

- *Keyword matching is plain substring containment.* "build me an analyzer" classifies as
  `powerful` because "analyzer" contains "analyze"; so does "the designer left". For a
  mixed workload this will over-route to the expensive tier, which is exactly the number
  Stage 4 step 3 is supposed to produce.
- *Only `user` messages are classified.* A keyword-heavy `system` prompt has no effect on
  the tier. Defensible, but it means a long system prompt costs tokens the router never
  sees — relevant once a tier is billed per token.

Boundaries pinned exactly where the constants say: 20 words is still `fast`, 21 is
`balanced`, 119 is `balanced`, 120 is `powerful`. An empty prompt is `fast`.

**Correction to the Stage 0 TestClient note.** I wrote that a streaming request after a
non-streaming one fails with `Event loop is closed`, and guessed Stage 2's registry would
fix it. The first half is right but my framing was too narrow — *any* request after the
first in one `TestClient` process can hit it, sync or streaming, and it is intermittent.
It is purely a test-harness artifact: the same four-request sequence against a real
uvicorn server returns 200/200/200/404 with correct headers throughout. Live checks in
this repo should go through a real server, not `TestClient`.

## Stage 1 — client disconnect (2026-11-15)

**The hypothesis was wrong, and that is the interesting part.** Ollama does not keep
generating after a streaming client leaves: 89 of 90 measured disconnects were cancelled
within 30–50 ms, wasting 1–3% of tokens. The upstream connection was already being closed
— by CPython dropping the last reference to the suspended async generator and asyncio's
finalizer hook scheduling `aclose()`. Nothing in the gateway was doing it on purpose,
which is why it looked like a leak when reading the code.

**What was actually broken was observability, and badly.** A cancelled request
incremented *nothing*: the `done` branch never runs and `except Exception` does not catch
the unwinding, so 60 disconnects produced no `stream="true"` row in `llm_requests_total`
at all, and `llm_tokens_generated_total` stayed at the warm-up's 8. Real GPU time, zero
trace on any dashboard. That is the defect worth writing about, and it is not the one we
set out to find.

**The fix is in `event_stream`'s `finally`, and it is weaker than it looks.** Reaching
that `finally` depends on the same generator finalization that was already closing the
upstream, so an explicit `aclose()` there cannot make cleanup deterministic — it only
makes it *stated*. I had written a comment claiming it fixed the determinism problem;
the measurements contradicted it and the comment is now corrected. Doing this properly
needs disconnect detection outside the generator being cleaned up (an `http.disconnect`
listener, or a pump task owned by the request). Not implemented, not tested, flagged in
both the comment and `RESULTS.md`.

**One disconnect out of 90 ran to its full 400 `max_tokens` anyway** — with the fix in
place, no `cancel task` line, 2.738 s after the client left. 0 of 50 in `before` at c=4,
1 of 40 in `after`. One event cannot separate the arms, so I have not claimed the fix
caused it, and I have not claimed it is unrelated either. It is the clearest evidence
that the cleanup path is not guaranteed, and it is exactly the behaviour the original
hypothesis described — just rare rather than routine.

**Two runs were silently invalid and I nearly wrote them up.** I launched the script
through `/usr/local/bin/timeout`, which is an x86_64 binary, so the entire process tree
ran under Rosetta — and Rosetta started the x86_64 half of Ollama's universal binary,
which has no Metal. Ollama logged `inference compute id=cpu library=cpu` and ran at
~33 tok/s instead of ~120–140. I caught it only because the transcript's `uname -m` said
`x86_64` while `RESULTS.md` claims an M5 Pro. The before/after pair was internally
consistent, so nothing looked wrong in the numbers themselves. Both runs are kept and
labelled invalid; the script now aborts when `uname -m` disagrees with
`hw.optional.arm64`, or when Ollama reports `library=cpu`.

This also mattered for sensitivity, not just labelling: on CPU the cancel window is worth
~1–3 tokens, so "almost nothing wasted" was partly an artifact of slow generation. On
Metal the same 30–50 ms is worth ~5 tokens, and the 2.7 s outlier is worth 299.

**A port collision wasted an hour and produced a confidently wrong conclusion.** A
locally-run `uvicorn --port 8000` binds `127.0.0.1` while compose binds `*:8000`, and both
can be up at once; requests then go to whichever the resolver prefers. I concluded from
this that the `finally` block "never runs on disconnect" — it does, I was reading metrics
from the compose container while exercising my local working copy. `run_disconnect.sh`
now refuses to start if a non-docker process is listening on 8000. Note that `lsof`
truncates COMMAND to 9 characters, so Docker Desktop appears as `com.docke` and a guard
matching `docker` never fires — the first version of this check was itself broken.

**Things the harness needed that were not obvious:**

- `--after` has to be enforced by cancelling the read, not by checking the clock after
  each chunk. The first version only looked at the deadline once a chunk had arrived, so
  a request still waiting on its first token hung up late — 1.5 s instead of 0.5 s — which
  quietly stops being a disconnect mid-generation.
- Ollama's log has no sub-second timestamps and only puts them on `[GIN]` lines, so the
  harness stamps lines itself as it tails them (50 ms poll).
- A cancelled task prints no llama.cpp timing block, so `eval_count` is simply absent for
  exactly the requests this experiment cares about. Total context minus prompt tokens
  gives the same number; the report labels which source each row used.
- Matching an HTTP request to an Ollama task needed a trick: each request is padded to a
  unique prompt length, and tasks are rank-matched by the `task.n_tokens` the log reports.
  No tokenizer knowledge required, only monotonicity.

**Unsure / flagged:** tokens generated for a cancelled request are still not counted in
`llm_tokens_generated_total`, because the gateway learns the count from a final chunk that
never arrives. The log has it, but the gateway cannot see the log. Left as a known gap.
Also untested: everything here is Ollama via `/api/chat`. Whether an OpenAI-compatible
backend cancels as promptly is unknown, and Bedrock in Stage 2 has no streaming
cancellation story I have verified at all.

**One more copy of the ~400 ms figure, found late.** Stage 0 corrected it in the
`_record_backend_timings` docstring, but the same misattribution was also in the README's
trace-attribute table ("~400 ms for the last of four when `OLLAMA_NUM_PARALLEL=1`") and I
missed it then. Same error as before: in that 2026-11-13 probe the 419 ms belonged to
request #1 of four, and the actual last request was 148 ms. Now points at `RESULTS.md`
like the docstring does. Worth remembering that a number which appears twice usually
appears three times.
