"""Run-validity checks, and the RESULTS citation audit.

Three runs in this project produced clean-looking numbers that meant nothing: two had
Ollama on a CPU-only Rosetta build, one had its kill land after the scenario finished.
Each was caught by eye, late. These tests cover the machinery that makes those
conditions declarable before a run and checkable after it.

The citation audit runs as a **warning**, not a failure: it reports tables whose numbers
cite no source file, and it cannot check prose, so failing the build on it would be
stronger than the evidence it produces.
"""
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "benchmark"))

from runmeta import RunInvalid, require_valid, run_state  # noqa: E402

VALIDITY_SH = ROOT / "benchmark" / "lib" / "validity.sh"


# --- runmeta: the three states ------------------------------------------------------

def test_unvalidated_run_is_reported_not_refused(tmp_path, capsys):
    """A run predating the check must still be readable, with a visible banner.

    Refusing here would make every preserved historical run unreadable, and those
    directories are deliberately never destroyed.
    """
    assert run_state(tmp_path) == "unvalidated"
    assert require_valid(tmp_path) == "unvalidated"
    out = capsys.readouterr().out
    assert "UNVALIDATED" in out
    assert "not verified" in out


def test_a_run_that_died_before_validating_is_refused_not_called_old(tmp_path, capsys):
    """An expected.txt with no marker means the run declared something and never checked
    it. That is not the same as predating the check, and must not borrow its banner.

    The first end-to-end smoke of this machinery landed here, with an empty expected.txt,
    and was reported as "it predates the validity check" -- the reassuring label.
    """
    (tmp_path / "expected.txt").write_text("present\tollama.log\tlibrary=Metal\t1\n")
    assert run_state(tmp_path) == "incomplete"
    with pytest.raises(RunInvalid):
        require_valid(tmp_path)
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out
    assert "never reached validate_run" in out
    assert "declared 1 expectation" in out
    assert "predates" not in out, "an aborted run must not be labelled a historical one"


def test_valid_run_is_reported_with_its_detail(tmp_path, capsys):
    (tmp_path / "VALID").write_text("validated 2026-11-24T00:00:00Z\nexpectations: 7\n")
    assert require_valid(tmp_path) == "valid"
    out = capsys.readouterr().out
    assert "run VALID" in out
    assert "expectations: 7" in out


def test_invalid_run_is_refused_and_says_which_expectation_failed(tmp_path, capsys):
    (tmp_path / "INVALID").write_text("failed\n1 of 5 expectations failed\n")
    (tmp_path / "validity-report.txt").write_text(
        "ok      something fine  (2 matches)\n"
        "FAILED  Ollama on Metal, not a CPU fallback  -- 0 match(es) of library=Metal\n"
    )
    with pytest.raises(RunInvalid):
        require_valid(tmp_path)
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "Refusing to report its numbers" in out
    assert "Ollama on Metal" in out, "the failed expectation must be named"
    assert "something fine" not in out, "only failures are worth printing"


# --- validity.sh: declare before, verify after ---------------------------------------

def run_shell(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True,
                          text=True, timeout=60)


def test_expectations_are_written_before_the_run(tmp_path):
    """expected.txt exists from the start, so a run that dies half way still records
    what it believed about itself."""
    script = f'''
        source "{VALIDITY_SH}"
        validity_init "{tmp_path}/run"
        expect "a thing" "{tmp_path}/some.log" "pattern" 1
    '''
    proc = run_shell(script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    expected = (tmp_path / "run" / "expected.txt").read_text()
    assert "a thing" in expected and "pattern" in expected


def test_a_met_expectation_produces_VALID(tmp_path):
    (tmp_path / "ollama.log").write_text("library=Metal\nOLLAMA_NUM_PARALLEL:4\n")
    script = f'''
        source "{VALIDITY_SH}"
        validity_init "{tmp_path}/run"
        expect "on metal" "{tmp_path}/ollama.log" "library=Metal" 1
        expect_absent "not cpu" "{tmp_path}/ollama.log" "library=cpu"
        validate_run
    '''
    proc = run_shell(script, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "run" / "VALID").is_file()
    assert not (tmp_path / "run" / "INVALID").is_file()


def test_the_cpu_only_run_that_fooled_us_is_now_caught(tmp_path):
    """The real regression: Rosetta gave Ollama a CPU build and the numbers looked fine."""
    (tmp_path / "ollama.log").write_text(
        'msg="inference compute" id=cpu library=cpu description=cpu\n'
        "OLLAMA_NUM_PARALLEL:4\n")
    script = f'''
        source "{VALIDITY_SH}"
        validity_init "{tmp_path}/run"
        expect "on metal" "{tmp_path}/ollama.log" "library=Metal" 1
        expect_absent "not cpu" "{tmp_path}/ollama.log" "library=cpu"
        validate_run
    '''
    proc = run_shell(script, tmp_path)
    assert proc.returncode != 0, "a CPU-only run must not validate"
    assert (tmp_path / "run" / "INVALID").is_file()
    report = (tmp_path / "run" / "validity-report.txt").read_text()
    assert report.count("FAILED") == 2, report
    assert "on metal" in report and "not cpu" in report


def test_a_missing_file_fails_rather_than_passing_quietly(tmp_path):
    """An expectation against a file the run never produced is a failure, not a skip."""
    script = f'''
        source "{VALIDITY_SH}"
        validity_init "{tmp_path}/run"
        expect "a report exists" "{tmp_path}/never-written.txt" "anything" 1
        validate_run
    '''
    proc = run_shell(script, tmp_path)
    assert proc.returncode != 0
    assert "FAILED" in (tmp_path / "run" / "validity-report.txt").read_text()


def test_every_runner_declares_expectations():
    """A runner with no expectations cannot produce a VALID run, so it would silently
    fall back to the unvalidated banner forever."""
    runners = sorted((ROOT / "benchmark").glob("run_*.sh"))
    assert runners, "no runners found"
    missing = [r.name for r in runners if "validity_init" not in r.read_text()]
    assert not missing, f"runners with no validity expectations: {missing}"

    no_validate = [r.name for r in runners if "validate_run" not in r.read_text()]
    assert not no_validate, f"runners that never validate: {no_validate}"


def test_expectations_only_reference_variables_already_set():
    """Expectations are declared *before* the run, which puts them above most of the
    script -- and `set -u` turns a variable not yet assigned into an immediate abort.

    Two runners shipped with exactly that: the expectation block referenced
    `$OLLAMA_LOG` and `$REPS` several lines before they were assigned, so the run died
    at line 33 having written an empty expected.txt. Nothing measured the runners
    themselves, so it took an end-to-end smoke to notice.
    """
    assign = re.compile(r"^\s*(?:readonly\s+|export\s+|local\s+)?([A-Z_][A-Z0-9_]*)=")
    inline_assign = re.compile(r"\b([A-Z_][A-Z0-9_]*)=")
    ref = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)")
    # Set by bash itself or by validity.sh, not by the runner.
    AMBIENT = {"BASH_SOURCE", "PWD", "HOME", "PATH", "IFS", "RANDOM", "LINENO"}

    problems = []
    for runner in sorted((ROOT / "benchmark").glob("run_*.sh")):
        lines = runner.read_text().split("\n")
        defined: set[str] = set()
        for lineno, line in enumerate(lines, 1):
            if line.lstrip().startswith(("expect ", "expect_absent ")):
                for name in ref.findall(line):
                    # `${VAR:-default}` and `${VAR:+x}` are safe under `set -u`.
                    if re.search(rf"\$\{{{name}:[-+?]", line):
                        continue
                    if name in defined or name in AMBIENT:
                        continue
                    problems.append(f"{runner.name}:{lineno} uses ${name} before it is set")
            defined.update(assign.findall(line))
            if line.lstrip().startswith("if ") or "; then " in line:
                defined.update(inline_assign.findall(line))

    assert not problems, "\n".join(problems)


# --- the citation audit, as a warning ------------------------------------------------

def test_results_tables_cite_their_sources():
    """Reports uncited numeric tables as a warning.

    Not a failure: the checker works at table granularity, cannot read prose, and does
    not verify that a cited file contains the number. It makes the specific failure that
    got through before — a number no script produced — visible.
    """
    proc = subprocess.run(
        [sys.executable, str(ROOT / "benchmark" / "check_citations.py")],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    actionable = None
    for line in proc.stdout.splitlines():
        if "UNCITED (actionable)" in line:
            actionable = int(line.rsplit(":", 1)[1].strip())
    assert actionable is not None, f"checker output not understood:\n{proc.stdout}"

    if actionable:
        warnings.warn(
            f"{actionable} RESULTS.md table(s) report numbers with no cited source. "
            f"Run benchmark/check_citations.py for the list.",
            UserWarning,
            stacklevel=2,
        )
