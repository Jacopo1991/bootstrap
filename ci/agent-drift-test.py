#!/usr/bin/env python3
"""Hosted tests for read-only installed-set drift detection."""
import json
import os
import shutil
from pathlib import Path
import tempfile
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_drift", ROOT / "checks/agent-drift.py")
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)

expected = {"claude", "codex", "bws", "secretspec", "gitleaks", "ccusage",
            "lychee", "osv-scanner", "pre-commit"}
assert drift.EXPECTED_BINARIES == expected
assert drift.compare_sets(expected, expected) == []
pins = drift.read_pins(ROOT / "home/.chezmoitemplates/pins.env")
for name, (key, marker) in drift.URL_VERSIONS.items():
    version = pins[key].split(marker, 1)[1].split("/", 1)[0]
    assert drift.version_matches(version, f"{name} {version}"), name
assert drift.version_matches("2.6.0", "osv-scanner version: 2.6.0\nosv-scalibr version: 0.5.2")
assert not drift.version_matches("2.6.0", "osv-scanner version: 2.6.1\nosv-scalibr version: 2.6.0x")
assert drift.version_matches("0.159.2", "codex-cli 0.159.2")
assert not drift.version_matches("0.159.2", "codex-cli 0.159.20")
assert drift.VERSION_COMMANDS["ccusage"] == ["ccusage", "--version"]
assert drift.version_matches("20.0.26", "ccusage 20.0.26")
assert not drift.version_matches("20.0.26", "ccusage 20.0.260")
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
    shim_dir = home / ".local" / "share" / "mise" / "shims"
    shim_dir.mkdir(parents=True)
    for directory, name, marker in (
        (bin_dir, "ccusage", "managed-ccusage"),
        (shim_dir, "npm", "managed-npm"),
        (shim_dir, "uv", "managed-uv"),
    ):
        executable = directory / name
        executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + marker + "'\n", encoding="utf-8")
        executable.chmod(0o755)
        assert drift.approved_runtime_path(home).split(os.pathsep)[0] == str(bin_dir)
        assert Path(shutil.which(name, path=drift.approved_runtime_path(home))).parent == directory
        rc, output = drift.run_quiet([name, "--version"], home=home)
        assert rc == 0 and output.strip() == marker, (name, rc, output)
    tool_root = home / ".local" / "share" / "uv" / "tools"
    tool_root.mkdir(parents=True)
    (tool_root / "foreign-uv-tool").mkdir()
    assert drift.installed_uv_tools(tool_root) == ["foreign-uv-tool"]
print("PASS: foreign npm, uv, and global CLI installs are reported as drift")
