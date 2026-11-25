#!/usr/bin/env python3
"""
Tokens Ollama generated after a client disconnected, per pair, from the raw logs.

  python3 benchmark/after_close.py benchmark/out/run-queued-disconnect-<id>

One counting rule, applied identically to every run so two runs can be compared:

  generated = eval_count                     when llama.cpp printed a timing block
            = context_at_release - prompt    otherwise

A cancelled task prints no timing block, so the fallback is the usual case and an
`eval_count` row means that task ran to a normal finish. The source is recorded per
pair, because mixing the two silently is how the first version of this analysis
produced a figure that could not be reconciled with the next run.

after_close = generated - chunks the client received. A chunk is one token in Ollama's
streaming protocol, so a pair that received 1 chunk and shows 12 generated wasted 11.

Writes after-close-by-pair.txt into the run directory.
"""
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runmeta import require_valid  # noqa: E402


def analyse(run_dir: Path) -> tuple[list[dict], list[str]]:
    pairs = json.loads((run_dir / "pairs.json").read_text())["pairs"]
    notes: list[str] = []

    prompt, evaltok, ctx = {}, {}, {}
    for line in (run_dir / "ollama.log").read_text(errors="replace").splitlines():
        m = re.search(r"task\s+(\d+)\s*\|.*task\.n_tokens\s*=\s*(\d+)", line)
        if m:
            prompt[int(m.group(1))] = int(m.group(2))
        m = re.search(r"task\s+(\d+)\s*\|\s*eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens", line)
        if m:
            evaltok[int(m.group(1))] = int(m.group(2))
        m = re.search(r"task\s+(\d+)\s*\|\s*stop processing:\s*n_tokens\s*=\s*(\d+)", line)
        if m:
            ctx[int(m.group(1))] = int(m.group(2))

    # A pair whose A finished on its own was never a disconnect.
    completed = set()
    for line in (run_dir / "gateway.log").read_text(errors="replace").splitlines():
        i = line.find("{")
        if i == -1:
            continue
        try:
            rec = json.loads(line[i:])
        except Exception:
            continue
        if rec.get("message") == "request_completed" and rec.get("req_id"):
            completed.add(rec["req_id"])

    rows = []
    for p in pairs:
        if p["arm"] != "disconnect":
            continue
        if p.get("a_req_id") in completed:
            notes.append(f"pair {p['index']}: excluded, A finished on its own")
            continue
        task = p.get("a_task")
        if task is None:
            notes.append(f"pair {p['index']}: no Ollama task matched")
            continue
        if task in evaltok:
            generated, source = evaltok[task], "eval_count"
        elif task in ctx and task in prompt:
            generated, source = max(0, ctx[task] - prompt[task]), "ctx-prompt"
        else:
            notes.append(f"pair {p['index']}: task {task} has neither timing nor release")
            continue
        rows.append({
            "pair": p["index"],
            "task": task,
            "received": p["a_chunks"],
            "generated": generated,
            "after_close": max(0, generated - p["a_chunks"]),
            "source": source,
            "late": (p.get("b_queue_wait_ms") or 0) > 10_000,
            "slot_held_ms": round(p.get("b_queue_wait_ms") or 0, 1),
        })
    return rows, notes


def report(run_dir: Path, rows: list[dict], notes: list[str]) -> str:
    out: list[str] = []
    w = out.append
    w(f"tokens generated after close | {run_dir.name}")
    w("rule: eval_count where llama.cpp printed a timing block, else context - prompt")
    w("")
    hdr = (f"{'pair':>5}{'task':>7}{'received':>10}{'generated':>11}"
           f"{'after':>8}{'source':>12}{'slot_ms':>10}{'late':>6}")
    w(hdr)
    w("-" * len(hdr))
    for r in sorted(rows, key=lambda r: r["pair"]):
        w(f"{r['pair']:>5}{r['task']:>7}{r['received']:>10}{r['generated']:>11}"
          f"{r['after_close']:>8}{r['source']:>12}{r['slot_held_ms']:>10.1f}"
          f"{'YES' if r['late'] else '':>6}")
    w("")
    for label, subset in (("normal", [r for r in rows if not r["late"]]),
                          ("late", [r for r in rows if r["late"]])):
        if not subset:
            continue
        a = [r["after_close"] for r in subset]
        w(f"{label} pairs: n={len(subset)}  after_close total {sum(a)}  "
          f"mean {sum(a)/len(a):.2f}  p50 {statistics.median(a):.0f}  max {max(a)}")
    src: dict[str, int] = {}
    for r in rows:
        src[r["source"]] = src.get(r["source"], 0) + 1
    w(f"source split: {src}")
    if notes:
        w("")
        w("notes")
        for n in notes:
            w(f"  {n}")
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for arg in sys.argv[1:]:
        run_dir = Path(arg)
        # Refuses outright on a run that failed its own checks; prints a banner
        # for one that predates them.
        require_valid(run_dir)
        rows, notes = analyse(run_dir)
        text = report(run_dir, rows, notes)
        (run_dir / "after-close-by-pair.txt").write_text(text + "\n")
        print(text)
        print(f"\nwrote {run_dir}/after-close-by-pair.txt\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
