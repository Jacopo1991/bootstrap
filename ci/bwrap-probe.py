#!/usr/bin/env python3
"""Probe whether a nested no-network bwrap works from the current sandbox.

Runs `bwrap --unshare-net --ro-bind / / true` and reports the exact failure, the process
facts that explain it (seccomp mode, a NETLINK_ROUTE socket), and a workaround that creates
the network namespace without bwrap's loopback setup, with proof it has no network.
Run it directly for a Claude lane, or as `codex sandbox python3 -I ci/bwrap-probe.py` for
Codex's workspace-write sandbox. Exit 0 when the bwrap probe worked, 1 when it failed.
"""
import errno
import os
import re
import socket
import subprocess
import sys

BWRAP = ["bwrap", "--unshare-net", "--ro-bind", "/", "/", "true"]
# The namespace comes from unshare(2), which opens no socket; bwrap then needs no --unshare-net.
UNSHARED = ["unshare", "--user", "--map-root-user", "--net"]


def status_field(name):
    with open("/proc/self/status", encoding="utf-8") as status:
        for line in status:
            if line.startswith(name + ":"):
                return line.split(":", 1)[1].strip()
    return "?"


def open_socket(family, kind, protocol=0):
    try:
        socket.socket(family, kind, protocol).close()
    except OSError as error:
        return f"denied: {errno.errorcode.get(error.errno, error.errno)} ({error.strerror})"
    return "ok"


def run(argv):
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout + done.stderr).strip()


def outcome(argv):
    code, text = run(argv)
    if code == 0:
        return f"ok {text}".strip()
    return f"rc={code}: {text}"


def isolation():
    """Run inside the unshared namespace: only a down loopback, no route, no connection."""
    with open("/proc/net/dev", encoding="utf-8") as devices:  # /sys/class/net shows the host's
        interfaces = sorted(line.split(":")[0].strip() for line in devices if ":" in line)
    reach = {}
    for label, address in (("127.0.0.1", "127.0.0.1"), ("1.1.1.1", "1.1.1.1")):
        try:
            socket.create_connection((address, 53), timeout=2).close()
            reach[label] = "CONNECTED"
        except OSError as error:
            reach[label] = errno.errorcode.get(error.errno, type(error).__name__)
    print(f"interfaces={interfaces} connect={reach}")
    return 1 if "CONNECTED" in reach.values() else 0


def main():
    if sys.argv[1:] == ["--isolation"]:
        return isolation()
    print(f"Seccomp: {status_field('Seccomp')}")
    print(f"NoNewPrivs: {status_field('NoNewPrivs')}")
    print(f"AF_INET socket: {open_socket(socket.AF_INET, socket.SOCK_DGRAM)}")
    print(f"AF_NETLINK/NETLINK_ROUTE socket: {open_socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)}")
    print(f"bwrap --unshare-user: {outcome(['bwrap', '--unshare-user', '--ro-bind', '/', '/', 'true'])}")
    print("probe: " + " ".join(BWRAP))
    code, text = run(BWRAP)
    if code == 0:
        print("RESULT: ok")
    else:
        print(f"RESULT: failed rc={code}: {text}")
        match = re.search(r"Failed to create (\w+) socket: (.+)$", text)
        if match:
            print(f"CAUSE: {match.group(1)} socket creation: {match.group(2)}")
    workaround = UNSHARED + ["bwrap", "--ro-bind", "/", "/", "true"]
    print("workaround: " + " ".join(workaround))
    print(f"WORKAROUND: {outcome(workaround)}")
    nested = UNSHARED + ["bwrap", "--ro-bind", "/", "/", sys.executable, "-I", os.path.abspath(__file__),
                         "--isolation"]
    print(f"WORKAROUND isolation: {outcome(nested)}")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
