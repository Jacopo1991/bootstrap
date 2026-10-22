#!/usr/bin/env python3
"""Keep the current revision and the newest published revisions."""
import os
from pathlib import Path
import re
import shutil
import sys

REVISION_NAME = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def prune(revisions_dir, keep):
    """Prune old revisions, returning an error message only when current is unsafe."""
    revisions = Path(revisions_dir).resolve(strict=True)
    current_link = revisions.parent / "current"
    try:
        current = current_link.resolve(strict=True)
        current.relative_to(revisions)
        if not current.is_dir():
            raise OSError("current target is not a directory")
    except (OSError, ValueError):
        print("prune-revisions: current does not resolve to a directory inside revisions; nothing removed")
        return

    candidates = []
    for path in revisions.iterdir():
        if not REVISION_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(revisions)
        except (OSError, ValueError):
            continue
        if resolved != current and resolved not in current.parents:
            candidates.append((path.stat().st_mtime, path))

    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, path in candidates[keep:]:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(revisions)
            if path.is_symlink() or not resolved.is_dir():
                raise OSError("candidate changed or is not a directory")
            shutil.rmtree(resolved)
            print(f"prune-revisions: removed {path.name}")
        except (OSError, ValueError) as error:
            print(f"prune-revisions: could not remove {path.name}: {error}", file=sys.stderr)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0:
        print("Run this system script with sudo.", file=sys.stderr)
        return 1
    if len(argv) != 2:
        print("Usage: prune-revisions.py REVISIONS_DIR KEEP_COUNT", file=sys.stderr)
        return 2
    try:
        keep = int(argv[1])
        if keep < 0:
            raise ValueError
        prune(argv[0], keep)
    except (OSError, ValueError) as error:
        print(f"prune-revisions: cannot inspect revisions: {error}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
