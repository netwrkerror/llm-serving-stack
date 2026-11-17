# Quality per tier

30 fixed prompts — 10 short factual, 10 reasoning/explanation, 10 code-or-structured-output
— run through every configured tier once at `temperature=0`.

```bash
# the two local tiers
python3 eval/run.py --tiers fast,balanced --out benchmark/out/run-eval-<hash>-<timestamp>
```

The prompt set is committed before it is run against anything, and
`eval/run.py` records the commit of `prompts.jsonl` in every report. A rate measured
against an edited set is not comparable with one measured against the committed set, so
the hash is part of the conditions, and an uncommitted edit is reported as `-DIRTY`.

## Adding the Bedrock tier later

The runner takes its tiers from gateway config, so there is no code to change. Point
`powerful` at a Bedrock model and rerun **that tier only**:

```bash
GATEWAY_TIER_POWERFUL="bedrock:<model-id>" GATEWAY_AWS_REGION=us-west-2 \
  docker compose up -d --build gateway

python3 eval/run.py --tiers powerful --out benchmark/out/run-eval-<hash>-<timestamp>-powerful
```

The local cells do not need rerunning: each tier's results are independent, recorded per
cell with their own model id and backend kind. Merge the reports in `RESULTS.md` rather
than re-measuring what has not changed.

## What the report gives you

- **Pass counts per category per tier.** Never a single blended score: the three
  categories are not commensurable, and one number across them cannot be acted on.
- **Prompt and completion tokens per cell**, from the backend's reported usage. The
  prompt count is required, not decorative — tiers differ in chat template as well as in
  weights, so without it a tier difference and a template difference are
  indistinguishable afterwards. See the template note in `benchmark/NOTES.md`.
- **What `classify()` would have done** with each prompt under `auto`, so the keyword
  heuristic's behaviour on a mixed workload is measurable rather than assumed.

## Checks

Each prompt carries one of:

- `exact` — string equality after stripping.
- `regex` — a pattern that must match. Deliberately loose on surrounding prose where the
  prompt does not demand terseness, and strict where it does.
- `json` — parsed and compared to `expect`. A fenced ```json block is tolerated, because
  small models add one even when told not to; anything beyond that counts as not
  following the instruction.
- `rubric` — **not graded by the runner.** Recorded with the completion for a human to
  grade. A rubric prompt can therefore never silently inflate a pass rate, which is what
  would happen if the runner guessed.

## Hand grades

`eval/grades.jsonl` holds the human grades, one line per (prompt, tier, dimension):

| dimension | applies to | question |
|-----------|-----------|----------|
| `rubric` | the 8 reasoning rubrics | does the answer contain what the rubric names? |
| `content` | all 10 code prompts | is the substantive answer right? |
| `format` | all 10 code prompts | did it obey "output only ..."? |

**Grader: Claude Opus 5, by hand**, named in every line. Each completion was read and
decided one at a time against the rubric, with the reason recorded. **No model was used
as a judge** — not even for a first pass — because a model grading its own family's
output produces a number that looks like evidence and is not.

Every grade carries the `sha256` of the completion it was made against.

```bash
python3 eval/report.py --run benchmark/out/run-eval-<hash>-<ts>
```

merges the grades into the mechanical results and **verifies those hashes**, reporting
any grade whose completion has changed as stale rather than counting it. A grade silently
attached to a different completion is worse than no grade at all. `report.py` exits
non-zero if any grade is stale.

Splitting code into `content` and `format` matters: a single mechanical column mixes "is
the answer right" with "did it do as it was told", and so attributes every failure to
capability. See the correction in `benchmark/RESULTS.md` — hand-grading reversed the
code finding, and two of the mechanical failures turned out to be bugs in the checks.

## The adversarial set

`eval/prompts-adversarial.jsonl` — five short prompts built from the substring-containment
false positives pinned in `tests/test_router.py` ("analyzer" matching "analyze"). It
exists because the 30-prompt set does **not** exercise that failure mode, so a `0` in the
`powerful` column there is not evidence the heuristic is safe.

These are routed through `classify()` only, never through a model, and reported as their
own table. **Never merge them into the 30-prompt results**: the committed set stays as it
was measured.
