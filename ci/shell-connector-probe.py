#!/usr/bin/env python3
"""Speak MCP over stdio to the AgentDev shell connector: list its tools and run `echo ok`.

Usage: shell-connector-probe.py [launcher] (default ~/.local/bin/agentdev-shell-mcp).
Prints the tool count and the command output; exits non-zero unless `echo ok` ran as agent.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

launcher = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".local/bin/agentdev-shell-mcp")
user = os.environ.get("PROBE_USER", "agent")
server = subprocess.Popen(["bash", launcher], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, bufsize=1)


def send(message):
    server.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    server.stdin.flush()


def response(request_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([server.stdout], [], [], deadline - time.monotonic())
        if not ready:
            break
        line = server.stdout.readline()
        if not line:
            break
        message = json.loads(line)  # anything else on stdout breaks MCP, so let it fail
        if message.get("id") == request_id:
            if "error" in message:
                raise SystemExit(f"FAIL: request {request_id}: {message['error']}")
            return message["result"]
    raise SystemExit(f"FAIL: no response to request {request_id}; stderr: {server.stderr.read()[-2000:]}")


try:
    send({"id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "shell-connector-probe", "version": "1"}}})
    info = response(1)["serverInfo"]
    send({"method": "notifications/initialized"})
    send({"id": 2, "method": "tools/list"})
    tools = {tool["name"] for tool in response(2)["tools"]}
    for name in ("start_process", "read_file", "list_directory"):
        if name not in tools:
            raise SystemExit(f"FAIL: tool {name} missing from {sorted(tools)}")
    send({"id": 3, "method": "tools/call", "params": {"name": "start_process", "arguments": {
        # One write: the server may return after the first output chunk, so `echo ok; id -un`
        # (two writes) failed about 2 runs in 5.
        "command": "printf 'ok\\n%s\\n' \"$(id -un)\"", "timeout_ms": 10000}}})
    text = "\n".join(part.get("text", "") for part in response(3)["content"])
    lines = [line.strip() for line in text.splitlines()]
    if "ok" not in lines or user not in lines:
        raise SystemExit(f"FAIL: expected 'ok' and '{user}' in the output, got: {text!r}")
    print(f"PASS: {info['name']} {info['version']} lists {len(tools)} tools and ran `echo ok` as {user}.")
finally:
    # MCP stdio shutdown: close stdin, then SIGTERM (the server does not exit on EOF alone).
    server.stdin.close()
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
