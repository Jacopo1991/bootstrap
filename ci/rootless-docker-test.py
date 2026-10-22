#!/usr/bin/env python3
"""Offline checks for rootless Docker and Inspect AI: pins, hook denies, sandbox config, unit file.

Nothing here starts a daemon or a container. The boundary check's socket logic is in
ci/boundary-test.sh, the monthly bump of the rootless package in ci/monthly_pins_test.py.
Live probes (daemon start, lane-sandbox containment, Inspect run) are checks/docker-containment.py
and docs/rootless-docker.md, run by the PM after install.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
from unittest.mock import patch

from policy_test_env import isolate_policy_environment

isolate_policy_environment()
ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "home/dot_config/systemd/user/docker-rootless.service"
INSPECT = ROOT / "home/dot_local/share/bootstrap/inspect"


def read(path):
    return Path(ROOT / path).read_text(encoding="utf-8")


def lock(path):
    return dict(line.split("=", 1) for line in read(path).splitlines() if line and not line.startswith("#"))


# Pins: rootless extras on the engine's version; uidmap in the base lock; Inspect hash-locked.
docker = lock("system/apt-docker.lock")
assert docker["docker-ce-rootless-extras"] == docker["docker-ce"] == docker["docker-ce-cli"], docker
assert "uidmap" in lock("system/apt-base.lock")
assert (INSPECT / "requirements.in").read_text() == "inspect-ai==0.3.283\nopenai==3.26.0\n"
inspect_lock = (INSPECT / "requirements.lock").read_text()
assert "\ninspect-ai==0.3.283 \\\n" in inspect_lock
assert "\nopenai==3.26.0 \\\n" in inspect_lock
blocks = re.findall(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+ \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", inspect_lock, re.M)
assert len(blocks) == len(re.findall(r"^[A-Za-z0-9][A-Za-z0-9._-]*==", inspect_lock, re.M)) > 50
assert "home/dot_local/share/bootstrap/inspect/requirements.in" in inspect_lock.splitlines()[1] + inspect_lock.splitlines()[2]
tools = read("home/.chezmoitemplates/tools.sh")
for needle in ('uv pip sync --quiet --require-hashes --python "$inspect_root/venv/bin/python" "$inspect_root/requirements.lock"',
               'ln -sfn "$inspect_root/venv/bin/inspect" "$HOME/.local/bin/inspect"',
               "m.version(\"inspect-ai\")", '== "$inspect_expected" ]]', '"$inspect_root/venv/bin/python" -c \'import openai\''):
    assert needle in tools, needle
assert "inspect/requirements.lock" in read("home/run_onchange_after_10-tools.sh.tmpl")
print("PASS: rootless extras, uidmap and Inspect AI 0.3.283 are pinned; Inspect installs from its hashed lock")

# Step 8 stays the only place Docker packages are installed; nothing starts or enables root Docker.
step = read("system/gpu-docker.sh")
assert "--rootless-only" in step and "ensure_subid_range /etc/subuid agent --add-subuids" in step
assert re.search(r"if \(\( ! rootless_only \)\); then\n  apt_locked [^\n]*apt-nvidia\.lock\"\n  nvidia-ctk", step)
assert not re.search(r"systemctl (start|enable)\b", step)
assert "id -nG agent" in step and "== disabled" in step
for path in [*ROOT.glob("system/*.sh"), ROOT / "install.sh", *ROOT.glob("home/**/*.sh"), *ROOT.glob("home/**/*.tmpl")]:
    if path.name != "gpu-docker.sh":
        assert "apt-docker.lock" not in path.read_text(), path
print("PASS: gpu-docker.sh installs the Docker packages, rootless step skips the toolkit and never starts Docker")

# Subordinate ids: added only when missing, never changed.
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    (base / "bin").mkdir()
    (base / "bin/usermod").write_text(f'#!/bin/sh\necho "$@" >> {base}/usermod.log\n')
    (base / "bin/usermod").chmod(0o755)

    def ensure(content):
        subid = base / "subuid"
        if content is None:
            subid.unlink(missing_ok=True)
        else:
            subid.write_text(content)
        (base / "usermod.log").unlink(missing_ok=True)
        env = {"PATH": f"{base}/bin:/usr/bin:/bin", "HOME": temp}
        done = subprocess.run(["bash", "-c", 'source "$1"; ensure_subid_range "$2" agent --add-subuids',
                               "x", str(ROOT / "system/common.sh"), str(subid)],
                              env=env, capture_output=True, text=True)
        log = (base / "usermod.log")
        return done.returncode, log.read_text().strip() if log.exists() else ""

    assert ensure("agent:165536:65536\n") == (0, "")
    assert ensure("other:165536:65536\nagent2:1:1\n") == (0, "--add-subuids 231072-296607 agent")
    assert ensure(None) == (0, "--add-subuids 165536-231071 agent")
    assert ensure("agent:1000:100\n")[0] == 1
print("PASS: agent's subuid/subgid range is added only when missing")

# The unit: no [Install], so it cannot be enabled; the socket and data root are agent's own.
unit = UNIT.read_text()
assert not re.search(r"^\[Install\]|^(WantedBy|RequiredBy|Alias)=", unit, re.M)
assert re.search(r"^ExecStart=/usr/bin/dockerd-rootless\.sh .*--data-root=%h/.*--host=unix://%t/docker\.sock$", unit, re.M)
assert "/var/run/docker" not in unit and "/etc/docker" not in unit
# No wants/symlink entry for it next to the other units, and no script enables or links it.
assert [p.name for p in ROOT.glob("home/dot_config/systemd/user/**/*docker-rootless*")] == [UNIT.name]
for path in [*ROOT.glob("home/dot_config/systemd/user/**/*"), *ROOT.glob("home/**/*.sh"), *ROOT.glob("system/*.sh"), ROOT / "install.sh"]:
    if path.is_file() and path != UNIT:
        assert not re.search(r"(enable|link)[^\n]*docker-rootless", path.read_text()), path
print("PASS: docker-rootless.service has no [Install] section and nothing enables it")

# Lane sandboxes: no Unix-socket allowance anywhere, so the Claude and Codex sandboxes keep denying it.
claude = json.loads(read("home/dot_claude/settings.json"))
managed = json.loads(read("system/claude-managed-guardrails.json"))
codex_config = tomllib.loads(read("home/.chezmoitemplates/codex-config.toml"))
codex_required = tomllib.loads(read("system/codex-requirements.toml"))


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


for config in (claude, managed, codex_config, codex_required):
    names = set(all_keys(config))
    assert not {name for name in names if "unix" in name.lower() and "socket" in name.lower()}, names
    assert "docker" not in json.dumps(config).lower().replace("~/.docker/config.json", "")
print("PASS: no Claude or Codex sandbox setting allows Unix sockets or mentions Docker")

# Hook: lane sessions (TASK_WORKTREE) may not run container CLIs or set a daemon; others are unchanged.
spec = importlib.util.spec_from_file_location("policy", ROOT / "system/agent-policy/pre_tool_use.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    task = base / "task"
    task.mkdir()
    subprocess.run(["git", "init", "-q", str(task)], check=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def judge(command, lane):
        environment = dict(env, **({"TASK_WORKTREE": str(task)} if lane else {}))
        with patch.dict(os.environ, environment, clear=True):
            return policy.evaluate({"tool_name": "Bash", "cwd": str(task), "tool_input": {"command": command}}, (base,))

    for command in ("docker ps", "/usr/bin/docker run --rm alpine true", "podman ps", "nerdctl ps",
                    "docker-compose up", "timeout 5 docker ps", "env docker ps",
                    "DOCKER_HOST=unix:///run/user/1001/docker.sock ls",
                    "env DOCKER_HOST=unix:///run/user/1001/docker.sock ls", "DOCKER_CONTEXT=x ls",
                    "CONTAINER_HOST=unix:///x ls"):
        reason = judge(command, lane=True)
        assert reason and getattr(reason, "rule_id", "") in {"container.lane_boundary", "command.unsafe_environment"}, (command, reason)
    for command in ("docker ps", "DOCKER_HOST=unix:///x ls"):
        reason = judge(command, lane=False)
        assert getattr(reason, "rule_id", "") not in {"container.lane_boundary", "command.unsafe_environment"}, (command, reason)
    assert judge("ls", lane=True) is None
print("PASS: hook denies docker, podman, nerdctl and DOCKER_HOST settings in lane sessions only")

# The example task: no network, no mounts, nothing privileged. Flat YAML subset, parsed by hand.
compose = read("docs/inspect/compose.yaml")
service = re.search(r"^  default:\n((?:    .*\n|\n)+)", compose, re.M).group(1)
keys_found = set(re.findall(r"^    ([a-z_]+):", service, re.M))
allowed = {"image", "command", "init", "network_mode", "cap_drop", "security_opt", "pids_limit", "mem_limit"}
assert keys_found <= allowed, keys_found - allowed
assert re.search(r"^    network_mode: none$", service, re.M)
assert not re.search(r"privileged|volumes|ports|devices|cap_add|seccomp=unconfined|docker\.sock|pid:|ipc:|extra_hosts", compose.split("services:")[1])
assert "- ALL" in service and "no-new-privileges:true" in service
task_source = read("docs/inspect/no_network_probe.py")
assert 'sandbox=("docker", "compose.yaml")' in task_source and "mockllm/model" in task_source
assert not re.search(r"(?i)api[_-]?key|token|secret", task_source + compose)
print("PASS: Inspect example task runs in a container with network_mode none, no mounts, nothing privileged")
