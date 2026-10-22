#!/usr/bin/env python3
"""Enforce the checked-in line budgets."""
import sys
import ast
from pathlib import Path


def counted_files(paths):
    if isinstance(paths, (str, Path)):
        paths = [paths]
    found = set()
    pending = [Path(path).resolve() for path in paths]
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
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module)
        for module in modules:
            if not module.startswith("task_"):
                continue
            sibling = current.parent / f"{module}.py"
            package = current.parent / module / "__init__.py"
            if sibling.is_file():
                pending.append(sibling)
            elif package.is_file():
                pending.append(package)
    return sorted(found)


def check(path, budget):
    files = counted_files(path)
    counts = [(file, len(file.read_text().splitlines())) for file in files]
    count = sum(lines for _, lines in counts)
    for file, lines in counts:
        print(f"{file}: {lines} lines")
    print(f"total: {count} lines across {len(files)} local files (budget {budget})")
    return count <= budget


def main(argv):
    if len(argv) == 3:
        return 0 if check(argv[1], int(argv[2])) else 1
    root = Path(__file__).resolve().parent.parent
    bin_dir = root / "home/dot_local/bin"
    entries = [bin_dir / "executable_task", bin_dir / "executable_task-batch"]
    entries.extend(path for path in bin_dir.glob("*watch*") if path.is_file())
    failed = False
    for line in (root / "ci/task-line-budget.txt").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        raw_budget, = line.split()
        if not check(entries, int(raw_budget)):
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
