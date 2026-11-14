#!/usr/bin/env python3
"""
Benchmark the LLM Serving Stack gateway.

Default sweep runs five scenarios, ending with one that oversubscribes the
gateway's queue so slot contention actually shows up. Pass -n/-c to run a
single custom scenario instead.

  python3 benchmark/bench.py
  python3 benchmark/bench.py -n 16 -c 8 --stream
  python3 benchmark/bench.py --ollama-num-parallel 4

OLLAMA_NUM_PARALLEL is reported, not detected: it belongs to the Ollama server
process, which this script cannot inspect. Pass the value you launched it with
so the output records the conditions. See README for how to set it (the macOS
menu-bar app will ignore shell env vars).
"""
import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass

import httpx

DEFAULT_GATEWAY = "http://localhost:8000"
DEFAULT_MODEL = "llama3.2:1b"
PROMPT = "Explain continuous batching in LLM inference in two sentences."


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (type 7, as used by numpy's default).

    statistics.quantiles needs n>=2 and the old durations[int(p*n)] indexing
    silently returned the max for small samples — at n=4, int(0.95*4) == 3.
    """
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass
class Result:
    duration: float
    ttft: float | None
    tokens: int
    success: bool
    # True when the count came from the gateway's usage block rather than
    # from counting chunks, which is approximate.
    tokens_exact: bool = False


async def call(client: httpx.AsyncClient, gateway: str, model: str, stream: bool,
               max_tokens: int) -> Result:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": stream,
        "max_tokens": max_tokens,
    }
    start = time.perf_counter()
    ttft = None
    chunk_tokens = 0
    reported_tokens = None
    try:
        if stream:
            async with client.stream("POST", f"{gateway}/v1/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    err = chunk.get("error")
                    if err:
                        # a mid-stream failure, not a short answer — count it as failed
                        msg = err.get("message") if isinstance(err, dict) else err
                        raise RuntimeError(f"stream error: {msg}")
                    usage = chunk.get("usage")
                    if usage and usage.get("completion_tokens") is not None:
                        reported_tokens = usage["completion_tokens"]
                    choices = chunk.get("choices")
                    if not choices:
                        continue
                    if choices[0].get("delta", {}).get("content"):
                        # Fallback only. A chunk is not guaranteed to be one token —
                        # OpenAI-compatible backends may pack several into one.
                        chunk_tokens += 1
                        if ttft is None:
                            ttft = time.perf_counter() - start
            exact = reported_tokens is not None
            return Result(time.perf_counter() - start, ttft,
                          reported_tokens if exact else chunk_tokens, True, exact)

        resp = await client.post(f"{gateway}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return Result(time.perf_counter() - start, ttft,
                      resp.json()["usage"]["completion_tokens"], True, True)
    except Exception as exc:
        print(f"    error: {exc}")
        return Result(time.perf_counter() - start, None, 0, False)


async def scenario(label: str, gateway: str, model: str, n: int, stream: bool,
                   concurrency: int, max_tokens: int, slots: int | None) -> None:
    mode = "stream" if stream else "sync"
    note = ""
    if slots and concurrency > slots:
        note = f"  <- oversubscribed, {concurrency - slots} will queue at the gateway"
    print(f"\n{'-' * 64}")
    print(f"  {label}")
    print(f"  {n} requests | {mode} | concurrency={concurrency}{note}")
    print(f"{'-' * 64}")

    sem = asyncio.Semaphore(concurrency)

    async def bounded(client):
        async with sem:
            return await call(client, gateway, model, stream, max_tokens)

    async with httpx.AsyncClient(timeout=300.0) as client:
        wall_start = time.perf_counter()
        results = await asyncio.gather(*[bounded(client) for _ in range(n)])
        wall = time.perf_counter() - wall_start

    ok = [r for r in results if r.success]
    if not ok:
        print("  all requests failed")
        return

    durations = [r.duration for r in ok]
    ttfts = [r.ttft for r in ok if r.ttft is not None]
    total_tok = sum(r.tokens for r in ok)

    print(f"  success       : {len(ok)}/{n}")
    print(f"  wall time     : {wall:.2f}s")
    print(f"  throughput    : {len(ok) / wall:.2f} req/s")
    print(f"  latency p50   : {percentile(durations, 0.50):.2f}s")
    print(f"  latency p95   : {percentile(durations, 0.95):.2f}s")
    source = ("backend usage" if all(r.tokens_exact for r in ok)
              else "chunk count, approximate" if not any(r.tokens_exact for r in ok)
              else "mixed: some usage, some chunk count")
    print(f"  total tokens  : {total_tok}  ({source})")
    print(f"  tokens/sec    : {total_tok / wall:.1f}")
    if ttfts:
        print(f"  TTFT p50      : {percentile(ttfts, 0.50):.3f}s")
        print(f"  TTFT p95      : {percentile(ttfts, 0.95):.3f}s")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark the LLM Serving Stack gateway.")
    p.add_argument("-n", "--requests", type=int, help="request count for a single custom scenario")
    p.add_argument("-c", "--concurrency", type=int, help="concurrency for a single custom scenario")
    p.add_argument("--stream", action="store_true", help="stream in the custom scenario")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--gateway", default=DEFAULT_GATEWAY)
    p.add_argument("--max-tokens", type=int, default=120)
    p.add_argument("--ollama-num-parallel", default="unknown",
                   help="value the Ollama server was launched with; reported, not detected")
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    print("\n  LLM Serving Stack — Benchmark")
    print(f"  {'gateway':<22}: {args.gateway}")
    print(f"  {'model':<22}: {args.model}")
    print(f"  {'max_tokens':<22}: {args.max_tokens}")

    slots = None
    async with httpx.AsyncClient() as c:
        try:
            health = (await c.get(f"{args.gateway}/health", timeout=5)).json()
        except Exception as exc:
            print(f"  gateway unreachable: {exc}")
            return
    slots = health.get("queue_slots")
    print(f"  {'gateway queue slots':<22}: {slots if slots is not None else 'unknown'}")
    print(f"  {'OLLAMA_NUM_PARALLEL':<22}: {args.ollama_num_parallel}  (reported, not detected)")
    print(f"  {'backend':<22}: {health.get('backend')}")
    print(f"  {'health':<22}: {health.get('status')}")

    if args.requests or args.concurrency:
        n = args.requests or 8
        conc = args.concurrency or 1
        await scenario(f"custom — {n} requests, concurrency {conc}", args.gateway, args.model,
                       n=n, stream=args.stream, concurrency=conc,
                       max_tokens=args.max_tokens, slots=slots)
    else:
        common = dict(gateway=args.gateway, model=args.model, max_tokens=args.max_tokens, slots=slots)
        await scenario("1 - sequential, sync", n=3, stream=False, concurrency=1, **common)
        await scenario("2 - sequential, streaming (TTFT visible)", n=3, stream=True, concurrency=1, **common)
        await scenario("3 - concurrent, sync", n=6, stream=False, concurrency=4, **common)
        await scenario("4 - concurrent, streaming", n=4, stream=True, concurrency=4, **common)
        await scenario("5 - oversubscribed, streaming (gateway queueing)", n=16, stream=True,
                       concurrency=8, **common)

    print("\n  Done. Grafana: http://localhost:3000   Jaeger: http://localhost:16686\n")


if __name__ == "__main__":
    asyncio.run(main())
