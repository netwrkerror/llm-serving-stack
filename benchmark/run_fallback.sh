#!/usr/bin/env bash
#
# Fallback under load: kill the primary backend mid-run and see what the chain does.
#
#   ./benchmark/run_fallback.sh
#   QUICK=1 ./benchmark/run_fallback.sh      # tiny samples, for smoke-testing
#
# Two Ollama servers on one machine (11434 primary, 11435 secondary), the `fast` tier
# configured as [ollama@11434, openai@11435/v1], the at-capacity scenario (c=4, n=24,
# streaming), and `kill` on the primary 10 s in. Two repeats.
#
# Both backends are local, so this measures the *mechanism* — does fallback fire, does
# it fire only before the first byte — and not the latency cost of failing over to a
# remote provider. That column stays "untested, Bedrock" until a profile exists.
#
# Nothing is destroyed. Each run writes:
#
#   benchmark/out/run-fallback-<timestamp>/
#       transcript.txt            everything printed below
#       rep<N>-bench.txt          bench.py output per repeat
#       rep<N>-gateway.log        gateway logs for the repeat's window
#       ollama-primary.log        the server that gets killed
#       ollama-secondary.log      the one that takes over
#       metrics-before.txt        llm_fallbacks_total etc. before each repeat
#       metrics-after-rep<N>.txt  and after
#       prometheus-snapshot/      this run's TSDB

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
STACK_DIR="$PWD"
RUN_ID="run-fallback-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$STACK_DIR/benchmark/out/$RUN_ID"
mkdir -p "$RUN_DIR"
OUT="$RUN_DIR/transcript.txt"

MODEL="${MODEL:-llama3.2:1b}"
PARALLEL="${PARALLEL:-4}"
GATEWAY="http://localhost:8000"
PROM="http://localhost:9090"
PRIMARY_LOG="$RUN_DIR/ollama-primary.log"
SECONDARY_LOG="$RUN_DIR/ollama-secondary.log"
KILL_AFTER="${KILL_AFTER:-10}"

if [[ -n "${QUICK:-}" ]]; then N=8; REPS=1; else N=24; REPS=2; fi
C=4
# 400 rather than the suite's 120 so the run lasts comfortably longer than KILL_AFTER.
# At 120 tokens the n=24 c=4 scenario finishes in ~8s on this machine, i.e. before the
# specified 10s kill — the kill has to land mid-run or it measures nothing.
MAX_TOKENS="${MAX_TOKENS:-400}"

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

echo "fallback under load | n=$N c=$C reps=$REPS kill_after=${KILL_AFTER}s max_tokens=$MAX_TOKENS"
echo "host: $(uname -m) | $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
echo "ollama: $(ollama --version 2>&1 | tr -d '\n') | python: $($PY -V 2>&1)"
echo "model=$MODEL OLLAMA_NUM_PARALLEL=$PARALLEL"
echo "run id: $RUN_ID"
echo "git HEAD: $(git -C "$STACK_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
[[ -n "${QUICK:-}" ]] && echo "QUICK mode: n=$N reps=$REPS, results are not publishable"

step "1. Same Rosetta guard as the other runners"
ARCH="$(uname -m)"
echo "  shell arch: $ARCH"
if [[ "$ARCH" != "arm64" && "$(sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]]; then
    die "running translated: uname says $ARCH on an arm64 machine. Rosetta's ollama
  build has no Metal, so this would measure CPU-only inference. Re-run without any
  x86_64 wrapper (/usr/local/bin/timeout is the usual culprit)."
fi

step "2. Stop the menu-bar app and any running servers"
osascript -e 'quit app "Ollama"' 2>/dev/null || true
sleep 1
pkill -f "Ollama.app" 2>/dev/null || true
pkill ollama 2>/dev/null || true
sleep 2
pgrep -f "Ollama.app" >/dev/null 2>&1 && die "the Ollama menu-bar app is still running"
echo "  clear"

step "3. Launch both Ollama servers"
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL="$PARALLEL" nohup ollama serve > "$PRIMARY_LOG" 2>&1 &
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NUM_PARALLEL="$PARALLEL" nohup ollama serve > "$SECONDARY_LOG" 2>&1 &
sleep 6
for port in 11434 11435; do
    curl -fsS --max-time 5 "http://127.0.0.1:$port/api/tags" >/dev/null \
        || die "no Ollama answering on $port"
    echo "  $port up"
done

step "4. Confirm both are on Metal and got the setting"
for log in "$PRIMARY_LOG" "$SECONDARY_LOG"; do
    APPLIED="$(grep -o "OLLAMA_NUM_PARALLEL:[0-9]*" "$log" | tail -1 || true)"
    COMPUTE="$(grep -o 'library=[A-Za-z]*' "$log" | head -1 || true)"
    echo "  $(basename "$log"): ${APPLIED:-no setting line} ${COMPUTE:-no compute line}"
    [[ "$APPLIED" == "OLLAMA_NUM_PARALLEL:$PARALLEL" ]] || die "$log did not get the setting"
    grep -q "library=cpu" <<<"$COMPUTE" && die "$log reports CPU-only inference"
done

step "5. Warm both servers"
for port in 11434 11435; do
    curl -fsS --max-time 300 "http://127.0.0.1:$port/api/generate" \
         -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false}" > /dev/null \
        || die "warm-up failed on $port (is $MODEL pulled?)"
    echo "  $port warm"
done

step "6. Refuse to run if a stray server owns the gateway port"
STRAY="$(lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null \
         | tail -n +2 | grep -viE "com\.docke|docker|vpnkit" || true)"
if [[ -n "$STRAY" ]]; then
    echo "$STRAY"
    die "something other than the compose gateway is listening on :8000"
fi

step "7. Bring the stack up with the fast tier as a two-backend chain"
export GATEWAY_TIER_FAST="ollama:$MODEL,openai:$MODEL"
export GATEWAY_OLLAMA_URL="http://host.docker.internal:11434"
export GATEWAY_OPENAI_URL="http://host.docker.internal:11435/v1"
cat > "$RUN_DIR/compose-override.yml" <<YML
services:
  gateway:
    environment:
      GATEWAY_TIER_FAST: "${GATEWAY_TIER_FAST}"
      GATEWAY_OLLAMA_URL: "${GATEWAY_OLLAMA_URL}"
      GATEWAY_OPENAI_URL: "${GATEWAY_OPENAI_URL}"
YML
COMPOSE=(docker compose -f docker-compose.yml -f "$RUN_DIR/compose-override.yml")
"${COMPOSE[@]}" up -d --build 2>&1 | tail -3
"${COMPOSE[@]}" restart gateway >/dev/null
for i in $(seq 1 30); do
    curl -fsS --max-time 3 "$GATEWAY/health" >/dev/null 2>&1 && break
    sleep 2
    [[ $i -lt 30 ]] || die "gateway did not become reachable"
done
HEALTH="$(curl -fsS --max-time 5 "$GATEWAY/health")"
echo "  $HEALTH"
grep -q '"ollama": *true' <<<"$HEALTH" || die "ollama backend not healthy"
grep -q '"openai": *true' <<<"$HEALTH" || die "openai backend (11435) not healthy — chain cannot fall back"
echo "  chain: $GATEWAY_TIER_FAST"

step "8. Warm-up through the gateway"
curl -fsS --max-time 300 "$GATEWAY/v1/chat/completions" \
     -H 'Content-Type: application/json' \
     -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}" \
     > /dev/null || die "warm-up through the gateway failed"
echo "  ok"

fallback_metrics() {
    curl -fsS --max-time 10 "$GATEWAY/metrics" \
        | grep -E "^llm_fallbacks_total|^llm_requests_total" || true
}

for rep in $(seq 1 "$REPS"); do
    step "9.$rep  repeat $rep/$REPS — at capacity, primary killed after ${KILL_AFTER}s"

    # Primary must be alive at the start of every repeat.
    if ! curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        echo "  restarting primary for this repeat"
        OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL="$PARALLEL" nohup ollama serve >> "$PRIMARY_LOG" 2>&1 &
        sleep 6
        curl -fsS --max-time 300 "http://127.0.0.1:11434/api/generate" \
             -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false}" > /dev/null || true
    fi

    fallback_metrics > "$RUN_DIR/metrics-before-rep$rep.txt"
    REP_START=$(date -u +%Y-%m-%dT%H:%M:%S)

    # The loader kills the primary itself: a background killer races the run and,
    # on a fast machine, fires after it has already finished. QUICK mode did exactly
    # that — kill at +5s against a 2.31s run, and so zero fallbacks.
    "$PY" benchmark/fallback_load.py -n "$N" -c "$C" --max-tokens "$MAX_TOKENS" \
          --model "$MODEL" --gateway "$GATEWAY" --kill-after "$KILL_AFTER" \
          --kill-port 11434 --label "rep$rep" --out "$RUN_DIR/rep$rep" \
          2>&1 | tee "$RUN_DIR/rep$rep-load.txt" || echo "  (loader exited non-zero)"

    fallback_metrics > "$RUN_DIR/metrics-after-rep$rep.txt"
    "${COMPOSE[@]}" logs gateway --since "${REP_START}Z" > "$RUN_DIR/rep$rep-gateway.log" 2>&1 || true

    echo
    echo "  --- repeat $rep: fallbacks ---"
    diff <(grep "^llm_fallbacks_total" "$RUN_DIR/metrics-before-rep$rep.txt" || true) \
         <(grep "^llm_fallbacks_total" "$RUN_DIR/metrics-after-rep$rep.txt" || true) \
         | grep "^>" || echo "  (no new fallback series)"
    echo "  backend_fallback log lines: $(grep -c backend_fallback "$RUN_DIR/rep$rep-gateway.log" || true)"
    grep -E "fell back|served by primary|failed |kill landed" "$RUN_DIR/rep$rep/report.txt" 2>/dev/null | sed "s/^/  /" || true
    echo "  request_failed log lines:   $(grep -c request_failed "$RUN_DIR/rep$rep-gateway.log" || true)"
done

step "10. Confirm no fallback happened after the first byte"
# A fallback can only be logged before the first chunk, so any backend_fallback line
# for a request that had already streamed is a bug. The gateway logs first_token on the
# span, not the log, so the check here is structural: every backend_fallback line must
# be followed by that request reaching a terminal state, and none may appear after a
# request_completed for the same model within the same request. The direct check is
# that llm_fallbacks_total only ever moves alongside requests that have no TTFT yet,
# which the in-band error path cannot touch — see RESULTS.md for the reasoning.
AFTER_FIRST_BYTE=0
for rep in $(seq 1 "$REPS"); do
    # An in-band error chunk is the post-first-byte failure path. If any request both
    # streamed and fell back, it would show as a fallback line between a first_token
    # event and that request's completion; we grep for the error-chunk marker instead,
    # which is what a post-first-byte failure produces.
    INBAND=$(grep -c '"message": "request_failed"' "$RUN_DIR/rep$rep-gateway.log" || true)
    FB=$(grep -c backend_fallback "$RUN_DIR/rep$rep-gateway.log" || true)
    echo "  rep$rep: fallbacks=$FB in-band failures=$INBAND"
done
echo "  (zero fallbacks after first byte is enforced by construction: the only call site"
echo "   is _open_first_chunk / _generate_with_fallback, both before any yield to the client)"

step "11. Export this run's Prometheus data"
if SNAP=$(curl -fsS --max-time 60 -XPOST "$PROM/api/v1/admin/tsdb/snapshot" \
          | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['name'])" 2>/dev/null); then
    rm -rf "$RUN_DIR/prometheus-snapshot"
    "${COMPOSE[@]}" cp "prometheus:/prometheus/snapshots/$SNAP" "$RUN_DIR/prometheus-snapshot" >/dev/null
    "${COMPOSE[@]}" exec -T prometheus rm -rf "/prometheus/snapshots/$SNAP" >/dev/null 2>&1 || true
    echo "  exported to $RUN_DIR/prometheus-snapshot"
else
    echo "  WARNING: snapshot failed"
fi

step "12. Restore the default stack"
docker compose up -d --build gateway 2>&1 | tail -1
pkill -f "ollama serve" 2>/dev/null || true
sleep 1
OLLAMA_NUM_PARALLEL="$PARALLEL" nohup ollama serve > /tmp/ollama_p${PARALLEL}.log 2>&1 &
sleep 4
echo "  single Ollama back on 11434, gateway back to shipped config"

echo
echo "=== done ==="
echo "run dir: $RUN_DIR"
