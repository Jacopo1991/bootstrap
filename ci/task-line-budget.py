#!/usr/bin/env python3
"""Report the footprint of the task entry points and their change against a base.

Sizes are measured and reported per merge; no threshold fails a check. The script
exits 0 unless it cannot read the files.
"""
import argparse
import ast
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BIN_RELATIVE = Path("home/dot_local/bin")
ENTRIES = ("executable_task", "executable_task-batch", "executable_task-watch")


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


def measure(entries):
    return [(file, len(file.read_text().splitlines())) for file in counted_files(entries)]


def revision_exists(root, rev):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                            capture_output=True)
    return result.returncode == 0


def base_total(root, rev):
    archive = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", rev, BIN_RELATIVE.as_posix()],
                             capture_output=True)
    if archive.returncode:
        return None
    with tempfile.TemporaryDirectory() as directory:
        extract_options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(directory, **extract_options)
        entries = [Path(directory) / BIN_RELATIVE / name for name in ENTRIES]
        try:
            return sum(lines for _, lines in measure(entries))
        except (OSError, UnicodeDecodeError):
            return None


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="revision to compare against (default: origin/main, else main)")
    args = parser.parse_args(argv[1:])

    root = Path(__file__).resolve().parent.parent
    bin_dir = root / BIN_RELATIVE
    try:
        counts = measure([bin_dir / name for name in ENTRIES])
    except (OSError, UnicodeDecodeError) as error:
        print(f"cannot read task entry points: {error}")
        return 1
    for file, lines in counts:
        print(f"{file}: {lines} lines")
    total = sum(lines for _, lines in counts)
    print(f"total: {total} lines across {len(counts)} local files")

    candidates = [args.base] if args.base else ["origin/main", "main"]
    rev = next((candidate for candidate in candidates if revision_exists(root, candidate)), None)
    if rev is None:
        print(f"base: cannot resolve {' or '.join(candidates)}; change not measured")
        return 0
    before = base_total(root, rev)
    if before is None:
        print(f"base {rev}: cannot read task entry points; change not measured")
        return 0
    print(f"base {rev}: {before} lines")
    print(f"change against {rev}: {total - before:+d} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
