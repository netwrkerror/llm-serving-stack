#!/usr/bin/env python3
"""
Three cells, one prompt set: what the gateway costs you on top of a provider.

  a  bedrock-direct   aioboto3 converse_stream from this machine, no gateway
  b  bedrock-gateway  the same model through the gateway
  c  local-gateway    local Ollama through the gateway

The gateway's overhead on Bedrock is (b) - (c is a separate baseline, not a term).

  python3 benchmark/bedrock_compare.py --cells c
  python3 benchmark/bedrock_compare.py --cells a,b,c \
      --bedrock-model <model-id> --region us-east-1

Cells a and b need AWS credentials from the standard chain and will say so rather
than guessing. Cell c needs a running gateway and Ollama.

Reports TTFT p50 and end-to-end p50 per cell, and the token usage each cell's
provider reported — cost comes from reported usage, never from an estimate.
Bedrock latency varies by region and time of day, so the wall-clock hour is
recorded with every run.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

DEFAULT_GATEWAY = "http://localhost:8000"
DEFAULT_LOCAL_MODEL = "llama3.2:1b"

# Fixed prompt set, same for every cell so the comparison is of backends and not
# of prompts. Deliberately short and varied in shape rather than in length.
PROMPTS = [
    "What is the capital of France?",
    "Name three sorting algorithms.",
    "What does TTFT stand for in model serving?",
    "Give one reason batching helps GPU utilisation.",
    "What is a KV cache, in one sentence?",
    "Which is larger, a gigabyte or a megabyte?",
    "Write a one-line Python list comprehension that squares 1..5.",
    "What HTTP status code means 'too many requests'?",
    "Define p50 latency in one sentence.",
    "What is the difference between a token and a word?",
]


@dataclass
class Sample:
    prompt_index: int
    ttft: float | None = None
    e2e: float | None = None
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_reported: bool = False
    error: str | None = None


@dataclass
class Cell:
    name: str
    model: str
    backend: str
    samples: list[Sample] = field(default_factory=list)
    unavailable: str | None = None

    def ok(self) -> list[Sample]:
        return [s for s in self.samples if s.error is None and s.e2e is not None]


def p50(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


# --- cell c / b: through the gateway --------------------------------------------------

async def gateway_sample(client: httpx.AsyncClient, gateway: str, model: str,
                         prompt: str, index: int, max_tokens: int) -> Sample:
    s = Sample(prompt_index=index)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    start = time.perf_counter()
    try:
        async with client.stream("POST", f"{gateway}/v1/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")[:200]
                s.error = f"HTTP {resp.status_code}: {body}"
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
                    s.error = str(data["error"])[:200]
                    return s
                usage = data.get("usage")
                if usage:
                    s.prompt_tokens = usage.get("prompt_tokens", 0)
                    s.completion_tokens = usage.get("completion_tokens", 0)
                    s.usage_reported = True
                for choice in data.get("choices") or []:
                    token = (choice.get("delta") or {}).get("content") or ""
                    if token:
                        s.tokens += 1
                        if s.ttft is None:
                            s.ttft = time.perf_counter() - start
        s.e2e = time.perf_counter() - start
    except Exception as exc:
        s.error = f"{type(exc).__name__}: {exc}"
    return s


# --- cell a: Bedrock directly ---------------------------------------------------------

async def bedrock_direct_sample(client, model: str, prompt: str, index: int,
                                max_tokens: int) -> Sample:
    """Same call the BedrockBackend makes, without the gateway in the path."""
    s = Sample(prompt_index=index)
    args = {
        "modelId": model,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.7, "topP": 1.0},
    }
    start = time.perf_counter()
    try:
        resp = await client.converse_stream(**args)
        async for event in resp["stream"]:
            if "contentBlockDelta" in event:
                token = (event["contentBlockDelta"].get("delta", {}) or {}).get("text", "")
                if token:
                    s.tokens += 1
                    if s.ttft is None:
                        s.ttft = time.perf_counter() - start
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {}) or {}
                if usage:
                    s.prompt_tokens = usage.get("inputTokens", 0)
                    s.completion_tokens = usage.get("outputTokens", 0)
                    s.usage_reported = True
        s.e2e = time.perf_counter() - start
    except Exception as exc:
        s.error = f"{type(exc).__name__}: {exc}"
    return s


async def run_bedrock_direct(cell: Cell, region: str, max_tokens: int, n: int) -> None:
    try:
        import aioboto3
    except ImportError:
        cell.unavailable = "aioboto3 is not installed (pip install -r gateway/requirements.txt)"
        return

    session = aioboto3.Session()
    creds = session.get_credentials()
    if asyncio.iscoroutine(creds):
        creds = await creds
    if creds is None:
        cell.unavailable = (
            "no AWS credentials resolved from the standard chain "
            "(env, profile, SSO, instance role) — cell not run")
        return

    kwargs = {}
    if region:
        kwargs["region_name"] = region
    async with session.client("bedrock-runtime", **kwargs) as client:
        for i in range(n):
            cell.samples.append(
                await bedrock_direct_sample(client, cell.model, PROMPTS[i % len(PROMPTS)],
                                            i % len(PROMPTS), max_tokens))


async def run_gateway(cell: Cell, gateway: str, max_tokens: int, n: int) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        try:
            health = await client.get(f"{gateway}/health")
            health.raise_for_status()
        except Exception as exc:
            cell.unavailable = f"gateway not reachable at {gateway}: {exc}"
            return
        for i in range(n):
            cell.samples.append(
                await gateway_sample(client, gateway, cell.model,
                                     PROMPTS[i % len(PROMPTS)], i % len(PROMPTS), max_tokens))


# --- reporting ------------------------------------------------------------------------

def fmt_report(args, cells: list[Cell]) -> str:
    out: list[str] = []
    w = out.append
    w(f"bedrock vs local | n={args.requests} c=1 max_tokens={args.max_tokens}")
    w(f"prompt set: {len(PROMPTS)} fixed prompts, same for every cell")
    w(f"wall clock: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}  "
      f"(Bedrock latency varies by region and hour)")
    w(f"region: {args.region or '(from credential chain)'}")
    w("")

    hdr = (f'{"cell":<20}{"backend":<10}{"model":<34}{"n":>4}{"ttft_p50_s":>12}'
           f'{"e2e_p50_s":>11}{"tok_p50":>9}{"usage":>8}')
    w(hdr)
    w("-" * len(hdr))
    def num(value: float | None, width: int, places: int = 3) -> str:
        return f"{'-':>{width}}" if value is None else f"{value:{width}.{places}f}"

    for cell in cells:
        prefix = f"{cell.name:<20}{cell.backend:<10}{cell.model[:33]:<34}"
        if cell.unavailable:
            w(f"{prefix}{'-':>4}{'not run':>12}{'-':>11}{'-':>9}{'-':>8}")
            continue
        ok = cell.ok()
        ttft = p50([s.ttft for s in ok if s.ttft is not None])
        e2e = p50([s.e2e for s in ok])
        tok = p50([float(s.tokens) for s in ok])
        usage = "yes" if ok and all(s.usage_reported for s in ok) else "partial"
        w(f"{prefix}{len(ok):>4}{num(ttft, 12)}{num(e2e, 11)}"
          f"{num(tok, 9, 0)}{usage:>8}")
    w("")

    by_name = {c.name: c for c in cells}
    direct, viagw = by_name.get("bedrock-direct"), by_name.get("bedrock-gateway")
    if direct and viagw and direct.ok() and viagw.ok():
        d_e2e, g_e2e = p50([s.e2e for s in direct.ok()]), p50([s.e2e for s in viagw.ok()])
        d_ttft = p50([s.ttft for s in direct.ok() if s.ttft is not None])
        g_ttft = p50([s.ttft for s in viagw.ok() if s.ttft is not None])
        w("gateway overhead on Bedrock  (b) - (a)")
        w(f"  ttft p50   {(g_ttft - d_ttft) * 1000:+.1f} ms")
        w(f"  e2e  p50   {(g_e2e - d_e2e) * 1000:+.1f} ms")
        w("")

    w("reported token usage (for cost; never estimated)")
    for cell in cells:
        ok = cell.ok()
        if not ok:
            continue
        pt = sum(s.prompt_tokens for s in ok)
        ct = sum(s.completion_tokens for s in ok)
        w(f"  {cell.name:<20} prompt={pt:<7} completion={ct:<7} "
          f"requests={len(ok)}  {'(reported)' if all(s.usage_reported for s in ok) else '(INCOMPLETE — do not cost this)'}")
    w("")

    for cell in cells:
        if cell.unavailable:
            w(f"not run: {cell.name} — {cell.unavailable}")
        else:
            for s in cell.samples:
                if s.error:
                    w(f"error: {cell.name} prompt {s.prompt_index}: {s.error}")
    return "\n".join(out)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare Bedrock direct, Bedrock via gateway, and local.")
    p.add_argument("-n", "--requests", type=int, default=10)
    p.add_argument("--max-tokens", type=int, default=120)
    p.add_argument("--cells", default="a,b,c",
                   help="comma-separated: a=bedrock-direct b=bedrock-gateway c=local-gateway")
    p.add_argument("--gateway", default=DEFAULT_GATEWAY)
    p.add_argument("--local-model", default=DEFAULT_LOCAL_MODEL,
                   help="comma-separated for several local cells, e.g. "
                        "llama3.2:1b,llama3.1:8b — one cell each")
    p.add_argument("--bedrock-model", default="",
                   help="exact Bedrock model id; required for cells a and b")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", ""))
    p.add_argument("--out", help="directory to write report.txt and samples.json into")
    return p.parse_args()


def check_no_credentials_in_repo() -> list[str]:
    """Refuse to run if AWS credentials look like they are in the repo.

    Credentials belong in the shell that launches compose (AWS_PROFILE=...), never in
    .env or a tracked file. Checks the places they get pasted into.

    Two things are deliberately not flagged, because flagging them would train you to
    ignore this: a variable passed through from the environment (AWS_REGION:
    "${AWS_REGION}") is correct usage, not a leak, and so is a name with no value. Key
    material (AKIA.../ASIA..., aws_secret_access_key) is always flagged. This file is
    skipped, since it necessarily contains the words it searches for.
    """
    import re
    import subprocess

    findings: list[str] = []
    # Key material: always a leak.
    secret_re = re.compile(r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
                           r"|aws_(?:access_key_id|secret_access_key|session_token)\s*=\s*\S)",
                           re.IGNORECASE)
    # An AWS_* setting with a literal value baked in.
    assign_re = re.compile(r"\b(AWS_[A-Z0-9_]+)\s*[=:]\s*(.*)$")
    root = Path(__file__).resolve().parent.parent
    myself = Path(__file__).resolve()

    candidates = [root / ".env", root / "docker-compose.yml"]
    try:
        tracked = subprocess.run(["git", "-C", str(root), "ls-files"],
                                 capture_output=True, text=True, timeout=30)
        candidates += [root / line for line in tracked.stdout.split("\n") if line.strip()]
    except Exception:
        findings.append("could not list tracked files; checked .env and compose only")

    for path in candidates:
        try:
            if not path.is_file() or path.resolve() == myself:
                continue
            text = path.read_text(errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.split("\n"), 1):
            if line.strip().startswith("#"):
                continue  # a commented example is documentation
            hit = secret_re.search(line)
            if hit:
                findings.append(f"{path.relative_to(root)}:{lineno}: {hit.group(1)[:24]}")
                continue
            match = assign_re.search(line)
            if not match:
                continue
            value = match.group(2).strip().strip('\'"').strip()
            # Empty, or an environment passthrough / placeholder: not a literal secret.
            if not value or value.startswith("${") or value.startswith("<"):
                continue
            findings.append(f"{path.relative_to(root)}:{lineno}: {match.group(1)} has a literal value")
    return findings


async def main() -> int:
    args = parse_args()

    leaks = check_no_credentials_in_repo()
    if leaks:
        print("ABORT: AWS credentials or AWS_* settings found in repository files.",
              file=sys.stderr)
        print("Credentials belong in the launching shell (AWS_PROFILE=...), not in the repo.",
              file=sys.stderr)
        for leak in leaks:
            print(f"  {leak}", file=sys.stderr)
        return 2

    wanted = {c.strip() for c in args.cells.split(",") if c.strip()}

    cells: list[Cell] = []
    if "a" in wanted:
        cells.append(Cell("bedrock-direct", args.bedrock_model, "bedrock"))
    if "b" in wanted:
        cells.append(Cell("bedrock-gateway", args.bedrock_model, "bedrock"))
    if "c" in wanted:
        local_models = [m.strip() for m in args.local_model.split(",") if m.strip()]
        for model in local_models:
            # One cell per local model. The balanced-vs-powerful pair (llama3.1:8b
            # local vs the same weights on Bedrock) is the controlled comparison;
            # llama3.2:1b stays as the series baseline.
            name = "local-gateway" if len(local_models) == 1 else f"local:{model}"
            cells.append(Cell(name, model, "ollama"))

    for cell in cells:
        if cell.backend == "bedrock" and not args.bedrock_model:
            cell.unavailable = "--bedrock-model not given; no model id is assumed"
            continue
        print(f"running {cell.name} ({cell.model}) ...", flush=True)
        if cell.backend == "ollama":
            await run_gateway(cell, args.gateway, args.max_tokens, args.requests)
        elif cell.name == "bedrock-direct":
            await run_bedrock_direct(cell, args.region, args.max_tokens, args.requests)
        elif cell.name == "bedrock-gateway":
            # Names the backend explicitly so the tier heuristic cannot send this
            # somewhere else; see the routing contract in the README.
            cell.model = f"bedrock:{args.bedrock_model}"
            await run_gateway(cell, args.gateway, args.max_tokens, args.requests)
            cell.model = args.bedrock_model

    report = fmt_report(args, cells)
    print()
    print(report)

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.txt").write_text(report + "\n")
        (out_dir / "samples.json").write_text(json.dumps({
            "conditions": {
                "requests": args.requests,
                "concurrency": 1,
                "max_tokens": args.max_tokens,
                "region": args.region,
                "local_models": [m.strip() for m in args.local_model.split(",") if m.strip()],
                "bedrock_model": args.bedrock_model,
                "prompts": PROMPTS,
                "wall_clock": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            "cells": [
                {"name": c.name, "model": c.model, "backend": c.backend,
                 "unavailable": c.unavailable,
                 "samples": [asdict(s) for s in c.samples]}
                for c in cells
            ],
        }, indent=2))
        print(f"wrote {out_dir}/report.txt and samples.json")

    return 0 if any(c.ok() for c in cells) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
