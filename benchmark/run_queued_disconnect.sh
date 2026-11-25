#!/usr/bin/env bash
#
# Stage 1b: a client disconnects while another request is queued behind it.
#
#   ./benchmark/run_queued_disconnect.sh
#   QUICK=1 ./benchmark/run_queued_disconnect.sh      # 3 pairs per arm
#
# One gateway slot and OLLAMA_NUM_PARALLEL=1, so request B must queue behind A and is
# admitted at the instant A's slot is released. Stage 1 releases that slot after
# agen.aclose() returns; this measures whether that is the same instant Ollama stops.
#
# Nothing is destroyed. Writes:
#
#   benchmark/out/run-queued-disconnect-<timestamp>/
#       transcript.txt        everything printed below
#       report.txt            the ordering table and the comparison
#       pairs.json            raw per-pair records
#       gateway.log           the gateway's JSON log for the run window
#       ollama.log            the server that was killed into (full run log)
#       prometheus-snapshot/  this run's TSDB

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
STACK_DIR="$PWD"
RUN_ID="run-queued-disconnect${LABEL:+-$LABEL}-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$STACK_DIR/benchmark/out/$RUN_ID"
mkdir -p "$RUN_DIR"
OUT="$RUN_DIR/transcript.txt"


MODEL="${MODEL:-llama3.2:1b}"
PARALLEL=1                 # the whole point: one Ollama slot
SLOTS=1                    # and one gateway slot
GATEWAY="http://localhost:8000"
PROM="http://localhost:9090"
OLLAMA_LOG="/tmp/ollama_p${PARALLEL}.log"
if [[ -n "${QUICK:-}" ]]; then PAIRS=3; else PAIRS="${PAIRS:-20}"; fi
CONTROL_PAIRS="${CONTROL_PAIRS:-$PAIRS}"
ARMS="${ARMS:-disconnect,control}"
LOG_POLL_MS="${LOG_POLL_MS:-10}"
# Diagnostics for the Stage 1b late-finalization fault. Both off unless asked for.
GC_INTERVAL="${GC_INTERVAL:-0}"
GC_SAVEALL="${GC_SAVEALL:-false}"
DIAG_STUCK="${DIAG_STUCK:-0}"
LABEL="${LABEL:-}"

# shellcheck source=benchmark/lib/validity.sh
source "$STACK_DIR/benchmark/lib/validity.sh"
validity_init "$RUN_DIR"
expect "Ollama on Metal, not a CPU fallback"      "$OLLAMA_LOG"            "library=Metal" 1
expect_absent "Ollama NOT on a CPU-only build"    "$OLLAMA_LOG"            "library=cpu"
expect "OLLAMA_NUM_PARALLEL reached the server"   "$OLLAMA_LOG"            "OLLAMA_NUM_PARALLEL:$PARALLEL"
expect "gateway came up with one queue slot"      "$RUN_DIR/transcript.txt" '"queue_slots":1'
expect "the clock offset was measurable"          "$RUN_DIR/report.txt"     "offset_ms_best"
expect "every disconnect pair ran"                "$RUN_DIR/report.txt"     "disconnect pairs usable        $PAIRS/$PAIRS"
expect "the gateway log carries req_ids"          "$RUN_DIR/gateway.log"    "req_id" 10

die() { echo "ABORT: $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }

PY=""
for cand in "${PYTHON:-}" "$STACK_DIR/.venv/bin/python" python3 python; do
    [[ -n "$cand" ]] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c "import sys,httpx; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
        PY="$cand"; break
    fi
done
[[ -n "$PY" ]] || die "no Python >= 3.10 with httpx installed. Easiest fix: make test"

exec > >(tee "$OUT") 2>&1

echo "queued disconnect | arms=$ARMS pairs=$PAIRS control=$CONTROL_PAIRS | OLLAMA_NUM_PARALLEL=$PARALLEL slots=$SLOTS"
echo "log poll=${LOG_POLL_MS}ms | gc_interval=${GC_INTERVAL}s | diag_stuck=${DIAG_STUCK}s | gc_saveall=${GC_SAVEALL}"
echo "host: $(uname -m) | $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
echo "ollama: $(ollama --version 2>&1 | tr -d '\n') | python: $($PY -V 2>&1)"
echo "run id: $RUN_ID"
echo "git HEAD: $(git -C "$STACK_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
[[ -n "${QUICK:-}" ]] && echo "QUICK mode: pairs=$PAIRS, results are not publishable"

step "1. Rosetta guard"
ARCH="$(uname -m)"
echo "  shell arch: $ARCH"
if [[ "$ARCH" != "arm64" && "$(sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]]; then
    die "running translated: uname says $ARCH on an arm64 machine. Rosetta's ollama
  build has no Metal, so this would measure CPU-only inference."
fi

step "2. Stop the menu-bar app and any running server"
osascript -e 'quit app "Ollama"' 2>/dev/null || true
sleep 1
pkill -f "Ollama.app" 2>/dev/null || true
pkill ollama 2>/dev/null || true
sleep 2
pgrep -f "Ollama.app" >/dev/null 2>&1 && die "the Ollama menu-bar app is still running"
echo "  clear"

step "3. Launch ollama serve with OLLAMA_NUM_PARALLEL=$PARALLEL"
OLLAMA_NUM_PARALLEL="$PARALLEL" nohup ollama serve > "$OLLAMA_LOG" 2>&1 &
sleep 6
curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null || die "no Ollama on 11434"

step "4. Confirm the setting reached the server, and that it is on Metal"
APPLIED="$(grep -o "OLLAMA_NUM_PARALLEL:[0-9]*" "$OLLAMA_LOG" | tail -1 || true)"
echo "  $APPLIED"
[[ "$APPLIED" == "OLLAMA_NUM_PARALLEL:$PARALLEL" ]] || die "server reports $APPLIED, expected :$PARALLEL"
COMPUTE="$(grep -o 'msg="inference compute".*library=[A-Za-z]*' "$OLLAMA_LOG" | tail -1 | grep -o 'library=[A-Za-z]*' || true)"
echo "  $COMPUTE"
grep -q "library=cpu" <<<"$COMPUTE" && die "Ollama reports CPU-only inference"

step "5. Warm the model"
curl -fsS --max-time 300 http://localhost:11434/api/generate \
     -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false}" > /dev/null \
    || die "warm-up failed (is $MODEL pulled?)"
echo "  ok"

step "6. Refuse to run if a stray server owns the gateway port"
STRAY="$(lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null \
         | tail -n +2 | grep -viE "com\.docke|docker|vpnkit" || true)"
if [[ -n "$STRAY" ]]; then
    echo "$STRAY"
    die "something other than the compose gateway is listening on :8000"
fi
echo "  :8000 is free or owned by docker"

step "7. Bring the stack up with ONE gateway slot"
cat > "$RUN_DIR/compose-override.yml" <<YML
services:
  gateway:
    environment:
      GATEWAY_MAX_CONCURRENT_REQUESTS: "$SLOTS"
      GATEWAY_GC_INTERVAL_SECONDS: "$GC_INTERVAL"
      GATEWAY_GC_SAVEALL: "$GC_SAVEALL"
      GATEWAY_DIAG_STUCK_SECONDS: "$DIAG_STUCK"
YML
COMPOSE=(docker compose -f docker-compose.yml -f "$RUN_DIR/compose-override.yml")
"${COMPOSE[@]}" up -d --build 2>&1 | tail -2
"${COMPOSE[@]}" restart gateway >/dev/null
for i in $(seq 1 30); do
    curl -fsS --max-time 3 "$GATEWAY/health" >/dev/null 2>&1 && break
    sleep 2
    [[ $i -lt 30 ]] || die "gateway did not become reachable"
done
HEALTH="$(curl -fsS --max-time 5 "$GATEWAY/health")"
echo "  $HEALTH"
grep -q '"queue_slots":1' <<<"$HEALTH" || die "gateway is not running with one slot"
grep -q '"now_ms"' <<<"$HEALTH" || die "gateway /health has no now_ms; clock offset cannot be measured"

step "8. Warm-up through the gateway"
curl -fsS --max-time 300 "$GATEWAY/v1/chat/completions" \
     -H 'Content-Type: application/json' \
     -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}" \
     > /dev/null || die "warm-up through the gateway failed"
echo "  ok"

RUN_START="$(date -u +%Y-%m-%dT%H:%M:%S)"

step "9. Run $PAIRS pairs per arm"
"$PY" benchmark/queued_disconnect.py \
    --pairs "$PAIRS" --control-pairs "$CONTROL_PAIRS" --arms "$ARMS" \
    --log-poll-ms "$LOG_POLL_MS" \
    --model "$MODEL" --gateway "$GATEWAY" \
    --ollama-log "$OLLAMA_LOG" \
    --gateway-log "$RUN_DIR/gateway-live.log" \
    --out "$RUN_DIR" &
LOADER=$!
# The loader reads the gateway log at the end, so stream it there while it runs.
"${COMPOSE[@]}" logs -f --no-log-prefix --since "${RUN_START}Z" gateway > "$RUN_DIR/gateway-live.log" 2>&1 &
LOGGER=$!
wait "$LOADER" || echo "  (loader exited non-zero)"
sleep 2
kill "$LOGGER" 2>/dev/null || true

step "10. Preserve the logs"
cp "$RUN_DIR/gateway-live.log" "$RUN_DIR/gateway.log"
cp "$OLLAMA_LOG" "$RUN_DIR/ollama.log"
echo "  gateway.log  $(wc -l < "$RUN_DIR/gateway.log") lines"
echo "  ollama.log   $(wc -l < "$RUN_DIR/ollama.log") lines"
echo "  cancel lines: $(grep -c 'cancel task' "$RUN_DIR/ollama.log" || true)"
echo "  slot_acquired lines: $(grep -c slot_acquired "$RUN_DIR/gateway.log" || true)"
echo "  stuck_stream lines:  $(grep -c stuck_stream "$RUN_DIR/gateway.log" || true)"
echo "  gc_collected lines:  $(grep -c gc_collected "$RUN_DIR/gateway.log" || true)"
if grep -q stuck_stream "$RUN_DIR/gateway.log"; then
    echo
    echo "  --- what held the suspended generator ---"
    grep stuck_stream "$RUN_DIR/gateway.log" | head -4
fi

if [[ "$GC_SAVEALL" == "true" ]]; then
    step "10b. What the collector found in cycles (GATEWAY_GC_SAVEALL)"
    if curl -fsS --max-time 30 "$GATEWAY/debug/gc-garbage" > "$RUN_DIR/gc-garbage.json" 2>/dev/null; then
        "$PY" benchmark/show_gc_garbage.py "$RUN_DIR/gc-garbage.json"
    else
        echo "  (could not read /debug/gc-garbage)"
    fi
fi

step "11. Export this run's Prometheus data"
if SNAP=$(curl -fsS --max-time 60 -XPOST "$PROM/api/v1/admin/tsdb/snapshot" \
          | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['name'])" 2>/dev/null); then
    rm -rf "$RUN_DIR/prometheus-snapshot"
    "${COMPOSE[@]}" cp "prometheus:/prometheus/snapshots/$SNAP" "$RUN_DIR/prometheus-snapshot" >/dev/null
    "${COMPOSE[@]}" exec -T prometheus rm -rf "/prometheus/snapshots/$SNAP" >/dev/null 2>&1 || true
    echo "  exported"
else
    echo "  WARNING: snapshot failed"
fi

step "12. Restore the default stack"
docker compose up -d --build gateway 2>&1 | tail -1
pkill -f "ollama serve" 2>/dev/null || true
sleep 1
OLLAMA_NUM_PARALLEL=4 nohup ollama serve > /tmp/ollama_p4.log 2>&1 &
sleep 4
echo "  gateway back to shipped config, Ollama back to NUM_PARALLEL=4"

echo
echo "=== done ==="
echo "run dir: $RUN_DIR"

validate_run || echo "  (numbers do not count)"
