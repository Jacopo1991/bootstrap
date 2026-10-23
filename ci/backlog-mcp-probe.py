#!/usr/bin/env python3
"""Speak MCP over stdio to the Backlog.md launcher: list its tools and run `task_list` (read-only).

Usage: backlog-mcp-probe.py [launcher] [repo] (default ~/.local/bin/agentdev-backlog-mcp and
cortex-kb-company). Prints the server version, the tool count and the size of the task list,
never its contents; exits non-zero unless every tool the Control Room page may call is listed
and `task_list` answers.
"""
import json
from pathlib import Path
import select
import subprocess
import sys
import time

# The Company Control Room page's capability manifest: the only tools it may call.
PAGE_TOOLS = ("task_view", "task_list", "task_search", "document_view")
launcher = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".local/bin/agentdev-backlog-mcp")
repo = sys.argv[2] if len(sys.argv) > 2 else "cortex-kb-company"
server = subprocess.Popen(["bash", launcher, repo], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
    server.terminate()
    raise SystemExit(f"FAIL: no response to request {request_id}; stderr: {server.stderr.read()[-2000:]}")


try:
    send({"id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "backlog-mcp-probe", "version": "1"}}})
    info = response(1)["serverInfo"]
    send({"method": "notifications/initialized"})
    send({"id": 2, "method": "tools/list"})
    tools = {tool["name"] for tool in response(2)["tools"]}
    missing = [name for name in PAGE_TOOLS if name not in tools]
    if missing:
        raise SystemExit(f"FAIL: tools {missing} missing from {sorted(tools)}")
    send({"id": 3, "method": "tools/call", "params": {"name": "task_list", "arguments": {}}})
    result = response(3)
    if result.get("isError"):
        raise SystemExit("FAIL: task_list returned an error")
    text = "\n".join(part.get("text", "") for part in result.get("content", []))
    print(f"PASS: {info['name']} {info['version']} for {repo} lists {len(tools)} tools; "
          f"task_list answered ({len(text.splitlines())} lines).")
finally:
    # MCP stdio shutdown: close stdin, then SIGTERM if the server does not exit on EOF.
    server.stdin.close()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
