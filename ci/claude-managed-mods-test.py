#!/usr/bin/env python3
"""Hosted checks: managed-mods drop-in content and the policy hook's denials.

The pinned Claude Code release may predate mods, so these checks assert the
deployed policy and the hook's verdicts, not mod loading itself.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from policy_test_env import isolate_policy_environment

isolate_policy_environment()

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
assert 'source "$(dirname -- "$0")/common.sh"' in installer
assert ('atomic_policy_install "$BOOTSTRAP_ROOT/system/claude-managed-mods.json" '
        + "\\\n" + '  "$dropins/50-managed-mods-only.json"') in installer
common = (ROOT / "system/common.sh").read_text(encoding="utf-8")
assert 'mktemp "$(dirname -- "$target")/.agent-policy.XXXXXX"' in common
assert 'install -o root -g root -m 0644 "$source" "$temp" && mv -f -- "$temp" "$target"' in common
assert "claude-managed.sh" in (ROOT / "install.sh").read_text(encoding="utf-8")


# The command-parsing hook was removed (decision-38). Windows escapes stay denied by the
# vendor's own managed deny rules; the agent account has no sudo.
assert not HOOK.exists()
deny = json.loads((ROOT / "system/claude-managed-guardrails.json").read_text(encoding="utf-8"))["permissions"]["deny"]
for rule in ("Bash(wsl.exe *)", "Bash(powershell.exe *)", "Bash(pwsh *)", "Bash(cmd.exe *)"):
    assert rule in deny, rule
print("claude managed-mods drop-in and managed deny-rule checks passed")
