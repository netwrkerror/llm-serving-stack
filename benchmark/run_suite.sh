#!/usr/bin/env bash
#
# One full benchmark run at a given OLLAMA_NUM_PARALLEL.
#
#   ./benchmark/run_suite.sh 1
#   ./benchmark/run_suite.sh 4
#   QUICK=1 ./benchmark/run_suite.sh 4      # tiny samples, for smoke-testing the script
#
# Launches Ollama itself and refuses to continue unless the setting actually reached
# the server — the macOS menu-bar app respawns its own server without shell env vars,
# which silently invalidates the run.
#
# Nothing is destroyed between runs. Each run writes a directory under benchmark/out/:
#
#   benchmark/out/run-<n>-<timestamp>/
#       transcript.txt          everything printed below
#       prometheus-snapshot/    this run's TSDB, exported at the end
#       traces-oversubscribed.txt   trace IDs for the c=8 scenario
#
# Prometheus series are cleared at the start of a run (after exporting), so the
# dashboard shows only the current run. Jaeger keeps everything, since badger is
# persistent — older traces stay queryable by ID or time range.
#
# See "Reading an earlier run" in the README for how to point Grafana at a snapshot.

set -euo pipefail

PARALLEL="${1:-}"
if [[ ! "$PARALLEL" =~ ^[0-9]+$ ]]; then
    echo "usage: $0 <OLLAMA_NUM_PARALLEL>   e.g. $0 4" >&2
    exit 2
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."
STACK_DIR="$PWD"
OLLAMA_LOG="/tmp/ollama_p${PARALLEL}.log"
RUN_ID="run-${PARALLEL}-$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$STACK_DIR/benchmark/out/$RUN_ID"
mkdir -p "$RUN_DIR"
OUT="$RUN_DIR/transcript.txt"

MODEL="${MODEL:-llama3.2:1b}"
GATEWAY="http://localhost:8000"
PROM="http://localhost:9090"
JAEGER="http://localhost:16686"

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
echo "run id: $RUN_ID"
echo "output: $RUN_DIR"
[[ -n "${QUICK:-}" ]] && echo "QUICK mode: tiny samples, results are not publishable"

step "1. Preserve whatever is already in Prometheus, then clear it"
# Not `down -v`: that destroys the previous run's metrics and traces outright.
if curl -fsS --max-time 5 "$PROM/-/healthy" >/dev/null 2>&1; then
    HAVE=$(curl -fsS --max-time 10 "$PROM/api/v1/query?query=count(llm_requests_total)" \
           | "$PY" -c "import sys,json;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)" 2>/dev/null || echo 0)
    if [[ "$HAVE" != "0" ]]; then
        UNARCHIVED="$(cat "$STACK_DIR/benchmark/out/.unarchived" 2>/dev/null || true)"
        EXPORTED="$(cat "$STACK_DIR/benchmark/out/.last-exported" 2>/dev/null || true)"
        if [[ -z "$UNARCHIVED" && -n "$EXPORTED" ]]; then
            # the live series were already snapshotted at the end of that run;
            # re-exporting them would just make a duplicate under a confusing name
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
# Counters like llm_tokens_generated_total live in the gateway process, so clearing
# Prometheus series does not reset them. Without this restart, "Total Tokens
# Generated" shows every run since the container started, not this one.
docker compose restart gateway >/dev/null
echo "  gateway restarted so in-process counters start from zero"
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
echo "  queue slots: $("$PY" -c "import json,sys;print(json.loads(sys.argv[1])['queue_slots'])" "$HEALTH")"

step "9. Warm-up through the gateway (first request pays import/connection costs)"
curl -fsS --max-time 300 "$GATEWAY/v1/chat/completions" \
     -H 'Content-Type: application/json' \
     -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}" \
     > /dev/null || die "warm-up through the gateway failed"
echo "  ok"

step "10. Scenarios (each repeated, so you can see run-to-run spread)"
REPS=2
if [[ -n "${QUICK:-}" ]]; then
    SEQ_N=2; CAP_N=4; OVER_N=4; REPS=1
else
    SEQ_N=8; CAP_N=24; OVER_N=16
fi
run_scenario() {
    local label="$1" n="$2" c="$3"
    for rep in $(seq 1 $REPS); do
        echo
        echo "----- $label | n=$n c=$c | repetition $rep/$REPS -----"
        "$PY" benchmark/bench.py -n "$n" -c "$c" --stream \
              --model "$MODEL" --ollama-num-parallel "$PARALLEL"
    done
}
run_scenario "sequential"     "$SEQ_N"  1
run_scenario "at capacity"    "$CAP_N"  4
OVER_START=$(date +%s)
run_scenario "oversubscribed" "$OVER_N" 8

step "11. Record the oversubscribed scenario's trace IDs"
sleep 6   # let the exporter flush and jaeger index
"$PY" - "$JAEGER" "$OVER_START" "$RUN_DIR/traces-oversubscribed.txt" <<'PYEOF'
import json, sys, urllib.request, urllib.parse, datetime
jaeger, since, outfile = sys.argv[1], int(sys.argv[2]), sys.argv[3]
url = jaeger + "/api/traces?" + urllib.parse.urlencode(
    {"service": "llm-serving-stack", "limit": 2000, "lookback": "2h"})
try:
    traces = json.load(urllib.request.urlopen(url, timeout=30))["data"]
except Exception as exc:
    print(f"  WARNING: could not query Jaeger: {exc}")
    raise SystemExit(0)

rows = []
for t in traces:
    inf = next((s for s in t["spans"] if s["operationName"] == "llm.inference"), None)
    qw = next((s for s in t["spans"] if s["operationName"] == "llm.queue.wait"), None)
    if not inf or inf["startTime"] / 1e6 < since:
        continue
    tags = {x["key"]: x["value"] for x in inf.get("tags", [])}
    rows.append((inf["startTime"], t["traceID"],
                 (qw["duration"] / 1000 if qw else 0.0),
                 tags.get("ollama.unattributed_ms"), tags.get("ollama.load_ms"),
                 tags.get("ollama.eval_ms"), tags.get("ollama.total_ms")))
rows.sort()
hdr = f'{"trace_id":<34}{"queue_wait_ms":>14}{"unattributed_ms":>17}{"load_ms":>10}{"eval_ms":>10}{"total_ms":>10}'
lines = [hdr, "-" * len(hdr)]
for _, tid, qw, un, ld, ev, to in rows:
    f = lambda v: f"{v:10.2f}" if isinstance(v, (int, float)) else f"{'n/a':>10}"
    lines.append(f"{tid:<34}{qw:14.2f}{(f'{un:17.2f}' if isinstance(un,(int,float)) else f'{chr(110)+chr(47)+chr(97):>17}')}{f(ld)}{f(ev)}{f(to)}")
text = "\n".join(lines)
open(outfile, "w").write(text + "\n")
print(f"  {len(rows)} traces recorded -> {outfile}")
print("\n".join(lines[:7]))
if len(rows) > 5:
    print(f"  ... {len(rows)-5} more in the file")
PYEOF

step "12. Export this run's Prometheus data"
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
echo "=== done: OLLAMA_NUM_PARALLEL=$PARALLEL ==="
echo "run dir: $RUN_DIR"
echo
echo "Preserved for later:"
echo "  metrics  $RUN_DIR/prometheus-snapshot   (see README, 'Reading an earlier run')"
echo "  traces   still in Jaeger — badger is persistent, look them up by ID from"
echo "           $RUN_DIR/traces-oversubscribed.txt"
echo
echo "Screenshots to take now:"
echo "  Grafana  http://localhost:3000  -> In-flight = waiting + running"
echo "                                     Request Latency p50 / p95 - all statuses"
echo "                                     Time to First Token - TTFT"
echo "  Jaeger   http://localhost:16686 -> a trace ID from traces-oversubscribed.txt"
echo "  Check every screenshot for your name, email, file paths and other browser tabs."
