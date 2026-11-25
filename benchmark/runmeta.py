#!/usr/bin/env python3
"""
Refuse to report numbers from a run that failed its own validity check.

    from runmeta import require_valid
    require_valid(run_dir)        # prints and raises SystemExit if the run is INVALID

Three states, deliberately distinguished
----------------------------------------
**VALID** — the run declared expectations before it started and met all of them.
Report its numbers.

**INVALID** — the run declared expectations and failed at least one. Refuse, and print
which. These are the runs that previously looked fine: a CPU-only Ollama under Rosetta,
a kill that landed after the scenario ended.

**incomplete** — an `expected.txt` exists but neither marker does, so the run declared
what it believed about itself and then died before checking any of it. Refuse. This is
not the same as predating the check: something was claimed and never tested. The first
end-to-end smoke of this machinery landed here, with an empty `expected.txt`, and the
earlier version of this file called it "unvalidated" — which read as "an old run",
exactly the reassuring label this whole check exists to deny.

**unvalidated** — no `expected.txt` at all, so the run predates this check. Report the
numbers with a visible banner. Refusing here would make every historical run
unreadable, and the run directories are deliberately never destroyed; a reader needs to
be told the run was not checked, not prevented from seeing it.

The distinction matters because the failure this guards against is a number that looks
trustworthy. A silent pass and a hard refusal are both worse than saying which it is.
"""
import sys
from pathlib import Path


class RunInvalid(SystemExit):
    pass


def run_state(run_dir: Path) -> str:
    """"valid" | "invalid" | "incomplete" | "unvalidated"."""
    run_dir = Path(run_dir)
    if (run_dir / "INVALID").is_file():
        return "invalid"
    if (run_dir / "VALID").is_file():
        return "valid"
    if (run_dir / "expected.txt").is_file():
        return "incomplete"
    return "unvalidated"


def require_valid(run_dir, *, stream=None) -> str:
    """Return the run's state, or exit non-zero if it is INVALID.

    Prints a one-line banner in every case, so a report can never be read without
    knowing which of the three states produced it.
    """
    out = stream or sys.stdout
    run_dir = Path(run_dir)
    state = run_state(run_dir)

    if state == "valid":
        detail = (run_dir / "VALID").read_text().strip().replace("\n", "; ")
        print(f"[run VALID] {run_dir.name}  ({detail})", file=out)
        return state

    if state == "unvalidated":
        print(f"[run UNVALIDATED] {run_dir.name} has no expected.txt — it predates the "
              f"validity check. Numbers below were not verified against declared "
              f"expectations.", file=out)
        return state

    if state == "incomplete":
        expected = (run_dir / "expected.txt").read_text().strip()
        n = len(expected.splitlines()) if expected else 0
        print(f"[run INCOMPLETE] {run_dir.name} declared {n} expectation(s) and never "
              f"reached validate_run — the run died part way. Refusing to report its "
              f"numbers.", file=out)
        raise RunInvalid(2)

    report = run_dir / "validity-report.txt"
    print(f"[run INVALID] {run_dir.name} failed its own validity check. "
          f"Refusing to report its numbers.", file=out)
    if report.is_file():
        for line in report.read_text().splitlines():
            if line.startswith("FAILED"):
                print(f"  {line}", file=out)
    else:
        print("  (no validity-report.txt; see INVALID)", file=out)
    raise RunInvalid(2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for arg in sys.argv[1:]:
        require_valid(arg)
