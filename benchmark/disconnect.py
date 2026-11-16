#!/usr/bin/env python3
"""
What happens upstream when a streaming client hangs up.

Opens N streaming requests, reads chunks for --after seconds, then drops the
connection. Then reports, per request, how many tokens the client actually
received and how many Ollama went on to generate after nobody was listening.

  python3 benchmark/disconnect.py -n 10 -c 1 --ollama-log /tmp/ollama_p4.log
  python3 benchmark/disconnect.py -n 10 -c 4 --out benchmark/out/run-disconnect-.../c4

Needs a real server. TestClient has no socket to drop, and the gateway's
upstream connection pool is what is under test.

How a request is matched to Ollama's log
----------------------------------------
Ollama logs a task id but nothing about which HTTP request it came from, so
each request here is padded to a *unique prompt length*. The log's
"task.n_tokens = M" line then identifies it: sort tasks by M, sort requests by
padding, and the k-th of each is the same request. This needs no knowledge of
the tokenizer -- only that more padding means more tokens, which is monotone.

Tokens are counted from the log's "eval time = ... / K tokens" line, which is
Ollama's own eval_count for that task. The client's chunk count is what the
client actually saw. The difference is the waste.

OLLAMA_NUM_PARALLEL is reported, not detected -- pass what you launched with.
"""
import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

DEFAULT_GATEWAY = "http://localhost:8000"
DEFAULT_MODEL = "llama3.2:1b"
DEFAULT_OLLAMA_LOG = "/tmp/ollama_p4.log"
DEFAULT_PROM = "http://localhost:9090"

# Long enough that a 400-token generation is still running well after the
# client leaves, so "kept generating" is unambiguous.
BASE_PROMPT = (
    "Write a detailed explanation of how key-value caching works in "
    "transformer inference, covering memory layout and eviction."
)
PAD_WORD = "additionally"
# Words of padding per request index. 3 keeps consecutive requests clearly
# apart in token count even if the tokenizer merges some of them.
PAD_STEP = 3


# --- Ollama log parsing ---------------------------------------------------------------
# Example lines (llama.cpp runner, via ollama serve):
#   slot   operator(): id  3 | task 45 | new prompt, n_ctx_slot = 4096, n_keep = 4, task.n_tokens = 33
#   slot print_timing: id  3 | task 45 |        eval time =      93.55 ms /    16 tokens (...)
#   slot      release: id  3 | task 45 | stop processing: n_tokens = 48, truncated = 0
#   srv          stop: cancel task, id_task = 0
_RE_PROMPT = re.compile(r"task\s+(\d+)\s*\|.*task\.n_tokens\s*=\s*(\d+)")
_RE_EVAL = re.compile(r"task\s+(\d+)\s*\|\s*eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens")
_RE_RELEASE = re.compile(r"task\s+(\d+)\s*\|\s*stop processing:\s*n_tokens\s*=\s*(\d+)")
_RE_LAUNCH = re.compile(r"task\s+(\d+)\s*\|\s*processing task")
_RE_CANCEL = re.compile(r"cancel task,\s*id_task\s*=\s*(\d+)")


@dataclass
class TaskRecord:
    """One generation task as Ollama's log describes it."""

    task: int
    prompt_tokens: int | None = None
    eval_tokens: int | None = None
    eval_ms: float | None = None
    context_tokens: int | None = None
    launched_at: float | None = None
    # When *we* observed the release line. The log has no sub-second timestamps,
    # so this is our read time; the follower polls every 50ms, so it trails
    # Ollama's own write by that much at worst.
    released_at: float | None = None
    # "srv stop: cancel task" — Ollama abandoning the generation, which is the
    # direct evidence that the upstream connection went away.
    cancelled_at: float | None = None

    @property
    def generated_tokens(self) -> int | None:
        """Tokens this task actually produced.

        eval_count is authoritative, but llama.cpp only prints its timing block on
        normal completion — a cancelled task has no eval line at all. For those,
        total context minus prompt is the same number by a different route.
        """
        if self.eval_tokens is not None:
            return self.eval_tokens
        if self.context_tokens is not None and self.prompt_tokens is not None:
            return max(0, self.context_tokens - self.prompt_tokens)
        return None

    @property
    def token_source(self) -> str:
        if self.eval_tokens is not None:
            return "eval_count"
        if self.context_tokens is not None and self.prompt_tokens is not None:
            return "ctx-prompt"
        return "none"


class OllamaLogFollower:
    """Tails the Ollama log from the current end, stamping lines as they arrive.

    The log's own timestamps are second-resolution and only on [GIN] lines, so
    arrival time is observed here instead. Started before any request is sent.
    """

    def __init__(self, path: Path, poll_interval: float = 0.05) -> None:
        self.path = path
        self.poll_interval = poll_interval
        self.tasks: dict[int, TaskRecord] = {}
        self._fh = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def _rec(self, task_id: int) -> TaskRecord:
        return self.tasks.setdefault(task_id, TaskRecord(task=task_id))

    def _consume(self, line: str, now: float) -> None:
        m = _RE_LAUNCH.search(line)
        if m:
            self._rec(int(m.group(1))).launched_at = now
        m = _RE_PROMPT.search(line)
        if m:
            self._rec(int(m.group(1))).prompt_tokens = int(m.group(2))
        m = _RE_EVAL.search(line)
        if m:
            rec = self._rec(int(m.group(1)))
            rec.eval_ms = float(m.group(2))
            rec.eval_tokens = int(m.group(3))
        m = _RE_RELEASE.search(line)
        if m:
            rec = self._rec(int(m.group(1)))
            rec.context_tokens = int(m.group(2))
            rec.released_at = now
        m = _RE_CANCEL.search(line)
        if m:
            self._rec(int(m.group(1))).cancelled_at = now

    async def _run(self) -> None:
        buf = ""
        while not self._stop.is_set():
            chunk = self._fh.read()
            if chunk:
                buf += chunk
                *lines, buf = buf.split("\n")
                now = time.monotonic()
                for line in lines:
                    self._consume(line, now)
            else:
                await asyncio.sleep(self.poll_interval)

    async def __aenter__(self) -> "OllamaLogFollower":
        self._fh = self.path.open("r", errors="replace")
        self._fh.seek(0, 2)  # only lines written from now on
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        if self._task:
            await self._task
        if self._fh:
            self._fh.close()

    async def drain(self, settle: float) -> None:
        """Keep reading for `settle` seconds, to catch tasks still finishing."""
        await asyncio.sleep(settle)


# --- the client side ------------------------------------------------------------------

@dataclass
class RequestRecord:
    index: int
    pad_words: int
    chunks: int = 0
    content_chars: int = 0
    started_at: float | None = None
    first_chunk_at: float | None = None
    closed_at: float | None = None
    status: int | None = None
    error: str | None = None
    # True if the stream reached [DONE] before the read window expired, i.e. this
    # request was never actually a disconnect and must not count as one.
    completed: bool = False
    # filled in after matching against the log
    matched_task: int | None = None
    ollama_generated: int | None = None
    ollama_token_source: str = "none"
    ollama_released_at: float | None = None
    ollama_cancelled_at: float | None = None

    @property
    def after_close_s(self) -> float | None:
        """Client close → Ollama finishing with this task."""
        if self.ollama_released_at is None or self.closed_at is None:
            return None
        return self.ollama_released_at - self.closed_at

    @property
    def cancel_lag_s(self) -> float | None:
        """Client close → Ollama logging "cancel task"."""
        if self.ollama_cancelled_at is None or self.closed_at is None:
            return None
        return self.ollama_cancelled_at - self.closed_at

    @property
    def tokens_after_close(self) -> int | None:
        """What Ollama generated that nobody received.

        A chunk is one token in Ollama's streaming protocol, so chunks is the
        client's token count. Approximate at the boundary by at most a token or
        two: one in flight when the socket closed is counted as received.
        """
        if self.ollama_generated is None:
            return None
        return max(0, self.ollama_generated - self.chunks)


def build_prompt(index: int) -> tuple[str, int]:
    pad_words = index * PAD_STEP
    padding = (" " + " ".join([PAD_WORD] * pad_words)) if pad_words else ""
    return BASE_PROMPT + padding, pad_words


async def one_request(
    rec: RequestRecord, gateway: str, model: str, max_tokens: int, after: float
) -> None:
    """Stream, read for `after` seconds, then drop the connection.

    A fresh client per request, closed without draining the response, so the
    socket is really gone rather than parked in a keep-alive pool.
    """
    prompt, _ = build_prompt(rec.index)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    async def drain(resp: httpx.Response) -> None:
        """Read chunks until the stream ends. Cancelled from outside at the deadline."""
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:"):].strip()
            if raw == "[DONE]":
                rec.completed = True
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices") or []
            if not choices:
                continue
            token = (choices[0].get("delta") or {}).get("content") or ""
            if token:
                rec.chunks += 1
                rec.content_chars += len(token)
                if rec.first_chunk_at is None:
                    rec.first_chunk_at = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            rec.started_at = time.monotonic()
            async with client.stream(
                "POST", f"{gateway}/v1/chat/completions", json=payload
            ) as resp:
                rec.status = resp.status_code
                # The read window is enforced by cancelling the read, not by checking
                # the clock after each chunk: a request still waiting for its first
                # token would otherwise never look at the deadline and would hang up
                # late, which stops being a disconnect mid-generation.
                try:
                    await asyncio.wait_for(drain(resp), timeout=after)
                except asyncio.TimeoutError:
                    pass
                # Leaving both context managers here closes the socket without
                # draining the response, so the connection is really gone rather
                # than parked in a keep-alive pool.
    except Exception as exc:
        rec.error = f"{type(exc).__name__}: {exc}"
    finally:
        rec.closed_at = time.monotonic()


async def run_requests(
    n: int, c: int, gateway: str, model: str, max_tokens: int, after: float
) -> list[RequestRecord]:
    records = [RequestRecord(index=i, pad_words=i * PAD_STEP) for i in range(n)]
    sem = asyncio.Semaphore(c)

    async def bounded(rec: RequestRecord) -> None:
        async with sem:
            await one_request(rec, gateway, model, max_tokens, after)

    await asyncio.gather(*(bounded(r) for r in records))
    return records


# --- matching -------------------------------------------------------------------------

def match_tasks(records: list[RequestRecord], tasks: dict[int, TaskRecord]) -> list[str]:
    """Rank-match requests to log tasks by prompt length. Returns warnings.

    Both sides are sorted by prompt size: requests by the padding we added,
    tasks by the n_tokens Ollama reported. More padding is always more tokens,
    so the orders agree. Anything that cannot be matched is reported rather
    than guessed at.
    """
    warnings: list[str] = []
    usable = [t for t in tasks.values() if t.prompt_tokens is not None]
    missing_prompt = [t.task for t in tasks.values() if t.prompt_tokens is None]
    if missing_prompt:
        warnings.append(
            f"{len(missing_prompt)} log task(s) had no task.n_tokens line and were "
            f"left unmatched (tasks {sorted(missing_prompt)}) -- a fully cached "
            f"prompt skips that line"
        )

    if len(usable) != len(records):
        warnings.append(
            f"matched {len(usable)} log task(s) against {len(records)} request(s); "
            f"counts differ, so the mapping below is only as good as the ordering"
        )

    by_tokens = sorted(usable, key=lambda t: (t.prompt_tokens, t.task))
    by_pad = sorted(records, key=lambda r: r.pad_words)
    distinct = len({t.prompt_tokens for t in usable})
    if distinct != len(usable):
        warnings.append(
            f"only {distinct} distinct prompt-token counts across {len(usable)} tasks; "
            f"padding did not fully separate them, so matching may be wrong"
        )

    for rec, task in zip(by_pad, by_tokens):
        rec.matched_task = task.task
        rec.ollama_generated = task.generated_tokens
        rec.ollama_token_source = task.token_source
        rec.ollama_released_at = task.released_at
        rec.ollama_cancelled_at = task.cancelled_at
        if task.generated_tokens is None:
            warnings.append(
                f"task {task.task} logged neither an eval line nor a release line "
                f"-- still generating when the run ended?")
    return warnings


# --- metrics --------------------------------------------------------------------------

async def sample_gateway_metrics(
    gateway: str, stop: asyncio.Event, interval: float, out: list[dict]
) -> None:
    """Poll the gateway's /metrics for the two gauges.

    Finer-grained than Prometheus's 5s scrape, so a slot that is released
    promptly is visible as more than one data point. Prometheus remains the
    authority for the "within one scrape interval" claim.
    """
    names = ("llm_active_requests", "llm_queue_waiting_requests")
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        while not stop.is_set():
            t = time.monotonic()
            try:
                resp = await client.get(f"{gateway}/metrics")
                vals: dict[str, float] = {}
                for line in resp.text.splitlines():
                    for name in names:
                        if line.startswith(name + "{"):
                            vals[name] = vals.get(name, 0.0) + float(line.rsplit(" ", 1)[1])
                out.append({"at": t, **vals})
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


def query_prometheus_range(
    prom: str, start_wall: float, end_wall: float, step: str = "1s"
) -> dict:
    """Range-query the two gauges over the run window.

    Separate from the /metrics polling above: this is what Prometheus actually
    recorded at its own 5s cadence, which is what "drops within one scrape
    interval" has to be judged against.
    """
    import urllib.parse
    import urllib.request

    out: dict = {"start": start_wall, "end": end_wall, "step": step, "series": {}}
    for name in ("llm_active_requests", "llm_queue_waiting_requests"):
        url = prom + "/api/v1/query_range?" + urllib.parse.urlencode({
            "query": f"sum({name})",
            "start": f"{start_wall:.3f}",
            "end": f"{end_wall:.3f}",
            "step": step,
        })
        try:
            with urllib.request.urlopen(url, timeout=30) as fh:
                data = json.load(fh)
            result = data.get("data", {}).get("result", [])
            out["series"][name] = result[0]["values"] if result else []
        except Exception as exc:
            out["series"][name] = []
            out.setdefault("errors", {})[name] = str(exc)
    return out


# --- reporting ------------------------------------------------------------------------

def fmt_report(args, records: list[RequestRecord], warnings: list[str],
               gauge_samples: list[dict], prom: dict) -> str:
    lines: list[str] = []
    w = lines.append

    w(f"client disconnect | n={args.requests} c={args.concurrency} "
      f"after={args.after}s max_tokens={args.max_tokens}")
    w(f"model={args.model} | OLLAMA_NUM_PARALLEL={args.ollama_num_parallel} "
      f"| label={args.label}")
    w(f"gateway={args.gateway} | ollama log={args.ollama_log}")
    w(f"wall clock: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    w("")

    hdr = (f'{"req":>4}{"task":>6}{"chunks":>8}{"generated":>11}{"src":>12}'
           f'{"after_close":>13}{"cancel_lag_s":>14}{"done_lag_s":>12}'
           f'{"status":>8}{"full":>6}')
    w(hdr)
    w("-" * len(hdr))
    for r in sorted(records, key=lambda x: x.index):
        task = r.matched_task if r.matched_task is not None else "-"
        gen = r.ollama_generated if r.ollama_generated is not None else "-"
        aft = r.tokens_after_close if r.tokens_after_close is not None else "-"
        lag = r.cancel_lag_s
        gap = r.after_close_s
        lag_s = f"{lag:14.3f}" if lag is not None else f"{'-':>14}"
        gap_s = f"{gap:12.3f}" if gap is not None else f"{'-':>12}"
        w(f"{r.index:>4}{str(task):>6}{r.chunks:>8}{str(gen):>11}"
          f"{r.ollama_token_source:>12}{str(aft):>13}{lag_s}{gap_s}"
          f"{str(r.status):>8}{('yes' if r.completed else 'no'):>6}")
    w("")

    disconnected = [r for r in records if not r.completed]
    completed = [r for r in records if r.completed]
    gens = [r.ollama_generated for r in disconnected if r.ollama_generated is not None]
    afters = [r.tokens_after_close for r in disconnected if r.tokens_after_close is not None]
    gaps = [r.after_close_s for r in disconnected if r.after_close_s is not None]
    lags = [r.cancel_lag_s for r in disconnected if r.cancel_lag_s is not None]

    w("totals  (over the requests that really were cut off)")
    w(f"  requests disconnected           {len(disconnected)} of {len(records)}")
    if completed:
        w(f"  requests that finished first    {len(completed)}  <- excluded from the "
          f"totals below; raise --max-tokens or lower --after")
    w(f"  chunks received by clients      {sum(r.chunks for r in disconnected)}")
    w(f"  tokens generated by Ollama      {sum(gens)}   ({len(gens)} task(s))")
    w(f"  tokens generated after close    {sum(afters)}")
    if gens and sum(gens):
        w(f"  wasted fraction                 {sum(afters) / sum(gens):.1%} of generated tokens")
    w(f"  ollama logged 'cancel task'     {len(lags)} of {len(disconnected)}")
    if lags:
        w(f"  close -> cancel (s)             median {statistics.median(lags):.3f} "
          f"max {max(lags):.3f}")
    if gaps:
        w(f"  close -> ollama done (s)        median {statistics.median(gaps):.3f} "
          f"max {max(gaps):.3f}")
    else:
        w("  close -> ollama done (s)        no release lines matched")
    w("")

    if gauge_samples:
        peak_active = max(s.get("llm_active_requests", 0) for s in gauge_samples)
        peak_wait = max(s.get("llm_queue_waiting_requests", 0) for s in gauge_samples)
        last = gauge_samples[-1]
        w("gateway gauges (polled from /metrics, finer than Prometheus' 5s scrape)")
        w(f"  peak llm_active_requests        {peak_active:g}")
        w(f"  peak llm_queue_waiting_requests {peak_wait:g}")
        w(f"  final llm_active_requests       {last.get('llm_active_requests', 0):g}")
        w("")

    for name, values in prom.get("series", {}).items():
        if values:
            peak = max(float(v[1]) for v in values)
            w(f"prometheus {name}: {len(values)} points, peak {peak:g}, "
              f"last {float(values[-1][1]):g}")
        else:
            err = prom.get("errors", {}).get(name, "no data in window")
            w(f"prometheus {name}: {err}")
    w("")

    if warnings:
        w("warnings")
        for warn in warnings:
            w(f"  - {warn}")
        w("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Measure what Ollama does after a streaming client disconnects.")
    p.add_argument("-n", "--requests", type=int, default=10)
    p.add_argument("-c", "--concurrency", type=int, default=1)
    p.add_argument("--after", type=float, default=1.0,
                   help="seconds to read chunks before dropping the connection")
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--gateway", default=DEFAULT_GATEWAY)
    p.add_argument("--ollama-log", default=DEFAULT_OLLAMA_LOG)
    p.add_argument("--prom", default=DEFAULT_PROM)
    p.add_argument("--settle", type=float, default=25.0,
                   help="seconds to keep reading the log after the last client closes")
    p.add_argument("--gauge-interval", type=float, default=0.25)
    p.add_argument("--label", default="unlabelled",
                   help="'before' or 'after' the cancellation fix, for the record")
    p.add_argument("--ollama-num-parallel", default="unknown",
                   help="reported, not detected -- pass what you launched with")
    p.add_argument("--out", help="directory to write report.txt and records.json into")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    log_path = Path(args.ollama_log)
    if not log_path.exists():
        print(f"ABORT: ollama log not found: {log_path}", file=sys.stderr)
        return 2

    print(f"disconnect: n={args.requests} c={args.concurrency} after={args.after}s "
          f"max_tokens={args.max_tokens} label={args.label}")
    print(f"following {log_path}")

    gauge_samples: list[dict] = []
    stop = asyncio.Event()
    gauge_task = asyncio.create_task(
        sample_gateway_metrics(args.gateway, stop, args.gauge_interval, gauge_samples))

    start_wall = time.time()
    async with OllamaLogFollower(log_path) as follower:
        records = await run_requests(
            args.requests, args.concurrency, args.gateway,
            args.model, args.max_tokens, args.after)
        print(f"all {len(records)} clients closed; "
              f"watching the log for {args.settle}s more")
        await follower.drain(args.settle)
        tasks = dict(follower.tasks)

    stop.set()
    await gauge_task
    end_wall = time.time()

    warnings = match_tasks(records, tasks)
    # Prometheus needs a scrape or two past the end to have the window covered.
    await asyncio.sleep(6)
    prom = query_prometheus_range(args.prom, start_wall - 5, end_wall + 6)

    report = fmt_report(args, records, warnings, gauge_samples, prom)
    print()
    print(report)

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.txt").write_text(report + "\n")
        (out_dir / "records.json").write_text(json.dumps({
            "conditions": {
                "requests": args.requests,
                "concurrency": args.concurrency,
                "after_s": args.after,
                "max_tokens": args.max_tokens,
                "model": args.model,
                "label": args.label,
                "ollama_num_parallel": args.ollama_num_parallel,
                "gateway": args.gateway,
                "ollama_log": str(log_path),
                "started_wall": start_wall,
                "ended_wall": end_wall,
            },
            "requests": [
                {**asdict(r),
                 "tokens_after_close": r.tokens_after_close,
                 "after_close_s": r.after_close_s}
                for r in sorted(records, key=lambda x: x.index)
            ],
            "ollama_tasks": [asdict(t) for t in sorted(tasks.values(), key=lambda t: t.task)],
            "gauge_samples": gauge_samples,
            "prometheus": prom,
            "warnings": warnings,
        }, indent=2))
        print(f"wrote {out_dir}/report.txt and records.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
