#!/usr/bin/env python3
"""
Merge a run's mechanical results with the hand grades and print the final tables.

  python3 eval/report.py --run benchmark/out/run-eval-<hash>-<ts> [--out <dir>]

Separate from run.py because grading happens after the run: the runner records what a
tier said, this merges in what a human decided about it. Keeping them apart means a
regrade never requires re-running the models.

Every item is graded twice. `grader: "author"` is **the result**; `grader:
"claude-session"` is reported alongside, because a model grading a model's output is not
independent of what is being measured. Disagreements are listed in full with both
reasons, since those are the informative part.

Every grade carries the sha256 of the completion it was made against, and this script
**verifies it**. A grade whose hash no longer matches is reported as stale rather than
counted, because a grade silently attached to a different completion is worse than no
grade at all.
"""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATEGORIES = ("factual", "reasoning", "code")


def load(run_dir: Path):
    data = json.loads((run_dir / "results.json").read_text())
    grades = []
    path = ROOT / "grades.jsonl"
    if path.exists():
        grades = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return data, grades


def _compare(a: dict, b: dict) -> tuple[int, list[tuple[str, tuple[str, str]]]]:
    """(items both graded the same, list of (dimension, key) where they differ)."""
    agree, differ = 0, []
    for dim, items in a.items():
        for key, verdict in items.items():
            other = b.get(dim, {}).get(key)
            if other is None:
                continue
            if other == verdict:
                agree += 1
            else:
                differ.append((dim, key))
    return agree, sorted(differ)


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge hand grades into a run's results.")
    ap.add_argument("--run", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()

    run_dir = Path(args.run)
    sys.path.insert(0, str(ROOT.parent / 'benchmark'))
    from runmeta import require_valid
    require_valid(run_dir)
    data, grades = load(run_dir)
    results = {(r["prompt_id"], r["tier"]): r for r in data["results"]}

    # grader -> dimension -> (prompt, tier) -> passed, and the reasons alongside
    by_grader: dict[str, dict[str, dict[tuple[str, str], bool]]] = defaultdict(
        lambda: defaultdict(dict))
    reasons: dict[tuple[str, str, str, str], str] = {}
    stale: list[str] = []
    orphan: list[str] = []
    for entry in grades:
        key = (entry["prompt_id"], entry["tier"])
        grader = entry.get("grader", "unknown")
        res = results.get(key)
        if res is None:
            orphan.append(f"{key[0]} [{key[1]}] — no such cell in this run")
            continue
        digest = hashlib.sha256(res["completion"].encode()).hexdigest()
        if digest != entry["completion_sha256"]:
            stale.append(f"{key[0]} [{key[1]}] {entry['dimension']} [{grader}] — "
                         f"graded {entry['completion_sha256'][:12]}, "
                         f"run has {digest[:12]}")
            continue
        if entry.get("passed") is None:
            continue  # not yet graded by this grader
        by_grader[grader][entry["dimension"]][key] = entry["passed"]
        reasons[(grader, entry["dimension"], *key)] = entry.get("reason", "")

    # The author's grades are the result. Until they exist, the session's are shown and
    # labelled as such rather than presented as the answer.
    primary = "author" if by_grader.get("author") else "claude-session"
    graded = by_grader[primary]
    secondary = "claude-session" if primary == "author" else None

    tiers = list(dict.fromkeys(r["tier"] for r in data["results"]))
    lines: list[str] = []
    w = lines.append

    w(f"eval report | run {run_dir.name}")
    w(f"eval set @ {data['conditions']['prompts_commit']} | "
      f"temperature={data['conditions']['temperature']} | "
      f"max_tokens={data['conditions']['max_tokens']}")
    filled = {g: sum(len(d) for d in dims.values()) for g, dims in by_grader.items()}
    total_items = sum(1 for e in grades if e.get("grader") == "author")
    w(f"graders: " + ", ".join(f"{g} {n}/{total_items}" for g, n in sorted(filled.items()))
      + (f" | {len(stale)} stale, {len(orphan)} orphaned" if stale or orphan else ""))
    w(f"reported column: {primary}"
      + ("" if primary == "author" else
         "  <-- PENDING: the author has not graded yet, so these are the session's "
         "grades and are not the result"))
    if secondary:
        agree, differ = _compare(by_grader["author"], by_grader[secondary])
        w(f"agreement: {agree}/{agree + len(differ)} items where both graders have a verdict")
    w("")

    # --- reasoning, now fully graded ---
    w("reasoning — all 10, mechanical checks plus hand-graded rubrics")
    hdr = f"{'tier':<10}{'model':<16}{'mechanical':>12}{'rubric':>9}{'total':>8}{'rate':>8}"
    w(hdr)
    w("-" * len(hdr))
    for tier in tiers:
        items = [r for r in data["results"] if r["tier"] == tier and r["category"] == "reasoning"]
        mech = [r for r in items if r["check_type"] != "rubric"]
        mech_pass = sum(1 for r in mech if r["passed"])
        rub = [r for r in items if r["check_type"] == "rubric"]
        rub_pass = sum(1 for r in rub if graded["rubric"].get((r["prompt_id"], tier)))
        rub_known = sum(1 for r in rub if (r["prompt_id"], tier) in graded["rubric"])
        total = mech_pass + rub_pass
        n = len(items)
        model = next((r["model"] for r in items if r["model"]), "?")
        w(f"{tier:<10}{model:<16}{f'{mech_pass}/{len(mech)}':>12}"
          f"{f'{rub_pass}/{rub_known}':>9}{f'{total}/{n}':>8}{f'{total/n*100:.0f}%':>8}")
    w("")

    # --- code, split ---
    w("code — content correct vs output instruction followed")
    hdr2 = (f"{'tier':<10}{'model':<16}{'content':>9}{'format':>9}"
            f"{'mechanical':>12}")
    w(hdr2)
    w("-" * len(hdr2))
    for tier in tiers:
        items = [r for r in data["results"] if r["tier"] == tier and r["category"] == "code"]
        n = len(items)
        content = sum(1 for r in items if graded["content"].get((r["prompt_id"], tier)))
        fmt = sum(1 for r in items if graded["format"].get((r["prompt_id"], tier)))
        mech = sum(1 for r in items if r["passed"])
        model = next((r["model"] for r in items if r["model"]), "?")
        w(f"{tier:<10}{model:<16}{f'{content}/{n}':>9}{f'{fmt}/{n}':>9}"
          f"{f'{mech}/{n}':>12}")
    w("")
    w("'mechanical' is what the committed regex/json check said. It is not a third")
    w("opinion: where it disagrees with content+format, the check is the thing that is")
    w("wrong, and the per-prompt reasons in grades.jsonl say how.")
    w("")

    # --- factual, unchanged ---
    w("factual — mechanical only, no hand grading needed")
    hdr3 = f"{'tier':<10}{'model':<16}{'passed':>9}{'rate':>8}"
    w(hdr3)
    w("-" * len(hdr3))
    for tier in tiers:
        items = [r for r in data["results"] if r["tier"] == tier and r["category"] == "factual"]
        passed = sum(1 for r in items if r["passed"])
        model = next((r["model"] for r in items if r["model"]), "?")
        w(f"{tier:<10}{model:<16}{f'{passed}/{len(items)}':>9}"
          f"{f'{passed/len(items)*100:.0f}%':>8}")
    w("")

    if stale:
        w("STALE GRADES — not counted, the completion changed since grading")
        for s in stale:
            w(f"  {s}")
        w("")
    if orphan:
        w("orphaned grades — for cells not in this run")
        for o in orphan:
            w(f"  {o}")
        w("")

    # --- disagreements ---
    if secondary:
        _agree, differ = _compare(by_grader["author"], by_grader[secondary])
        w(f"disagreements between author and {secondary} — {len(differ)} items")
        if not differ:
            w("  none")
        for dim, key in differ:
            pid, tier = key
            a = by_grader["author"][dim][key]
            b = by_grader[secondary][dim][key]
            w(f"  {pid} [{tier}] {dim}: author={'pass' if a else 'FAIL'}, "
              f"{secondary}={'pass' if b else 'FAIL'}")
            w(f"      author: {reasons.get(('author', dim, pid, tier), '')}")
            w(f"      {secondary}: {reasons.get((secondary, dim, pid, tier), '')}")
        w("")

    # --- per prompt, every dimension ---
    w("per prompt")
    hdr4 = (f"{'id':<10}{'category':<11}{'tier':<10}{'mech':<7}{'content':<9}"
            f"{'format':<8}{'rubric':<8}")
    w(hdr4)
    w("-" * len(hdr4))

    def mark(value):
        return {True: "pass", False: "FAIL", None: "-"}[value]

    for r in data["results"]:
        key = (r["prompt_id"], r["tier"])
        w(f"{r['prompt_id']:<10}{r['category']:<11}{r['tier']:<10}"
          f"{mark(r['passed']):<7}"
          f"{mark(graded['content'].get(key)):<9}"
          f"{mark(graded['format'].get(key)):<8}"
          f"{mark(graded['rubric'].get(key)):<8}")

    text = "\n".join(lines)
    print(text)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "graded-report.txt").write_text(text + "\n")
        print(f"\nwrote {out}/graded-report.txt")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
