#!/usr/bin/env python3
"""Run hook-calling suites with a disposable HOME; catch audit-state escapes."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "agent-config-test.py", "managed-policy-test.py", "claude-managed-mods-test.py",
    "git-default-merge-test.py", "task-test.py", "claude-managed-install-test.py",
}
# Fail when a new caller is added without coverage. Includes imports and the
# review subprocess stub, not just direct subprocess invocations of the hook.
callers = {p.name for p in (ROOT / "ci").glob("*.py")
           if p.name != Path(__file__).name and "pre_tool_use.py" in p.read_text()}
# skills-hooks only checks managed configuration; it executes context_reminder.
assert callers == SUITES | {"skills-hooks-test.py"}, callers

with tempfile.TemporaryDirectory(prefix="hook-state-guard-") as temp:
    base = Path(temp)
    home = base / "home"
    state = base / "state"
    home.mkdir()
    state.mkdir()
    env = {k: os.environ[k] for k in ("PATH", "TASK_GATE_RUN_ID") if k in os.environ}
    # All suite-owned temporary directories live under state. HOME is outside
    # it so an inherited/default audit destination is detected after each run.
    env.update(HOME=str(home), TMPDIR=str(state), PYTHONDONTWRITEBYTECODE="1")
    # Reproduce a lane's anchors: suites must replace them before policy calls.
    env.update(TASK_WORKTREE=str(ROOT), XDG_STATE_HOME=str(home / "lane-state"))
    for suite in sorted(SUITES):
        if suite == "claude-managed-install-test.py":
            # Exercise the actual deployed-hook calling block without running
            # installation/immutability checks or needing a machine install.
            tree = ast.parse((ROOT / "ci" / suite).read_text())
            start = next(i for i, node in enumerate(tree.body)
                         if isinstance(node, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == "pre_tool_use"
                                 for t in node.targets))
            body = ast.Module(body=tree.body[start:], type_ignores=[])
            hook_argv = ["/usr/bin/env", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH",
                         "PATH=/usr/local/bin:/usr/bin:/bin", "/usr/bin/python3", "-I",
                         str(ROOT / "system/agent-policy/pre_tool_use.py")]
            code = ("import json, os, subprocess, tempfile\nfrom pathlib import Path\n"
                    f"argvs = {[hook_argv]!r}\n"
                    + ast.unparse(body))
            argv = [sys.executable, "-c", code]
        else:
            argv = [sys.executable, str(ROOT / "ci" / suite)]
        result = subprocess.run(argv, env=env, cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, (suite, result.stdout, result.stderr)
        escaped = [p for p in base.rglob("denials.jsonl") if not p.is_relative_to(state)]
        assert not escaped, (suite, escaped)
        print(f"PASS: {suite} leaves no denial log outside temporary state", flush=True)
