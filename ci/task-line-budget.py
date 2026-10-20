#!/usr/bin/env python3
"""Enforce the checked-in line budgets."""
import sys
from pathlib import Path


def check(path, budget):
    count = len(Path(path).read_text().splitlines())
    print(f"{path}: {count} lines (budget {budget})")
    return count <= budget


def main(argv):
    if len(argv) == 3:
        return 0 if check(argv[1], int(argv[2])) else 1
    root = Path(__file__).resolve().parent.parent
    failed = False
    for line in (root / "ci/task-line-budget.txt").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        path, raw_budget = line.split()
        if not check(root / path, int(raw_budget)):
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
