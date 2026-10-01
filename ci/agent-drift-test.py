#!/usr/bin/env python3
"""Hosted tests for read-only installed-set drift detection."""
import json
from pathlib import Path
import tempfile
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_drift", ROOT / "checks/agent-drift.py")
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)

expected = {"claude", "codex", "bws", "secretspec", "gitleaks", "ccusage"}
assert drift.compare_sets(expected, expected) == []
assert drift.version_matches("0.159.2", "codex-cli 0.159.2")
assert not drift.version_matches("0.159.2", "codex-cli 0.159.20")
npm_fixture = json.dumps({"dependencies": {
    "npm": {"version": "10.9.0"},
    "corepack": {"version": "0.34.0"},
    "foreign-agent-cli": {"version": "1.0.0"},
}})
assert drift.unexpected_global_npm_packages(npm_fixture) == ["foreign-agent-cli"]
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp)
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in expected:
        (bin_dir / name).touch()
    assert drift.unexpected_global_binaries(home) == []
    (bin_dir / "foreign-agent-cli").touch()
    assert drift.unexpected_global_binaries(home) == ["foreign-agent-cli"]
    tool_root = home / ".local" / "share" / "uv" / "tools"
    tool_root.mkdir(parents=True)
    (tool_root / "foreign-uv-tool").mkdir()
    assert drift.installed_uv_tools(tool_root) == ["foreign-uv-tool"]
print("PASS: foreign npm, uv, and global CLI installs are reported as drift")
