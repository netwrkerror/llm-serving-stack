#!/usr/bin/env python3
"""
List every RESULTS.md table that reports numbers without citing where they came from.

  python3 benchmark/check_citations.py
  python3 benchmark/check_citations.py --strict     # exit non-zero if any are uncited

Why this exists
---------------
RESULTS.md once contained the sentence "4 wasted in the other 16 pairs". No script had
produced that number; it was asserted, it contradicted a later run by a factor of forty,
and it survived several reviews because nothing in the file said which run it came from.
A number with a cited source can be checked against its file. A number without one can
only be believed.

What counts as a citation
-------------------------
A path to a file inside a run directory — `run-<something>/<file>` — appearing in the
table, in its heading block, or within CONTEXT_LINES above or below it. Per-cell
citations are not practical in Markdown tables, so the unit is the table.

What this cannot do
-------------------
It does not check prose, and it does not verify that a cited file actually contains the
number. It makes an uncited number *visible*, which is the specific failure that got
through before. Treat a clean run as "every table says where to look", not as "every
number is right".
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmark" / "RESULTS.md"

CONTEXT_LINES = 8

# Two kinds of citation count.
#
# A path inside a run directory -- run-<id>/<file>, or a preserved transcript
# run-<id>.txt -- which points into local output the runs deliberately keep. This is
# how every measured figure in this file is referenced.
#
# A URL, because not every number in RESULTS comes from a run: model lifecycle dates
# and published prices come from a vendor's documentation, and the honest citation
# there is the page they were read from, with the date already recorded alongside.
CITATION = re.compile(
    r"run-[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+"      # file inside a run directory
    r"|run-[A-Za-z0-9._-]+\.txt"                 # a preserved transcript
    r"|https?://\S+"                             # a documented external source
    r"|eval/[A-Za-z0-9._-]+"                     # a committed fixture, e.g. the eval set
)

# A cell is "numeric" if it carries a digit that is not purely part of a date, a version
# or an identifier. Deliberately loose: a false positive costs a glance, a false
# negative is the thing this is meant to catch.
HAS_NUMBER = re.compile(r"\d")
NOT_A_MEASUREMENT = re.compile(
    r"^\s*\**\s*(?:"
    r"20\d\d-\d\d-\d\d.*"                 # dates
    r"|v?\d+\.\d+(\.\d+)?(\s*\(.*\))?"    # bare version numbers
    r"|#\d+"                              # issue/PR references
    r"|`[^`]*`"                           # a single code span: model ids, env vars
    r")\s*\**\s*$"
)
# Rows whose first cell is one of these are conditions, not measurements.
CONDITION_KEYS = {
    "date", "hardware", "ollama", "model", "models", "gateway", "load", "setting",
    "setting verified", "run dir", "run dirs", "trials", "eval set", "clock offset",
    "ollama log poll", "instruments", "kill", "backends", "tier", "why 4", "runner",
    "transcripts", "grading", "agreement", "source", "sources", "uvicorn 0.32.0",
    "starlette 0.38.6", "the `debug_saveall` census", "signal",
}


class Table:
    def __init__(self, start: int) -> None:
        self.start = start
        self.end = start
        self.rows: list[str] = []
        self.heading = ""

    @property
    def numeric_cells(self) -> list[tuple[int, str, str]]:
        """(line number, first cell, offending cell) for cells reporting a number."""
        out = []
        for offset, row in enumerate(self.rows):
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if not cells:
                continue
            key = cells[0].strip("* `").lower()
            if key in CONDITION_KEYS:
                continue
            for cell in cells[1:] or cells:
                if not HAS_NUMBER.search(cell):
                    continue
                if NOT_A_MEASUREMENT.match(cell):
                    continue
                out.append((self.start + offset + 1, cells[0][:40], cell[:40]))
        return out


def parse_tables(lines: list[str]) -> list[Table]:
    tables, current, heading = [], None, ""
    for i, line in enumerate(lines):
        if line.startswith("#"):
            heading = line.strip("# ").strip()
        is_row = line.strip().startswith("|")
        if is_row:
            if current is None:
                current = Table(i)
                current.heading = heading
            current.end = i
            # Skip the separator row.
            if not re.match(r"^\|[\s\-:|]+\|$", line.strip()):
                current.rows.append(line)
        elif current is not None:
            tables.append(current)
            current = None
    if current is not None:
        tables.append(current)
    return tables


# An explicit admission that a number cannot be traced. Not a citation, but not the
# same failure either: the first is a reader being told where to look and finding
# nothing recorded, the second is a reader not being told anything. Counting them
# together would make the actionable number never reach zero, and a warning that never
# clears is a warning people learn to skip.
ACKNOWLEDGED = re.compile(r"\(source not recorded")


def acknowledged(lines: list[str], table: Table) -> bool:
    lo = max(0, table.start - CONTEXT_LINES)
    hi = min(len(lines), table.end + CONTEXT_LINES + 1)
    return any(ACKNOWLEDGED.search(l) for l in lines[lo:hi])


def cited(lines: list[str], table: Table) -> str | None:
    lo = max(0, table.start - CONTEXT_LINES)
    hi = min(len(lines), table.end + CONTEXT_LINES + 1)
    for line in lines[lo:hi]:
        m = CITATION.search(line)
        if m:
            return m.group(0)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Find uncited numbers in RESULTS tables.")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any table reports numbers with no cited source")
    ap.add_argument("--file", default=str(RESULTS))
    args = ap.parse_args()

    path = Path(args.file)
    lines = path.read_text().split("\n")
    tables = parse_tables(lines)

    uncited, marked = [], []
    for table in tables:
        cells = table.numeric_cells
        if not cells:
            continue
        if cited(lines, table) is not None:
            continue
        (marked if acknowledged(lines, table) else uncited).append((table, cells))

    numeric_tables = sum(1 for t in tables if t.numeric_cells)
    print(f"{path.name}: {len(tables)} tables, {numeric_tables} reporting numbers")
    print(f"  cited:                  {numeric_tables - len(uncited) - len(marked)}")
    print(f"  marked untraceable:     {len(marked)}")
    print(f"  UNCITED (actionable):   {len(uncited)}")
    if marked:
        print()
        print("Marked `(source not recorded)` — acknowledged, not actionable:")
        for table, _cells in marked:
            print(f"  line {table.start + 1}  {table.heading}")

    if uncited:
        print()
        print("Tables reporting numbers with no `run-.../file` citation within "
              f"{CONTEXT_LINES} lines:")
        for table, cells in uncited:
            print(f"\n  line {table.start + 1}  under: {table.heading}")
            for lineno, key, cell in cells[:6]:
                print(f"    L{lineno}  {key:<40} | {cell}")
            if len(cells) > 6:
                print(f"    ... and {len(cells) - 6} more numeric cells in this table")
        print()
        print("Add a line naming the file the numbers came from, for example:")
        print("  Source: `run-queued-disconnect-validate-.../after-close-by-pair.txt`")
        print("Where a number cannot be traced to a file, mark it "
              "`(source not recorded)` rather than guessing.")

    return 1 if (uncited and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
