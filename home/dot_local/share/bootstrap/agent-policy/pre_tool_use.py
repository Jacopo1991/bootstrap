#!/usr/bin/env python3
"""Fail-closed user-level agent hook for workspace boundaries and staged secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

HOST_NAMES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd.exe",
              "wsl", "wsl.exe", "wslconfig.exe", "diskpart.exe", "schtasks.exe"}
HOST_OPS = {"sudo", "su", "systemctl", "service", "apt", "apt-get", "dpkg",
            "mount", "umount", "shutdown", "reboot", "modprobe", "useradd",
            "adduser", "usermod", "passwd", "visudo"}
WINDOWS = re.compile(r"(?i)(?:[A-Z]:[\\/]|/mnt/[a-z](?:/|$)|\\\\[^\\]+\\|"
                     r"(?<![A-Za-z0-9_])(?:powershell(?:\.exe)?|pwsh(?:\.exe)?|"
                     r"cmd\.exe|wsl(?:\.exe)?|diskpart(?:\.exe)?|schtasks\.exe)(?![A-Za-z0-9_]))")
WRITERS = {"touch", "mkdir", "rmdir", "rm", "cp", "mv", "install", "ln", "tee",
           "truncate", "dd", "chmod", "chown"}


def root_for(cwd: str, approved_roots: tuple[Path, ...] | None = None) -> Path:
    here = Path(cwd).resolve()
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=3, check=True)
        root = Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        root = here
    approved = approved_roots or (
        Path("/home/agent/dev_workspace").resolve(),
        Path("/home/agent/cortex").resolve(),
    )
    if not any(root == base or base in root.parents for base in approved):
        return Path("/__bootstrap_outside_approved_roots__")
    return root


def inside(path_text: str, cwd: str, root: Path) -> bool:
    p = Path(path_text)
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        p.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def deny(message: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny", "permissionDecisionReason": message}}


def secret_scan(root: Path) -> str | None:
    try:
        diff = subprocess.run(["git", "diff", "--cached", "--no-ext-diff", "--unified=0"],
                              cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return "Cannot inspect staged changes; commit blocked."
    if not diff.stdout:
        return None
    scanner = shutil.which("gitleaks")
    if not scanner:
        return "Pinned secret scanner unavailable; commit blocked."
    try:
        result = subprocess.run([scanner, "stdin", "--no-banner", "--redact"],
                                cwd=root, input=diff.stdout, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "Secret scan failed; commit blocked."
    if result.returncode != 0:
        return "Staged changes failed secret scanning; commit blocked."
    return None


def evaluate(event: dict, approved_roots: tuple[Path, ...] | None = None) -> str | None:
    tool = event.get("tool_name")
    data = event.get("tool_input")
    if not isinstance(data, dict):
        return "Unrecognized tool input; blocked by policy."
    cwd = str(event.get("cwd") or os.getcwd())
    root = root_for(cwd, approved_roots)
    if str(root) == "/__bootstrap_outside_approved_roots__":
        return "Work outside approved code and knowledge roots is blocked."
    if tool in {"PowerShell", "Computer", "ComputerUse"}:
        return "Windows and host-computer actions are blocked."
    if tool in {"Write", "Edit", "MultiEdit"}:
        paths = [data.get(k) for k in ("file_path", "path", "filename")
                 if isinstance(data.get(k), str)]
        if not paths or any(not inside(p, cwd, root) for p in paths):
            return "File writes outside the current approved repository are blocked."
        return None
    if tool == "apply_patch":
        patch = data.get("patch", data.get("command", ""))
        if not isinstance(patch, str):
            return "Patch input is unrecognized; blocked by policy."
        paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$",
                           patch, re.MULTILINE)
        if not paths or any(not inside(p, cwd, root) for p in paths):
            return "Patch writes outside the current approved repository are blocked."
        return None
    if tool != "Bash":
        return None
    command = data.get("command")
    if not isinstance(command, str) or not command.strip():
        return "Shell command is unrecognized; blocked by policy."
    if WINDOWS.search(command):
        return "Windows paths and host executables are blocked."
    if "\n" in command or "\r" in command or "$(" in command or "`" in command:
        return "Compound or dynamic shell commands are blocked."
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return "Shell syntax is unrecognized; blocked by policy."
    if any(t in {";", "&&", "||", "|", "&"} for t in tokens):
        return "Compound shell commands are blocked."
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    names = {Path(t).name.lower() for t in tokens}
    if names & (HOST_NAMES | HOST_OPS):
        return "Host, Windows, and operating-system commands are blocked."
    if executable in {"bash", "sh", "dash", "python", "python3", "node", "ruby", "perl"} and any(
        t in {"-c", "-e", "--eval"} for t in tokens[1:]
    ):
        return "Inline shell and interpreter programs are blocked."
    for i, token in enumerate(tokens):
        if token in {">", ">>", ">|"} and (i + 1 >= len(tokens) or not inside(tokens[i + 1], cwd, root)):
            return "Shell output outside the current repository is blocked."
    args = [t for t in tokens[1:] if not t.startswith("-")]
    targets = []
    if executable in WRITERS:
        if executable in {"cp", "mv", "install"}:
            targets = args[-1:]
        elif executable == "dd":
            targets = [t.split("=", 1)[1] for t in tokens[1:] if t.startswith("of=")]
        elif executable == "truncate":
            targets = args[-1:]
        else:
            targets = args
    if executable == "sed" and any(t == "-i" or t.startswith("-i") for t in tokens[1:]):
        targets = args[-1:]
    if any(not inside(t, cwd, root) for t in targets):
        return "Shell writes outside the current repository are blocked."
    if executable == "git":
        if any(tokens[i] == "-C" and i + 1 < len(tokens) and not inside(tokens[i + 1], cwd, root)
               for i in range(len(tokens))):
            return "Git operations outside the current repository are blocked."
        if "commit" in tokens[1:]:
            return secret_scan(root)
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        print(json.dumps(deny("Hook input invalid; blocked by policy."), separators=(",", ":")))
        return 0
    reason = evaluate(event)
    if reason:
        print(json.dumps(deny(reason), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
