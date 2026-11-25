#!/usr/bin/env bash
#
# The client-disconnect experiment, at c=1 and c=4, against OLLAMA_NUM_PARALLEL=4.
#
#   ./benchmark/run_disconnect.sh before     # before the cancellation fix
#   ./benchmark/run_disconnect.sh after      # after it
#   QUICK=1 ./benchmark/run_disconnect.sh before     # n=3, for smoke-testing
#
# Same scaffolding as run_suite.sh: it launches Ollama itself and refuses to
# continue unless OLLAMA_NUM_PARALLEL actually reached the server, because the
# macOS menu-bar app respawns its own server without shell env vars.
#
# Nothing is destroyed. Each run writes a directory under benchmark/out/:
#
#   benchmark/out/run-disconnect-<label>-<timestamp>/
#       transcript.txt          everything printed below
#       c1/report.txt           per-request table and totals, c=1
#       c1/records.json         raw per-request and per-task records
#       c4/...                  the same at c=4
#       ollama-log-slice.txt    only the lines Ollama wrote during this run
#       prometheus-snapshot/    this run's TSDB, exported at the end
#
# The label goes in the directory name and in each report, so the before/after
# pair stays identifiable without reading the contents.

set -euo pipefail

LABEL="${1:-}"
if [[ -z "$LABEL" ]]; then
    echo "usage: $0 <label>    e.g. $0 before" >&2
    exit 2
fi
if [[ ! "$LABEL" =~ ^[a-z0-9-]+$ ]]; then
    echo "label must be lowercase alphanumeric/dashes (it becomes a directory name)" >&2
    exit 2
fi

PARALLEL="${PARALLEL:-4}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
STACK_DIR="$PWD"
OLLAMA_LOG="/tmp/ollama_p${PARALLEL}.log"
RUN_ID="run-disconnect-${LABEL}-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$STACK_DIR/benchmark/out/$RUN_ID"
mkdir -p "$RUN_DIR"
OUT="$RUN_DIR/transcript.txt"
# shellcheck source=benchmark/lib/validity.sh
source "$STACK_DIR/benchmark/lib/validity.sh"
validity_init "$RUN_DIR"
expect "Ollama on Metal, not a CPU fallback"      "$OLLAMA_LOG" "library=Metal" 1
expect_absent "Ollama NOT on a CPU-only build"    "$OLLAMA_LOG" "library=cpu"
expect "OLLAMA_NUM_PARALLEL reached the server"   "$OLLAMA_LOG" "OLLAMA_NUM_PARALLEL:$PARALLEL"
expect "the shell was not running translated"     "$RUN_DIR/transcript.txt" "shell arch: arm64"
expect "both cells produced a report"             "$RUN_DIR/c1/report.txt" "requests disconnected"


MODEL="${MODEL:-llama3.2:1b}"
GATEWAY="http://localhost:8000"
PROM="http://localhost:9090"

# n=10 per the experiment; --after 1.0s with max_tokens=400 leaves most of the
# generation still to come when the client leaves.
if [[ -n "${QUICK:-}" ]]; then N=3; else N=10; fi
AFTER="${AFTER:-1.0}"
MAX_TOKENS="${MAX_TOKENS:-400}"
SETTLE="${SETTLE:-25}"

die() { echo "ABORT: $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }

# Same Python discovery as run_suite.sh: needs >= 3.10 with httpx, and falling
# through to an old interpreter that happens to have httpx is worse than failing.
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

echo "client-disconnect experiment | label=$LABEL | OLLAMA_NUM_PARALLEL=$PARALLEL"
echo "host: $(uname -m) | ollama: $(ollama --version 2>&1 | tr -d '\n') | python: $($PY -V 2>&1)"
echo "model=$MODEL n=$N after=${AFTER}s max_tokens=$MAX_TOKENS settle=${SETTLE}s"
echo "run id: $RUN_ID"
echo "output: $RUN_DIR"
echo "git HEAD: $(git -C "$STACK_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
[[ -n "${QUICK:-}" ]] && echo "QUICK mode: n=$N, results are not publishable"

step "1. Preserve whatever is already in Prometheus, then clear it"
if curl -fsS --max-time 5 "$PROM/-/healthy" >/dev/null 2>&1; then
    HAVE=$(curl -fsS --max-time 10 "$PROM/api/v1/query?query=count(llm_requests_total)" \
           | "$PY" -c "import sys,json;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)" 2>/dev/null || echo 0)
    if [[ "$HAVE" != "0" ]]; then
        UNARCHIVED="$(cat "$STACK_DIR/benchmark/out/.unarchived" 2>/dev/null || true)"
        EXPORTED="$(cat "$STACK_DIR/benchmark/out/.last-exported" 2>/dev/null || true)"
        if [[ -z "$UNARCHIVED" && -n "$EXPORTED" ]]; then
            echo "  existing series already archived by $EXPORTED, clearing only"
        else
            PREV_ID="${UNARCHIVED:-orphaned-$(date +%Y%m%d-%H%M%S)}"
            PREV_DIR="$STACK_DIR/benchmark/out/$PREV_ID"
            echo "  found unarchived series from: $PREV_ID"
            mkdir -p "$PREV_DIR"
            if SNAP=$(curl -fsS --max-time 60 -XPOST "$PROM/api/v1/admin/tsdb/snapshot" \
                      | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['name'])" 2>/dev/null); then
                rm -rf "$PREV_DIR/prometheus-snapshot"
                docker compose cp "prometheus:/prometheus/snapshots/$SNAP" "$PREV_DIR/prometheus-snapshot" >/dev/null
                docker compose exec -T prometheus rm -rf "/prometheus/snapshots/$SNAP" >/dev/null 2>&1 || true
                echo "  exported to $PREV_DIR/prometheus-snapshot"
            else
                echo "  WARNING: snapshot failed; not clearing series so nothing is lost"
                SKIP_CLEAR=1
            fi
        fi
    else
        echo "  no existing series"
    fi
    if [[ -z "${SKIP_CLEAR:-}" ]]; then
        curl -fsS --max-time 30 -XPOST -g \
             "$PROM/api/v1/admin/tsdb/delete_series?match[]={__name__=~\"llm_.*\"}" >/dev/null
        curl -fsS --max-time 60 -XPOST "$PROM/api/v1/admin/tsdb/clean_tombstones" >/dev/null
        echo "  cleared llm_* series for a fresh dashboard"
    fi
else
    echo "  prometheus not up yet, nothing to preserve"
fi
echo "$RUN_ID" > "$STACK_DIR/benchmark/out/.unarchived"

step "2. Stop the menu-bar app and any running server"
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
pgrep -f "Ollama.app" >/dev/null 2>&1 && \
    die "the Ollama menu-bar app is still running; it will respawn its own server and ignore OLLAMA_NUM_PARALLEL"
echo "  no menu-bar app running"

step "3. Launch ollama serve with OLLAMA_NUM_PARALLEL=$PARALLEL"
OLLAMA_NUM_PARALLEL="$PARALLEL" nohup ollama serve > "$OLLAMA_LOG" 2>&1 &
sleep 5

step "4. Confirm the server is the one we started"
PROCS="$(pgrep -fl "ollama serve" || true)"
[[ -n "$PROCS" ]] || die "no 'ollama serve' process found"
echo "$PROCS"
grep -q "Ollama.app" <<<"$PROCS" && die "the running server belongs to the menu-bar app"

step "5. Confirm the setting reached the server"
APPLIED="$(grep -o "OLLAMA_NUM_PARALLEL:[0-9]*" "$OLLAMA_LOG" | tail -1 || true)"
[[ -n "$APPLIED" ]] || die "no OLLAMA_NUM_PARALLEL line in $OLLAMA_LOG"
echo "  $APPLIED  (from $OLLAMA_LOG)"
[[ "$APPLIED" == "OLLAMA_NUM_PARALLEL:$PARALLEL" ]] || die "server reports $APPLIED, expected :$PARALLEL"

step "6. Confirm Ollama is on the GPU and not a translated CPU build"
# Rosetta launches the x86_64 half of ollama's universal binary, which has no Metal
# support, so inference silently falls back to CPU -- about 33 tok/s instead of ~190.
# The giveaway is uname disagreeing with hw.optional.arm64. The usual cause is an
# x86_64 wrapper somewhere in the parent chain: /usr/local/bin/timeout is one.
ARCH="$(uname -m)"
echo "  shell arch: $ARCH"
if [[ "$ARCH" != "arm64" && "$(sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]]; then
    die "running translated: uname says $ARCH on an arm64 machine.
  Rosetta's ollama build has no Metal, so this would measure CPU-only inference.
  Something in the parent process chain is an x86_64 binary — /usr/local/bin/timeout
  is the usual culprit. Re-run without it."
fi
COMPUTE="$(grep -o 'msg="inference compute".*' "$OLLAMA_LOG" | tail -1 | cut -c1-160 || true)"
[[ -n "$COMPUTE" ]] && echo "  $COMPUTE"
if grep -q 'library=cpu' <<<"$COMPUTE"; then
    die "Ollama reports library=cpu — it found no GPU, so this run would measure
  CPU-only inference and would not be comparable with the Metal runs in RESULTS.md."
fi

step "7. Warm the model directly"
curl -fsS --max-time 300 http://localhost:11434/api/generate \
     -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false}" > /dev/null \
    || die "warm-up against Ollama failed — is the model pulled? (ollama pull $MODEL)"
echo "  ok"

step "8. Refuse to run if a stray server owns the gateway port"
# A locally-run `uvicorn --port 8000` binds 127.0.0.1 while compose binds *:8000,
# and both can be up at once. Requests then go to whichever the resolver prefers,
# so the run silently measures the wrong process — including, if it is a working
# copy, code that is not the code under test. Same class of trap as the menu-bar
# app above, and just as invisible in the results.
# lsof truncates COMMAND to 9 characters, so Docker Desktop shows up as
# "com.docke" -- matching on "docker" silently never fires.
STRAY="$(lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null \
         | tail -n +2 | grep -viE "com\.docke|docker|vpnkit" || true)"
if [[ -n "$STRAY" ]]; then
    echo "$STRAY"
    die "something other than the compose gateway is listening on :8000 (above).
  Stop it and re-run — otherwise it is undefined which server this measures."
fi
echo "  :8000 is free or owned by docker"

step "9. Bring the stack up"
docker compose up -d --build
docker compose restart gateway >/dev/null
echo "  gateway restarted so in-process counters start from zero"
for i in $(seq 1 30); do
    curl -fsS --max-time 3 "$GATEWAY/health" >/dev/null 2>&1 && break
    sleep 2
    [[ $i -lt 30 ]] || die "gateway did not become reachable"
done
HEALTH="$(curl -fsS --max-time 5 "$GATEWAY/health")"
echo "  $HEALTH"
grep -q '"status":"ok"' <<<"$HEALTH" || die "gateway reports a degraded backend: $HEALTH"

step "10. Warm-up through the gateway"
curl -fsS --max-time 300 "$GATEWAY/v1/chat/completions" \
     -H 'Content-Type: application/json' \
     -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}" \
     > /dev/null || die "warm-up through the gateway failed"
echo "  ok"

# Everything Ollama logs from here belongs to the experiment.
LOG_OFFSET=$(wc -c < "$OLLAMA_LOG")

run_cell() {
    local c="$1"
    step "11.$c  n=$N c=$c"
    "$PY" benchmark/disconnect.py \
        -n "$N" -c "$c" --after "$AFTER" --max-tokens "$MAX_TOKENS" \
        --settle "$SETTLE" --model "$MODEL" --gateway "$GATEWAY" --prom "$PROM" \
        --ollama-log "$OLLAMA_LOG" --ollama-num-parallel "$PARALLEL" \
        --label "$LABEL" --out "$RUN_DIR/c$c"
}
run_cell 1
# Let the backend settle so c=4 does not inherit c=1's tail.
sleep 10
run_cell 4

step "12. Save the slice of Ollama's log this run produced"
tail -c "+$((LOG_OFFSET + 1))" "$OLLAMA_LOG" > "$RUN_DIR/ollama-log-slice.txt"
echo "  $(wc -l < "$RUN_DIR/ollama-log-slice.txt") lines -> $RUN_DIR/ollama-log-slice.txt"
echo "  cancel lines: $(grep -c "cancel task" "$RUN_DIR/ollama-log-slice.txt" || true)"

validate_run || echo "  (continuing so the run is preserved; the numbers do not count)"

step "13. Export this run's Prometheus data"
if SNAP=$(curl -fsS --max-time 60 -XPOST "$PROM/api/v1/admin/tsdb/snapshot" \
          | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['name'])" 2>/dev/null); then
    rm -rf "$RUN_DIR/prometheus-snapshot"
    docker compose cp "prometheus:/prometheus/snapshots/$SNAP" "$RUN_DIR/prometheus-snapshot" >/dev/null
    docker compose exec -T prometheus rm -rf "/prometheus/snapshots/$SNAP" >/dev/null 2>&1 || true
    echo "  exported to $RUN_DIR/prometheus-snapshot"
    rm -f "$STACK_DIR/benchmark/out/.unarchived"
    echo "$RUN_ID" > "$STACK_DIR/benchmark/out/.last-exported"
else
    echo "  WARNING: snapshot failed; the next run will try again before clearing"
fi

echo
echo "=== done: $LABEL ==="
echo "run dir: $RUN_DIR"
echo
echo "Headline numbers are in c1/report.txt and c4/report.txt, under 'totals'."
echo "Check every screenshot for your name, email, file paths and other browser tabs."
