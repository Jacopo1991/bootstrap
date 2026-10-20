#!/usr/bin/env python3
"""Lane skip checks always run; real Chromium and teardown checks are gate-only."""
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "home/dot_local/bin/executable_gate-browser"
env = dict(os.environ)
env.pop("TASK_GATE_RUN_ID", None)
lane = subprocess.run([sys.executable, str(HELPER)], env=env, capture_output=True, text=True)
assert lane.returncode == 77, lane
assert lane.stdout.strip() == "SKIP (lane: browser tests run in the trusted gate)"
print("PASS: lane skip (77), before any server or argument handling", flush=True)
if not os.environ.get("TASK_GATE_RUN_ID"):
    print("SKIP (lane: browser tests run in the trusted gate)")
    sys.exit(77)

base = int(os.environ["TASK_GATE_PORT_BASE"])
for offset, fail in [(10, False), (11, True)]:
    env = dict(os.environ)
    if fail:
        env["GATE_BROWSER_FIXTURE_FAIL"] = "1"
    result = subprocess.run([
        sys.executable, str(HELPER), "--port-offset", str(offset),
        "--server", "exec python3 ci/fixtures/gate-browser/server.py",
        "ci/fixtures/gate-browser/*.spec.cjs",
    ], cwd=ROOT, env=env, text=True, capture_output=True, timeout=160)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    assert result.returncode == (1 if fail else 0), result
    assert "local page uses the allocated port" in result.stdout, result.stdout
    # A closed port proves both successful and failing test runs stopped their server.
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", base + offset)) != 0
print("PASS: real Chromium, allocated ports, success/failure server teardown")
