#!/usr/bin/env python3
"""Read-only drift report for the chezmoi-managed agent installation.

Output contains fixed reason codes and installed component names only. Command
output, config values, secrets, file contents, and task arguments are never emitted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

EXPECTED_BINARIES = {"claude", "bws", "secretspec", "codex", "ccusage", "gitleaks",
                     "lychee", "osv-scanner", "pre-commit", "qmd", "backlog", "just",
                     "playwright", "playwright-mcp", "osv-db-refresh", "pre-commit-enable",
                     "verify-enable", "new-project", "qmd-refresh"}
VERSION_COMMANDS = {
    "claude": ["claude", "--version"],
    "codex": ["codex", "--version"],
    "bws": ["bws", "--version"],
    "secretspec": ["secretspec", "--version"],
    "gitleaks": ["gitleaks", "version"],
    "ccusage": ["ccusage", "--version"],
    "lychee": ["lychee", "--version"],
    "osv-scanner": ["osv-scanner", "--version"],
}
# Release binaries pinned only by URL: the version is the URL's tag.
URL_VERSIONS = {
    "gitleaks": ("GITLEAKS_URL", "/download/v"),
    "lychee": ("LYCHEE_URL", "/download/lychee-v"),
    "osv-scanner": ("OSV_SCANNER_URL", "/download/v"),
}


def compare_sets(expected: set[str], actual: set[str]) -> list[str]:
    return sorted(actual - expected)


def approved_runtime_path(home: Path) -> str:
    paths = (
        home / ".local" / "bin",
        home / ".local" / "share" / "mise" / "shims",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/lib/wsl/lib"),
    )
    return os.pathsep.join(dict.fromkeys(str(path) for path in paths))


def run_quiet(args: list[str], timeout: int = 10, home: Path | None = None) -> tuple[int, str]:
    home = home or Path.home()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = approved_runtime_path(home)
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=timeout,
                                check=False, cwd=home, env=env)
        return result.returncode, result.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def read_pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Z0-9_]+)='([^']*)'", line.strip())
        if match:
            pins[match.group(1)] = match.group(2)
    return pins


def installed_names(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {entry.name for entry in directory.iterdir() if not entry.name.startswith(".")}


OSV_DB_MAX_AGE = 15 * 24 * 3600  # weekly timer plus a missed week


def osv_db_findings(home: Path, now: float) -> list[str]:
    """The pre-commit gate's offline vulnerability data, refreshed by osv-db-refresh.timer."""
    stamp = home / ".cache" / "osv-scalibr" / ".last-refresh"
    try:
        age = now - stamp.stat().st_mtime
    except OSError:
        return ["osv-db-never-refreshed"]
    return ["osv-db-stale"] if age > OSV_DB_MAX_AGE else []


def unexpected_global_binaries(home: Path) -> list[str]:
    return compare_sets(EXPECTED_BINARIES, installed_names(home / ".local" / "bin"))


def configured_mcp_names(home: Path) -> set[str]:
    names: set[str] = set()
    codex_path = home / ".codex" / "config.toml"
    if codex_path.exists():
        config = tomllib.loads(codex_path.read_text(encoding="utf-8"))
        names.update((config.get("mcp_servers") or {}).keys())
    for path in (home / ".claude.json", home / ".claude" / "settings.json"):
        if path.exists():
            config = json.loads(path.read_text(encoding="utf-8"))
            names.update((config.get("mcpServers") or {}).keys())
    return names


def configured_plugin_names(home: Path) -> set[str]:
    names: set[str] = set()
    metadata_files = {"installed_plugins.json", "known_marketplaces.json"}
    for path in (home / ".codex" / "plugins", home / ".claude" / "plugins"):
        if path.is_dir():
            names.update(entry.name for entry in path.iterdir()
                         if entry.name not in metadata_files and not entry.name.startswith("."))
    metadata = home / ".claude" / "plugins" / "installed_plugins.json"
    if metadata.exists():
        value = json.loads(metadata.read_text(encoding="utf-8"))
        names.update((value.get("plugins") or {}).keys())
    return names


def version_matches(expected: str, actual: str) -> bool:
    tokens = re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?(?![A-Za-z0-9])", actual)
    return expected in tokens


def unexpected_global_npm_packages(output: str) -> list[str]:
    value = json.loads(output)
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("npm global inventory is not a dependency map")
    return sorted(set(dependencies) - {"npm", "corepack"})


def installed_uv_tools(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir()
                  if entry.is_dir() and not entry.name.startswith("."))


def scan(root: Path, home: Path) -> list[str]:
    findings: set[str] = set()
    rc, _ = run_quiet(["chezmoi", "verify"], home=home)
    if rc:
        findings.add("chezmoi-verify-failed")
    rc, diff = run_quiet(["chezmoi", "diff"], home=home)
    if rc or diff.strip():
        findings.add("chezmoi-diff-nonempty")

    try:
        pins = read_pins(root / "home" / ".chezmoitemplates" / "pins.env")
    except OSError:
        findings.add("pin-file-unavailable")
        pins = {}
    for name, args in VERSION_COMMANDS.items():
        expected = pins.get(name.upper() + "_VERSION")
        if name in URL_VERSIONS:
            key, marker = URL_VERSIONS[name]
            url = pins.get(key, "")
            expected = url.split(marker, 1)[1].split("/", 1)[0] if marker in url else None
        if not expected:
            findings.add("pin-unavailable:" + name)
            continue
        resolved = shutil.which(name, path=approved_runtime_path(home))
        if not resolved or Path(resolved).parent.resolve() != (home / ".local" / "bin").resolve():
            findings.add("cli-path-drift:" + name)
        rc, actual = run_quiet(args, home=home)
        if rc or not version_matches(expected, actual):
            findings.add("cli-version-drift:" + name)

    try:
        tools = tomllib.loads((home / ".config/mise/config.toml").read_text(encoding="utf-8"))["tools"]
        expected_mise = {f"{name}@{version}" for name, version in tools.items()}
        installs = home / ".local/share/mise/installs"
        actual_mise = set()
        if installs.is_dir():
            actual_mise = {f"{tool.name}@{version.name}" for tool in installs.iterdir()
                           if tool.is_dir() for version in tool.iterdir() if version.is_dir()}
        findings.update("mise-install-outside-lock:" + item
                        for item in compare_sets(expected_mise, actual_mise))
        findings.update("mise-lock-not-installed:" + item
                        for item in compare_sets(actual_mise, expected_mise))
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        findings.add("mise-lock-unavailable")

    npm_rc, npm_output = run_quiet(["npm", "list", "-g", "--depth=0", "--json"], home=home)
    try:
        npm_extras = unexpected_global_npm_packages(npm_output)
    except (json.JSONDecodeError, ValueError):
        findings.add("npm-global-inventory-unavailable")
    else:
        if npm_rc:
            findings.add("npm-global-inventory-unavailable")
        findings.update("npm-global-outside-bootstrap:" + name for name in npm_extras)

    uv_dir_rc, uv_dir_output = run_quiet(["uv", "tool", "dir"], home=home)
    if uv_dir_rc or not uv_dir_output.strip():
        findings.add("uv-tool-inventory-unavailable")
    else:
        try:
            uv_names = installed_uv_tools(Path(uv_dir_output.strip().splitlines()[0]))
        except OSError:
            findings.add("uv-tool-inventory-unavailable")
        else:
            findings.update("uv-tool-outside-bootstrap:" + name for name in uv_names)

    findings.update(osv_db_findings(home, time.time()))
    extras = unexpected_global_binaries(home)
    findings.update("global-cli-outside-bootstrap:" + name for name in extras)
    for rel in (".agents/skills", ".claude/skills"):
        names = installed_names(home / rel)
        findings.update("global-skill-outside-bootstrap:" + name for name in names)
    for name in configured_mcp_names(home):
        findings.add("mcp-outside-empty-baseline:" + name)
    for name in configured_plugin_names(home):
        findings.add("plugin-outside-empty-baseline:" + name)
    return sorted(findings)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    try:
        findings = scan(root, home)
    except Exception:
        findings = ["drift-check-unavailable"]
    result = {
        "schemaVersion": 1,
        "status": "DRIFT" if findings else "CLEAN",
        "findings": findings,
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
