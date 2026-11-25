#!/usr/bin/env python3
"""
Run the router's classify() over the adversarial prompt set and write the result.

  python3 eval/classify_adversarial.py

These prompts are never sent to a model: they exist to show what the keyword heuristic
does with words that merely *contain* a trigger. Writes adversarial-classify.txt beside
the prompt set, so the table in RESULTS.md cites a file rather than an inline command
that was run once and forgotten.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from gateway.router import _COMPLEXITY_SIGNALS, classify            # noqa: E402
from gateway.schemas import ChatCompletionRequest, ChatMessage      # noqa: E402

rows = [json.loads(l) for l in (ROOT / "prompts-adversarial.jsonl").read_text().splitlines() if l.strip()]

out = [f"classify() over eval/prompts-adversarial.jsonl ({len(rows)} prompts)",
       "no model is called; this is the router's decision only", ""]
hdr = f"{'id':<8}{'words':>6}  {'claimed':<12}{'actually matched':<20}{'routed to':<10}"
out += [hdr, "-" * len(hdr)]
for r in rows:
    text = r["prompt"].lower()
    fired = sorted(s for s in _COMPLEXITY_SIGNALS if s in text)
    tier = classify(ChatCompletionRequest(
        messages=[ChatMessage(role="user", content=r["prompt"])]))
    out.append(f"{r['id']:<8}{len(r['prompt'].split()):>6}  {r['trips']:<12}"
               f"{','.join(fired) or 'NONE':<20}{tier:<10}")
counts: dict[str, int] = {}
for r in rows:
    t = classify(ChatCompletionRequest(messages=[ChatMessage(role="user", content=r["prompt"])]))
    counts[t] = counts.get(t, 0) + 1
out += ["", f"totals: {counts}"]
text = "\n".join(out) + "\n"
(ROOT / "adversarial-classify.txt").write_text(text)
print(text)
print(f"wrote {ROOT / 'adversarial-classify.txt'}")
