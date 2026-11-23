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

## Stage 2 — Bedrock backend (2026-11-15)

**The model this stage was designed around no longer exists.** Llama 3.2 1B Instruct
reached end of life on Bedrock on 2026-07-07, and so did 3.2 3B. The whole point of
using 1B on both sides was to control for quality and isolate the backend; that is now
impossible. The smallest Llama Bedrock still serves is 3.1 8B, against `llama3.2:1b`
locally — 8× the parameters, so any comparison between them measures model *and*
backend at once. `RESULTS.md` labels it as different models, as the stage plan required.

**The cheaper fix nobody asked for: move the local side, not the remote one.** Bedrock's
floor is 8B, but Ollama will happily run `llama3.1:8b`. Pulling that locally restores a
genuine like-for-like comparison — same weights, same prompt set, one side local and one
side billed — and it is a `ollama pull` plus a `GATEWAY_TIER_FAST` change, no code. I
have not done it: it changes Part 1's local baseline model, which every earlier table in
`RESULTS.md` is measured against, and that is a decision about the write-up rather than
a gap in the code. Worth deciding before the cost table is built, because it is the
difference between "Bedrock costs N× local" and "a 70B costs N× a 1B, also one is remote".

**Bedrock's own index page is not an availability signal.** "Models at a glance" still
links Llama 3.2 1B and 3B; both per-model cards say EOL and that calls fail with
`ResourceNotFoundException`. I nearly recorded the index listing as confirmation. The
per-model card is the authority, and it is the one with the dates on it.

**Region availability is per-model and asymmetric, which is a config trap.** Llama 3.1 8B
is in-region only in `us-west-2`; Llama 3.3 70B only in `us-east-2`. Anywhere else the
geo inference id (`us.meta.…`) is required. So a tier string that works in one region
fails in another, and the failure is a model-not-found rather than anything that hints at
the real cause. Noted in `RESULTS.md` next to the ids.

**Why async, concretely.** Using `boto3` in a thread would have made Stage 1's rare leak
routine: a synchronous `converse_stream` drained from a worker thread cannot be
cancelled, so every disconnect would bill for a full generation. With `aioboto3`, closing
the event stream closes the connection, which gets us at least the finalization-driven
behaviour measured on Ollama. `generate_stream` closes the stream in a `finally`, and
there is a test that an early-exiting consumer closes it. Whether Bedrock then *stops
billing* is untested and is the measurement that matters most here.

**What the unit tests do and do not prove.** 33 tests cover the Converse mapping: system
messages to `system` rather than the message list, `inferenceConfig`, usage translated to
the `prompt_tokens`/`completion_tokens` keys the other backends use, usage arriving in a
`metadata` event *after* `messageStop` (which is why `messageStop` is not treated as the
end of the stream), empty deltas not counted as tokens, and the event stream being closed
on early exit. All of it against synthetic events. The contract itself is unverified —
if Bedrock's event shapes differ from the documented ones, every one of these tests still
passes. The one real check I could make: constructing an actual `bedrock-runtime` client
via aioboto3 and confirming it exposes `converse` and `converse_stream`, which it does,
and that the docs' own Converse example matches the argument shape I build.

**`health_check` lied until I looked at it.** Building a Bedrock client succeeds with no
credentials at all, so the first version reported the backend healthy on a machine that
cannot call AWS. It now resolves credentials explicitly and returns False when none
exist — verified, since this machine is exactly that case. It still does not prove the
model is callable or the caller is authorised, because that would mean paying for a
health check; `/health` green means "configured", not "reachable".

**Reaching Bedrock without pointing a tier at it.** The stage said Bedrock should be
reachable when a client names a Bedrock model id, but a registry built only from
configured tiers has no Bedrock client to hand such a request to — it returned a 503. The
rule now is that setting `GATEWAY_AWS_REGION` is the operator opting in, and that builds
the client even with all tiers local. Clients name it as `bedrock:<model-id>`; there is
deliberately no heuristic that guesses whether a model id "looks like" Bedrock, because
guessing wrong sends a request somewhere that charges for it.

**A stale container made a routing test look like a routing bug.** `bedrock_compare.py`
cell (b) sent `bedrock:<id>` and the request arrived at Ollama. That was the compose image
still running Stage 1 code, which read the whole string as a model name. Against the new
code the same request is a 503 naming the kinds that were built. Same family of mistake as
the Stage 1 port collision: the thing answering was not the thing I was editing. Checking
`X-Routed-Backend` on the response is the cheap way to tell.

**Unsure / flagged:** the `llm_tokens_streamed_total` counter suggested after Stage 1 is
deliberately not in this PR — it would give cancelled requests a non-zero lower bound on
dashboards, but it is independent of Bedrock and this change is already large. The
`/v1/models` endpoint now merges configured tier models with whatever each backend can
enumerate, because Bedrock cannot enumerate itself from the runtime client; that is a
behaviour change to an endpoint nothing in the benchmarks reads, and it is untested
against a real multi-backend setup.

**Untested edge, carried over from Stage 1 review:** if the client disconnects between
`return StreamingResponse(...)` and Starlette's first `__anext__`, the generator body
never starts, so its `finally` never runs — the slot, `IN_FLIGHT`, and both spans leak,
and `llm_requests_cancelled_total` does not count it. The window is a few microseconds,
so n=90 would not have seen it and did not. Not fixed, not measured. The
`http.disconnect`-listener mechanism named in `RESULTS.md` would close this too, since
cleanup would no longer live inside the generator being cleaned up — one more reason
that is the right shape when it is built.

**The TestClient event-loop bug is fixed, measured rather than predicted.** The
mechanism was as described in review: `backend = _make_backend()` ran at import, so the
`httpx.AsyncClient` and its pool were created outside any event loop, and TestClient
drives each request through its own short-lived loop — a pooled connection from request 1
belonged to a loop that was gone by request 2, which is why it was intermittent rather
than constant. The Stage 2 registry builds clients inside `lifespan`, on the running
loop, and `with TestClient(app):` is what runs `lifespan`. Verified: four consecutive
requests alternating sync and streaming through one TestClient all return 200, where
before the second could 500 with `Event loop is closed`.
`tests/test_bedrock.py::test_registry_is_built_in_lifespan_and_closed_after` pins it with
a fake backend, so it does not need a live Ollama.

## Stage 2 follow-up — review fixes and the local 8B cell (2026-11-15)

**The 8B fits, and the honest version of "fits" has three parts.** Metal, full offload
(`offloaded 33/33 layers to GPU`), 6.87 GB resident against 17.3 GiB of VRAM — so the GPU
was never the question. System memory was: the machine was *already* swapping ~12.8 GB
before `llama3.1:8b` was pulled, and the run pushed swap to 17.7 GB with free memory down
to 20%. So the 8B latency figures (e2e p50 1.899 s, 2.8× the 1B) are an upper bound for
this host rather than a property of the model. Recorded as the result, since that is what
was asked for; worth re-running on a quiet machine before anything leans on the ratio.

**An unasked-for finding that affects the cost table: the same prompts are not the same
number of input tokens across models.** The ten fixed prompts tokenize to 346 prompt
tokens on `llama3.2:1b` and 196 on `llama3.1:8b` — different chat templates and
tokenizers. Any "cost per 1k requests for the measured prompt set" is therefore
per-model, not per-prompt-set, and comparing two models' costs on "the same prompts"
silently compares different input-token counts. This is an argument for reading cost from
reported `usage` per cell, which `bedrock_compare.py` already does, and never computing it
once and reusing it.

**The token-metric fix is the kind of change that could have silently rewritten history.**
`TOKENS_GENERATED`/`THROUGHPUT` now prefer `usage.completion_tokens` over the delta count.
On Ollama those are equal, so Part 1's numbers are untouched — but "should be equal" is
not evidence, so it was checked against a running server: one 60-token request moved the
metric by exactly 60, which matched both the reported usage and the content-chunk count.
The 1B latency cell also reproduces the earlier run on the new build (0.204/0.678 against
0.202/0.663). The fallback is load-bearing in one specific place: a cancelled request
never receives the final chunk, so usage never arrives and the delta count is all there is.

**`_bedrock_config`'s docstring was confidently wrong in the safe direction**, which is
the easy kind to leave in. It claimed retries were left alone to avoid paying twice for a
retried generation, while the code asks for standard mode (three attempts). The behaviour
was fine — botocore retries throttles and pre-response failures, and a mid-stream failure
is surfaced rather than retried, so a retried attempt generated nothing billable — but the
comment described a decision that had not been made. Docstring now says what the code does.

**The lifecycle check is the gateway's answer to this stage's main finding.** Nothing told
us Llama 3.2 1B was being retired; at EOL, calls just start returning
`ResourceNotFoundException`. Now every `bedrock` tier gets a `GetFoundationModel` lookup at
startup (control-plane, not inference, so no bill) and one log line each: info when ACTIVE,
**warning** when LEGACY, warning with the error when the call failed. It never blocks
startup — the runtime role may well not hold `bedrock:GetFoundationModel`, and refusing to
serve because a label could not be read would be worse than not knowing. Needs a second
client (`bedrock`, not `bedrock-runtime`), built lazily on the same exit stack. Tested with
an injected client, including the permission-denied and missing-field paths, plus a test
that a fully local configuration makes no AWS call at startup at all. **Untested against
the live API**, like everything else Bedrock-side.

**The credential guard flagged its own source first.** `bedrock_compare.py` now refuses to
run if AWS credentials appear in `.env`, `docker-compose.yml`, or any tracked file — but
the first version matched the literal strings in its own help text and abort message, and
would also have flagged `AWS_REGION: "${AWS_REGION}"`, which is correct passthrough rather
than a leak. Both now pass: it skips itself, ignores `${...}` and `<...>` placeholders and
empty values, and always flags key material (`AKIA…`, `ASIA…`, `aws_secret_access_key=`).
Verified in all three directions — clean repo runs, a planted `.env` aborts, commented
examples in compose do not fire. A guard that cries wolf is a guard people learn to skip.

**Unsure / flagged:** the 8B cell was measured with both local models resident at once
(1B 1.3 GB + 8B 6.87 GB), because one invocation runs both cells. That is additional
memory pressure the single-model runs did not have, and it is the configuration the tier
mapping implies anyway, so it was not separated out. If the 8B numbers get used for
anything load-bearing, run it alone first and compare.

**Next, when credentials arrive** (`AWS_PROFILE=serving-stack` in the launching shell,
never in `.env` — the guard above enforces that). Each its own run directory, n=10, c=1,
`max_tokens=120`, same prompt set:

1. Bedrock direct, 8B — cell (a).
2. Bedrock via the gateway, 8B — cell (b). Overhead is (b) − (a), reported with both p50s,
   the region and the hour. Bedrock latency drifts, so no averaging across hours.
3. Both cells again for whichever `powerful` model the startup lifecycle check reports
   ACTIVE in the configured region.
4. Disconnect cell on Bedrock, 8B, `disconnect.py` shape (read 1.0 s, drop the socket,
   `max_tokens=400`). Billed tokens come from Bedrock model invocation logging to
   CloudWatch Logs for the run window (per-invocation output token count), or failing that
   the CloudWatch `OutputTokenCount` metric for the model id. Report billed-after-close as
   tokens and as dollars per 1k disconnects. Until this runs, the cost table keeps its
   "cancellation cost: untested" row.
5. Prices from the pricing page in a browser with the Meta tab selected — it is
   JS-rendered, which is why fetching it returned only Llama 2 rows. Screenshot it, check
   the screenshot for account details before keeping it, and record price, date, URL.
   Cost per 1k requests from reported `usage`, per model, not per prompt set.

**On the 8B cell — unified memory means swap can reach the generation, not just the
host.** Calling the 17.3 GiB "VRAM" is a convenience; on Apple Silicon it is the same
physical memory the OS is swapping out of. So host swap pressure is not merely noise
around the measurement — it can touch the pages the model is generating from. That is a
second, stronger reason to treat 1.899 s as an upper bound rather than this model's
latency on this machine.

*Rerun condition, recorded so it is not re-litigated later:* if the balanced-vs-powerful
comparison ever rests on this cell, rerun it with everything else quit and the 8B resident
alone — same n=10, c=1, `max_tokens=120`, same ten prompts — and compare against
`run-local-8b-20261115-221501/`. Until that is done, any ratio derived from this cell
carries the swap caveat with it.

**On 346 vs 196 prompt tokens: it is the chat template, and the mechanism is more
specific than "llama3.2 adds a preamble".** Verified by diffing `ollama show --template`
for both models, then confirmed against the running gateway.

Both templates contain the `Cutting Knowledge Date: December 2023` line, so the
difference is not that one has it and the other does not — it is *when* each emits a
system block at all:

- `llama3.2:1b` opens `<|start_header_id|>system<|end_header_id|>` **unconditionally** and
  always emits the cutoff line, whether or not the request has a system message or tools.
- `llama3.1:8b` wraps the entire system block in `{{- if or .System .Tools }}`, and keeps
  the cutoff line inside the `{{- if .Tools }}` branch. With neither a system message nor
  tools — which is every prompt in this set — it emits **no system block whatsoever**.

Measured on the gateway with one identical prompt, `max_tokens=4`:

| | `llama3.2:1b` | `llama3.1:8b` | difference |
|---|---|---|---|
| no system message | 32 | 17 | **15** |
| with a system message | 35 | 25 | 10 |

15 tokens per request is exactly the aggregate gap: (346 − 196) / 10 = 15. The gap
narrows to 10 once a system message is present, because the 8B then emits a block too
while the 1B still carries its unconditional cutoff line. Same Llama 3 tokenizer in both;
the tokenizer was never the variable.

**Consequence, stated plainly: Converse applies its own template server-side, so local 8B
and Bedrock 8B will also see different prompt tokens for an identical request.** The
like-for-like cell is therefore "same weights, different prompt", not "same weights,
different hosting". It is still the best controlled comparison available — the weights are
the expensive variable and they are held fixed — but the write-up must not claim the
prompt is identical, and cost-per-request must come from each cell's own reported `usage`.

**Carried into Stage 4 before it is built:** the quality eval must record **prompt token
count per prompt per cell**, alongside the pass/fail. Without it, a tier difference and a
template difference are indistinguishable after the fact, and the only way to tell them
apart is to re-run everything. With it, "did the 70B do better, or did it just receive a
different prompt" is answerable from the saved records.

## Stage 3 — policy routing and fallback (2026-11-16)

**The precedence table, as it ended up:**

| Order | Source | Beats | Under `local_only`, if it resolves to a non-local backend |
|-------|--------|-------|-----------------------------------------------------------|
| 1 | a named `model` | everything | **403** — never re-routed, so refused instead |
| 2 | `routing.tier` | the policy, classify() | **403** |
| 3 | the key's policy, when it names a tier | classify() | **403** |
| 4 | `classify()` | — | **downgraded** to the nearest local tier, logged |

Two things in that table are decisions rather than consequences, and both could have
gone the other way:

*`routing.tier` beats a tier policy.* So a key whose policy is `fast` can still ask for
`powerful`. That makes a tier policy a **default, not a cap** — which is the right
reading of "then the policy" in the precedence, but it means a tier policy is not a
spend control. `local_only` is the spend control, and it is deliberately not
expressible as a tier so it cannot be overridden the same way.

*Automatic routing is downgraded, everything else is refused.* The asymmetry is the
point: automatic routing was never promised a particular tier, so rewriting it keeps
faith with the client. An explicit request was promised exactly what it asked for, so
the honest answer is 403 with the tier and the policy named. Silently serving a cheaper
tier would be the quality-side mirror of the paid-backend leak — and the harder one to
notice, because nothing fails, the answers just get worse.

*The downgrade goes down, not sideways.* `_first_local_tier` walks from the requested
tier **downwards** through `fast → balanced → powerful`, so a blocked `powerful` becomes
`balanced` if that is local, not `fast`. A downgrade should cost capability, never add
it.

**A misspelt policy refuses startup.** `GATEWAY_API_KEYS="k=localonly"` raises rather
than defaulting to `auto`, because defaulting would hand that key exactly the paid
backends it was configured not to reach, and nothing downstream would look wrong. This
is the one place in the gateway that refuses to boot on bad config, and it earns it.

**Policies live on the key, after an `=`.** `GATEWAY_API_KEYS="k1,k2=local_only"`.
`=` rather than `:` because `:` already separates kind from model everywhere else, and a
key is far more likely to contain a colon than an equals sign. Part 1's plain
comma-separated list still parses, every entry defaulting to `auto`.

**Policy is `auto` whenever auth is off**, which is the shipped default. A policy
attached to an unauthenticated caller is theatre: anyone could choose their own by
omitting the header.

### What surprised me about fallback

**The specified 10 s kill measured nothing, twice, and looked like a clean result.**
Both repeats reported 24/24 succeeded and zero fallbacks. Nothing in that output says
"your kill was too late" — it reads exactly like "fallback never fired", and if I had
stopped there I would have written up a broken mechanism as a working one, or a working
one as broken, with equal confidence. The giveaway was the loader printing `wall 10.18s`
next to `killed at +10.17s`. The run is kept, and the loader now prints
`kill landed mid-run: yes/no` precisely so this cannot be read past again. The cause is
mundane: `max_tokens=400` does not mean 400 tokens, it means *up to* 400, and these
prompts finish in ~60.

**`bench.py` could not answer the question.** It records latency but not which backend
served each request, so "p50 of fallen-back requests vs normal" is unanswerable from it.
That is why `fallback_load.py` exists: it records `X-Routed-Backend` per request and
kills the primary itself, so the kill is correlated with the requests rather than raced
against them by a background `sleep`.

**The post-first-byte case produced better evidence than a dedicated test would have.**
I expected to have to construct a case where a request fails mid-stream. The kill
produced 6 of them for free (4 and 2), and they are unambiguous: HTTP 200, a recorded
TTFT of 0.19–0.34 s, then an in-band `incomplete chunked read`, and **not one of them
fell back**. `llm_fallbacks_total` moved by exactly the count of requests with no TTFT
yet. That is the invariant stated as a measurement rather than as a comment.

**Falling back is cheap here, and that number is the least trustworthy one in the
table.** +0.52 s and +0.19 s on p50 across two repeats — a refused connection plus a
second prefill, between two servers on one machine. The between-repeat spread is larger
than the effect. The number worth having is the remote one, and it is untested.

**Unsure / flagged:** there is no timeout-triggered fallback. A backend that is *slow*
rather than dead will hold the request until `request_timeout` (120 s) and only then
fall back, which at that point is far past useful. A per-attempt budget shorter than the
whole-request timeout is the obvious fix and is not implemented. Also untested: fallback
across a real network partition, and policy routing under load — the `local_only` tests
are all unit-level.

## Stage 4 — quality per tier, local tiers (2026-11-16)

**The 8B is worse than the 1B at code, and the reason is formatting, not capability.**
70% → 60%. The two lost prompts are both "output only JSON": the 8B answered correctly
and then kept explaining, 113 and 77 completion tokens against the 1B's 22 and 12, so the
JSON check failed on the surrounding prose. That makes this column a measure of
instruction-following as much as of ability, which is worth saying out loud before anyone
reads "bigger model, lower score" as a surprise about model quality. Untested whether a
stricter prompt or a JSON-mode request reverses it — and if it does, that is a finding
about the eval, not the models.

**`powerful` is unreachable for this workload, not merely unused.** Zero of 30 prompts
contain any of the 25 complexity keywords, and the longest is 33 words against a 120-word
threshold. So every decision is the 20-word boundary, 22 prompts go to `fast`, 8 to
`balanced`, and the keyword half of the heuristic contributes **nothing at all**. The
cheap outcome, arrived at by accident: `reason-03` goes to `fast`, which scored 0/2 on
graded reasoning.

**My own eval set does not exercise the heuristic's known failure mode.** The
substring-containment false positives pinned in `tests/test_router.py` — "analyzer"
matching "analyze", "designer" matching "design" — never fire here, because no prompt
contains those substrings. I wrote the prompts to be a realistic mixed workload and
separately know the router has that bug, and did not connect the two until reading the
`auto` column. The 0 in the `powerful` column is therefore not evidence the heuristic is
safe; it is evidence this set does not test that path. A deliberate adversarial prompt or
two would fix it, and would also change the routing distribution, which is why it is
flagged rather than quietly added after the fact.

**The 15-token template gap reproduced exactly at scale.** 47.5 vs 32.5 mean prompt
tokens across 30 prompts, against 32 vs 17 measured on one prompt last stage. 450 tokens
of difference for identical inputs, same tokenizer, purely because `llama3.2`'s template
emits an unconditional system block and `llama3.1`'s does not. This is the clearest
argument for the per-cell token column being mandatory rather than nice to have: a
per-token cost comparison between these tiers would silently compare different inputs.

**Rubrics are recorded, not guessed.** 8 of the 10 reasoning prompts are rubric checks,
and the runner returns `None` for them rather than attempting a grade. The cost is an
honest two-prompt-wide graded reasoning column; the alternative was a plausible-looking
rate built on the runner's opinion of its own output. Flagged in `eval/README.md` so the
narrow column is not mistaken for a measurement.

**On the 1B-resident caveat, which this run avoids.** Nothing was resident at the start,
and Ollama evicted the 1B when the 8B loaded — `api/ps` showed only `llama3.1:8b`
afterwards. The runner does all of one tier then all of the next, so the 8B cell ran with
the 8B alone, unlike the latency cell where both were resident. That is the cleaner
configuration, and it happened by Ollama's choice rather than mine. Swap still grew
14.67 → 18.22 GB, which does not qualify pass counts but does qualify the latency numbers
sitting in `results.json`.

**Unsure / flagged:** single run per tier, `temperature=0`, no repeats — so no
variance figure, and `temperature=0` is not fully deterministic across a batched backend
anyway. The regex checks are hand-written and loose where the prompt does not demand
terseness; a model could pass `fact-05` by saying "time to first token" inside an
otherwise wrong answer. And the graded rates are not comparable across categories, which
is exactly why there is no blended number.

## Stage 3 follow-up — observability for the decisions (2026-11-16)

**A downgrade now has a counter, and the reason is the Stage 1 lesson applied early.**
`llm_policy_downgrades_total{from_tier,to_tier}`. `X-Model-Tier` already reported the
tier that *served* the request, which is right, but it means the tier that was *asked
for* left no trace at all once the log line scrolled away — exactly the shape of the
disconnect defect in Stage 1, where 60 cancelled requests produced no metric. Caught here
by analogy rather than by measurement, which is the cheaper way to catch it.

**Two legal settings that are dangerous together.** Auth off is the shipped default; a
paid tier is a deliberate choice. Together they mean any caller who can reach the port
can spend money — and because a routing policy attaches to an API key, there is no key
and therefore no policy that can stop them. `remote_tiers_configured_without_auth` is now
its own warning event rather than a sentence inside the existing one, so it can be
alerted on. Tested in all four combinations.

**The 401/403 fallback decision, written down because it could reasonably go either
way.** A backend auth failure *does* fall back. Credentials are per backend, not per
request: a 401 from Bedrock says this gateway's AWS credentials are wrong, and says
nothing about whether the next backend — often with separate credentials, or none, for a
local one — can serve the request. Not falling back would let one misconfigured backend
take down a tier that has a working fallback behind it. The cost of being wrong this way
is bounded and visible: a second failed attempt, and a
`llm_fallbacks_total{reason="upstream_error"}` series that makes a chain-wide auth problem
obvious. The cost of the other way is a tier that is down while a healthy backend sits
unused. **Untested against a real 401** — no credentialed backend exists yet.

**The fallback table's baseline moves between repeats and the reason is the test rig.**
"normal" p50 is 0.953 s in repeat 1 and 1.364 s in repeat 2, with nothing in the request
path changed. Both Ollama servers share one Metal device, so they contend for the same
GPU, and the kill changes how much. Only the within-repeat normal-vs-fell-back comparison
is valid; neither column is comparable across repeats, and 0.953 s is not this machine's
uncontended baseline — the single-server figure for the same model is 0.678 s. Noted in
`RESULTS.md` next to the table, because a reader would otherwise reasonably read those
two numbers as a trend.

## Stage 4 follow-up — grading, and what the split revealed (2026-11-16)

**Hand-grading reversed the code finding, and the earlier version was wrong in a way
that sounded insightful.** I had written "the 8B is better on facts and worse on code"
from the mechanical column, with a tidy explanation about chattier models failing
format checks. On content the 8B is **10/10 against the 1B's 8/10** — strictly better,
and perfect. It only loses on following the output instruction. Worse, two of its four
mechanical failures are not the model at all: `code-04` answered `` `-L` `` and my regex
required whitespace before the flag, and `code-08` answered `git log --format=oneline`,
which genuinely prints one line per commit, while my regex demanded the literal
`--oneline`. So the mechanical column understates both tiers, and the story I told about
it was built on two of my own bugs. The checks stay as committed at `1bad7ca` — loosening
them after seeing results is how an eval stops measuring anything — and `grades.jsonl`
records where they are wrong.

**The lesson generalises past this table:** a single mechanical column that mixes "is the
answer right" with "did it do as it was told" will attribute every failure to capability,
because that is the only story the number can tell.

**The 1B scored zero on reasoning, all ten.** Not a narrow miss. It got p50-vs-p99
backwards, invented $10,000-vs-$1,000,000 arithmetic for two equal token totals, and
explained differing prompt-token counts with "the 8B generates more nuanced prompts". The
router sends 4 of the 10 reasoning prompts to exactly this tier.

**Grades carry the hash of what was graded, and `report.py` checks it.** A grade silently
attached to a different completion is worse than no grade, because it looks like evidence.
0 stale, 0 orphaned on this run. Grader is me, by hand, named in the file and in
`eval/README.md`; no model-as-judge, so the reasoning column is a human opinion with its
reasons written down rather than a model's opinion of its own output.

**The adversarial set lands 5 of 5 on `powerful`** — four-word questions like "the
designer left early" routing to the most expensive tier. Separate file, separate table,
never merged into the 30-prompt results. Side by side with 0 of 30 genuine reasoning and
code prompts reaching `powerful`, that is the finding in full: the heuristic is matching
substrings, not reading intent.

**Carry into the write-up:** on this workload the router has a tier it never uses — no
prompt reached 120 words or matched a keyword — so the expensive tier's cost is set
entirely by the keyword list, not by the workload.

**The 8B ran alone by Ollama's choice, not by the runner's.** Nothing was resident at the
start; the runner does all of one tier then the next, and Ollama evicted the 1B when the
8B loaded (`api/ps` showed only `llama3.1:8b` afterwards). That is the cleaner
configuration, but **nobody should rely on the ordering**: it is the scheduler's eviction
policy, not an arrangement the eval makes or asserts. If a future run needs the 8B alone,
make it so explicitly rather than assuming this repeats.

**Grading went to two graders, and the session's column lost the one disagreement.**
Author and session agreed on 55 of 56. The exception is `code-02` [fast] content: the
model emitted `from typing import Add` — which does not exist — above a correct function
signature. The session passed it on the grounds that the requested artifact (the
signature) was right. The author failed it: the prompt asked for code, and code that
raises `ImportError` on line one is not correct code. The author's reading is the result
and is the better one.

Worth noting *why* the session got it wrong, because the mechanical check got it wrong
the same way: both graded the fragment the prompt named and ignored the rest of what was
emitted. The regex looked for the signature and never saw the broken import; I did the
same thing in prose. A narrow question asked twice gives the same wrong answer twice, and
two sources agreeing is not corroboration when they share the error.

**And the sentence I was asked to add turned out to be false.** The review asked for "the
mechanical column understates both tiers" next to the quality table. The author's grades
do not support it: the checks understate `balanced` by 2, but for `fast` they are wrong
twice in opposite directions — a false FAIL on `code-08` and a false pass on `code-02` —
and land on 7/10 either way. A check that is wrong twice and right in total is worse than
one that is simply wrong, because nothing in the number invites a second look. RESULTS now
says "wrong in four places, in both directions" instead.
