#!/usr/bin/env python3
"""
Quality per tier: every prompt through every configured tier, once, at temperature 0.

  python3 eval/run.py --tiers fast,balanced --out benchmark/out/run-eval-<hash>-<ts>
  python3 eval/run.py --tiers powerful            # when a Bedrock tier is configured

Records per prompt per cell: model id, backend kind, prompt tokens and completion
tokens from the backend's reported usage, pass/fail, and the raw completion.

Prompt token count is not optional here. Tiers differ in chat template as well as in
weights — llama3.2 and llama3.1 differ by 15 tokens of system preamble on an identical
prompt (see benchmark/NOTES.md) — so without the per-cell token count a tier difference
and a template difference are indistinguishable after the fact.

Pass counts are reported per category per tier. There is deliberately no blended
score: "19/30" across three unlike categories is a number that cannot be acted on.

Rubric checks are NOT graded here. They are recorded with the completion for a human
(or a separate judge) to grade, and counted separately, so a rubric prompt can never
silently inflate a pass rate.
"""
import argparse
import asyncio
import json
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts.jsonl"
CATEGORIES = ("factual", "reasoning", "code")


@dataclass
class Result:
    prompt_id: str
    category: str
    tier: str
    model: str | None = None
    backend: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_reported: bool = False
    completion: str = ""
    check_type: str = ""
    passed: bool | None = None       # None = needs human grading (rubric)
    latency: float | None = None
    error: str | None = None
    classified_tier: str | None = None


def load_prompts(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate prompt ids in the eval set")
    return rows


def grade(check: dict, completion: str) -> bool | None:
    """True/False for a mechanical check, None for one needing a human."""
    kind = check.get("type")
    text = completion.strip()
    if kind == "exact":
        return text == check["expect"]
    if kind == "regex":
        return re.search(check["pattern"], text) is not None
    if kind == "json":
        # Tolerate a fenced block, which small models add even when told not to.
        candidate = text
        fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fence:
            candidate = fence.group(1).strip()
        else:
            start = min((i for i in (candidate.find("{"), candidate.find("[")) if i != -1),
                        default=-1)
            if start > 0:
                candidate = candidate[start:]
        try:
            return json.loads(candidate) == check["expect"]
        except Exception:
            return False
    if kind == "rubric":
        return None
    raise SystemExit(f"unknown check type {kind!r}")


async def ask(client: httpx.AsyncClient, gateway: str, tier: str, row: dict,
              max_tokens: int) -> Result:
    res = Result(prompt_id=row["id"], category=row["category"], tier=tier,
                 check_type=row["check"]["type"])
    payload = {
        # "auto" plus an explicit tier: the tier is what this eval varies, and naming a
        # model instead would pin the backend and bypass the tier's chain.
        "model": "auto",
        "routing": {"tier": tier},
        "messages": [{"role": "user", "content": row["prompt"]}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    start = time.monotonic()
    try:
        resp = await client.post(f"{gateway}/v1/chat/completions", json=payload)
        res.latency = time.monotonic() - start
        res.backend = resp.headers.get("X-Routed-Backend")
        res.model = resp.headers.get("X-Routed-Model")
        if resp.status_code != 200:
            res.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return res
        body = resp.json()
        res.completion = (body["choices"][0]["message"].get("content") or "")
        usage = body.get("usage") or {}
        res.prompt_tokens = usage.get("prompt_tokens", 0)
        res.completion_tokens = usage.get("completion_tokens", 0)
        res.usage_reported = bool(usage)
        res.passed = grade(row["check"], res.completion)
    except Exception as exc:
        res.latency = time.monotonic() - start
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def classify_locally(rows: list[dict]) -> dict[str, str]:
    """What the router's heuristic would do with each prompt under `auto`.

    Imported rather than inferred, so this is the same code the gateway runs.
    """
    import sys
    sys.path.insert(0, str(ROOT.parent))
    from gateway.router import classify
    from gateway.schemas import ChatCompletionRequest, ChatMessage

    out = {}
    for row in rows:
        req = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content=row["prompt"])])
        out[row["id"]] = classify(req)
    return out


def report(rows, results, classified, args, prompts_hash) -> str:
    lines: list[str] = []
    w = lines.append
    by_tier = defaultdict(list)
    for r in results:
        by_tier[r.tier].append(r)

    w(f"quality per tier | {len(rows)} prompts | temperature=0 | "
      f"max_tokens={args.max_tokens}")
    w(f"eval set: eval/prompts.jsonl @ {prompts_hash}")
    w(f"wall clock: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    w("")

    for tier, items in by_tier.items():
        models = sorted({r.model for r in items if r.model})
        backends = sorted({r.backend for r in items if r.backend})
        w(f"tier={tier}  model={','.join(models) or '?'}  backend={','.join(backends) or '?'}")
    w("")

    w("pass counts per category per tier  (graded = mechanical checks only)")
    hdr = f"{'tier':<10}{'category':<12}{'graded':>8}{'passed':>8}{'rate':>8}{'rubric':>8}{'errors':>8}"
    w(hdr)
    w("-" * len(hdr))
    for tier, items in by_tier.items():
        for cat in CATEGORIES:
            cat_items = [r for r in items if r.category == cat]
            graded = [r for r in cat_items if r.passed is not None and not r.error]
            passed = [r for r in graded if r.passed]
            rubric = [r for r in cat_items if r.passed is None and not r.error]
            errors = [r for r in cat_items if r.error]
            rate = f"{len(passed)/len(graded)*100:.0f}%" if graded else "-"
            w(f"{tier:<10}{cat:<12}{len(graded):>8}{len(passed):>8}{rate:>8}"
              f"{len(rubric):>8}{len(errors):>8}")
    w("")
    w("No blended score is reported: the three categories are not commensurable, and")
    w("rubric prompts are ungraded here by design — they are recorded for grading.")
    w("")

    w("prompt tokens per cell  (tier differences are partly template differences)")
    hdr2 = f"{'tier':<10}{'prompt_tok_total':>18}{'prompt_tok_mean':>18}{'completion_tok':>16}"
    w(hdr2)
    w("-" * len(hdr2))
    for tier, items in by_tier.items():
        ok = [r for r in items if r.usage_reported]
        total = sum(r.prompt_tokens for r in ok)
        mean = total / len(ok) if ok else 0
        w(f"{tier:<10}{total:>18}{mean:>18.1f}{sum(r.completion_tokens for r in ok):>16}")
    w("")

    w("what classify() would have done under `auto`")
    counts = defaultdict(lambda: defaultdict(int))
    for row in rows:
        counts[row["category"]][classified[row["id"]]] += 1
    hdr3 = f"{'category':<12}" + "".join(f"{t:>11}" for t in ("fast", "balanced", "powerful"))
    w(hdr3)
    w("-" * len(hdr3))
    for cat in CATEGORIES:
        w(f"{cat:<12}" + "".join(f"{counts[cat][t]:>11}" for t in ("fast", "balanced", "powerful")))
    totals = defaultdict(int)
    for row in rows:
        totals[classified[row["id"]]] += 1
    w(f"{'TOTAL':<12}" + "".join(f"{totals[t]:>11}" for t in ("fast", "balanced", "powerful")))
    w("")
    w("Cost of that routing: untested, Bedrock. The local tiers have no measured")
    w("marginal cost, so there is no price to multiply these counts by.")
    w("")

    w("per prompt")
    hdr4 = (f"{'id':<10}{'category':<11}{'tier':<10}{'check':<8}{'result':<8}"
            f"{'p_tok':>7}{'c_tok':>7}{'auto':>10}")
    w(hdr4)
    w("-" * len(hdr4))
    for r in results:
        if r.error:
            result = "ERROR"
        elif r.passed is None:
            result = "rubric"
        else:
            result = "pass" if r.passed else "FAIL"
        w(f"{r.prompt_id:<10}{r.category:<11}{r.tier:<10}{r.check_type:<8}{result:<8}"
          f"{r.prompt_tokens:>7}{r.completion_tokens:>7}"
          f"{classified.get(r.prompt_id, '?'):>10}")
    errs = [r for r in results if r.error]
    if errs:
        w("")
        w("errors")
        for r in errs:
            w(f"  {r.prompt_id} [{r.tier}]: {r.error}")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Run the eval set through each configured tier.")
    ap.add_argument("--tiers", default="fast,balanced",
                    help="comma-separated tier names to evaluate")
    ap.add_argument("--gateway", default="http://localhost:8000")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--prompts", default=str(PROMPTS))
    ap.add_argument("--out")
    args = ap.parse_args()

    path = Path(args.prompts)
    rows = load_prompts(path)
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]

    # The eval set's commit is part of the conditions: a pass rate against an edited
    # set is not comparable with one against the committed set.
    try:
        prompts_hash = subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", str(path)],
            capture_output=True, text=True, timeout=30, cwd=ROOT).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", str(path)],
                               capture_output=True, text=True, timeout=30,
                               cwd=ROOT).stdout.strip()
        if dirty:
            prompts_hash = f"{prompts_hash or 'uncommitted'}-DIRTY"
    except Exception:
        prompts_hash = "unknown"

    classified = classify_locally(rows)

    results: list[Result] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for tier in tiers:
            print(f"running tier={tier} ({len(rows)} prompts) ...", flush=True)
            for row in rows:
                # Sequential on purpose: concurrency would add queue wait to the
                # latency column and this eval is about answers, not throughput.
                results.append(await ask(client, args.gateway, tier, row, args.max_tokens))

    text = report(rows, results, classified, args, prompts_hash)
    print()
    print(text)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.txt").write_text(text + "\n")
        (out / "results.json").write_text(json.dumps({
            "conditions": {
                "prompts_file": str(path),
                "prompts_commit": prompts_hash,
                "prompt_count": len(rows),
                "tiers": tiers,
                "temperature": 0,
                "max_tokens": args.max_tokens,
                "gateway": args.gateway,
                "wall_clock": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            "classify_under_auto": classified,
            "results": [asdict(r) for r in results],
        }, indent=2))
        print(f"wrote {out}/report.txt and results.json")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
