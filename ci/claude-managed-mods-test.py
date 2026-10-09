#!/usr/bin/env python3
"""Hosted checks: managed-mods drop-in content and the policy hook's denials.

The pinned Claude Code release may predate mods, so these checks assert the
deployed policy and the hook's verdicts, not mod loading itself.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "system/agent-policy/pre_tool_use.py"
DROPIN = json.loads((ROOT / "system/claude-managed-mods.json").read_text(encoding="utf-8"))

# Exactly the documented guard option; no wider switch (disableAllHooks would
# switch off the managed hook path, allowManagedHooksOnly the user's own hooks).
assert DROPIN == {"pluginConfigs": {"cc-plugin-sec-default@builtin": {
    "options": {"allowManagedModsOnly": True}}}}, DROPIN

# The guard reads this option from managed settings only; a copy in the
# agent's own settings would be ignored and misleading.
user_settings = (ROOT / "home/dot_claude/settings.json").read_text(encoding="utf-8")
for key in ("pluginConfigs", "allowManagedModsOnly", "prependPlugins"):
    assert key not in user_settings, key
# enabledPlugins is an ordinary user setting (lean sessions, #54): it may only
# switch plugins off, never on, so it cannot widen what loads in agent sessions.
enabled = json.loads(user_settings).get("enabledPlugins", {})
assert all(value is False for value in enabled.values()), enabled

installer = (ROOT / "system/claude-managed.sh").read_text(encoding="utf-8")
assert "/etc/claude-code/managed-settings.d" in installer
assert "-o root -g root -m 0644" in installer
assert "claude-managed.sh" in (ROOT / "install.sh").read_text(encoding="utf-8")


def hook(event: dict, cwd: Path) -> dict:
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                            capture_output=True, text=True, cwd=cwd, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


with tempfile.TemporaryDirectory() as temp:
    cwd = Path(temp)
    for command in ("wsl.exe -d AgentDev", "sudo true"):
        out = hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}, cwd)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", command
    out = hook({"tool_name": "PowerShell", "tool_input": {}, "cwd": str(cwd)}, cwd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
print("claude managed-mods drop-in and hook denial checks passed")
