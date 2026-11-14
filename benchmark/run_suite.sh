#!/usr/bin/env bash
#
# One full benchmark run at a given OLLAMA_NUM_PARALLEL.
#
#   ./benchmark/run_suite.sh 1
#   ./benchmark/run_suite.sh 4
#
# Starts from a clean Prometheus volume, launches Ollama itself, and refuses to
# continue unless the setting actually reached the server — the macOS menu-bar app
# respawns its own server without shell env vars, which silently invalidates the run.
#
# Output goes to benchmark/out/run-<n>-<timestamp>.txt as well as the terminal.

set -euo pipefail

PARALLEL="${1:-}"
if [[ ! "$PARALLEL" =~ ^[0-9]+$ ]]; then
    echo "usage: $0 <OLLAMA_NUM_PARALLEL>   e.g. $0 4" >&2
    exit 2
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."
STACK_DIR="$PWD"
OLLAMA_LOG="/tmp/ollama_p${PARALLEL}.log"
OUT_DIR="$STACK_DIR/benchmark/out"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/run-${PARALLEL}-$(date +%Y%m%d-%H%M%S).txt"

MODEL="${MODEL:-llama3.2:1b}"
GATEWAY="http://localhost:8000"

die() { echo "ABORT: $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }

# Needs httpx AND >= 3.10 (bench.py uses `X | None`). python3 and pip often diverge,
# and falling through to an old anaconda python that happens to have httpx is worse
# than failing, so check both. The repo venv from `make test` is the best candidate.
PY=""
for cand in "${PYTHON:-}" "$STACK_DIR/.venv/bin/python" python3 python; do
    [[ -n "$cand" ]] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c "import sys,httpx; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
        PY="$cand"; break
    fi
done
if [[ -z "$PY" ]]; then
    die "no Python >= 3.10 with httpx installed.
  Easiest fix:  make test     (creates .venv with everything needed)
  Or:           python3 -m pip install httpx
  Or:           PYTHON=/path/to/python $0 $PARALLEL"
fi

exec > >(tee "$OUT") 2>&1

echo "benchmark suite | OLLAMA_NUM_PARALLEL=$PARALLEL | model=$MODEL"
echo "host: $(uname -m) | ollama: $(ollama --version 2>&1 | tr -d '\n') | python: $($PY -V 2>&1)"
echo "output: $OUT"

step "1. Clean slate (drops the Prometheus volume)"
docker compose down -v

step "2. Stop the menu-bar app and any running server"
# Ask politely first; the app does not always honour the quit event, so fall back
# to terminating it. It must be gone, or it respawns its own server on 11434 and
# that server never sees OLLAMA_NUM_PARALLEL.
osascript -e 'quit app "Ollama"' 2>/dev/null || true
for _ in $(seq 1 5); do
    pgrep -f "Ollama.app" >/dev/null 2>&1 || break
    sleep 1
done
if pgrep -f "Ollama.app" >/dev/null 2>&1; then
    echo "  quit event ignored, terminating the app"
    pkill -f "Ollama.app" 2>/dev/null || true
    sleep 3
fi
pkill ollama 2>/dev/null || true
sleep 2
if pgrep -f "Ollama.app" >/dev/null 2>&1; then
    die "the Ollama menu-bar app is still running; it will respawn its own server and ignore OLLAMA_NUM_PARALLEL. Quit it from the menu bar and re-run."
fi
echo "  no menu-bar app running"

step "3. Launch ollama serve with OLLAMA_NUM_PARALLEL=$PARALLEL"
OLLAMA_NUM_PARALLEL="$PARALLEL" nohup ollama serve > "$OLLAMA_LOG" 2>&1 &
sleep 5

step "4. Confirm the server is the one we started"
PROCS="$(pgrep -fl "ollama serve" || true)"
[[ -n "$PROCS" ]] || die "no 'ollama serve' process found"
echo "$PROCS"
if grep -q "Ollama.app" <<<"$PROCS"; then
    die "the running server belongs to the menu-bar app, not this script"
fi

step "5. Confirm the setting reached the server"
APPLIED="$(grep -o "OLLAMA_NUM_PARALLEL:[0-9]*" "$OLLAMA_LOG" | tail -1 || true)"
[[ -n "$APPLIED" ]] || die "no OLLAMA_NUM_PARALLEL line in $OLLAMA_LOG — server may not have started; check that log"
echo "  $APPLIED  (from $OLLAMA_LOG)"
[[ "$APPLIED" == "OLLAMA_NUM_PARALLEL:$PARALLEL" ]] || die "server reports $APPLIED, expected OLLAMA_NUM_PARALLEL:$PARALLEL"

step "6. Warm the model directly (so load_duration does not dominate)"
curl -fsS --max-time 300 http://localhost:11434/api/generate \
     -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false}" > /dev/null \
    || die "warm-up against Ollama failed — is the model pulled? (ollama pull $MODEL)"
echo "  ok"

step "7. Bring the stack up"
docker compose up -d --build
echo "  waiting for the gateway..."
for i in $(seq 1 30); do
    if curl -fsS --max-time 3 "$GATEWAY/health" >/dev/null 2>&1; then break; fi
    sleep 2
    [[ $i -lt 30 ]] || die "gateway did not become reachable"
done

step "8. Health check"
HEALTH="$(curl -fsS --max-time 5 "$GATEWAY/health")"
echo "  $HEALTH"
grep -q '"status":"ok"' <<<"$HEALTH" || die "gateway reports a degraded backend: $HEALTH"
SLOTS="$($PY -c "import json,sys;print(json.loads(sys.argv[1])['queue_slots'])" "$HEALTH")"
echo "  queue slots: $SLOTS"

step "9. Warm-up through the gateway (first request pays import/connection costs)"
curl -fsS --max-time 300 "$GATEWAY/v1/chat/completions" \
     -H 'Content-Type: application/json' \
     -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}" \
     > /dev/null || die "warm-up through the gateway failed"
echo "  ok"

step "10. Scenarios (each repeated, so you can see run-to-run spread)"
run_scenario() {
    local label="$1" n="$2" c="$3"
    for rep in 1 2; do
        echo
        echo "----- $label | n=$n c=$c | repetition $rep/2 -----"
        "$PY" benchmark/bench.py -n "$n" -c "$c" --stream \
              --model "$MODEL" --ollama-num-parallel "$PARALLEL"
    done
}
run_scenario "sequential"     8  1
run_scenario "at capacity"   24  4
run_scenario "oversubscribed" 16  8

echo
echo "=== done: OLLAMA_NUM_PARALLEL=$PARALLEL ==="
echo "transcript: $OUT"
echo
echo "Screenshots to take now, before starting the other run:"
echo "  Grafana  http://localhost:3000  -> In-flight = waiting + running"
echo "                                     Request Latency p50 / p95 - all statuses"
echo "                                     Time to First Token - TTFT"
echo "  Jaeger   http://localhost:16686 -> service llm-serving-stack, a trace from the"
echo "                                     oversubscribed run: llm.queue.wait + llm.inference,"
echo "                                     and ollama.unattributed_ms on the inference span"
echo "  Check every screenshot for your name, email, file paths and other browser tabs."
