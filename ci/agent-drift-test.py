#!/usr/bin/env python3
"""Hosted tests for read-only installed-set drift detection."""
import json
import hashlib
import os
import shutil
from pathlib import Path
import tempfile
import importlib.util
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_drift", ROOT / "checks/agent-drift.py")
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)

expected = {"claude", "codex", "bws", "secretspec", "gitleaks", "ccusage",
            "lychee", "osv-scanner", "pre-commit", "qmd", "backlog", "just", "agy", "playwright",
            "playwright-mcp", "osv-db-refresh", "pre-commit-enable", "verify-enable", "gate-browser",
            "package-cap-check",
            "new-project", "qmd-refresh", "gh-skill-update", "workspace-tidy", "task", "task-batch", "job-ping",
            "task-watch", "task-smoke", "agentdev-shell-mcp", "agentdev-backlog-mcp"}
assert drift.EXPECTED_BINARIES == expected
allowed_helpers = {"task_limits.py", "task_routing.py", "task_usage.py", "task_lock.py"}
# Every command the bootstrap installs into ~/.local/bin is expected, not drift.
for script in (ROOT / "home/dot_local/bin").iterdir():
    if script.is_file():
        assert script.name.removeprefix("executable_") in expected | allowed_helpers | {"security-watch"}, script.name
# Exercise imports from the installed layout, outside the source directory.
with tempfile.TemporaryDirectory() as temp:
    installed = Path(temp) / ".local/bin"
    installed.mkdir(parents=True)
    for name in ("task", "task-batch", "task-watch", *allowed_helpers):
        source = name if name in allowed_helpers else "executable_" + name
        shutil.copy2(ROOT / "home/dot_local/bin" / source, installed / name)
    for name in ("task", "task-batch", "task-watch"):
        option = "--coverage" if name == "task-watch" else "--help"
        flags = [] if name == "task-watch" else ["-I"]
        result = subprocess.run([sys.executable, *flags, str(installed / name), option],
                                cwd=temp, capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stderr)
assert drift.compare_sets(expected, expected) == []
pins = drift.read_pins(ROOT / "home/.chezmoitemplates/pins.env")
assert pins["CLAUDE_VERSION"] == "2.1.296", "Claude Code alias routing requires the pinned 2.1.296 release"
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
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp)
    now = 2_000_000_000.0
    assert drift.osv_db_findings(home, now) == ["osv-db-never-refreshed"]
    stamp = home / ".cache/osv-scalibr/.last-refresh"
    stamp.parent.mkdir(parents=True)
    stamp.touch()
    os.utime(stamp, (now - 8 * 86400, now - 8 * 86400))
    assert drift.osv_db_findings(home, now) == []
    os.utime(stamp, (now - 16 * 86400, now - 16 * 86400))
    assert drift.osv_db_findings(home, now) == ["osv-db-stale"]
# The Claude app connectors (shell and Backlog.md MCP) are drift in an agent session.
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp)
    (home / ".claude").mkdir()
    settings = home / ".claude/settings.json"
    settings.write_text(json.dumps({"mcpServers": {"qmd": {"command": "qmd", "args": ["mcp"]}}}))
    assert not drift.backlog_mcp_references(home) and not drift.shell_connector_references(home)
    for server in ({"command": "wsl.exe", "args": ["--", "/home/agent/.local/bin/agentdev-backlog-mcp", "cortex-kb-company"]},
                   {"command": "/home/agent/.local/bin/agentdev-backlog-mcp", "args": ["cortex-kb-orw"]},
                   {"command": "npx", "args": ["backlog.md", "mcp", "start"]}):
        settings.write_text(json.dumps({"mcpServers": {"kb": server}}))
        assert drift.backlog_mcp_references(home), server
        assert not drift.shell_connector_references(home), server
# T-48: agy is pinned by URL to the release tarball, checked before extraction, and read back with
# `agy --version`. The vendor install script is never run.
assert pins["AGY_VERSION"] == "1.4.0"
assert pins["AGY_URL"].split("/download/", 1)[1].split("/", 1)[0] == pins["AGY_VERSION"]
assert pins["AGY_SHA256"] == "fd96967583c1eed94733c039638fa59b71ddbd823c75ebc8dd0e677463565aad"
assert drift.VERSION_COMMANDS["agy"] == ["agy", "--version"]
assert drift.URL_VERSIONS["agy"] == ("AGY_URL", "/download/")
assert "antigravity" not in drift.EXPECTED_BINARIES
tools_text = (ROOT / "home/.chezmoitemplates/tools.sh").read_text(encoding="utf-8")
assert '{{ include ".chezmoitemplates/agy-install.sh" }}' in tools_text
agy_block = (ROOT / "home/.chezmoitemplates/agy-install.sh").read_text(encoding="utf-8")
assert agy_block.index('download "$AGY_URL" "$AGY_SHA256"') < agy_block.index('tar -xzf "$tmp/agy.tar.gz"')
assert agy_block.index('tar -xzf "$tmp/agy.tar.gz"') < agy_block.index('install -m 0755 "$tmp/antigravity" "$agy_root/antigravity"')
assert "install.sh" not in agy_block and "antigravity.google" not in tools_text and "| bash" not in agy_block
template_text = (ROOT / "home/run_onchange_after_10-tools.sh.tmpl").read_text(encoding="utf-8")
assert f"agy {pins['AGY_VERSION']} release sha256: {pins['AGY_SHA256']}" in template_text
assert drift.version_matches("1.4.0", "antigravity 1.4.0") and drift.version_matches("1.4.0", "agy version 1.4.0")
assert not drift.version_matches("1.4.0", "antigravity 1.4.10")
# The read-back runs through the injectable runner, so no real agy binary is executed here.
def fake_runner(agy_output):
    def run(args, timeout=10, home=None):
        if args == ["agy", "--version"]:
            return 0, agy_output
        if args == ["npm", "list", "-g", "--depth=0", "--json"]:
            return 0, json.dumps({"dependencies": {"npm": {}, "corepack": {}}})
        if args == ["uv", "tool", "dir"]:
            return 0, str(home / ".local/share/uv/tools")
        return (0, "") if args[:2] == ["chezmoi", "verify"] or args[:2] == ["chezmoi", "diff"] else (127, "")
    return run
with tempfile.TemporaryDirectory() as temp:
    home = Path(temp)
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    agy_link = bin_dir / "agy"
    agy_link.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agy_link.chmod(0o755)
    agy_binary = home / ".local/share/bootstrap/agy/1.4.0/antigravity"
    agy_binary.parent.mkdir(parents=True)
    agy_binary.write_bytes(b"pinned fake binary")
    (agy_binary.parent / "antigravity.sha256").write_text(
        hashlib.sha256(agy_binary.read_bytes()).hexdigest(), encoding="ascii")
    agy_findings = lambda findings: [f for f in findings if f.endswith(":agy") or ":agy:" in f]
    assert agy_findings(drift.scan(ROOT, home, run=fake_runner("antigravity 1.4.0"))) == []
    assert "cli-version-drift:agy" in drift.scan(ROOT, home, run=fake_runner("antigravity 1.3.9"))
    assert "agy-version-drift" in drift.scan(ROOT, home, run=fake_runner("antigravity 1.3.9"))
    assert "cli-version-drift:agy" in drift.scan(ROOT, home, run=fake_runner(""))
    agy_link.unlink()
    assert "cli-path-drift:agy" in drift.scan(ROOT, home, run=fake_runner("antigravity 1.4.0"))
    agy_link.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agy_link.chmod(0o755)
    agy_binary.write_bytes(b"changed fake binary")
    assert "agy-version-drift" in drift.scan(ROOT, home, run=fake_runner("antigravity 1.4.0"))
# The installed Codex requirements must carry the checked-in read-deny profiles.
with tempfile.TemporaryDirectory() as temp:
    installed = Path(temp) / "requirements.toml"
    source = (ROOT / "system/codex-requirements.toml").read_text()
    assert drift.codex_deny_list_missing(ROOT, installed)
    installed.write_text(source)
    assert not drift.codex_deny_list_missing(ROOT, installed)
    for stripped in (source.split("\n[permissions.lane]")[0], source.replace('"~/project-data" = "deny"', '', 1),
                     source.replace('default_permissions = "lane"', 'default_permissions = "review"')):
        installed.write_text(stripped)
        assert drift.codex_deny_list_missing(ROOT, installed)
print("PASS: foreign npm, uv, and global CLI installs, stale OSV data and connector registrations are reported as drift")
print("PASS: agy is pinned to the 1.4.0 tarball, checked before extraction, and read back with agy --version")
