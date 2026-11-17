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
- `rubric` — **not graded by the runner.** Recorded with the completion and counted in
  its own column for a human or a separate judge. A rubric prompt can therefore never
  silently inflate a pass rate, which is what would happen if the runner guessed.

Rubric prompts are 8 of the 10 reasoning prompts, so the graded reasoning column is only
2 prompts wide. That is a real limitation of these numbers, not a rounding detail: treat
the reasoning rate as indicative at best until the rubrics are graded.
