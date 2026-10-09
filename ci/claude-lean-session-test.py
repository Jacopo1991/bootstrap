#!/usr/bin/env python3
"""Claude Code session settings stay lean and warning-free: no malformed deny rules,
claude.ai connectors off, only the approved plugins and MCP servers load."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "home/dot_local/share/bootstrap"
settings = json.loads((ROOT / "home/dot_claude/settings.json").read_text(encoding="utf-8"))
# The deny rules are managed (root-owned drop-in, ci/managed-policy-test.py).
deny = json.loads((ROOT / "system/claude-managed-guardrails.json").read_text(
    encoding="utf-8"))["permissions"]["deny"]

# Rules with a colon after a wildcard (for example 'git push * :* *') or a trailing ':*'
# after a space print a warning in every session; the policy hook enforces refspec
# colons instead (ci/agent-config-test.py).
for entry in deny:
    assert not re.search(r"\*\s+:|\s:\*|:\*\)$", entry), f"warning-prone deny rule: {entry}"
for entry in ("Bash(git push * :* *)", "Bash(/usr/bin/git push * :* *)"):
    assert entry not in deny, entry

# claude.ai connectors (Gmail, Calendar, Drive, ...) stay out of coding sessions.
assert settings["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"

# Plugins a coding lane never needs are off. desktop-commander runs commands and edits
# files outside the policy hook, so it must never load in an agent session.
plugins = settings["enabledPlugins"]
for name in ("desktop-commander", "design", "product-management", "cowork-plugin-management"):
    assert plugins.get(name + "@synced") is False, name
assert not any(value is True for value in plugins.values())
# The plugins whose skills lanes use (engineering code-review/debug/testing, cortex) stay on.
for name in ("engineering", "cortex", "slim-workflow"):
    assert plugins.get(name + "@synced", True) is not False, name

# Engineering's servers need a login we do not have.
denied = {entry["serverName"] for entry in settings["deniedMcpServers"]}
assert denied == {"plugin:engineering:" + name
                  for name in ("slack", "linear", "atlassian", "notion", "datadog")}

# Expected servers: qmd, playwright, context7, each registered at user scope on install.
registered = {
    "qmd": (SHARE / "executable_qmd-setup.sh", "mcp add --scope user --transport http qmd "),
    "playwright": (SHARE / "executable_verify-setup.sh", "mcp add --scope user playwright "),
    "context7": (SHARE / "executable_context7-setup.sh",
                 'mcp add --scope user --transport http context7 https://mcp.context7.com/mcp'),
}
for name, (script, command) in registered.items():
    assert command in script.read_text(encoding="utf-8"), name
install = (ROOT / "install.sh").read_text(encoding="utf-8")
assert install.index("chezmoi --source") < install.index("context7-setup.sh")
assert not settings.get("mcpServers")
print("PASS: lean Claude Code session settings (no warning-prone rules, connectors off, "
      "plugins and MCP servers as approved)")
