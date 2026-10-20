#!/usr/bin/env python3
"""Exercise the line budget checker at and above its limit."""
import subprocess
import sys
import tempfile
from pathlib import Path

checker = Path(__file__).with_name("task-line-budget.py")
with tempfile.TemporaryDirectory() as directory:
    fixture = Path(directory) / "fixture"
    for count, expected in ((3, 0), (4, 1)):
        fixture.write_text("line\n" * count)
        result = subprocess.run([sys.executable, checker, fixture, "3"],
                                text=True, capture_output=True)
        assert result.returncode == expected, result
        assert f"{count} lines (budget 3)" in result.stdout, result.stdout
