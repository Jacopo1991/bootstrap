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
        assert f"total: {count} lines across 1 local files (budget 3)" in result.stdout, result.stdout

    entry = Path(directory) / "executable_task"
    entry.write_text("import task_extra\nentry\n")
    (Path(directory) / "task_extra.py").write_text("extra\nextra\nextra\n")
    result = subprocess.run([sys.executable, checker, entry, "4"],
                            text=True, capture_output=True)
    assert result.returncode == 1, result
    assert "5 lines across 2 local files (budget 4)" in result.stdout, result.stdout
    assert f"{entry}: 2 lines" in result.stdout, result.stdout
    assert f"{Path(directory) / 'task_extra.py'}: 3 lines" in result.stdout, result.stdout
