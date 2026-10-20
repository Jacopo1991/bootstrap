#!/usr/bin/env python3
"""Enforce the checked-in line budgets."""
import sys
import ast
from pathlib import Path


def counted_files(path):
    path = Path(path).resolve()
    found = set()
    pending = [path]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        try:
            tree = ast.parse(current.read_text())
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split('.')[0])
        for module in modules:
            sibling = current.parent / f"{module}.py"
            package = current.parent / module / "__init__.py"
            if sibling.is_file():
                pending.append(sibling)
            elif package.is_file():
                pending.append(package)
    return sorted(found)


def check(path, budget):
    files = counted_files(path)
    count = sum(len(file.read_text().splitlines()) for file in files)
    print(f"{path}: {count} lines across {len(files)} local files (budget {budget})")
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
