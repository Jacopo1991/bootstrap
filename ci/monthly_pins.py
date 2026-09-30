#!/usr/bin/env python3
"""Resolve monthly pin candidates and publish an exact-head checked PR.

Only Python's standard library is used. Network failures retain the affected
pin cohort and are reported; apt source lists and indexes live in a temporary
directory and never replace the runner's package sources.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
MAX_DOWNLOAD = 150 * 1024 * 1024
MONTHLY_BRANCH_PREFIX = "automation/monthly-pin-bump-"


class ResolveError(RuntimeError):
    pass


def request_bytes(url: str, limit: int = MAX_DOWNLOAD) -> bytes:
    headers = {"User-Agent": "bootstrap-monthly-pin-review"}
    if url.startswith(API + "/") and os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
        headers["Accept"] = "application/vnd.github+json"
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            data = response.read(limit + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        code = getattr(exc, "code", None)
        raise ResolveError("upstream request failed" + (f" (HTTP {code})" if code else "")) from None
    if len(data) > limit:
        raise ResolveError("upstream artifact exceeds the download limit")
    return data


def request_json(url: str) -> Any:
    try:
        return json.loads(request_bytes(url))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ResolveError("upstream response was not valid JSON") from None


def stable_version(value: str) -> bool:
    return bool(re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", value))


def version_tuple(value: str) -> tuple[int, ...]:
    if not stable_version(value):
        raise ResolveError("version is not an exact stable numeric release")
    return tuple(int(part) for part in value.split("."))


def version_is_newer(candidate: str, current: str) -> bool:
    left, right = version_tuple(candidate), version_tuple(current)
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)) > right + (0,) * (size - len(right))


def debian_version_is_not_lower(candidate: str, current: str) -> bool:
    try:
        result = subprocess.run(
            ["dpkg", "--compare-versions", candidate, "ge", current],
            check=False, capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ResolveError("Debian version comparison is unavailable") from None
    return result.returncode == 0


def apt_source_filename(source_text: str) -> str:
    return "sources.sources" if re.search(r"(?m)^Types:\s*deb\s*$", source_text) else "sources.list"


def apt_options(temp: Path, source: Path, lists: Path, archives: Path, status: Path) -> list[str]:
    return [
        "-o", f"Dir::State::lists={lists}/",
        "-o", f"Dir::State::status={status}",
        "-o", f"Dir::Cache::archives={archives}/",
        "-o", f"Dir::Cache::pkgcache={temp / 'pkgcache.bin'}",
        "-o", f"Dir::Cache::srcpkgcache={temp / 'srcpkgcache.bin'}",
        "-o", f"Dir::Etc::sourcelist={source}",
        "-o", "Dir::Etc::sourceparts=-",
        "-o", "Dir::Etc::parts=-",
        "-o", "Dir::Etc::main=/dev/null",
        "-o", "Dir::Etc::preferences=/dev/null",
        "-o", "Dir::Etc::preferencesparts=-",
        "-o", "APT::Get::List-Cleanup=0",
        "-o", "APT::Sandbox::User=",
    ]


def snapshot_candidates(now: dt.datetime, current: str, days: int = 8) -> list[str]:
    dates = []
    for offset in range(days):
        value = (now.date() - dt.timedelta(days=offset)).strftime("%Y%m%dT000000Z")
        if value > current:
            dates.append(value)
    return dates


def parse_pins(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)='([^']*)'", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def replace_pin(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}='[^']*'$", re.MULTILINE)
    result, count = pattern.subn(lambda _: f"{key}='{value}'", text)
    if count != 1:
        raise ResolveError(f"expected exactly one {key} pin")
    return result


def lock_entries(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("=")
        if not sep or not name or not version:
            raise ResolveError("malformed apt lock")
        entries.append((name, version))
    if not entries:
        raise ResolveError("empty apt lock")
    return entries


def render_lock(entries: list[tuple[str, str]], candidates: dict[str, str]) -> str:
    lines: list[str] = []
    for name, old_version in entries:
        candidate = candidates.get(name)
        if not candidate:
            raise ResolveError(f"no candidate for locked package {name}")
        if not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", candidate):
            raise ResolveError(f"invalid candidate for locked package {name}")
        if not debian_version_is_not_lower(candidate, old_version):
            raise ResolveError(f"candidate would downgrade locked package {name}")
        lines.append(f"{name}={candidate}")
    return "\n".join(lines) + "\n"


def gate_run_matches(
    run: dict[str, Any], workflow_id: int, branch: str, head_sha: str, dispatched_at: dt.datetime,
    prior_run_ids: set[int] | None = None,
) -> bool:
    if prior_run_ids and run.get("id") in prior_run_ids:
        return False
    if run.get("workflow_id") != workflow_id or run.get("event") != "workflow_dispatch":
        return False
    if run.get("head_branch") != branch or run.get("head_sha") != head_sha:
        return False
    try:
        created = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return created >= dispatched_at


def select_gate_run(
    runs: list[dict[str, Any]], workflow_id: int, branch: str, head_sha: str, dispatched_at: dt.datetime,
    prior_run_ids: set[int] | None = None,
) -> dict[str, Any] | None:
    candidates = [
        run for run in runs
        if gate_run_matches(run, workflow_id, branch, head_sha, dispatched_at, prior_run_ids)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda run: run.get("run_number", 0))


def run_quietly(args: list[str], timeout: int = 900, env: dict[str, str] | None = None,
                cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(args, check=False, capture_output=True, text=True,
                                   timeout=timeout, env=env, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        raise ResolveError("required resolver command failed or timed out") from None
    if completed.returncode:
        # Upstream command output can contain download URLs. Keep reports terse.
        raise ResolveError("required resolver command returned a failure")
    return completed.stdout


def isolated_apt_candidates(source_text: str, packages: list[str], key_material: bytes | None = None,
                            armored: bool = False) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="bootstrap-apt-probe-") as temp_name:
        temp = Path(temp_name)
        lists = temp / "lists"
        (lists / "partial").mkdir(parents=True)
        archives = temp / "archives"
        archives.mkdir()
        status = temp / "status"
        status.touch()
        source = temp / apt_source_filename(source_text)
        if key_material is not None:
            raw_key = temp / "source-key"
            raw_key.write_bytes(key_material)
            key_path = temp / "source-key.gpg"
            if armored:
                run_quietly(["gpg", "--batch", "--yes", "--dearmor", "--output", str(key_path), str(raw_key)])
            else:
                key_path.write_bytes(key_material)
            source_text = source_text.replace("@KEY@", str(key_path))
        source.write_text(source_text, encoding="utf-8")
        options = apt_options(temp, source, lists, archives, status)
        env = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
        run_quietly(["apt-get", *options, "update"], timeout=1200, env=env)
        candidates: dict[str, str] = {}
        for package in packages:
            output = run_quietly(["apt-cache", *options, "policy", package], timeout=60, env=env)
            match = re.search(r"^\s*Candidate:\s*(\S+)\s*$", output, re.MULTILINE)
            if not match or match.group(1) == "(none)":
                raise ResolveError(f"no candidate for locked package {package}")
            candidates[package] = match.group(1)
        return candidates

def apt_key(pins: dict[str, str], prefix: str) -> tuple[bytes, bool]:
    url = pins.get(prefix + "_KEY_URL")
    expected = pins.get(prefix + "_KEY_SHA256")
    if not url or not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise ResolveError(f"pinned {prefix} signing key is incomplete")
    data = request_bytes(url, limit=2 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ResolveError(f"pinned {prefix} signing key content changed")
    return data, prefix != "GH"


def build_base_cohort(pins_text: str, lock_text: str, candidates: dict[str, str],
                      snapshot: str) -> tuple[str, str]:
    lock = render_lock(lock_entries(lock_text), candidates)
    updated_pins = replace_pin(pins_text, "APT_SNAPSHOT", snapshot)
    return lock, updated_pins


def resolve_apt(root: Path, pins_text: str, pins: dict[str, str], staged: dict[str, str],
                notes: list[str], now: dt.datetime) -> None:
    pins_path = root / "home/.chezmoitemplates/pins.env"
    base_path = root / "system/apt-base.lock"
    base_lock = staged.get(str(base_path), base_path.read_text(encoding="utf-8"))
    base_entries = lock_entries(base_lock)
    snapshots = snapshot_candidates(now, pins["APT_SNAPSHOT"])
    resolved_base = False
    latest_reason = "no newer daily snapshot date is available"
    for index, snapshot in enumerate(snapshots):
        source_text = (
            "Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/" + snapshot +
            "/\nSuites: noble noble-updates noble-security\n"
            "Components: main universe restricted multiverse\n"
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
            "Check-Valid-Until: no\n"
        )
        try:
            candidates = isolated_apt_candidates(
                source_text, [name for name, _ in base_entries]
            )
            new_lock, new_pins = build_base_cohort(
                staged.get(str(pins_path), pins_text), base_lock, candidates, snapshot
            )
            staged[str(base_path)] = new_lock
            staged[str(pins_path)] = new_pins
            pins.clear()
            pins.update(parse_pins(new_pins))
            extra = f" (used {index} day(s) of fallback)" if index else ""
            notes.append(f"Resolved Ubuntu snapshot/base package cohort at {snapshot}{extra}.")
            resolved_base = True
            break
        except ResolveError as exc:
            latest_reason = str(exc)
    if not resolved_base:
        if snapshots:
            notes.append(
                f"Retained Ubuntu snapshot/base cohort after {len(snapshots)} daily candidates: {latest_reason}."
            )
        else:
            notes.append(f"Retained Ubuntu snapshot/base cohort: {latest_reason}.")

    groups = [
        ("GitHub CLI", "apt-gh.lock",
         "deb [arch=amd64 signed-by=@KEY@] https://cli.github.com/packages stable main\n",
         "GH", False),
        ("Docker apt", "apt-docker.lock",
         "deb [arch=amd64 signed-by=@KEY@] https://download.docker.com/linux/ubuntu noble stable\n",
         "DOCKER", True),
        ("NVIDIA container apt", "apt-nvidia.lock",
         "deb [arch=amd64 signed-by=@KEY@] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /\n",
         "NVIDIA", True),
    ]
    for label, filename, source_text, key_prefix, armored in groups:
        path = root / "system" / filename
        try:
            entries = lock_entries(staged.get(str(path), path.read_text(encoding="utf-8")))
            packages = [name for name, _ in entries]
            material, armored = apt_key(pins, key_prefix)
            candidates = isolated_apt_candidates(source_text, packages, material, armored)
            staged[str(path)] = render_lock(entries, candidates)
            notes.append(f"Resolved {label} package cohort ({len(packages)} exact packages).")
        except ResolveError as exc:
            notes.append(f"Retained {label} cohort: {exc}.")

def latest_release(repo: str) -> tuple[str, dict[str, Any]]:
    data = request_json(f"{API}/repos/{repo}/releases/latest")
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise ResolveError("no stable published release")
    tag = str(data.get("tag_name", ""))
    version = tag[1:] if tag.startswith("v") else tag
    if not stable_version(version):
        raise ResolveError("latest release tag is not a stable exact version")
    return version, data


def release_asset(release: dict[str, Any], name: str) -> tuple[str, bytes]:
    assets = release.get("assets", [])
    asset = next((item for item in assets if item.get("name") == name), None)
    if not asset or not asset.get("browser_download_url"):
        raise ResolveError("expected official release artifact is unavailable")
    return asset["browser_download_url"], request_bytes(asset["browser_download_url"])


def update_release_pin(pins_text: str, pins: dict[str, str], notes: list[str], *,
                       label: str, repo: str, version_key: str, url_key: str, sha_key: str,
                       asset_name: Any) -> tuple[str, dict[str, str]]:
    try:
        version, release = latest_release(repo)
        if not version_is_newer(version, pins[version_key]):
            notes.append(f"Retained {label} pin; upstream is equal to or older than the committed version.")
            return pins_text, pins
        url, artifact = release_asset(release, asset_name(version))
        digest = hashlib.sha256(artifact).hexdigest()
        updated = pins_text
        updated = replace_pin(updated, version_key, version)
        updated = replace_pin(updated, url_key, url)
        updated = replace_pin(updated, sha_key, digest)
        pins.update({version_key: version, url_key: url, sha_key: digest})
        notes.append(f"Updated {label} to {version}; SHA-256 was calculated from its official release artifact.")
        return updated, pins
    except (ResolveError, KeyError) as exc:
        notes.append(f"Retained {label} pin: {exc}.")
        return pins_text, pins


def update_version_only(pins_text: str, pins: dict[str, str], key: str, version: str,
                        label: str, notes: list[str]) -> tuple[str, dict[str, str]]:
    if not version_is_newer(version, pins[key]):
        notes.append(f"Retained {label} pin; upstream is equal to or older than the committed version.")
        return pins_text, pins
    pins_text = replace_pin(pins_text, key, version)
    pins[key] = version
    notes.append(f"Updated {label} to {version}.")
    return pins_text, pins


def latest_node_24() -> str:
    records = request_json("https://nodejs.org/dist/index.json")
    versions = []
    for item in records:
        version = str(item.get("version", ""))
        if re.fullmatch(r"v24\.\d+\.\d+", version) and item.get("lts"):
            versions.append(version[1:])
    if not versions:
        raise ResolveError("Node 24 LTS release metadata unavailable")
    version = max(versions, key=lambda value: tuple(map(int, value.split("."))))
    sums = request_bytes(f"https://nodejs.org/dist/v{version}/SHASUMS256.txt",
                         limit=2 * 1024 * 1024).decode()
    artifact = f"node-v{version}-linux-x64.tar.xz"
    if not any(line.split()[-1:] == [artifact] and re.fullmatch(r"[0-9a-f]{64}", line.split()[0])
               for line in sums.splitlines() if len(line.split()) >= 2):
        raise ResolveError("official Node 24 archive checksum is unavailable")
    return version


def latest_python_312() -> str:
    listing = request_bytes("https://www.python.org/ftp/python/", limit=4 * 1024 * 1024).decode()
    versions = set(re.findall(r'href=["\'](3\.12\.\d+)/["\']', listing))
    if not versions:
        raise ResolveError("Python 3.12 release listing unavailable")
    return max(versions, key=lambda value: tuple(map(int, value.split("."))))


def npm_latest(name: str) -> str:
    encoded = name.replace("/", "%2f")
    metadata = request_json(f"https://registry.npmjs.org/{encoded}")
    version = str(metadata.get("dist-tags", {}).get("latest", ""))
    if not stable_version(version):
        raise ResolveError("npm latest tag is not a stable exact version")
    return version


def npm_lock_has_full_integrity(lock: dict[str, Any]) -> bool:
    if lock.get("lockfileVersion") != 2 or not isinstance(lock.get("packages"), dict):
        return False
    packages = lock["packages"]
    if "" not in packages:
        return False
    for path, item in packages.items():
        if path == "" or item.get("link"):
            continue
        if not item.get("integrity") or not item.get("resolved"):
            return False
    return True


def update_npm(root: Path, staged: dict[str, str], notes: list[str]) -> None:
    package_path = root / "home/dot_local/share/bootstrap/npm/package.json"
    lock_path = root / "home/dot_local/share/bootstrap/npm/package-lock.json"
    package_text = staged.get(str(package_path), package_path.read_text(encoding="utf-8"))
    lock_text = staged.get(str(lock_path), lock_path.read_text(encoding="utf-8"))
    try:
        package = json.loads(package_text)
        deps = package["dependencies"]
        candidates = {name: npm_latest(name) for name in ("@openai/codex", "ccusage")}
        for name, candidate in candidates.items():
            if version_is_newer(candidate, deps[name]):
                deps[name] = candidate
                notes.append(f"Updated npm tool {name} to {candidate}.")
            elif candidate == deps[name]:
                notes.append(f"Current npm tool {name} pin is latest stable.")
            else:
                notes.append(f"Retained npm tool {name}; registry latest tag is older than the committed version.")
        new_package_text = json.dumps(package, indent=2) + "\n"
        with tempfile.TemporaryDirectory(prefix="bootstrap-npm-lock-") as temp_name:
            temp = Path(temp_name)
            (temp / "package.json").write_text(new_package_text, encoding="utf-8")
            (temp / "package-lock.json").write_text(lock_text, encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
            try:
                generated = subprocess.run(
                    ["npm", "install", "--package-lock-only", "--lockfile-version=2",
                     "--ignore-scripts", "--no-audit", "--no-fund"],
                    cwd=temp, check=False, capture_output=True, text=True, timeout=900, env=env
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ResolveError("npm lock generation failed or timed out") from None
            if generated.returncode:
                raise ResolveError("npm lock generation failed")
            lock_result = json.loads((temp / "package-lock.json").read_text(encoding="utf-8"))
            if not npm_lock_has_full_integrity(lock_result):
                raise ResolveError("generated lock lacks complete package integrity metadata")
            try:
                installed = subprocess.run(
                    ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                    cwd=temp, check=False, capture_output=True, text=True, timeout=1200, env=env
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ResolveError("npm ci validation failed or timed out") from None
            if installed.returncode:
                raise ResolveError("npm ci validation failed")
            staged[str(package_path)] = new_package_text
            staged[str(lock_path)] = json.dumps(lock_result, indent=2) + "\n"
            notes.append("Regenerated and verified the complete npm v2 lock with npm ci.")
    except (ResolveError, KeyError, ValueError, json.JSONDecodeError) as exc:
        notes.append(f"Retained the complete npm cohort: {exc}.")


def staged_text(path: Path, staged: dict[str, str]) -> str:
    return staged.get(str(path), path.read_text(encoding="utf-8"))


def update_mise_config(root: Path, pins: dict[str, str], staged: dict[str, str]) -> None:
    path = root / "home/dot_config/mise/config.toml"
    text = staged_text(path, staged)
    for tool, key in (("node", "NODE_VERSION"), ("python", "PYTHON_VERSION"), ("uv", "UV_VERSION")):
        pattern = re.compile(rf'^{tool}\s*=\s*"[^"]*"$', re.MULTILINE)
        text, count = pattern.subn(f'{tool} = "{pins[key]}"', text)
        if count != 1:
            raise ResolveError(f"mise config must contain one exact {tool} pin")
    staged[str(path)] = text


def update(root: Path, dry_run: bool) -> str:
    pins_path = root / "home/.chezmoitemplates/pins.env"
    original_files: dict[str, str] = {}
    staged: dict[str, str] = {}
    pins_text = pins_path.read_text(encoding="utf-8")
    original_files[str(pins_path)] = pins_text
    pins = parse_pins(pins_text)
    notes: list[str] = []
    now = dt.datetime.now(dt.timezone.utc)

    resolve_apt(root, pins_text, pins, staged, notes, now)
    pins_text = staged_text(pins_path, staged)
    pins = parse_pins(pins_text)

    specs = [
        ("chezmoi", "twpayne/chezmoi", "CHEZMOI_VERSION", "CHEZMOI_URL", "CHEZMOI_SHA256",
         lambda v: f"chezmoi_{v}_linux_amd64.tar.gz"),
        ("mise", "jdx/mise", "MISE_VERSION", "MISE_URL", "MISE_SHA256",
         lambda v: f"mise-v{v}-linux-x64.tar.xz"),
        ("BWS", "bitwarden/sdk-sm", "BWS_VERSION", "BWS_URL", "BWS_SHA256",
         lambda v: f"bws-x86_64-unknown-linux-gnu-{v}.zip"),
        ("SecretSpec", "cachix/secretspec", "SECRETSPEC_VERSION", "SECRETSPEC_URL", "SECRETSPEC_SHA256",
         lambda v: f"secretspec-x86_64-unknown-linux-musl.tar.xz"),
        ("Gitleaks", "gitleaks/gitleaks", "GITLEAKS_VERSION", "GITLEAKS_URL", "GITLEAKS_SHA256",
         lambda v: f"gitleaks_{v}_linux_x64.tar.gz"),
        ("ShellCheck", "koalaman/shellcheck", "SHELLCHECK_VERSION", "SHELLCHECK_URL", "SHELLCHECK_SHA256",
         lambda v: f"shellcheck-v{v}.linux.x86_64.tar.xz"),
    ]
    for label, repo, version_key, url_key, sha_key, asset in specs:
        pins_text, pins = update_release_pin(
            pins_text, pins, notes, label=label, repo=repo, version_key=version_key,
            url_key=url_key, sha_key=sha_key, asset_name=asset
        )
    staged[str(pins_path)] = pins_text

    try:
        node = latest_node_24()
        pins_text, pins = update_version_only(pins_text, pins, "NODE_VERSION", node, "Node 24 LTS", notes)
    except ResolveError as exc:
        notes.append(f"Retained Node 24 LTS pin: {exc}.")
    try:
        python = latest_python_312()
        pins_text, pins = update_version_only(pins_text, pins, "PYTHON_VERSION", python, "Python 3.12", notes)
    except ResolveError as exc:
        notes.append(f"Retained Python 3.12 pin: {exc}.")

    try:
        version, release = latest_release("astral-sh/uv")
        if version != pins["UV_VERSION"]:
            # Confirm the corresponding official Linux artifact exists and can be
            # fetched before changing the version consumed by mise.
            release_asset(release, "uv-x86_64-unknown-linux-gnu.tar.gz")
            pins_text, pins = update_version_only(pins_text, pins, "UV_VERSION", version, "uv", notes)
        else:
            notes.append("Current uv pin is the latest stable release.")
    except (ResolveError, KeyError) as exc:
        notes.append(f"Retained uv pin: {exc}.")

    staged[str(pins_path)] = pins_text
    try:
        update_npm(root, staged, notes)
    finally:
        # Keep top-level npm versions and their duplicate environment pins aligned.
        try:
            package_path = root / "home/dot_local/share/bootstrap/npm/package.json"
            package = json.loads(staged_text(package_path, staged))
            current_pins = parse_pins(staged_text(pins_path, staged))
            for name, key in (("@openai/codex", "CODEX_VERSION"), ("ccusage", "CCUSAGE_VERSION")):
                if package["dependencies"][name] != current_pins[key]:
                    pins_text = replace_pin(pins_text, key, package["dependencies"][name])
            staged[str(pins_path)] = pins_text
        except (KeyError, ValueError, json.JSONDecodeError):
            pass

    pins = parse_pins(staged_text(pins_path, staged))
    notes.append("Retained Claude Code version and installer hash: the installer endpoint is moving and no stable version-to-artifact checksum mapping is available.")
    notes.append("Retained apt signing-key hashes; key rotation requires a separately reviewed official-key change.")
    update_mise_config(root, pins, staged)

    changed = [
        Path(path).relative_to(root).as_posix()
        for path, content in staged.items()
        if Path(path).read_text(encoding="utf-8") != content
    ]
    if not dry_run:
        for path_string, content in staged.items():
            Path(path_string).write_text(content, encoding="utf-8")
    action = "Would update" if dry_run else "Updated"
    summary = [f"Monthly pin resolver {'preview' if dry_run else 'result'}", ""]
    summary.extend(f"- {note}" for note in notes)
    summary.append("")
    summary.append(f"{action} {len(changed)} file(s): " + (", ".join(changed) if changed else "no changes"))
    return "\n".join(summary) + "\n"


def append_report(report: str, path_value: str | None) -> None:
    if path_value:
        Path(path_value).write_text(report, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(report + "\n")
    print(report)


def api_request(repository: str, path: str, method: str = "GET",
                payload: dict[str, Any] | None = None) -> Any:
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ResolveError("built-in Actions token is unavailable")
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{API}/repos/{repository}/{path.lstrip('/')}",
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bootstrap-monthly-pin-review",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024)
    except HTTPError as exc:
        raise ResolveError(f"GitHub API request failed (HTTP {exc.code})") from None
    except (URLError, TimeoutError, OSError):
        raise ResolveError("GitHub API request failed") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ResolveError("GitHub API returned invalid JSON") from None


def find_monthly_pr(repository: str) -> dict[str, Any] | None:
    owner = repository.split("/", 1)[0]
    for page in range(1, 4):
        pulls = api_request(
            repository,
            "pulls?" + urlencode({"state": "open", "base": "main", "per_page": 100, "page": page})
        )
        for pr in pulls:
            head = pr.get("head", {})
            if (head.get("ref", "").startswith(MONTHLY_BRANCH_PREFIX)
                    and head.get("user", {}).get("login", "").lower() == owner.lower()):
                return pr
        if len(pulls) < 100:
            break
    return None


def check_pending(repository: str, output_path: str | None) -> bool:
    pr = find_monthly_pr(repository)
    if output_path:
        with open(output_path, "a", encoding="utf-8") as stream:
            stream.write(f"pending={'true' if pr else 'false'}\n")
    if pr:
        message = f"An earlier monthly pin update is still open: [PR #{pr['number']}]({pr['html_url']}). Skipping this run to avoid duplicate pin reviews."
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(message + "\n")
        print(message)
    return pr is not None


def publish_pr(repository: str, branch: str, head_sha: str, report_path: str) -> str:
    report = Path(report_path).read_text(encoding="utf-8")
    pr_body = (
        "Monthly exact pin refresh from official package and release metadata.\n\n"
        "The updater retains a component when it cannot resolve its complete safe cohort. "
        "Review retained components and candidate changes below.\n\n"
        + report
        + "\nCI status: awaiting the existing gate.yml workflow dispatched for this exact head."
    )
    existing = find_monthly_pr(repository)
    if existing and existing["head"]["ref"] == branch:
        pr = api_request(repository, f"pulls/{existing['number']}", "PATCH", {"body": pr_body})
    else:
        pr = api_request(repository, "pulls", "POST", {
            "title": "chore: refresh monthly bootstrap pins",
            "body": pr_body,
            "head": branch,
            "base": "main",
            "draft": False,
        })
    number = pr.get("number")
    if not number:
        raise ResolveError("GitHub did not return the monthly pin PR")
    pr_url = pr.get("html_url", "")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = api_request(repository, f"pulls/{number}")
        if current.get("head", {}).get("sha") == head_sha:
            break
        time.sleep(5)
    else:
        raise ResolveError("monthly PR head did not match the generated commit")
    try:
        gate_url, conclusion = dispatch_gate(repository, branch, head_sha)
    except ResolveError as exc:
        api_request(repository, f"pulls/{number}", "PATCH", {
            "body": pr_body + f"\n\nRequired gate could not be verified for {head_sha}: {exc}"
        })
        raise
    current = api_request(repository, f"pulls/{number}")
    if current.get("head", {}).get("sha") != head_sha:
        raise ResolveError("monthly PR head changed while its required gate was running")
    status = "passed" if conclusion == "success" else conclusion
    body = (
        "Monthly exact pin refresh from official package and release metadata.\n\n"
        "The updater retains a component when it cannot resolve its complete safe cohort. "
        "Review retained components and candidate changes below.\n\n"
        + report
        + f"\nExisting required CI gate: [{status}]({gate_url}) for {head_sha}."
    )
    api_request(repository, f"pulls/{number}", "PATCH", {"body": body})
    if conclusion != "success":
        raise ResolveError(f"required gate did not pass: {gate_url}")
    result = f"Opened or updated [PR #{number}]({pr_url}); exact-head gate passed: [{gate_url}]({gate_url})."
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(result + "\n")
    return result


def dispatch_gate(repository: str, branch: str, head_sha: str,
                  timeout_seconds: int = 2700) -> tuple[str, str]:
    workflow = api_request(repository, "actions/workflows/gate.yml")
    workflow_id = workflow.get("id")
    if not workflow_id:
        raise ResolveError("existing gate workflow could not be identified")
    ref = api_request(repository, "git/ref/heads/" + branch)
    if ref.get("object", {}).get("sha") != head_sha:
        raise ResolveError("branch head changed before the gate dispatch")
    query = urlencode({"event": "workflow_dispatch", "branch": branch, "per_page": 100})
    before = api_request(repository, f"actions/workflows/{workflow_id}/runs?{query}")
    prior_run_ids = {item.get("id") for item in before.get("workflow_runs", [])}
    dispatched_at = dt.datetime.now(dt.timezone.utc)
    api_request(repository, f"actions/workflows/{workflow_id}/dispatches", "POST", {"ref": branch})
    deadline = time.monotonic() + timeout_seconds
    run: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        result = api_request(repository, f"actions/workflows/{workflow_id}/runs?{query}")
        run = select_gate_run(
            result.get("workflow_runs", []), workflow_id, branch, head_sha, dispatched_at, prior_run_ids
        )
        if run and run.get("status") == "completed":
            final_ref = api_request(repository, "git/ref/heads/" + branch)
            if final_ref.get("object", {}).get("sha") != head_sha:
                raise ResolveError("branch head changed while its required gate was running")
            return str(run.get("html_url", "")), str(run.get("conclusion", "unknown"))
        time.sleep(15)
    if run:
        return str(run.get("html_url", "")), "timed out"
    raise ResolveError("no exact-head workflow_dispatch run appeared before timeout")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    update_parser = sub.add_parser("update")
    update_parser.add_argument("--root", type=Path, default=ROOT)
    update_parser.add_argument("--dry-run", action="store_true")
    update_parser.add_argument("--report")
    pending_parser = sub.add_parser("check-pending")
    pending_parser.add_argument("--repository", required=True)
    pending_parser.add_argument("--output")
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--repository", required=True)
    publish_parser.add_argument("--branch", required=True)
    publish_parser.add_argument("--head-sha", required=True)
    publish_parser.add_argument("--report", required=True)
    args = parser.parse_args()

    try:
        if args.command == "update":
            report = update(args.root.resolve(), args.dry_run)
            append_report(report, args.report)
            return 0
        if args.command == "check-pending":
            check_pending(args.repository, args.output)
            return 0
        if args.command == "publish":
            print(publish_pr(args.repository, args.branch, args.head_sha, args.report))
            return 0
    except ResolveError as exc:
        print(f"Monthly pin workflow stopped: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
