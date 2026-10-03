#!/usr/bin/env python3
"""Run as agent after install.sh: managed drop-in is root-owned and not agent-writable."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

assert os.getuid() != 0, "run as the agent account"
CLAUDE_DIR = Path("/etc/claude-code")
DROPINS = CLAUDE_DIR / "managed-settings.d"
FILE = DROPINS / "50-managed-mods-only.json"
SOURCE = Path("/opt/machine-bootstrap/current/system/claude-managed-mods.json")

for path, mode in ((CLAUDE_DIR, 0o755), (DROPINS, 0o755), (FILE, 0o644)):
    info = path.lstat()
    assert not stat.S_ISLNK(info.st_mode), path
    assert info.st_uid == 0 and info.st_gid == 0, path
    assert stat.S_IMODE(info.st_mode) == mode, (path, oct(stat.S_IMODE(info.st_mode)))
assert FILE.read_bytes() == SOURCE.read_bytes()
assert json.loads(FILE.read_text(encoding="utf-8"))["pluginConfigs"][
    "cc-plugin-sec-default@builtin"]["options"]["allowManagedModsOnly"] is True


def refused(action) -> bool:
    try:
        action()
    except PermissionError:
        return True
    return False


assert refused(lambda: FILE.open("a")), "agent could append to the drop-in"
assert refused(lambda: (DROPINS / "99-agent.json").write_text("{}")), "agent could add a drop-in"
assert refused(lambda: FILE.rename(DROPINS / "50-moved.json")), "agent could rename the drop-in"
assert refused(lambda: FILE.unlink()), "agent could delete the drop-in"
assert refused(lambda: (CLAUDE_DIR / "managed-settings.json").write_text("{}")), \
    "agent could create managed-settings.json"

# The deployed policy hook still denies blocked commands for the agent.
hook = Path.home() / ".local/share/bootstrap/agent-policy/pre_tool_use.py"
with tempfile.TemporaryDirectory() as cwd:
    for command in ("wsl.exe -d AgentDev", "sudo true"):
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
        result = subprocess.run([sys.executable, str(hook)], input=json.dumps(event),
                                capture_output=True, text=True, cwd=cwd, check=True)
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", command
print("managed drop-in is root-owned, agent-immutable, and the hook still denies")
