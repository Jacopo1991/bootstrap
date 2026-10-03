#!/usr/bin/env python3
"""Run as root after install.sh: the inventory timer and its copy are correct."""
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile

assert os.getuid() == 0, "run as root"
UNIT_DIR = Path("/etc/systemd/system")
SERVICE = UNIT_DIR / "inventory-mirror.service"
TIMER = UNIT_DIR / "inventory-mirror.timer"
SCRIPT = Path("/opt/machine-bootstrap/current/system/inventory-mirror.py")
PROJECT_DATA = Path("/home/agent/project-data")
LATEST = PROJECT_DATA / "inventory/latest.json"
agent = pwd.getpwnam("agent")
SNAPSHOT = b'{"schemaVersion":2,"createdAtISO":"2026-10-03T02:30:00+02:00","distros":[]}'


def owned(path, uid, gid, mode):
    info = path.lstat()
    assert not stat.S_ISLNK(info.st_mode), path
    assert (info.st_uid, info.st_gid) == (uid, gid), (path, info.st_uid, info.st_gid)
    assert stat.S_IMODE(info.st_mode) == mode, (path, oct(stat.S_IMODE(info.st_mode)))


def run_script(*args):
    return subprocess.run(["/usr/bin/python3", str(SCRIPT), *args], capture_output=True, text=True)


# The timer and service are root-owned, installed, and enabled.
owned(SERVICE, 0, 0, 0o644)
owned(TIMER, 0, 0, 0o644)
info = SCRIPT.stat()
assert info.st_uid == 0 and not info.st_mode & 0o022, "copier must not be writable by the agent"
for source, installed in (("inventory-mirror.service", SERVICE), ("inventory-mirror.timer", TIMER)):
    assert installed.read_bytes() == Path("/opt/machine-bootstrap/current/system", source).read_bytes(), source
service = SERVICE.read_text(encoding="utf-8")
timer = TIMER.read_text(encoding="utf-8")
for line in ("User=root", "PrivateMounts=yes", "NoNewPrivileges=yes", "Type=oneshot",
             "ExecStart=/usr/bin/python3 /opt/machine-bootstrap/current/system/inventory-mirror.py"):
    assert line in service.splitlines(), line
for line in ("OnBootSec=2min", "OnUnitActiveSec=15min", "WantedBy=timers.target"):
    assert line in timer.splitlines(), line
assert "/mnt/" not in service and "wsl" not in service.lower(), "no automount or Windows launcher"
wants = Path("/etc/systemd/system/timers.target.wants/inventory-mirror.timer")
assert wants.is_symlink(), "timer is not enabled"
enabled = subprocess.run(["systemctl", "--root=/", "is-enabled", "inventory-mirror.timer"],
                         capture_output=True, text=True)
assert enabled.stdout.strip() == "enabled", enabled
if shutil.which("systemd-analyze"):
    verify = subprocess.run(["systemd-analyze", "verify", str(SERVICE), str(TIMER)],
                            capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr
else:
    print("SKIP: systemd-analyze verify (not installed)")

# Mounting Windows files in the shared namespace is refused before any mount.
refused = run_script()
assert refused.returncode == 1 and "shared mount namespace" in refused.stderr, refused

existed = PROJECT_DATA.exists()
try:
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "latest.json"
        source.write_bytes(SNAPSHOT)
        for round_ in range(2):  # the second run replaces the first atomically
            copied = run_script("--source-file", str(source))
            assert copied.returncode == 0 and "copied" in copied.stdout, copied
            owned(PROJECT_DATA, agent.pw_uid, agent.pw_gid, 0o700)
            owned(LATEST.parent, agent.pw_uid, agent.pw_gid, 0o700)
            owned(LATEST, agent.pw_uid, agent.pw_gid, 0o600)
            assert LATEST.read_bytes() == SNAPSHOT
            assert [p.name for p in LATEST.parent.iterdir()] == ["latest.json"]
            source.write_bytes(SNAPSHOT.replace(b"02:30", b"03:30"))
        # The agent can read its own copy, and it is the only account that can.
        read = subprocess.run(["sudo", "-H", "-u", "agent", "cat", str(LATEST)], capture_output=True)
        assert read.returncode == 0 and json.loads(read.stdout)["schemaVersion"] == 2

        # A symlink, hardlink or FIFO as the source or the destination is refused.
        real = Path(temp) / "real.json"
        real.write_bytes(SNAPSHOT)
        before = LATEST.read_bytes()
        for plant in (lambda p: p.symlink_to(real), lambda p: os.link(real, p),
                      lambda p: os.mkfifo(p, 0o600)):
            source.unlink()
            plant(source)
            result = run_script("--source-file", str(source))
            assert result.returncode == 1 and "source" in result.stderr, result
            assert LATEST.read_bytes() == before
            source.unlink()
            source.write_bytes(SNAPSHOT)
            LATEST.unlink()
            plant(LATEST)
            result = run_script("--source-file", str(source))
            assert result.returncode == 1 and "latest.json" in result.stderr, result
            assert real.read_bytes() == SNAPSHOT
            assert [p.name for p in LATEST.parent.iterdir()] == ["latest.json"]
            LATEST.unlink()
            assert run_script("--source-file", str(source)).returncode == 0
            owned(LATEST, agent.pw_uid, agent.pw_gid, 0o600)
            before = LATEST.read_bytes()
finally:
    if not existed:
        shutil.rmtree(PROJECT_DATA, ignore_errors=True)
print("inventory timer is root-owned and enabled; copy is agent-owned 0600, atomic, and refuses links")
