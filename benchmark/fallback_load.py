#!/usr/bin/env python3
"""
At-capacity load with the primary backend killed mid-run.

Exists because bench.py cannot answer the question this experiment asks: it records
latency but not *which* backend served each request, so it cannot separate
fallen-back requests from normal ones. This records `X-Routed-Backend` per request
and kills the primary itself, so the kill is correlated with the requests rather
than raced against them.

  python3 benchmark/fallback_load.py -n 24 -c 4 --kill-after 10 --out <dir>

Reports: requests failed, requests that fell back, p50 end-to-end of fallen-back
requests against normal ones, and the TTFT of every fallen-back request — which is
what shows the fallback happened before any byte reached the client.
"""
import argparse
import asyncio
import json
import os
import signal
import statistics
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

PROMPT = "Explain continuous batching in LLM inference in two sentences."


@dataclass
class Sample:
    index: int
    started_at: float
    backend: str | None = None
    tier: str | None = None
    status: int | None = None
    ttft: float | None = None
    e2e: float | None = None
    tokens: int = 0
    error: str | None = None
    # True if this request was in flight when the primary was killed.
    spans_kill: bool = False


def p50(values):
    return statistics.median(values) if values else None


async def one(client, gateway, model, max_tokens, index, t0) -> Sample:
    s = Sample(index=index, started_at=time.monotonic() - t0)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    start = time.monotonic()
    try:
        async with client.stream("POST", f"{gateway}/v1/chat/completions", json=payload) as resp:
            s.status = resp.status_code
            s.backend = resp.headers.get("X-Routed-Backend")
            s.tier = resp.headers.get("X-Model-Tier")
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")[:160]
                s.error = f"HTTP {resp.status_code}: {body}"
                s.e2e = time.monotonic() - start
                return s
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:"):].strip()
                if raw == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    # A failure after the first byte can only arrive in band.
                    s.error = f"in-band: {str(data['error'])[:140]}"
                    continue
                for choice in data.get("choices") or []:
                    token = (choice.get("delta") or {}).get("content") or ""
                    if token:
                        s.tokens += 1
                        if s.ttft is None:
                            s.ttft = time.monotonic() - start
        s.e2e = time.monotonic() - start
    except Exception as exc:
        s.error = f"{type(exc).__name__}: {exc}"
        s.e2e = time.monotonic() - start
    return s


def kill_primary(port: int) -> tuple[int | None, str]:
    """Kill the ollama serve process listening on `port`. Returns (pid, note)."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception as exc:
        return None, f"lsof failed: {exc}"
    pids = [int(p) for p in out.split() if p.strip().isdigit()]
    if not pids:
        return None, f"nothing listening on {port}"
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            return pid, f"kill failed: {exc}"
    return pids[0], "killed"


async def main() -> int:
    ap = argparse.ArgumentParser(description="At-capacity load with a mid-run backend kill.")
    ap.add_argument("-n", "--requests", type=int, default=24)
    ap.add_argument("-c", "--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--model", default="llama3.2:1b")
    ap.add_argument("--gateway", default="http://localhost:8000")
    ap.add_argument("--kill-after", type=float, default=10.0,
                    help="seconds into the run to kill the primary; 0 disables")
    ap.add_argument("--kill-port", type=int, default=11434)
    ap.add_argument("--primary-kind", default="ollama",
                    help="X-Routed-Backend value meaning 'served by the primary'")
    ap.add_argument("--label", default="rep")
    ap.add_argument("--out")
    args = ap.parse_args()

    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.monotonic()
    killed_at: float | None = None
    kill_note = "not scheduled"
    kill_pid = None

    async def killer():
        nonlocal killed_at, kill_note, kill_pid
        if args.kill_after <= 0:
            return
        await asyncio.sleep(args.kill_after)
        kill_pid, kill_note = kill_primary(args.kill_port)
        killed_at = time.monotonic() - t0
        print(f"  killed primary on :{args.kill_port} at +{killed_at:.2f}s "
              f"(pid={kill_pid}, {kill_note})", flush=True)

    samples: list[Sample] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        async def bounded(i):
            async with sem:
                samples.append(await one(client, args.gateway, args.model,
                                         args.max_tokens, i, t0))

        kill_task = asyncio.create_task(killer())
        await asyncio.gather(*(bounded(i) for i in range(args.requests)))
        await kill_task

    wall = time.monotonic() - t0
    samples.sort(key=lambda s: s.index)
    if killed_at is not None:
        for s in samples:
            end = s.started_at + (s.e2e or 0)
            s.spans_kill = s.started_at <= killed_at <= end

    ok = [s for s in samples if s.status == 200 and not s.error]
    failed = [s for s in samples if s.status != 200 or s.error]
    fellback = [s for s in ok if s.backend and s.backend != args.primary_kind]
    normal = [s for s in ok if s.backend == args.primary_kind]

    lines = []
    w = lines.append
    w(f"fallback load | n={args.requests} c={args.concurrency} "
      f"max_tokens={args.max_tokens} label={args.label}")
    w(f"wall {wall:.2f}s | kill scheduled at +{args.kill_after}s | "
      f"actually killed at +{killed_at:.2f}s ({kill_note})" if killed_at is not None
      else f"wall {wall:.2f}s | no kill ({kill_note})")
    w(f"kill landed mid-run: {'yes' if killed_at is not None and killed_at < wall else 'NO — after the run ended, result is not a fallback test'}")
    w("")
    w(f"{'idx':>4}{'start_s':>9}{'backend':>10}{'tier':>10}{'status':>8}"
      f"{'ttft_s':>9}{'e2e_s':>8}{'tok':>5}{'spans_kill':>12}  error")
    w("-" * 92)
    for s in samples:
        w(f"{s.index:>4}{s.started_at:>9.2f}{str(s.backend):>10}{str(s.tier):>10}"
          f"{str(s.status):>8}"
          f"{('-' if s.ttft is None else f'{s.ttft:.3f}'):>9}"
          f"{('-' if s.e2e is None else f'{s.e2e:.2f}'):>8}{s.tokens:>5}"
          f"{('yes' if s.spans_kill else ''):>12}  {s.error or ''}")
    w("")
    w("summary")
    w(f"  requests                {len(samples)}")
    w(f"  succeeded               {len(ok)}")
    w(f"  failed                  {len(failed)}")
    w(f"  served by primary       {len(normal)} ({args.primary_kind})")
    w(f"  fell back               {len(fellback)}")
    if fellback:
        kinds = sorted({s.backend for s in fellback})
        w(f"  fallback backends       {', '.join(kinds)}")
    w(f"  p50 e2e, normal         {fmt(p50([s.e2e for s in normal]))}")
    w(f"  p50 e2e, fell back      {fmt(p50([s.e2e for s in fellback]))}")
    w(f"  p50 ttft, normal        {fmt(p50([s.ttft for s in normal if s.ttft]))}")
    w(f"  p50 ttft, fell back     {fmt(p50([s.ttft for s in fellback if s.ttft]))}")
    w("")
    # Every fallen-back request must have its first token *after* the fallback, so a
    # fallen-back request with no TTFT at all would mean bytes were promised and never
    # delivered. Reported rather than asserted; the structural guarantee is in main.py.
    no_ttft = [s.index for s in fellback if s.ttft is None]
    w(f"  fallen-back requests with no first token: {no_ttft or 'none'}")
    inband = [s.index for s in samples if s.error and s.error.startswith("in-band")]
    w(f"  in-band errors (post-first-byte failures): {inband or 'none'}")

    report = "\n".join(lines)
    print()
    print(report)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.txt").write_text(report + "\n")
        (out / "samples.json").write_text(json.dumps({
            "conditions": {
                "requests": args.requests, "concurrency": args.concurrency,
                "max_tokens": args.max_tokens, "model": args.model,
                "kill_after": args.kill_after, "kill_port": args.kill_port,
                "killed_at": killed_at, "kill_note": kill_note, "kill_pid": kill_pid,
                "wall": wall, "label": args.label,
                "wall_clock": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            "samples": [asdict(s) for s in samples],
        }, indent=2))
        print(f"wrote {out}/report.txt and samples.json")
    return 0


def fmt(v):
    return "-" if v is None else f"{v:.3f}s"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
