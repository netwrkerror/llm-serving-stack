#!/usr/bin/env bash
#
# Run-validity checks: declare what must be true for a run to count, then verify it.
#
#   source benchmark/lib/validity.sh
#   validity_init "$RUN_DIR"
#   expect "Metal, not a CPU fallback" "$OLLAMA_LOG" "library=Metal" 1
#   expect "the setting reached the server" "$OLLAMA_LOG" "OLLAMA_NUM_PARALLEL:4" 1
#   ...run the experiment...
#   validate_run    # writes VALID, or INVALID naming every expectation that failed
#
# Why
# ---
# Three runs in this project produced clean-looking numbers that meant nothing: two had
# Ollama on a CPU-only Rosetta build, and one had its kill land after the scenario had
# already finished. Each was caught by eye, late, and only because someone went looking.
# An expectation written down *before* the run is checkable afterwards by a script.
#
# expected.txt is written at the start, so it also records what the run believed about
# itself even when the run dies half way.

set -uo pipefail

_VALIDITY_RUN_DIR=""

validity_init() {
    _VALIDITY_RUN_DIR="$1"
    mkdir -p "$_VALIDITY_RUN_DIR"
    : > "$_VALIDITY_RUN_DIR/expected.txt"
    rm -f "$_VALIDITY_RUN_DIR/VALID" "$_VALIDITY_RUN_DIR/INVALID"
}

# expect <description> <file> <grep -E pattern> [min_count]
#
# The file may not exist yet: expectations are declared before the run produces them.
expect() {
    local description="$1" file="$2" pattern="$3" min="${4:-1}"
    printf '%s\t%s\t%s\t%s\n' "$description" "$file" "$pattern" "$min" \
        >> "$_VALIDITY_RUN_DIR/expected.txt"
}

# expect_absent <description> <file> <pattern>
#
# For things that must NOT appear: an instrument flag in a run claiming to be clean,
# a CPU-only inference line, a Rosetta architecture.
expect_absent() {
    expect "$1" "$2" "$3" 0
}

validate_run() {
    local expected="$_VALIDITY_RUN_DIR/expected.txt"
    local failures=0 checked=0
    local report="$_VALIDITY_RUN_DIR/validity-report.txt"
    : > "$report"

    while IFS=$'\t' read -r description file pattern min; do
        [[ -n "${description:-}" ]] || continue
        checked=$((checked + 1))
        local count=0
        if [[ -f "$file" ]]; then
            count=$(grep -cE -- "$pattern" "$file" 2>/dev/null || true)
            count=${count:-0}
        fi
        if [[ "$min" -eq 0 ]]; then
            if [[ "$count" -eq 0 ]]; then
                printf 'ok      %s  (absent, as required)\n' "$description" >> "$report"
            else
                printf 'FAILED  %s  -- found %s occurrence(s) of %s in %s, expected none\n' \
                    "$description" "$count" "$pattern" "$file" >> "$report"
                failures=$((failures + 1))
            fi
        elif [[ "$count" -ge "$min" ]]; then
            printf 'ok      %s  (%s match(es))\n' "$description" "$count" >> "$report"
        else
            printf 'FAILED  %s  -- %s match(es) of %s in %s, expected >= %s\n' \
                "$description" "$count" "$pattern" "$file" "$min" >> "$report"
            failures=$((failures + 1))
        fi
    done < "$expected"

    echo
    echo "=== run validity: $checked expectation(s), $failures failed ==="
    cat "$report"

    if [[ "$failures" -eq 0 ]]; then
        {
            echo "validated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
            echo "expectations: $checked"
        } > "$_VALIDITY_RUN_DIR/VALID"
        echo "  -> VALID"
        return 0
    fi
    {
        echo "failed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "$failures of $checked expectations failed; see validity-report.txt"
    } > "$_VALIDITY_RUN_DIR/INVALID"
    echo "  -> INVALID. The numbers in this run directory do not count."
    return 1
}
