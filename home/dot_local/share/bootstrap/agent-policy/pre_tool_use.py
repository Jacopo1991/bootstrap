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
COMMAND_WRAPPERS = {"env", "command", "exec", "nohup", "timeout", "nice", "setsid",
                    "stdbuf", "xargs", "busybox"}
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
READ_ONLY_GIT = {"log", "show", "diff", "status", "rev-parse", "ls-files", "grep", "blame"}
CURRENT_REPO_GIT = READ_ONLY_GIT | {
    "add", "commit", "push", "fetch", "pull", "checkout", "switch", "reset",
    "restore", "clean", "rm", "mv", "branch", "tag", "remote", "stash",
    "rebase", "cherry-pick", "revert", "config", "rev-list", "ls-remote", "describe",
}
SHELL_EXPANSION = re.compile(r"[$~*?\[\]{}]")



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


def git_target(tokens: list[str], cwd: str) -> tuple[Path, str, list[str], set[str]] | None:
    """Resolve repeated -C options without permitting config/worktree overrides."""
    target = Path(cwd).resolve()
    selectors: set[str] = set()
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-C":
            index += 1
            if index >= len(tokens):
                return None
            value = tokens[index]
        elif token.startswith("-C") and len(token) > 2:
            value = token[2:]
        elif token in {"--no-pager", "--literal-pathspecs", "--no-optional-locks"}:
            selectors.add(token)
            index += 1
            continue
        elif token.startswith("-"):
            return None
        else:
            return target, token, tokens[index + 1:], selectors
        # Git resolves each relative -C against the preceding effective directory.
        if SHELL_EXPANSION.search(value):
            return None
        if value:
            target = (target / value).resolve()
        index += 1
    return None


def git_policy(tokens: list[str], cwd: str, root: Path,
               approved_roots: tuple[Path, ...] | None) -> str | None:
    parsed = git_target(tokens, cwd)
    if parsed is None:
        return "Unsupported Git selector or global option; use plain git with -C or --no-pager."
    target, subcommand, arguments, selectors = parsed
    target_root = root_for(str(target), approved_roots)
    if str(target_root) == "/__bootstrap_outside_approved_roots__":
        return "Git target is outside approved code and knowledge roots."
    cross_repo = target_root != root
    if cross_repo and subcommand not in READ_ONLY_GIT:
        return "Git writes outside the current repository are blocked."
    if subcommand == "merge":
        return "Build agents never merge."
    if subcommand not in CURRENT_REPO_GIT:
        return "Git topology changes and unsupported write primitives require separate setup."
    # Reject abbreviated as well as full long options. Git accepts unique prefixes.
    def option_matches(argument: str, option: str) -> bool:
        key = argument.split("=", 1)[0]
        return key.startswith("--") and len(key) > 2 and option.startswith(key)

    if any(option_matches(arg, "--output") for arg in arguments):
        return "Git output-file options are blocked; redirect only inside the current repository."
    if subcommand == "config":
        if any(arg.startswith("-f") or any(option_matches(arg, option) for option in
               ("--global", "--system", "--file", "--worktree", "--scope"))
               for arg in arguments):
            return "Git config may change only the current repository's local configuration."
    if cross_repo:
        if not {"--no-pager", "--no-optional-locks"}.issubset(selectors):
            return "Cross-repository reads require --no-pager and --no-optional-locks."
        if subcommand in {"show", "diff", "blame"} and "--no-textconv" not in arguments:
            return "Cross-repository show/diff/blame require --no-textconv."
        forbidden = ("--ext-diff", "--textconv", "--no-index", "--open-files-in-pager")
        if any(any(option_matches(arg, option) for option in forbidden)
               or arg.startswith("-O") or (subcommand == "blame" and arg.startswith("-S"))
               for arg in arguments):
            return "Side-effecting or external-file read options are blocked across repositories."
    # Check filesystem operands, including attached option values, in the effective
    # repository. Remote URLs are reviewed by the native prompt; local remote
    # destinations are forbidden so a push cannot write a sibling repository.
    for arg in arguments:
        if SHELL_EXPANSION.search(arg) and (cross_repo or subcommand not in READ_ONLY_GIT):
            return "Expanded Git operands are blocked; use literal repository paths."
        value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
        if value.startswith("file://"):
            return "Local filesystem Git remotes are blocked."
        if "://" in value or re.match(r"^[^/]+@[^:]+:", value):
            continue
        if value.startswith(("/", "../", "./")) or (cross_repo and "/" in value):
            if not inside(value, str(target), target_root):
                return "Git path is outside the inspected repository."
        if subcommand in {"push", "fetch", "pull", "remote"} and value.startswith(("/", "../", "./")):
            return "Use a named or network Git remote, not a filesystem destination."
    if subcommand == "commit":
        return secret_scan(root)
    return None


def gh_merges(tokens: list[str]) -> bool:
    """Find the pr/merge command through inherited repository/hostname options."""
    words: list[str] = []
    index = 1
    while index < len(tokens) and len(words) < 2:
        token = tokens[index]
        if token in {"-R", "--repo", "--hostname"}:
            index += 2
            continue
        if token.startswith(("--repo=", "--hostname=")) or (token.startswith("-R") and len(token) > 2):
            index += 1
            continue
        if SHELL_EXPANSION.search(token):
            return True  # Dynamic command selection cannot establish never-merge.
        if not token.startswith("-"):
            words.append(token)
        index += 1
    return words == ["pr", "merge"]


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
    if executable in COMMAND_WRAPPERS or ENV_ASSIGNMENT.fullmatch(tokens[0]):
        return "Shell command wrappers and environment-prefixed commands are blocked."
    if executable in {"bash", "sh", "dash", "python", "python3", "node", "ruby", "perl"} and any(
        t in {"-c", "-e", "--eval"} for t in tokens[1:]
    ):
        return "Inline shell and interpreter programs are blocked."
    names = {Path(t).name.lower() for t in tokens}
    if names & (HOST_NAMES | HOST_OPS):
        return "Host, Windows, and operating-system commands are blocked."
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
        return git_policy(tokens, cwd, root, approved_roots)
    if executable == "gh" and gh_merges(tokens):
        return "Build agents never merge."
    return None


def permission_reason(event: dict, approved_roots: tuple[Path, ...] | None = None) -> str | None:
    """Codex: only plain git/gh commands may reach an unsandboxed approval."""
    reason = evaluate(event, approved_roots)
    if reason:
        return reason
    data = event.get("tool_input", {})
    command = data.get("command")
    if event.get("tool_name") != "Bash" or not isinstance(command, str):
        return "Only Git and GitHub CLI shell requests may leave the sandbox."
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "Unrecognized approval command; blocked by policy."
    if not tokens or tokens[0] not in {"git", "gh", "/usr/bin/git", "/usr/bin/gh"}:
        return "Only Git and GitHub CLI may request network or sandbox escalation."
    # No allow response: native user approval remains mandatory.
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        print(json.dumps(deny("Hook input invalid; blocked by policy."), separators=(",", ":")))
        return 0
    permission_request = event.get("hook_event_name") == "PermissionRequest"
    reason = permission_reason(event) if permission_request else evaluate(event)
    if reason:
        response = ({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": reason}}}
                    if permission_request else deny(reason))
        print(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
