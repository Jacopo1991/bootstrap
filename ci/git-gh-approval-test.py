#!/usr/bin/env python3
"""Check prompt rules with the installed pinned Codex CLI; execute no Git/gh command."""
import json
import os
from pathlib import Path
import subprocess

agent_home = Path.home()
os.environ["PATH"] = f"{agent_home}/.local/bin:{agent_home}/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"
codex = agent_home / ".local/bin/codex"
assert codex.is_file(), "pinned Codex installation missing"
rules = Path.home() / ".codex/rules/default.rules"
for command in (
    ["git", "push", "origin", "ci-fixture"],
    ["gh", "issue", "list"],
    ["/usr/bin/git", "fetch", "origin"],
    ["/usr/bin/gh", "pr", "create"],
):
    result = subprocess.run(
        [str(codex), "execpolicy", "check", "--rules", str(rules), "--", *command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=True, timeout=20,
    )
    assert json.loads(result.stdout)["decision"] == "prompt", command
print("PASS: installed Codex git/gh rules require user approval")
