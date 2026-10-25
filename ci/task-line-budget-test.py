#!/usr/bin/env python3
"""Exercise the footprint report: sizes are printed, nothing fails on size, change is against a base."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

checker = Path(__file__).with_name("task-line-budget.py")
# Fixture repositories must not inherit a build lane's git environment.
clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def make_root(directory):
    root = Path(directory) / "repo"
    bin_dir = root / "home/dot_local/bin"
    bin_dir.mkdir(parents=True)
    (root / "ci").mkdir()
    (root / "ci/task-line-budget.py").write_text(checker.read_text())
    return root, bin_dir


def run_checker(root, *args):
    return subprocess.run([sys.executable, root / "ci/task-line-budget.py", *args],
                          cwd=root, text=True, capture_output=True, env=clean_env)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Budget Test",
                    "-c", "user.email=budget-test@example.invalid", *args],
                   check=True, capture_output=True, env=clean_env)


with tempfile.TemporaryDirectory() as directory:
    # An over-size tree reports its size and still exits 0.
    root, bin_dir = make_root(Path(directory) / "large")
    (bin_dir / "executable_task").write_text("import task_extra\n" + "line\n" * 9000)
    (bin_dir / "executable_task-batch").write_text("batch\n")
    (bin_dir / "executable_task-watch").write_text("watch\n" * 200)
    (bin_dir / "task_extra.py").write_text("extra\n" * 500)
    (bin_dir / "executable_security-watch").write_text("outside footprint\n" * 100)
    result = run_checker(root, "--base", "no-such-rev")
    assert result.returncode == 0, result
    assert f"{bin_dir / 'executable_task'}: 9001 lines" in result.stdout, result.stdout
    assert f"{bin_dir / 'task_extra.py'}: 500 lines" in result.stdout, result.stdout
    assert "total: 9702 lines across 4 local files" in result.stdout, result.stdout
    assert "security-watch" not in result.stdout, result.stdout
    assert "cannot resolve no-such-rev; change not measured" in result.stdout, result.stdout

    # An entry point that cannot be read is the only failure.
    root, bin_dir = make_root(Path(directory) / "missing")
    (bin_dir / "executable_task-batch").write_text("batch\n")
    (bin_dir / "executable_task-watch").write_text("watch\n")
    result = run_checker(root)
    assert result.returncode == 1, result
    assert "cannot read task entry points" in result.stdout, result.stdout

    # The change line compares the working tree with the base revision.
    root, bin_dir = make_root(Path(directory) / "git")
    (bin_dir / "executable_task").write_text("import task_extra\nentry\n")
    (bin_dir / "executable_task-batch").write_text("batch\n")
    (bin_dir / "executable_task-watch").write_text("watch\n")
    (bin_dir / "task_extra.py").write_text("extra\nextra\nextra\n")
    git(root, "init", "-q")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    base_commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                                 capture_output=True, text=True, env=clean_env).stdout.strip()
    (bin_dir / "executable_task").write_text("import task_extra\nentry\nmore\nmore\nmore\n")
    result = run_checker(root)
    assert result.returncode == 0, result
    assert "total: 10 lines across 4 local files" in result.stdout, result.stdout
    assert "base main: 7 lines" in result.stdout, result.stdout
    assert "change against main: +3 lines" in result.stdout, result.stdout

    # origin/main is preferred over main when it exists.
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "grow")
    git(root, "update-ref", "refs/remotes/origin/main", base_commit)
    result = run_checker(root)
    assert result.returncode == 0, result
    assert "base origin/main: 7 lines" in result.stdout, result.stdout
    assert "change against origin/main: +3 lines" in result.stdout, result.stdout

    # Without origin/main, main is the base; --base overrides both.
    git(root, "update-ref", "-d", "refs/remotes/origin/main")
    result = run_checker(root)
    assert result.returncode == 0, result
    assert "change against main: +0 lines" in result.stdout, result.stdout
    result = run_checker(root, "--base", base_commit)
    assert result.returncode == 0, result
    assert "change against " + base_commit + ": +3 lines" in result.stdout, result.stdout
