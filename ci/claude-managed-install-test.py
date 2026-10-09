#!/usr/bin/env python3
"""Run as agent after install.sh: the managed guardrails are root-owned and agent-immutable.

Covers the Claude Code drop-ins (/etc/claude-code/managed-settings.d), the Codex requirements
(/etc/codex/requirements.toml), the scripts and binaries the managed hooks run, and the
deployed hook itself. The founder can run it as agent to verify an install.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import tomllib

assert os.getuid() != 0, "run as the agent account"
CLAUDE_DIR = Path("/etc/claude-code")
DROPINS = CLAUDE_DIR / "managed-settings.d"
CODEX_DIR = Path("/etc/codex")
OPT = Path("/opt/machine-bootstrap/current")
FILES = {
    DROPINS / "10-agent-guardrails.json": OPT / "system/claude-managed-guardrails.json",
    DROPINS / "50-managed-mods-only.json": OPT / "system/claude-managed-mods.json",
    CODEX_DIR / "requirements.toml": OPT / "system/codex-requirements.toml",
}

for path, mode in ((CLAUDE_DIR, 0o755), (DROPINS, 0o755), (CODEX_DIR, 0o755),
                   *((path, 0o644) for path in FILES)):
    info = path.lstat()
    assert not stat.S_ISLNK(info.st_mode), path
    assert info.st_uid == 0 and info.st_gid == 0, path
    assert stat.S_IMODE(info.st_mode) == mode, (path, oct(stat.S_IMODE(info.st_mode)))
for path, source in FILES.items():
    assert path.read_bytes() == source.read_bytes(), path
assert json.loads((DROPINS / "50-managed-mods-only.json").read_text(encoding="utf-8"))["pluginConfigs"][
    "cc-plugin-sec-default@builtin"]["options"]["allowManagedModsOnly"] is True


def root_owned_chain(path: Path) -> None:
    """The file and every directory above it: owned by root, writable by nobody else."""
    path = path.resolve(strict=True)
    for part in (path, *path.parents):
        info = part.stat()
        assert info.st_uid == 0 and not info.st_mode & 0o022, (part, oct(info.st_mode))


managed = json.loads((DROPINS / "10-agent-guardrails.json").read_text(encoding="utf-8"))
requirements = tomllib.loads((CODEX_DIR / "requirements.toml").read_text(encoding="utf-8"))
assert requirements["hooks"]["managed_dir"] == "/usr/local/lib/agent-policy"
argvs = [[h["command"], *h["args"]] for groups in managed["hooks"].values()
         for group in groups for h in group["hooks"]]
argvs += [h["command"].split(" ") for event, groups in requirements["hooks"].items()
          if event != "managed_dir" for group in groups for h in group["hooks"]]
assert argvs and all(argv[:8] == ["/usr/bin/env", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH", "PATH=/usr/local/bin:/usr/bin:/bin",
                                  "/usr/bin/python3", "-I"] for argv in argvs), argvs
for path in {*(Path(argv[8]) for argv in argvs), Path("/usr/bin/env"), Path("/usr/bin/python3"),
             Path("/usr/bin/git"), Path("/usr/local/bin/gitleaks")}:
    root_owned_chain(path)
for argv in argvs:
    path = Path(argv[8])
    assert path.parent == Path("/usr/local/lib/agent-policy"), path
    assert path.read_bytes() == (OPT / "system/agent-policy" / path.name).read_bytes()
    info = path.lstat()
    assert not stat.S_ISLNK(info.st_mode) and info.st_gid == 0
    assert stat.S_IMODE(info.st_mode) == 0o644
assert stat.S_IMODE(Path("/usr/local/lib/agent-policy").stat().st_mode) == 0o755


def refused(action) -> bool:
    try:
        action()
    except PermissionError:
        return True
    return False


for path in FILES:
    assert refused(lambda: path.open("a")), f"agent could append to {path}"
    assert refused(lambda: path.rename(path.with_name("moved"))), f"agent could rename {path}"
    assert refused(lambda: path.unlink()), f"agent could delete {path}"
for path in (DROPINS / "99-agent.json", CLAUDE_DIR / "managed-settings.json",
             CODEX_DIR / "managed_config.toml", CODEX_DIR / "config.toml"):
    assert refused(lambda: path.write_text("{}")), f"agent could create {path}"
assert refused(lambda: (CODEX_DIR / "rules").mkdir()), "agent could add system Codex rules"
for argv in argvs:
    assert refused(lambda: Path(argv[8]).open("a")), f"agent could edit {argv[8]}"

# One source of truth: chezmoi removed the agent's old copies and user-level guardrails.
home = Path.home()
for stale in (home / ".codex/hooks.json", home / ".local/share/bootstrap/agent-policy/pre_tool_use.py",
              home / ".local/share/bootstrap/agent-policy/context_reminder.py"):
    assert not stale.exists(), stale
user = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
assert "deny" not in user["permissions"] and "hooks" not in user

# The deployed managed hook, run exactly as configured, still denies blocked commands.
pre_tool_use = next(argv for argv in argvs if argv[8].endswith("/pre_tool_use.py"))
env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
with tempfile.TemporaryDirectory() as cwd:
    for command in ("wsl.exe -d AgentDev", "sudo true"):
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
        result = subprocess.run(pre_tool_use, input=json.dumps(event), capture_output=True,
                                text=True, cwd=cwd, env=env, check=True, timeout=30)
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", command
print("managed guardrails are root-owned and agent-immutable, and the managed hook still denies")
