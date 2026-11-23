#!/usr/bin/env python3
"""
A streaming client disconnects while another request is queued behind it.

Stage 1 measured what happens to the upstream when a client hangs up. It did not
measure what happens to the *next* request. With one gateway slot and one Ollama slot,
request B is admitted the moment A's slot is released — and Stage 1's fix releases that
slot after `agen.aclose()` returns, not after Ollama has actually stopped. If those are
not the same instant, B is admitted into a backend still working on A, and the wait
moves from the gateway queue (where it is visible) into Ollama (where it is not).

  python3 benchmark/queued_disconnect.py --pairs 20 --out <dir>

Each pair: A streams with max_tokens=400 and its client closes right after the first
content chunk; B is sent while A is in flight, so it queues. A control arm runs the same
shape with A completing normally.

Five timestamps per pair
------------------------
  1. client closes A's socket          host wall clock
  2. A's `finally` begins              gateway wall clock (request_cancelled.finally_at_ms)
  3. `agen.aclose()` returns           gateway wall clock (backend_stream_closed.at_ms)
  4. Ollama logs `cancel task` for A   host wall clock, harness-stamped at the poll
  5. B admitted                        gateway wall clock (slot_acquired.at_ms)

2, 3 and 5 share a clock. 1 and 4 share a different one. Comparing across those two
groups needs the offset between them, which this measures (see `measure_clock_offset`)
and reports with its uncertainty instead of assuming it is zero.

The conclusion that needs no clock alignment at all is B's `ollama.unattributed_ms` —
Ollama's own accounting of time this request spent waiting inside Ollama. Disconnect
pairs against control pairs answers the question directly, in one process's clock.
"""
import argparse
import asyncio
import json
import re
import statistics
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

BASE_PROMPT = ("Write a detailed explanation of how key-value caching works in "
               "transformer inference, covering memory layout and eviction.")
PAD = "additionally"

# Ollama prints no sub-second timestamps, so the harness stamps lines as it reads them.
# At a 10ms poll the stamp trails Ollama's own write by up to one interval.
LOG_POLL_SECONDS = 0.010

_RE_LAUNCH = re.compile(r"task\s+(\d+)\s*\|\s*processing task")
_RE_PROMPT_TOKENS = re.compile(r"task\s+(\d+)\s*\|.*task\.n_tokens\s*=\s*(\d+)")
_RE_CANCEL = re.compile(r"cancel task,\s*id_task\s*=\s*(\d+)")
_RE_RELEASE = re.compile(r"task\s+(\d+)\s*\|\s*stop processing")


@dataclass
class OllamaTask:
    task: int
    prompt_tokens: int | None = None
    launched_at: float | None = None
    cancelled_at: float | None = None
    released_at: float | None = None


class LogFollower:
    """Tails Ollama's log, stamping each line with host wall-clock milliseconds."""

    def __init__(self, path: Path, poll: float = LOG_POLL_SECONDS) -> None:
        self.path = path
        self.poll = poll
        self.tasks: dict[int, OllamaTask] = {}
        self._fh = None
        self._stop = asyncio.Event()
        self._task = None

    def _rec(self, tid: int) -> OllamaTask:
        return self.tasks.setdefault(tid, OllamaTask(task=tid))

    def _consume(self, line: str, now_ms: float) -> None:
        m = _RE_LAUNCH.search(line)
        if m:
            self._rec(int(m.group(1))).launched_at = now_ms
        m = _RE_PROMPT_TOKENS.search(line)
        if m:
            self._rec(int(m.group(1))).prompt_tokens = int(m.group(2))
        m = _RE_CANCEL.search(line)
        if m:
            self._rec(int(m.group(1))).cancelled_at = now_ms
        m = _RE_RELEASE.search(line)
        if m:
            self._rec(int(m.group(1))).released_at = now_ms

    async def _run(self) -> None:
        buf = ""
        while not self._stop.is_set():
            chunk = self._fh.read()
            if chunk:
                buf += chunk
                *lines, buf = buf.split("\n")
                now = time.time() * 1000.0
                for line in lines:
                    self._consume(line, now)
            else:
                await asyncio.sleep(self.poll)

    async def __aenter__(self):
        self._fh = self.path.open("r", errors="replace")
        self._fh.seek(0, 2)
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        if self._task:
            await self._task
        if self._fh:
            self._fh.close()


@dataclass
class Pair:
    index: int
    arm: str                     # "disconnect" | "control"
    a_req_id: str | None = None
    b_req_id: str | None = None
    a_prompt_tokens: int = 0
    b_prompt_tokens: int = 0
    # client side, host clock
    a_closed_at: float | None = None
    a_first_chunk_at: float | None = None
    b_sent_at: float | None = None
    a_chunks: int = 0
    b_status: int | None = None
    error: str | None = None
    # filled from the gateway log, gateway clock
    a_finally_at: float | None = None
    a_aclose_returned_at: float | None = None
    a_aclose_ms: float | None = None
    b_admitted_at: float | None = None
    b_queue_wait_ms: float | None = None
    b_unattributed_ms: float | None = None
    b_total_ms: float | None = None
    # from Ollama's log, host clock
    a_task: int | None = None
    a_cancelled_at: float | None = None
    a_released_at: float | None = None


def prompt_for(index: int, which: str) -> tuple[str, int]:
    """Unique prompt length per request, so Ollama's task.n_tokens identifies it."""
    pad = index * 4 + (0 if which == "a" else 2)
    return BASE_PROMPT + ((" " + " ".join([PAD] * pad)) if pad else ""), pad


async def measure_clock_offset(gateway: str, samples: int = 25) -> dict:
    """Estimate (gateway clock - host clock) in ms, NTP-style, with its uncertainty.

    /health returns the gateway's own wall clock. For each probe the server's stamp
    falls somewhere between the host's send (t0) and receive (t1), so

        offset  =  server_ms - (t0 + t1) / 2        +/- (t1 - t0) / 2

    The tightest bracket is the sample with the smallest round trip, and that round
    trip is the honest uncertainty. Reported with every cross-clock figure rather than
    assumed to be zero: the gateway runs in a Linux VM under Docker Desktop, whose
    clock is not the host's by construction.
    """
    probes = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        for _ in range(samples):
            t0 = time.time() * 1000.0
            try:
                resp = await client.get(f"{gateway}/health")
                t1 = time.time() * 1000.0
                server = (resp.json() or {}).get("now_ms")
            except Exception:
                continue
            if server is None:
                continue
            probes.append({"offset": server - (t0 + t1) / 2.0, "rtt": t1 - t0})
    if not probes:
        return {"usable": False,
                "note": "gateway did not return now_ms; cross-clock figures unsupported"}
    best = min(probes, key=lambda p: p["rtt"])
    offsets = [p["offset"] for p in probes]
    return {
        "usable": True,
        "samples": len(probes),
        "offset_ms_best": round(best["offset"], 3),
        "uncertainty_ms": round(best["rtt"] / 2.0, 3),
        "offset_ms_median": round(statistics.median(offsets), 3),
        "offset_spread_ms": round(max(offsets) - min(offsets), 3),
        "rtt_min_ms": round(best["rtt"], 3),
        "method": "NTP-style bracket via /health now_ms",
    }


async def run_pair(client: httpx.AsyncClient, gateway: str, model: str, pair: Pair,
                   max_tokens_a: int, max_tokens_b: int, settle_ms: float) -> None:
    """A streams and (in the disconnect arm) hangs up after its first chunk; B queues."""
    a_prompt, a_pad = prompt_for(pair.index, "a")
    b_prompt, b_pad = prompt_for(pair.index, "b")
    pair.a_prompt_tokens, pair.b_prompt_tokens = a_pad, b_pad

    b_task: asyncio.Task | None = None

    async def send_b():
        pair.b_sent_at = time.time() * 1000.0
        try:
            resp = await client.post(f"{gateway}/v1/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": b_prompt}],
                "max_tokens": max_tokens_b,
                "stream": False,
            })
            pair.b_status = resp.status_code
            pair.b_req_id = resp.headers.get("X-Request-Id")
        except Exception as exc:
            pair.error = f"B: {type(exc).__name__}: {exc}"

    try:
        async with client.stream("POST", f"{gateway}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": a_prompt}],
            "max_tokens": max_tokens_a,
            "stream": True,
        }) as resp:
            pair.a_req_id = resp.headers.get("X-Request-Id")
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
                choices = data.get("choices") or []
                if not choices:
                    continue
                token = (choices[0].get("delta") or {}).get("content") or ""
                if not token:
                    continue
                pair.a_chunks += 1
                if pair.a_chunks == 1:
                    pair.a_first_chunk_at = time.time() * 1000.0
                    # B is sent now, while A holds the only slot, so it must queue.
                    b_task = asyncio.create_task(send_b())
                    await asyncio.sleep(settle_ms / 1000.0)
                    if pair.arm == "disconnect":
                        break   # leaving the context drops A's socket
            if pair.arm == "disconnect":
                pass
    except Exception as exc:
        pair.error = f"A: {type(exc).__name__}: {exc}"
    finally:
        if pair.arm == "disconnect":
            pair.a_closed_at = time.time() * 1000.0

    if b_task is not None:
        await b_task


def attach_gateway_log(pairs: list[Pair], lines: list[str]) -> list[str]:
    """Pull the gateway-clock timestamps out of the gateway's JSON log, by req_id."""
    by_id: dict[str, dict] = {}
    warnings: list[str] = []
    for raw in lines:
        # `docker compose logs` prefixes each line with the service name, so the JSON
        # does not start at column 0. Take everything from the first brace.
        brace = raw.find("{")
        if brace == -1:
            continue
        try:
            rec = json.loads(raw[brace:])
        except json.JSONDecodeError:
            continue
        rid = rec.get("req_id")
        if not rid:
            continue
        by_id.setdefault(rid, {})[rec.get("message")] = rec

    for p in pairs:
        a = by_id.get(p.a_req_id or "", {})
        b = by_id.get(p.b_req_id or "", {})
        cancel = a.get("request_cancelled")
        closed = a.get("backend_stream_closed")
        if cancel:
            p.a_finally_at = cancel.get("finally_at_ms")
        if closed:
            p.a_aclose_returned_at = closed.get("at_ms")
            p.a_aclose_ms = closed.get("aclose_ms")
            if p.a_finally_at is None:
                p.a_finally_at = closed.get("finally_at_ms")
        slot = b.get("slot_acquired")
        if slot:
            p.b_admitted_at = slot.get("at_ms")
            p.b_queue_wait_ms = slot.get("waited_ms")
        done = b.get("request_completed")
        if done:
            p.b_unattributed_ms = done.get("ollama_unattributed_ms")
            p.b_total_ms = done.get("ollama_total_ms")
        if p.arm == "disconnect" and not cancel:
            warnings.append(f"pair {p.index}: no request_cancelled for A ({p.a_req_id})")
        if slot is None:
            warnings.append(f"pair {p.index}: no slot_acquired for B ({p.b_req_id})")
    return warnings


def attach_ollama(pairs: list[Pair], tasks: dict[int, OllamaTask]) -> list[str]:
    """Match A to its Ollama task by prompt length, and take its cancel stamp."""
    warnings: list[str] = []
    seen_tasks: dict[int, int] = {}
    usable = [t for t in tasks.values() if t.prompt_tokens is not None]
    for p in pairs:
        # A's padding is unique per pair and strictly below B's for the same pair.
        candidates = [t for t in usable
                      if t.launched_at is not None
                      and p.a_first_chunk_at is not None
                      and t.launched_at <= p.a_first_chunk_at]
        if not candidates:
            warnings.append(f"pair {p.index}: no Ollama task launched before A's first chunk")
            continue
        task = max(candidates, key=lambda t: t.launched_at)
        p.a_task = task.task
        if task.task in seen_tasks:
            warnings.append(
                f"pair {p.index}: matched Ollama task {task.task}, already claimed by "
                f"pair {seen_tasks[task.task]} — ordering heuristic is ambiguous here")
        seen_tasks[task.task] = p.index
        p.a_cancelled_at = task.cancelled_at
        p.a_released_at = task.released_at
        if p.arm == "disconnect" and task.cancelled_at is None:
            warnings.append(f"pair {p.index}: Ollama logged no cancel for task {task.task}")
    return warnings


def p50(xs):
    return statistics.median(xs) if xs else None


def fmt(v, nd=1):
    return "-" if v is None else f"{v:.{nd}f}"


def report(args, pairs: list[Pair], offset: dict, warnings: list[str]) -> str:
    L, w = [], lambda s: L.append(s)
    disc = [p for p in pairs if p.arm == "disconnect" and not p.error]
    ctrl = [p for p in pairs if p.arm == "control" and not p.error]

    # Host-clock stamps (A's close, Ollama's cancel) moved onto the gateway's clock.
    # Every figure derived from these carries `unc` below as its error bar.
    off = offset.get("offset_ms_best") if offset.get("usable") else None
    unc = offset.get("uncertainty_ms") if offset.get("usable") else None

    def to_gw(v):
        """Host-clock ms -> gateway-clock ms. None when the offset is unmeasured."""
        if v is None or off is None:
            return None
        return v + off

    w(f"queued disconnect | pairs={args.pairs} per arm | max_tokens A={args.max_tokens_a} "
      f"B={args.max_tokens_b}")
    w(f"gateway slots=1, OLLAMA_NUM_PARALLEL=1 (B must queue behind A)")
    w(f"wall clock: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    w(f"ollama log poll: {args.log_poll_ms:.0f} ms  "
      f"(a cancel stamp trails Ollama's own write by up to one interval)")
    w(f"clock offset probe: {offset}")
    w("")

    w("ORDERING per disconnect pair  (2,3,5 gateway clock | 4 host clock)")
    hdr = (f"{'pair':>5}{'A task':>8}{'2 finally':>12}{'3 aclose':>11}"
           f"{'5 B admit':>12}{'4 cancel':>11}{'B wait ms':>11}{'B unattr':>10}")
    w(hdr)
    w("-" * len(hdr))
    base = None
    for p in sorted(disc, key=lambda x: x.index):
        # Everything relative to that pair's own `finally`, so the columns are readable;
        # column 4 is on the other clock and is marked as such in the summary.
        b0 = p.a_finally_at
        rel = lambda v: "-" if (v is None or b0 is None) else f"{v - b0:+.1f}"
        w(f"{p.index:>5}{str(p.a_task):>8}{'0.0':>12}{rel(p.a_aclose_returned_at):>11}"
          f"{rel(p.b_admitted_at):>12}{rel(to_gw(p.a_cancelled_at)):>11}"
          f"{fmt(p.b_queue_wait_ms):>11}{fmt(p.b_unattributed_ms):>10}")
    w("")

    w("SUMMARY")
    w(f"  disconnect pairs usable        {len(disc)}/{args.pairs}")
    w(f"  control pairs usable           {len(ctrl)}/{args.pairs}")

    both = [p for p in disc
            if p.b_admitted_at is not None and to_gw(p.a_cancelled_at) is not None]
    if both:
        gaps = [p.b_admitted_at - to_gw(p.a_cancelled_at) for p in both]
        before = [g for g in gaps if g < 0]
        resolvable = [g for g in gaps if unc is not None and abs(g) > unc]
        w(f"  pairs with both stamps         {len(both)}")
        w(f"  B admitted BEFORE A cancelled  {len(before)}/{len(both)}")
        w(f"  gap B_admit - A_cancel (ms)    p50 {fmt(p50(gaps))}  "
          f"min {fmt(min(gaps))}  max {fmt(max(gaps))}")
        w(f"  CROSS-CLOCK. offset {fmt(off, 3)} ms, uncertainty +/-{fmt(unc, 3)} ms,")
        w(f"  plus up to {args.log_poll_ms:.0f} ms of log-poll lag on the cancel stamp,")
        w(f"  which biases the cancel stamp LATE and so makes B look earlier than it was.")
        poll = args.log_poll_ms
        if p50(gaps) is not None and abs(p50(gaps)) < poll:
            w(f"  |p50 gap| {abs(p50(gaps)):.1f} ms < poll {poll:.0f} ms -> ORDERING NOT RESOLVED.")
            w(f"  The clock-independent check below is what answers the question.")
        w(f"  gaps larger than the offset uncertainty: {len(resolvable)}/{len(gaps)}"
          f"  <- only these are individually resolvable")
    else:
        w("  B admitted vs A cancelled      no pair had both stamps"
          + ("" if off is not None else "  (offset unmeasured: cross-clock unsupported)"))

    aclose = [p.a_aclose_ms for p in disc if p.a_aclose_ms is not None]
    if aclose:
        w(f"  aclose() duration (ms)         p50 {fmt(p50(aclose), 3)}  max {fmt(max(aclose), 3)}")
    g_order = [p for p in disc if p.a_finally_at and p.b_admitted_at]
    if g_order:
        d = [p.b_admitted_at - p.a_finally_at for p in g_order]
        w(f"  finally -> B admitted (ms)     p50 {fmt(p50(d))}  max {fmt(max(d))}  "
          f"(same clock, reliable)")
    w("")

    w("THE CLOCK-INDEPENDENT CHECK — B's ollama.unattributed_ms")
    w("  Ollama's own accounting of time B spent waiting inside Ollama.")
    for arm, items in (("disconnect", disc), ("control", ctrl)):
        vals = [p.b_unattributed_ms for p in items if p.b_unattributed_ms is not None]
        if vals:
            w(f"  {arm:<11} n={len(vals):<3} p50 {fmt(p50(vals))} ms   "
              f"min {fmt(min(vals))}   max {fmt(max(vals))}")
        else:
            w(f"  {arm:<11} no values")
    dv = [p.b_unattributed_ms for p in disc if p.b_unattributed_ms is not None]
    cv = [p.b_unattributed_ms for p in ctrl if p.b_unattributed_ms is not None]
    if dv and cv:
        w(f"  difference of medians          {p50(dv) - p50(cv):+.1f} ms")
    w("")

    w("B's gateway queue wait")
    for arm, items in (("disconnect", disc), ("control", ctrl)):
        vals = [p.b_queue_wait_ms for p in items if p.b_queue_wait_ms is not None]
        if vals:
            w(f"  {arm:<11} n={len(vals):<3} p50 {fmt(p50(vals))} ms   max {fmt(max(vals))}")
    w("")

    late = [p for p in disc if p.a_finally_at is None]
    w(f"LATE FINALIZATION: {len(late)} disconnect pair(s) with no request_cancelled")
    if late:
        for p in late:
            w(f"  pair {p.index}: B queue wait {fmt(p.b_queue_wait_ms)} ms, "
              f"B unattributed {fmt(p.b_unattributed_ms)} ms")
    w("")

    if warnings:
        w("WARNINGS")
        for x in warnings[:40]:
            w(f"  {x}")
    return "\n".join(L)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Disconnect with a request queued behind it.")
    ap.add_argument("--pairs", type=int, default=20)
    ap.add_argument("--max-tokens-a", type=int, default=400)
    ap.add_argument("--max-tokens-b", type=int, default=32)
    ap.add_argument("--model", default="llama3.2:1b")
    ap.add_argument("--gateway", default="http://localhost:8000")
    ap.add_argument("--ollama-log", default="/tmp/ollama_p1.log")
    ap.add_argument("--gateway-log", required=True,
                    help="file with the gateway's JSON log for this run")
    ap.add_argument("--log-poll-ms", type=float, default=LOG_POLL_SECONDS * 1000,
                    help="Ollama log poll interval; bounds how late a cancel stamp is. "
                         "Resolving an ordering of a few ms needs this below that.")
    ap.add_argument("--settle-ms", type=float, default=60,
                    help="pause after sending B so it is certainly queued before A closes")
    ap.add_argument("--settle-s", type=float, default=2.0, help="pause between pairs")
    ap.add_argument("--out")
    args = ap.parse_args()

    log_path = Path(args.ollama_log)
    if not log_path.exists():
        print(f"ABORT: ollama log not found: {log_path}")
        return 2

    offset = await measure_clock_offset(args.gateway)
    pairs: list[Pair] = []

    async with LogFollower(log_path, poll=args.log_poll_ms / 1000.0) as follower:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
            for arm in ("disconnect", "control"):
                for i in range(args.pairs):
                    p = Pair(index=i, arm=arm)
                    await run_pair(client, args.gateway, args.model, p,
                                   args.max_tokens_a, args.max_tokens_b, args.settle_ms)
                    pairs.append(p)
                    print(f"  {arm} pair {i}: A chunks={p.a_chunks} B status={p.b_status}",
                          flush=True)
                    await asyncio.sleep(args.settle_s)
        await asyncio.sleep(3)   # let the last cancel reach the log
        tasks = dict(follower.tasks)

    gw_lines = Path(args.gateway_log).read_text(errors="replace").splitlines()
    warnings = attach_gateway_log(pairs, gw_lines)
    warnings += attach_ollama(pairs, tasks)

    text = report(args, pairs, offset, warnings)
    print()
    print(text)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.txt").write_text(text + "\n")
        (out / "pairs.json").write_text(json.dumps({
            "conditions": {
                "pairs_per_arm": args.pairs,
                "max_tokens_a": args.max_tokens_a,
                "max_tokens_b": args.max_tokens_b,
                "model": args.model,
                "ollama_log_poll_ms": args.log_poll_ms,
                "clock_offset_probe": offset,
                "wall_clock": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            "pairs": [asdict(p) for p in pairs],
            "ollama_tasks": [asdict(t) for t in sorted(tasks.values(), key=lambda t: t.task)],
            "warnings": warnings,
        }, indent=2))
        print(f"wrote {out}/report.txt and pairs.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
