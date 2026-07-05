#!/usr/bin/env python3
"""
Benchmark the LLM Serving Stack gateway.

4 scenarios:
  1. Sequential, non-streaming
  2. Sequential, streaming  — shows TTFT
  3. Concurrent, non-streaming — queue pressure
  4. Concurrent, streaming
"""
import asyncio
import json
import statistics
import time
from dataclasses import dataclass

import httpx

GATEWAY = "http://localhost:8000"
MODEL = "llama3.2:1b"
PROMPT = "Explain continuous batching in LLM inference in two sentences."


@dataclass
class Result:
    duration: float
    ttft: float | None
    tokens: int
    success: bool


async def call(client: httpx.AsyncClient, stream: bool) -> Result:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": stream,
        "max_tokens": 120,
    }
    start = time.perf_counter()
    ttft = None
    tokens = 0
    try:
        if stream:
            async with client.stream("POST", f"{GATEWAY}/v1/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta and ttft is None:
                            ttft = time.perf_counter() - start
                        tokens += len(delta.split()) if delta else 0
                    except (json.JSONDecodeError, KeyError):
                        pass
        else:
            resp = await client.post(f"{GATEWAY}/v1/chat/completions", json=payload)
            resp.raise_for_status()
            tokens = resp.json()["usage"]["completion_tokens"]

        return Result(time.perf_counter() - start, ttft, tokens, True)
    except Exception as exc:
        print(f"  error: {exc}")
        return Result(time.perf_counter() - start, None, 0, False)


async def scenario(label: str, n: int, stream: bool, concurrency: int) -> None:
    mode = "stream" if stream else "sync "
    print(f"\n{'─'*52}")
    print(f"  {label}")
    print(f"  {n} requests | {mode} | concurrency={concurrency}")
    print(f"{'─'*52}")

    sem = asyncio.Semaphore(concurrency)

    async def bounded(client):
        async with sem:
            return await call(client, stream)

    async with httpx.AsyncClient(timeout=120.0) as client:
        wall_start = time.perf_counter()
        results = await asyncio.gather(*[bounded(client) for _ in range(n)])
        wall = time.perf_counter() - wall_start

    ok = [r for r in results if r.success]
    if not ok:
        print("  all requests failed")
        return

    durations = sorted(r.duration for r in ok)
    ttfts = [r.ttft for r in ok if r.ttft is not None]
    total_tok = sum(r.tokens for r in ok)

    print(f"  success       : {len(ok)}/{n}")
    print(f"  wall time     : {wall:.2f}s")
    print(f"  throughput    : {len(ok)/wall:.2f} req/s")
    print(f"  latency p50   : {statistics.median(durations):.2f}s")
    print(f"  latency p95   : {durations[int(0.95 * len(durations))]:.2f}s")
    print(f"  total tokens  : {total_tok}")
    print(f"  tokens/sec    : {total_tok/wall:.1f}")
    if ttfts:
        print(f"  TTFT p50      : {statistics.median(ttfts):.3f}s")


async def main() -> None:
    print("\n  LLM Serving Stack — Benchmark")
    print(f"  gateway : {GATEWAY}")
    print(f"  model   : {MODEL}")

    async with httpx.AsyncClient() as c:
        try:
            h = await c.get(f"{GATEWAY}/health", timeout=5)
            print(f"  health  : {h.json()}")
        except Exception as exc:
            print(f"  gateway unreachable: {exc}")
            return

    await scenario("1 — sequential, non-streaming", n=3, stream=False, concurrency=1)
    await scenario("2 — sequential, streaming (TTFT visible)", n=3, stream=True, concurrency=1)
    await scenario("3 — concurrent, non-streaming (queue pressure)", n=6, stream=False, concurrency=4)
    await scenario("4 — concurrent, streaming", n=4, stream=True, concurrency=4)

    print("\n  Done. Check Grafana at http://localhost:3000\n")


if __name__ == "__main__":
    asyncio.run(main())
