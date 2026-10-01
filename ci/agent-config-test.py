#!/usr/bin/env python3
"""Hosted tests for native settings and policy-hook negative controls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
assert (ROOT / ".chezmoiroot").read_text(encoding="utf-8").strip() == "home"
codex = tomllib.loads((ROOT / "home/dot_codex/config.toml").read_text(encoding="utf-8"))
assert codex["approval_policy"] == "untrusted"
assert codex["approvals_reviewer"] == "user"
assert codex["sandbox_mode"] == "workspace-write"
assert codex["sandbox_workspace_write"]["network_access"] is False
assert codex["features"]["hooks"] is True
codex_hooks = json.loads((ROOT / "home/dot_codex/hooks.json").read_text(encoding="utf-8"))
assert set(codex_hooks["hooks"]) == {"PreToolUse"}
assert {x["matcher"] for x in codex_hooks["hooks"]["PreToolUse"]} == {"^Bash$", "^(apply_patch|Edit|Write)$"}

claude = json.loads((ROOT / "home/dot_claude/settings.json").read_text(encoding="utf-8"))
assert claude["permissions"]["defaultMode"] == "default"
assert "allow" not in claude["permissions"]
assert claude["sandbox"] == {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True}
assert "PreToolUse" in claude["hooks"]
assert not claude.get("mcpServers") and not claude.get("plugins")

spec = importlib.util.spec_from_file_location(
    "agent_policy", ROOT / "home/dot_local/share/bootstrap/agent-policy/pre_tool_use.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    root = base / "repo"
    root.mkdir()
    outside = base / "outside.txt"
    assert policy.inside(str(root / "file.py"), str(root), root)
    assert not policy.inside(str(outside), str(root), root)
    assert policy.evaluate({"tool_name": "PowerShell", "tool_input": {}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "wsl.exe -d AgentDev"}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "sudo true"}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "touch " + str(outside)}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Write", "tool_input": {"file_path": str(outside)}, "cwd": str(root)}) is not None
    approved = (base,)
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "git status --short"},
                            "cwd": str(root)}, approved) is None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "python3 -c 'print(1)'"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "env sudo true"},
                            "cwd": str(root)}, approved) is not None

for path in ("home/dot_codex/AGENTS.md", "home/dot_claude/CLAUDE.md"):
    rules = (ROOT / path).read_text(encoding="utf-8").lower()
    for phrase in ("claim work", "never merge", "read the affected", "secret values",
                   "approved roots", "scheduled project jobs remain deferred",
                   "customerharness and typo3"):
        assert phrase in rules, (path, phrase)
print("PASS: native settings, hook schema, and policy negative controls")
