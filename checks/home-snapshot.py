"""Metadata-only idempotence evidence; never prints file contents."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

home = Path.home()
paths = set()
for name in subprocess.check_output(['chezmoi', 'managed', '--include=files']).decode().splitlines():
    paths.add(home / name)
for directory in ['.local/bin', '.local/share/bootstrap/npm', '.local/share/mise/installs']:
    for base, dirs, files in os.walk(home / directory):
        for name in files:
            paths.add(Path(base) / name)
for path in sorted(paths):
    stat = path.lstat()
    if path.is_symlink():
        content = os.readlink(path)
    else:
        content = hashlib.sha256(path.read_bytes()).hexdigest()
    print(json.dumps([str(path.relative_to(home)), stat.st_mode, stat.st_mtime_ns, content]))
