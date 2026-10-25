#!/usr/bin/env python3
"""Exercise the line budget checker at and above its limit."""
import subprocess
import sys
import tempfile
import contextlib
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch

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

    # The default budget covers task's three entry points and their task_* modules,
    # including transitive imports, but excludes independently scheduled jobs.
    root = Path(directory) / "repo"
    bin_dir = root / "home/dot_local/bin"
    bin_dir.mkdir(parents=True)
    (root / "ci").mkdir()
    (root / "ci/task-line-budget.txt").write_text("6\n")
    (bin_dir / "executable_task").write_text("import task_extra\n")
    (bin_dir / "executable_task-batch").write_text("batch\n")
    (bin_dir / "executable_task-watch").write_text("watch\n")
    (bin_dir / "task_extra.py").write_text("import task_nested\n")
    (bin_dir / "task_nested.py").write_text("nested\nnested\n")
    for name in ("executable_security-watch", "executable_workspace-tidy", "unrelated.py"):
        (bin_dir / name).write_text("outside budget\n" * 100)
    spec = importlib.util.spec_from_file_location("line_budget", checker)
    budget = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(budget)
    output = io.StringIO()
    with patch.object(budget, "__file__", str(root / "ci/task-line-budget.py")):
        with contextlib.redirect_stdout(output):
            assert budget.main([str(checker)]) == 0, output.getvalue()
    counted = {line.split(": ", 1)[0] for line in output.getvalue().splitlines()
               if line.startswith(str(bin_dir))}
    assert counted == {str(bin_dir / name) for name in (
        "executable_task", "executable_task-batch", "executable_task-watch",
        "task_extra.py", "task_nested.py")}, output.getvalue()
    assert "total: 6 lines across 5 local files (budget 6)" in output.getvalue()
