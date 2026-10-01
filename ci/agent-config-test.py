#!/usr/bin/env python3
"""Validate the small, managed Codex and Claude Code configuration surface."""
from pathlib import Path
import json
import tomllib


ROOT = Path(__file__).resolve().parents[1]
assert (ROOT / ".chezmoiroot").read_text(encoding="utf-8").strip() == "home"

codex = tomllib.loads((ROOT / "home/dot_codex/config.toml").read_text(encoding="utf-8"))
assert codex == {
    "approval_policy": "on-request",
    "approvals_reviewer": "user",
    "sandbox_mode": "workspace-write",
    "sandbox_workspace_write": {"network_access": False},
}

claude = json.loads((ROOT / "home/dot_claude/settings.json").read_text(encoding="utf-8"))
permissions = claude["permissions"]
assert permissions["defaultMode"] == "default"
assert "allow" not in permissions
assert "hooks" not in claude and "plugins" not in claude
required_denies = {
    "Bash(git merge *)",
    "Bash(gh pr merge *)",
    "Bash(powershell.exe *)",
    "Bash(pwsh.exe *)",
    "Bash(cmd.exe *)",
    "Bash(wsl.exe *)",
    "Bash(/mnt/c/Windows/**)",
    "Read(~/.ssh/**)",
    "Read(~/.config/gh/**)",
    "Read(~/.codex/auth.json)",
    "Read(//mnt/c/**)",
    "Read(//mnt/d/**)",
    "Edit(//mnt/c/**)",
    "Edit(//mnt/d/**)",
}
assert required_denies <= set(permissions["deny"])

for path in ("home/dot_codex/AGENTS.md", "home/dot_claude/CLAUDE.md"):
    instructions = (ROOT / path).read_text(encoding="utf-8").lower()
    for phrase in ("claim work", "pull request", "never merge", "read the affected",
                   "secret values", "windows-host actions", "stop and report"):
        assert phrase in instructions, (path, phrase)

print("PASS: agent instructions and native permission defaults")
