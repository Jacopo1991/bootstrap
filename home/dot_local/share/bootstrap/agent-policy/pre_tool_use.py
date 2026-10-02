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
GIT_OVERRIDE_ENV = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_EXEC_PATH", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_PAGER", "GIT_EXTERNAL_DIFF"}


def git_environment_override(event: dict, data: dict) -> bool:
    """Do not let inherited or per-tool environment change Git's control plane."""
    environments = [os.environ]
    for source in (event, data):
        for key in ("env", "environment"):
            if key in source:
                if not isinstance(source[key], dict):
                    return True
                environments.append(source[key])
    return any(name.startswith("GIT_CONFIG") or name in GIT_OVERRIDE_ENV
               for environment in environments for name in environment)


def protected_git_path(path_text: str, cwd: str, metadata: tuple[Path, ...]) -> bool:
    """Protect .git itself, descendants, symlink aliases and worktree Git dirs."""
    path = Path(path_text)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        resolved = path.resolve()
        if (".git" in path.parts or ".git" in resolved.parts
                or any(resolved == directory or directory in resolved.parents
                       for directory in metadata)):
            return True
        # An owner-provisioned nested repo may store its Git directory under
        # another name. Inspect only target ancestors (no recursive traversal):
        # normal Git dirs carry HEAD/objects/refs; linked worktree dirs carry
        # HEAD/commondir. Metadata remains protected from the enclosing repo.
        for ancestor in (resolved, *resolved.parents):
            if ((ancestor / "HEAD").is_file()
                    and ((ancestor / "commondir").is_file()
                         or (ancestor / "objects").is_dir() and (ancestor / "refs").is_dir())):
                return True
        return False
    except (OSError, ValueError):
        return True


def git_metadata(root: Path) -> tuple[Path, ...] | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, check=True, timeout=3)
        paths = tuple(Path(line).resolve() for line in result.stdout.splitlines())
        return paths if len(paths) == 2 and all(path.is_absolute() for path in paths) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def writable(path_text: str, cwd: str, root: Path, metadata: tuple[Path, ...] | None) -> bool:
    return (metadata is not None and inside(path_text, cwd, root)
            and not protected_git_path(path_text, cwd, metadata))


def transfer_paths(tokens: list[str], executable: str, cwd: str) -> tuple[list[str], list[str]] | None:
    """Parse file transfers; refuse directory copies and ambiguous option forms."""
    flags = {"-f", "-i", "-n", "-v", "-p", "-T", "-D", "--force", "--interactive",
             "--no-clobber", "--verbose", "--no-target-directory", "--preserve-timestamps",
             "--remove-destination"}
    operands: list[str] = []
    target_directory: str | None = None
    index = 1
    options = True
    while index < len(tokens):
        token = tokens[index]
        if options and token == "--":
            options = False
        elif options and token in {"-t", "--target-directory"}:
            index += 1
            if index >= len(tokens) or target_directory is not None:
                return None
            target_directory = tokens[index]
        elif options and (token.startswith("--target-directory=") or token.startswith("-t") and len(token) > 2):
            if target_directory is not None:
                return None
            target_directory = token.split("=", 1)[1] if token.startswith("--") else token[2:]
        elif options and executable == "install" and token in {"-m", "--mode"}:
            index += 1
            if index >= len(tokens):
                return None
        elif options and executable == "install" and (token.startswith("--mode=") or token.startswith("-m") and len(token) > 2):
            pass
        elif options and token.startswith("-"):
            if token not in flags:
                return None
        else:
            operands.append(token)
        index += 1
    if target_directory is None and len(operands) < 2 or target_directory is not None and not operands:
        return None
    sources = operands if target_directory is not None else operands[:-1]
    destination = target_directory if target_directory is not None else operands[-1]
    try:
        if any((Path(cwd) / source).is_dir() for source in sources):
            return None  # Recursive/whole-directory transfers can hide nested .git writes.
        dest_path = Path(cwd) / destination
        targets = [str(dest_path / Path(source).name) for source in sources] if dest_path.is_dir() else [destination]
    except OSError:
        return None
    # mv deletes its source too; cp/install inspect sources for protected metadata.
    return sources + [destination], targets + (sources if executable == "mv" else [])


def git_config_read_only(arguments: list[str]) -> bool:
    modifiers = {"--local", "--show-origin", "--show-scope"}
    operation = [argument for argument in arguments if argument not in modifiers]
    return (operation in (["--list"], ["-l"])
            or len(operation) == 2 and operation[0] == "--get"
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+", operation[1]) is not None)




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


def git_network_remote(arguments: list[str], target: Path, subcommand: str) -> str | None:
    """Require an explicit remote whose effective URLs stay on GitHub."""
    flags = {
        "push": {"-u", "--set-upstream", "-f", "--force", "--force-with-lease",
                 "--atomic", "--tags", "--all", "-d", "--delete", "--prune",
                 "-n", "--dry-run", "--porcelain", "-v", "--verbose", "-q", "--quiet"},
        "fetch": {"-p", "--prune", "-t", "--tags", "--no-tags", "-f", "--force",
                  "-n", "--dry-run", "-v", "--verbose", "-q", "--quiet"},
        "pull": {"--rebase", "--no-rebase", "--ff-only", "--ff", "--no-ff",
                 "--autostash", "--no-autostash", "-v", "--verbose", "-q", "--quiet"},
    }
    attached_values = {"--force-with-lease"} if subcommand == "push" else {
        "--depth", "--filter", "--shallow-since", "--shallow-exclude",
    }
    operands: list[str] = []
    for argument in arguments:
        if not argument.startswith("-"):
            operands.append(argument)
        elif argument not in flags[subcommand]:
            key, separator, value = argument.partition("=")
            if key not in attached_values or not separator or not value:
                return "Unsupported remote option; use ordinary flags or supported --key=value options."
    if not operands:
        return "Git network commands require an explicit named GitHub remote."
    remote = operands[0]
    # Named remotes let Git expand both insteadOf and pushInsteadOf itself.
    # A direct URL cannot be resolved as a push URL without synthetic config.
    command = ["git", "remote", "get-url"]
    if subcommand == "push":
        command.append("--push")
    command.extend(["--all", remote])
    def github_url(value: str) -> bool:
        return value.startswith(("https://github.com/", "git@github.com:", "ssh://git@github.com/"))
    try:
        result = subprocess.run(command, cwd=target, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return "Git remote cannot be resolved safely; use a configured GitHub remote."
    urls = result.stdout.splitlines()
    if not urls or any(not github_url(url) for url in urls):
        return "Local filesystem and non-GitHub remote destinations are blocked."
    return None


def git_policy(tokens: list[str], cwd: str, root: Path,
               approved_roots: tuple[Path, ...] | None) -> str | None:
    if any(token == "-c" or token.startswith("-c") and len(token) > 2
           or token.startswith("--config-env")
           or "=" in token and (token.split("=", 1)[0].startswith("GIT_CONFIG")
                               or token.split("=", 1)[0] in GIT_OVERRIDE_ENV)
           for token in tokens[1:]):
        return "Git configuration and execution overrides are blocked."
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
    if subcommand == "clean":
        return "Agent Git cleanup is blocked because it can remove nested repository metadata."
    if subcommand in {"add", "mv", "rm", "checkout", "restore", "reset"}:
        metadata = git_metadata(target_root)
        if metadata is None:
            return "Cannot identify protected Git metadata; Git file operation blocked."
        if any(option_matches(arg, "--pathspec-from-file") for arg in arguments):
            return "Indirect Git pathspec files are blocked; use explicit file paths."
        operands: list[str] = []
        literal_paths = False
        for argument in arguments:
            if argument == "--" and not literal_paths:
                literal_paths = True
            elif literal_paths or not argument.startswith("-"):
                operands.append(argument)
        if any(protected_git_path(arg, str(target), metadata) for arg in operands):
            return "Git file operands cannot modify or stage protected repository metadata."
        if subcommand in {"mv", "rm", "restore"} and any(
                (target / arg).is_dir() for arg in operands):
            return "Git directory mutations are blocked; use explicit non-metadata file paths."
    if subcommand == "config" and not git_config_read_only(arguments):
        return "Agent Git configuration writes are blocked; only --get/--list reads are permitted."
    if cross_repo:
        if not {"--no-pager", "--no-optional-locks"}.issubset(selectors):
            return "Cross-repository reads require --no-pager and --no-optional-locks."
        if subcommand in {"log", "show", "diff", "blame"} and "--no-textconv" not in arguments:
            return "Cross-repository log/show/diff/blame require --no-textconv."
        if subcommand in {"log", "show", "diff"} and "--no-ext-diff" not in arguments:
            return "Cross-repository log/show/diff require --no-ext-diff."
        if any(arg.startswith("-") and not arg.startswith("--") and len(arg) > 2
               and not arg[1:].isdigit() for arg in arguments):
            return "Spell cross-repository short options separately; aggregation is blocked."
        forbidden = ("--ext-diff", "--textconv", "--no-index", "--open-files-in-pager")
        external_files = {
            "grep": ("--file",),
            "ls-files": ("--exclude-from",),
            "blame": ("--contents", "--ignore-revs-file"),
        }.get(subcommand, ())
        if any(any(option_matches(arg, option) for option in external_files)
               or (subcommand == "grep" and arg.startswith("-f"))
               or (subcommand == "ls-files" and arg.startswith("-X"))
               for arg in arguments):
            return "External input-file options are blocked across repositories."
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
        if cross_repo or value.startswith(("/", "../", "./")):
            if not inside(value, str(target), target_root):
                return "Git path is outside the inspected repository."
        if subcommand in {"push", "fetch", "pull", "remote"} and value.startswith(("/", "../", "./")):
            return "Use a named or network Git remote, not a filesystem destination."
    if subcommand in {"push", "fetch", "pull"}:
        reason = git_network_remote(arguments, target, subcommand)
        if reason:
            return reason
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
    if git_environment_override(event, data):
        return "Git configuration and execution environment overrides are blocked."
    cwd = str(event.get("cwd") or os.getcwd())
    root = root_for(cwd, approved_roots)
    if str(root) == "/__bootstrap_outside_approved_roots__":
        return "Work outside approved code and knowledge roots is blocked."
    if tool in {"PowerShell", "Computer", "ComputerUse"}:
        return "Windows and host-computer actions are blocked."
    if tool in {"Write", "Edit", "MultiEdit"}:
        paths = [data.get(k) for k in ("file_path", "path", "filename")
                 if isinstance(data.get(k), str)]
        metadata = git_metadata(root)
        if not paths or any(not writable(p, cwd, root, metadata) for p in paths):
            return "File writes outside the current repository or into Git metadata are blocked."
        return None
    if tool == "apply_patch":
        patch = data.get("patch", data.get("command", ""))
        if not isinstance(patch, str):
            return "Patch input is unrecognized; blocked by policy."
        paths = re.findall(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$",
                           patch, re.MULTILINE)
        metadata = git_metadata(root)
        if not paths or any(not writable(p, cwd, root, metadata) for p in paths):
            return "Patch writes outside the current repository or into Git metadata are blocked."
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
    output_operators = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
    metadata = git_metadata(root) if executable in WRITERS | {"sed"} or any(
        token in output_operators for token in tokens) else None
    for i, token in enumerate(tokens):
        if token in output_operators and (i + 1 >= len(tokens) or SHELL_EXPANSION.search(tokens[i + 1])
                or not writable(tokens[i + 1], cwd, root, metadata)):
            return "Shell output outside the current repository or into Git metadata is blocked."
    args = [t for t in tokens[1:] if not t.startswith("-")]
    targets: list[str] = []
    inspected: list[str] = []
    if executable in {"cp", "mv", "install"}:
        transfer = transfer_paths(tokens, executable, cwd)
        if transfer is None:
            return "Ambiguous options and whole-directory file transfers are blocked."
        inspected, targets = transfer
    elif executable in WRITERS:
        if executable == "dd":
            targets = [t.split("=", 1)[1] for t in tokens[1:] if t.startswith("of=")]
        elif executable == "truncate":
            targets = args[-1:]
        else:
            targets = args
        inspected = args + [t.split("=", 1)[1] for t in tokens[1:] if t.startswith("--") and "=" in t]
        if executable in {"rm", "chmod", "chown"} and any(
                SHELL_EXPANSION.search(t) or (Path(cwd) / t).is_dir() for t in targets):
            return "Directory mutation commands are blocked; use explicit file paths."
    if executable == "sed" and any(t == "-i" or t.startswith("-i") or t.startswith("--in-place")
                                  for t in tokens[1:]):
        targets = args[-1:]
        inspected = args
    if any(SHELL_EXPANSION.search(t) or metadata is None or protected_git_path(t, cwd, metadata)
           for t in inspected):
        return "Direct shell writes to Git metadata or expanded file operands are blocked."
    if any(SHELL_EXPANSION.search(t) or not writable(t, cwd, root, metadata) for t in targets):
        return "Shell writes outside the current repository or into Git metadata are blocked."
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
