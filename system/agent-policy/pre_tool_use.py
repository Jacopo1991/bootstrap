#!/usr/bin/env python3
"""Fail-closed managed agent hook for workspace boundaries and staged secrets.

Claude Code and Codex run it from their root-owned managed settings, as
/usr/bin/python3 -I from /usr/local/lib/agent-policy/.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone

HOST_NAMES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd.exe",
              "wsl", "wsl.exe", "wslconfig.exe", "diskpart", "diskpart.exe", "schtasks", "schtasks.exe"}
HOST_OPS = {"sudo", "su", "systemctl", "service", "apt", "apt-get", "dpkg",
            "mount", "umount", "shutdown", "reboot", "modprobe", "useradd",
            "adduser", "usermod", "passwd", "visudo"}
# Inspect paths in argv, never a substring in an entire shell/body string.
# Inspect before shell escape removal as well, so unquoted paths stay visible.
WINDOWS_PATH = re.compile(r"(?i)(?:(?<![A-Za-z0-9_])[A-Z]:[\\/](?!/)|"
                          r"(?:^|=)(?:-[A-Za-z]+)?[A-Z]:[\\/]|/mnt/[a-z](?:/|$)|"
                          r"(?:^|=)(?:-[A-Za-z]+)?\\\\[^\\/\s]+\\[^\\/\s]+)")
HOST_EXECUTABLE = re.compile(r"(?i)\.(?:exe|com)$")
WRITERS = {"touch", "mkdir", "rmdir", "rm", "cp", "mv", "install", "ln", "tee",
           "truncate", "dd", "chmod", "chown"}
# Wrappers whose wrapped command is evaluated with the ordinary rules.
EVALUATED_WRAPPERS = {"env", "timeout", "nice", "stdbuf", "time"}
# Wrappers that stay refused: they hide or detach the command they run.
REFUSED_WRAPPERS = {"command", "exec", "nohup", "setsid", "xargs", "busybox"}
WRAPPER_OPTIONS = {  # (switches, options taking a value)
    "time": ({"-p"}, set()),
    "timeout": ({"--preserve-status", "--foreground", "-v", "--verbose"},
                {"-k", "-s", "--kill-after", "--signal"}),
    "nice": (set(), {"-n", "--adjustment"}),
    "stdbuf": (set(), {"-i", "-o", "-e", "--input", "--output", "--error"}),
}
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\+?=.*$")
# Variables that change what runs or where Git looks: never settable from a command.
UNSAFE_ENV_PREFIXES = ("GIT_", "GH_", "LD_", "DYLD_", "BASH_", "PYTHON", "PERL", "RUBY")
UNSAFE_ENV_NAMES = {"PATH", "IFS", "ENV", "HOME", "SHELL", "PAGER", "EDITOR", "VISUAL",
                    "CDPATH", "GLOBIGNORE", "PROMPT_COMMAND", "SHELLOPTS", "ZDOTDIR",
                    "FPATH", "fpath", "path", "cdpath", "EXECIGNORE", "POSIXLY_CORRECT",
                    "NODE_OPTIONS", "NODE_PATH", "SSH_ASKPASS", "GITHUB_TOKEN",
                    "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS", "MAKEFLAGS", "MFLAGS", "MAKEFILES",
                    "JUSTFILE", "JUST_SHELL", "JUST_SHELL_ARG", "JUST_WORKING_DIRECTORY",
                    "JUST_JUSTFILE", "JUST_JUSTFILE_NAME", "JUST_DOTENV_COMMAND",
                    "npm_config_script_shell", "NPM_CONFIG_SCRIPT_SHELL",
                    "npm_config_prefix", "NPM_CONFIG_PREFIX"}
INTERPRETERS = {"bash", "sh", "dash", "zsh", "ksh", "csh", "tcsh", "fish", "python",
                "node", "nodejs", "deno", "bun", "ruby", "perl", "php", "lua"}
INLINE_FLAG = re.compile(r"^-[A-Za-z]{1,4}$")
# Shell syntax that would run commands the segment checks cannot see.
SHELL_CONSTRUCTS = {"eval", "source", ".", "export", "declare", "typeset", "readonly",
                    "local", "set", "unset", "alias", "unalias", "builtin", "shopt", "trap",
                    "coproc", "function", "if", "then", "else", "elif", "fi", "for", "while",
                    "until", "do", "done", "case", "esac", "select", "time", "!", "{", "}",
                    "(", ")", "[[", "]]"}
CHAIN_SEPARATORS = {";", "&&", "||", "|"}
# Segments that cannot change repository state before a state-dependent Git command.
READ_ONLY_COMMANDS = {"cd", "echo", "printf", "true", "false", "pwd", "ls", "cat", "head",
                      "tail", "wc", "grep", "sort", "uniq", "date", "stat", "basename", "dirname", "du"}
STATE_DEPENDENT_GIT = {"commit", "merge"}
MIXED_CHAIN_TEXT_TOOLS = {"grep", "head", "tail", "wc", "sort", "uniq", "cut", "jq", "cat",
                        "pwd", "ls", "find", "rg", "stat", "file", "diff", "test", "[",
                        "echo", "printf", "true", "du", "sed"}
NULL_DEVICES = {"/dev/null"}
READ_ONLY_GIT = {"log", "show", "diff", "status", "rev-parse", "ls-files", "grep", "blame",
                 "rev-list", "ls-tree", "show-ref", "describe", "cat-file", "merge-base", "name-rev",
                 "check-ignore", "for-each-ref", "shortlog", "count-objects"}
WORKTREE_LIST_OPTIONS = {"--porcelain", "-v", "--verbose", "-z"}
# Where the Codex app (agent on the AgentDev SSH host) creates its worktrees ($CODEX_HOME/worktrees).
MANAGED_WORKTREE_BASES = (Path("/home/agent/.codex/worktrees"),)
CURRENT_REPO_GIT = READ_ONLY_GIT | {
    "add", "commit", "push", "fetch", "pull", "checkout", "switch", "reset",
    "restore", "clean", "rm", "mv", "branch", "tag", "remote", "stash",
    "rebase", "cherry-pick", "revert", "config", "rev-list", "ls-remote", "describe",
}
SHELL_EXPANSION = re.compile(r"[$~*?\[\]{}]")
GIT_OVERRIDE_ENV = {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                    "GIT_EXEC_PATH", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_PAGER", "GIT_EXTERNAL_DIFF"}


def policy_subprocess(*args, **kwargs):
    """Keep inherited Git selectors and configuration out of policy checks."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(*args, env=env, **kwargs)


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
    except (OSError, ValueError, RuntimeError):
        return True


def git_metadata(root: Path) -> tuple[Path, ...] | None:
    try:
        result = policy_subprocess(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
            cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, check=True, timeout=3)
        paths = tuple(Path(line).resolve() for line in result.stdout.splitlines())
        return paths if len(paths) == 2 and all(path.is_absolute() for path in paths) else None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None


def folder_delete_reason(folder: Path, root: Path | None,
                         metadata: tuple[Path, ...] | None, action: str = "Deleting") -> str | None:
    """Deleting or staging an ordinary folder inside the repository is fine; nothing
    that holds Git metadata (the repository root, a .git entry, a nested repository,
    or a Git directory under any name) may be deleted or staged as a folder."""
    if root is None or metadata is None:
        return f"Cannot identify the repository safely; {action.lower()} folders is blocked."
    try:
        target = folder.resolve()
        resolved_root = Path(root).resolve()
    except (OSError, ValueError, RuntimeError):
        return f"Cannot resolve folder safely; {action.lower()} folders is blocked."
    if target == resolved_root:
        return f"{action} the repository root as a folder is blocked."
    if any(path == target or target in path.parents for path in metadata):
        return f"{action} Git metadata is blocked."
    for current, dirs, files in os.walk(target):
        here = Path(current)
        if (".git" in dirs or ".git" in files or (here / "HEAD").is_file() and (
                (here / "commondir").is_file()
                or (here / "objects").is_dir() and (here / "refs").is_dir())):
            return f"{action} folders that contain Git metadata is blocked."
    return None


SCRATCH_PARENT = Path("/tmp")


def scratch_root() -> Path | None:
    """Writable scratch outside every repository: <Claude Code $TMPDIR>/scratch, i.e.
    /tmp/claude-<uid>/scratch. Only when /tmp/claude-<uid> is a real directory owned by
    this user and closed to others; the rest of that directory is Claude Code's own."""
    tmpdir = SCRATCH_PARENT / f"claude-{os.getuid()}"
    try:
        info = tmpdir.lstat()
    except OSError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        return None
    return tmpdir / "scratch"


def writable(path_text: str, cwd: str, root: Path, metadata: tuple[Path, ...] | None) -> bool:
    if metadata is None:
        return False
    if inside(path_text, cwd, root):
        return not protected_git_path(path_text, cwd, metadata)
    # Unresolved scratch path: a symlink in or as scratch that leads elsewhere is refused.
    scratch = scratch_root()
    return (scratch is not None and inside(path_text, cwd, scratch)
            and not protected_git_path(path_text, cwd, ()))


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


def work_copy_paths(tokens: list[str], cwd: str, root: Path,
                    metadata: tuple[Path, ...] | None) -> tuple[list[str], list[str]] | None:
    """Narrow recursive-copy exception: literal/glob sources within this .work only.

    Inspect both trees, including existing destination entries: cp can otherwise
    follow a destination symlink or carry protected metadata inside a directory.
    """
    if len(tokens) < 4 or tokens[1] not in {"-r", "-a"} or metadata is None:
        return None
    operands = tokens[2:]
    if operands[:1] == ["--"]:
        operands = operands[1:]
    if len(operands) < 2 or any(p.startswith("-") or re.search(r"[$`{}~]", p) for p in operands):
        return None
    work = root / ".work"
    if not inside(str(work), cwd, root) or work.is_symlink():
        return None
    destination = operands[-1]
    if glob.has_magic(destination):
        return None
    sources = []
    for operand in operands[:-1]:
        if not inside(operand, cwd, work):
            return None
        matches = glob.glob(str(Path(cwd) / operand)) if glob.has_magic(operand) else [operand]
        if not matches:
            return None
        sources.extend(matches)
    paths = sources + [destination]
    for path in paths:
        candidate = Path(cwd) / path
        if not inside(path, cwd, work) or candidate.is_symlink():
            return None
        if protected_git_path(path, cwd, metadata):
            return None
        if candidate.is_dir():
            if folder_delete_reason(candidate, root, metadata, "Copying"):
                return None
            for here, dirs, files in os.walk(candidate):
                for name in dirs + files:
                    child = Path(here) / name
                    if child.is_symlink() or protected_git_path(str(child), cwd, metadata):
                        return None
    return paths, [destination]


def git_config_read_only(arguments: list[str]) -> bool:
    modifiers = {"--local", "--show-origin", "--show-scope"}
    operation = [argument for argument in arguments if argument not in modifiers]
    return (operation in (["--list"], ["-l"])
            or len(operation) == 2 and operation[0] == "--get"
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+", operation[1]) is not None)




def root_for(cwd: str, approved_roots: tuple[Path, ...] | None = None) -> Path:
    try:
        here = Path(cwd).resolve()
    except (OSError, ValueError, RuntimeError):
        return Path("/__bootstrap_outside_approved_roots__")
    try:
        result = policy_subprocess(["git", "rev-parse", "--show-toplevel"], cwd=here,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=3, check=True)
        root = Path(result.stdout.strip()).resolve()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        root = here
    approved = approved_roots or (
        Path("/home/agent/dev_workspace").resolve(),
        Path("/home/agent/cortex").resolve(),
    )
    if any(root == base or base in root.parents for base in approved):
        return root
    if managed_worktree_of_approved_repo(root, approved):
        return root
    return Path("/__bootstrap_outside_approved_roots__")


CROSS_REPO_HINT = ("If the task needs another repository's files, ask the PM for a pinned copy in "
                   "this repository's .work/ instead.")


def same_repository(first: Path, second: Path) -> bool:
    """True when both checkouts share one Git common directory (main checkout and its worktrees)."""
    def common(path: Path) -> Path | None:
        try:
            result = policy_subprocess(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    cwd=path, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, timeout=3, check=True)
            return Path(result.stdout.strip()).resolve()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None
    left, right = common(first), common(second)
    return left is not None and left == right


def managed_worktree_of_approved_repo(root: Path, approved: tuple[Path, ...]) -> bool:
    """A worktree the Codex app made under ~/.codex/worktrees counts as its own root when it
    belongs to a repository under an approved root. The root stays the worktree itself."""
    try:
        bases = [base.resolve() for base in MANAGED_WORKTREE_BASES]
    except (OSError, ValueError, RuntimeError):
        return False
    if not any(base in root.parents for base in bases):
        return False
    try:
        result = policy_subprocess(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=3, check=True)
        common = Path(result.stdout.strip()).resolve()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False
    if common.name != ".git" or (root / ".git").is_dir():
        return False                      # bare repositories and independent clones do not count
    repository = common.parent
    return any(repository == base or base in repository.parents for base in approved)


def inside(path_text: str, cwd: str, root: Path) -> bool:
    p = Path(path_text)
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        p.resolve().relative_to(root)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


DENIAL_SUFFIX = " Rewrite the command; this is a policy limit, not missing access."


def deny(message: str, event: dict | None = None, vendor: str | None = None) -> dict:
    if event is not None:
        log_denial(event, message, vendor)
    if event is not None and event.get("hook_event_name") == "PermissionRequest":
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": message + DENIAL_SUFFIX}}}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny", "permissionDecisionReason": message + DENIAL_SUFFIX}}


class Denial(str):
    """A user-facing denial message with the identifier of the rule that emitted it."""

    def __new__(cls, rule_id: str, message: str):
        value = super().__new__(cls, message)
        value.rule_id = rule_id
        return value


def secret_scan(root: Path) -> str | None:
    try:
        diff = policy_subprocess(["git", "diff", "--cached", "--no-ext-diff", "--unified=0"],
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
        result = policy_subprocess([scanner, "stdin", "--no-banner", "--redact"],
                                cwd=root, input=diff.stdout, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "Secret scan failed; commit blocked."
    if result.returncode != 0:
        return "Staged changes failed secret scanning; commit blocked."
    return None


def git_target(tokens: list[str], cwd: str) -> tuple[Path, str, list[str], set[str]] | None:
    """Resolve repeated -C options without permitting config/worktree overrides."""
    try:
        target = Path(cwd).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
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
        for prefix in ("~", "$HOME", "${HOME}"):
            if value == prefix or value.startswith(prefix + "/"):
                value = str(Path.home()) + value[len(prefix):]
                break
        if SHELL_EXPANSION.search(value):
            return None
        if value:
            try:
                target = (target / value).resolve()
            except (OSError, ValueError, RuntimeError):
                return None
        index += 1
    return None


def forbidden_push(arguments: list[str]) -> bool:
    """Inspect every argv position; native prefix rules cannot do that."""
    for argument in arguments:
        key = argument.split("=", 1)[0]
        if (key.startswith("--force") or key.startswith("--delete")
                or key in {"--mirror", "--prune"} or argument.startswith(("+", ":"))
                or argument.startswith("-") and not argument.startswith("--")
                and any(letter in argument[1:] for letter in ("f", "d"))):
            return True
    return False


def git_needs_approval(subcommand: str, arguments: list[str]) -> bool:
    """Bounded destructive local primitives with matching native prompt rules."""
    return (subcommand in {"reset", "rebase", "cherry-pick", "revert"}
            or subcommand == "stash" and bool(arguments)
            and arguments[0] in {"pop", "drop", "clear"})


def sibling_fetch_has_submodules(root: Path) -> bool:
    """Fail closed: plain fetch can recurse into unchecked submodule remotes."""
    metadata = git_metadata(root)
    if metadata is None:
        return True
    try:
        # Retained/inherited activation or URL config can reactivate an embedded
        # child when a freshly fetched tree reintroduces its gitlink. Inspect
        # effective configuration without collecting or printing its values.
        configured = policy_subprocess(
            ["git", "config", "--get-regexp", "^submodule[.]"], cwd=root,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=3)
        if configured.returncode != 1:
            return True
        modules_file = root / ".gitmodules"
        if (modules_file.exists() or modules_file.is_symlink()
                or any((directory / "modules").exists() for directory in metadata)):
            return True
        index = policy_subprocess(["git", "ls-files", "--stage", "-z"], cwd=root,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, check=True, timeout=3)
        if any(record.startswith("160000 ") for record in index.stdout.split(chr(0))):
            return True
        head = policy_subprocess(["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                              cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=3)
        if head.returncode == 1:
            return False  # An unborn repository has no committed gitlinks.
        if head.returncode != 0:
            return True
        tree = policy_subprocess(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=root,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, check=True, timeout=3)
        return any(record.startswith("160000 ") for record in tree.stdout.split(chr(0)))
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return True


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
        if subcommand != "fetch" or arguments:
            return "Git network commands require an explicit named GitHub remote (except plain fetch)."
        # Bare fetch uses the current branch's configured remote, then origin.
        # Resolve that choice before checking URLs; checking origin alone would
        # miss a local/non-GitHub upstream selected by Git.
        remote = "origin"
        try:
            head = policy_subprocess(["git", "symbolic-ref", "--quiet", "HEAD"],
                                  cwd=target, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, timeout=3)
            if head.returncode == 0:
                reference = head.stdout.strip()
                if not reference.startswith("refs/heads/"):
                    return "Cannot identify the default fetch branch safely; specify origin."
                configured = policy_subprocess(
                    ["git", "config", "--get", "branch." + reference[11:] + ".remote"],
                    cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, timeout=3)
                if configured.returncode == 0:
                    remote = configured.stdout.strip()
                elif configured.returncode != 1:
                    return "Cannot identify the default fetch remote safely; specify origin."
            elif head.returncode != 1:
                return "Cannot identify the default fetch remote safely; specify origin."
        except (OSError, subprocess.SubprocessError):
            return "Cannot identify the default fetch remote safely; specify origin."
    else:
        remote = operands[0]
    if subcommand == "fetch":
        # A fetch group can expand one name into multiple unchecked remotes.
        try:
            group = policy_subprocess(["git", "config", "--get-all", "remotes." + remote],
                                   cwd=target, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return "Cannot inspect fetch groups safely."
        if group.returncode != 1:
            return "Fetch groups are blocked; use a single configured GitHub remote."
    # Named remotes let Git expand both insteadOf and pushInsteadOf itself.
    # A direct URL cannot be resolved as a push URL without synthetic config.
    command = ["git", "remote", "get-url"]
    if subcommand == "push":
        command.append("--push")
    command.extend(["--all", remote])
    def github_url(value: str) -> bool:
        return value.startswith(("https://github.com/", "git@github.com:", "ssh://git@github.com/"))
    try:
        result = policy_subprocess(command, cwd=target, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return "Git remote cannot be resolved safely; use a configured GitHub remote."
    urls = result.stdout.splitlines()
    if not urls or any(not github_url(url) for url in urls):
        return "Local filesystem and non-GitHub remote destinations are blocked."
    return None



LOCAL_DEFAULT_BRANCH = "main"


def _read_git(target: Path, *args: str) -> str:
    return policy_subprocess(
        ["git", *args], cwd=target, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, check=True, timeout=3).stdout.strip()


def _merge_options_configured(target: Path, current: str) -> bool:
    """Git applies branch.<name>.mergeOptions before argv, including custom strategies."""
    options = policy_subprocess(
        ["git", "config", "--get-all", "branch." + current[len("refs/heads/"):] + ".mergeOptions"],
        cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, timeout=3)
    return options.returncode != 1


def local_only_merge(arguments: list[str], target: Path, current: str) -> str | None:
    """Repositories with no remote at all (local-only projects): a task branch may merge the
    local main branch, nothing else. Its commits were scanned when they were committed."""
    if not current.startswith("refs/heads/"):
        return "Cannot identify the current branch safely."
    local_main = policy_subprocess(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}"],
        cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, timeout=3)
    if local_main.returncode:
        local_master = policy_subprocess(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/master^{commit}"],
            cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=3)
        if local_master.returncode == 0:
            return "This local-only repository uses master; rename the default branch to main."
    if current == "refs/heads/" + LOCAL_DEFAULT_BRANCH:
        return "Merging into the default branch is blocked; update only a task branch."
    if arguments not in ([LOCAL_DEFAULT_BRANCH], ["--no-edit", LOCAL_DEFAULT_BRANCH]):
        return ("In a repository without a remote, only git merge [--no-edit] "
                + LOCAL_DEFAULT_BRANCH + " is allowed.")
    source = "refs/heads/" + LOCAL_DEFAULT_BRANCH
    _read_git(target, "rev-parse", "--verify", source + "^{commit}")
    if _read_git(target, "rev-parse", "--symbolic-full-name", "--verify",
                 LOCAL_DEFAULT_BRANCH) != source:
        return "The merge source must resolve uniquely to the local " + LOCAL_DEFAULT_BRANCH + " branch."
    if _merge_options_configured(target, current):
        return "Configured merge options or unreadable configuration are blocked."
    return None


def git_merge_preview(arguments: list[str], target: Path) -> str | None:
    """Read-only merge preview: git merge-tree --write-tree [--name-only] HEAD <source>, where
    <source> is what git merge would accept here (origin/<default>, or the allowed local main)."""
    names = [argument for argument in arguments if argument != "--name-only"]
    if (len(arguments) - len(names) > 1 or len(names) != 3
            or names[0] != "--write-tree" or names[1] != "HEAD"):
        return "Only git merge-tree --write-tree [--name-only] HEAD <merge source> is allowed."
    return git_default_branch_merge(["--no-edit", names[2]], target, preview=True)


def git_default_branch_merge(arguments: list[str], target: Path,
                             preview: bool = False) -> str | None:
    """Only merge the locally recorded GitHub origin default into a task branch, or
    fast-forward a task branch to its own origin/<branch> (catching up from GitHub).
    Plain main is also accepted when main is origin's recorded default.
    Without any remote (local-only projects), merge the local main into a task branch."""
    try:
        def read_git(*args: str) -> str:
            return _read_git(target, *args)
        current = read_git("symbolic-ref", "--quiet", "HEAD")
        if not read_git("remote"):
            return local_only_merge(arguments, target, current)
        default = read_git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        prefix = "refs/remotes/origin/"
        if (not current.startswith("refs/heads/") or not default.startswith(prefix)
                or default == prefix + "HEAD"):
            return "Cannot identify the current and origin default branch safely."
        name = default[len(prefix):]
        if current == "refs/heads/" + name:
            return "Merging into the default branch is blocked; update only a task branch."
        own = current[len("refs/heads/"):]
        wanted = "origin/" + name
        own_remote = "origin/" + own
        if arguments == ["--ff-only", own_remote] and plain_branch_name(own):
            source, label = prefix + own, "its own origin branch"
        elif arguments in ([wanted], ["--no-edit", wanted]):
            source, label = default, "the origin default ref"
        elif name == "main" and (arguments == ["main"]
                                or (preview and arguments == ["--no-edit", "main"])):
            source, label = "refs/heads/main", "the local default ref"
        else:
            return ("Only git merge [--no-edit] origin/<default branch>, plain git merge main "
                    "when main is the default, or "
                    "git merge --ff-only origin/<current branch> is allowed.")
        read_git("rev-parse", "--verify", source + "^{commit}")
        spelling = "main" if source == "refs/heads/main" else "origin/" + source[len(prefix):]
        if read_git("rev-parse", "--symbolic-full-name", "--verify", spelling) != source:
            return "The merge source must resolve uniquely to " + label + "."
        if _merge_options_configured(target, current):
            return "Configured merge options or unreadable configuration are blocked."
        urls = read_git("remote", "get-url", "--all", "origin").splitlines()
        if len(urls) != 1:
            return "Origin must have one unambiguous GitHub URL."
    except (OSError, subprocess.SubprocessError):
        return ("Cannot verify merge metadata; origin/HEAD and its commit must exist "
                "(or, without any remote, the local main branch).")
    if preview:
        return None
    return git_network_remote(["origin"], target, "fetch")


def plain_branch_name(value: str) -> bool:
    """Literal branch-ref syntax without revision operators or control characters."""
    return bool(value and value != "@" and not value.startswith(("-", "/"))
                and not any(ord(char) <= 32 or ord(char) == 127 or char in "~^:?*[\\"
                            for char in value)
                and ".." not in value and "@{" not in value and "//" not in value
                and not value.endswith((".", "/"))
                and all(not part.startswith(".") and not part.endswith(".lock")
                        for part in value.split("/")))


def git_switch_creation(arguments: list[str]) -> bool:
    """Only -c/--create <name> [plain object ID or origin/<branch>]."""
    if (len(arguments) not in {2, 3} or arguments[0] not in {"-c", "--create"}
            or arguments[1].startswith("-")):
        return False
    if len(arguments) == 2:
        return True  # Preserve the pre-existing no-start-point creation grammar.
    if not plain_branch_name(arguments[1]):
        return False
    start = arguments[2]
    return bool(re.fullmatch(r"(?:[0-9a-fA-F]{4,40}|[0-9a-fA-F]{64})", start)
                or start.startswith("origin/") and plain_branch_name(start[7:]))


def git_policy(tokens: list[str], cwd: str, root: Path,
               approved_roots: tuple[Path, ...] | None) -> str | None:
    if any(token.startswith("--config-env")
           or "=" in token and (token.split("=", 1)[0].startswith("GIT_CONFIG")
                               or token.split("=", 1)[0] in GIT_OVERRIDE_ENV)
           for token in tokens[1:]):
        return "Git configuration and execution overrides are blocked."
    parsed = git_target(tokens, cwd)
    if parsed is None:
        return "Unsupported Git selector or global option; use plain git with -C or --no-pager."
    target, subcommand, arguments, selectors = parsed
    # Global -c remains blocked by git_target. Switch has a distinct, bounded
    # creation grammar; an optional start point never becomes another option.
    switch_creation = subcommand == "switch" and git_switch_creation(arguments)
    config_options = [index for index, argument in enumerate(arguments)
                      if argument == "-c" or argument.startswith("-c") and len(argument) > 2]
    if config_options and not switch_creation:
        return "Git configuration overrides or unsupported switch creation arguments are blocked."
    if subcommand == "switch" and any(arg.startswith("-") for arg in arguments) and not switch_creation:
        return "Switch creation permits only -c/--create <name> [commit SHA or origin/<branch>]."
    if subcommand == "push" and forbidden_push(arguments):
        return "Force pushes and remote branch deletion are blocked by founder policy."
    if git_needs_approval(subcommand, arguments) and tokens[1] != subcommand:
        return "Use a plain Git destructive command without global selectors so native approval applies."
    target_root = root_for(str(target), approved_roots)
    if str(target_root) == "/__bootstrap_outside_approved_roots__":
        return "Git target is outside approved code and knowledge roots."
    # A linked worktree of the current repository (e.g. <repo>/.worktrees/<task>) is the same
    # repository: it shares the object store, refs and secret-scanned index rules.
    scratch_repo = inside(str(target_root), cwd, root / ".work")
    cross_repo = target_root != root and not scratch_repo and not same_repository(target_root, root)
    sibling_fetch = cross_repo and subcommand == "fetch"
    if sibling_fetch and arguments not in ([], ["origin"]):
        return "Sibling fetch permits only fetch or fetch origin; options/refspecs are blocked."
    if sibling_fetch and sibling_fetch_has_submodules(target_root):
        return "Sibling fetch with submodules is blocked to avoid unchecked recursive destinations."
    read_only = (subcommand in READ_ONLY_GIT | {"for-each-ref"}
                 or subcommand == "branch" and (not arguments or all(
                     arg in {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv",
                             "--show-current"}
                     or not arg.startswith("-") and "--list" in arguments
                     for arg in arguments))
                 or subcommand == "config" and git_config_read_only(arguments)
                 or subcommand == "remote" and (arguments == ["-v"] or
                     arguments[:1] == ["show"] and "-n" in arguments and all(
                         arg == "-n" or not arg.startswith("-") for arg in arguments[1:]))
                 or subcommand == "worktree" and arguments[:1] == ["list"])
    if cross_repo and not read_only and not sibling_fetch:
        return "Git writes outside the current repository are blocked."
    if subcommand == "merge":
        return git_default_branch_merge(arguments, target)
    if subcommand == "merge-tree":
        return git_merge_preview(arguments, target)
    if subcommand == "worktree":
        # Listing is read-only. Worktrees are created and removed by the tool that runs the lane
        # (the Codex app, `claude --worktree`) and cleaned by workspace-tidy, never by agents.
        if arguments[:1] == ["list"] and all(a in WORKTREE_LIST_OPTIONS for a in arguments[1:]):
            return None
        return ("Only `git worktree list` is allowed; the Codex app or `claude --worktree` "
                "creates and removes worktrees.")
    if subcommand not in CURRENT_REPO_GIT and not read_only:
        return "Git topology changes and unsupported write primitives require separate setup."
    # Reject abbreviated as well as full long options. Git accepts unique prefixes.
    def option_matches(argument: str, option: str) -> bool:
        key = argument.split("=", 1)[0]
        known = {"--textconv", "--ext-diff", "--filters"}
        if subcommand != "cat-file":
            known.add("--text")
        return key == option or (key not in known and key.startswith("--")
                                 and len(key) > 2 and option.startswith(key))

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
            elif (subcommand == "add" and not literal_paths
                  and option_matches(argument, "--chmod")):
                # Git consumes a separate chmod value as an option argument,
                # not a filename. Accept only exact attached values so they
                # cannot satisfy the explicit-file staging requirement.
                if argument not in {"--chmod=+x", "--chmod=-x"}:
                    return "Git add chmod requires an exact --chmod=+x/--chmod=-x option."
            elif literal_paths or not argument.startswith("-"):
                operands.append(argument)
        if any(argument.startswith(":") for argument in operands):
            return "Git pathspec magic is blocked; use literal non-metadata file paths."
        if subcommand == "add" and not operands:
            return "Agent staging requires explicit non-metadata file paths."
        if any(protected_git_path(arg, str(target), metadata) for arg in operands):
            return "Git file operands cannot modify or stage protected repository metadata."
        folders = [arg for arg in operands if (target / arg).is_dir()]
        if (subcommand == "rm" and target == target_root
                and arguments in (["--cached", "-r", ".work"],
                                  ["--cached", "-r", "-q", ".work"],
                                  ["--cached", "-r", "--quiet", ".work"])):
            return None  # Index-only cleanup; never removes worktree files.
        if folders and subcommand in {"mv", "rm", "restore"}:
            return "Git directory mutations are blocked; use explicit non-metadata file paths."
        if folders and subcommand == "add":
            for arg in folders:
                reason = folder_delete_reason(target / arg, target_root, metadata, "Staging")
                if reason:
                    return reason
    if subcommand == "config" and not git_config_read_only(arguments):
        return "Agent Git configuration writes are blocked; only --get/--list reads are permitted."
    if cross_repo and not sibling_fetch:
        if any(option_matches(arg, option) for arg in arguments
               for option in ("--ext-diff", "--textconv")):
            return "External diff and textconv are blocked across repositories."
        if subcommand == "cat-file" and any(option_matches(arg, "--filters") for arg in arguments):
            return "External diff and textconv are blocked across repositories."
        forbidden = ("--no-index", "--open-files-in-pager")
        external_files = {
            "grep": ("--file",),
            "ls-files": ("--exclude-from",),
            "blame": ("--contents", "--ignore-revs-file"),
        }.get(subcommand, ())
        if any(any(option_matches(arg, option) for option in external_files)
               or (subcommand == "grep" and re.match(r"^-[^-]*f", arg))
               or (subcommand == "ls-files" and re.match(r"^-[^-]*X", arg))
               for arg in arguments):
            return "External input-file options are blocked across repositories."
        if any(any(option_matches(arg, option) for option in forbidden)
               or re.match(r"^-[^-]*O", arg)
               or (subcommand == "blame" and re.match(r"^-[^-]*S", arg))
               for arg in arguments):
            return "Side-effecting or external-file read options are blocked across repositories."
    # Check filesystem operands, including attached option values, in the effective
    # repository. Remote URLs are reviewed by the native prompt; local remote
    # destinations are forbidden so a push cannot write a sibling repository.
    literal_diff_paths = False
    for arg in arguments:
        if arg == "--":
            literal_diff_paths = True
        if SHELL_EXPANSION.search(arg) and (cross_repo or subcommand not in READ_ONLY_GIT):
            return "Expanded Git operands are blocked; use literal repository paths."
        value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
        if (subcommand == "diff" and "--no-index" in arguments
                and (SHELL_EXPANSION.search(value) or "`" in value)):
            return "Expanded Git diff operands are blocked; use literal repository paths."
        if subcommand == "diff" and "--no-index" in arguments and value == "/dev/null":
            continue
        if value.startswith("file://"):
            return "Local filesystem Git remotes are blocked."
        if "://" in value or re.match(r"^[^/]+@[^:]+:", value):
            if subcommand == "diff" and "--no-index" in arguments:
                return "Git diff operands must be literal repository paths or /dev/null."
            continue
        if (cross_repo or value.startswith(("/", "../", "./"))
                or subcommand == "diff" and "--no-index" in arguments
                and (literal_diff_paths or not value.startswith("-"))):
            if not inside(value, str(target), target_root):
                return "Git path is outside the inspected repository."
        if subcommand in {"push", "fetch", "pull", "remote"} and value.startswith(("/", "../", "./")):
            return "Use a named or network Git remote, not a filesystem destination."
    if subcommand in {"push", "fetch", "pull"}:
        reason = git_network_remote(arguments, target, subcommand)
        if reason:
            return reason
    if subcommand == "commit":
        if commit_stages_working_tree(arguments):
            return ("git commit -a/--all/--include/--only or a pathspec commits content the secret "
                    "scan has not seen; stage with git add first, then run a plain git commit.")
        return secret_scan(target_root)
    return None


COMMIT_VALUE_LONG = {"--message", "--file", "--reuse-message", "--reedit-message", "--template",
                     "--author", "--date", "--cleanup", "--trailer", "--fixup", "--squash"}
COMMIT_VALUE_SHORT = "mFCctSu"  # letters whose value is the rest of the cluster or the next argument


def commit_stages_working_tree(arguments: list[str]) -> bool:
    """True if the commit would take working-tree content the staged-diff scan cannot see."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if not argument.startswith("-") or argument in {"-", "--"}:
            return True  # pathspec operand, or everything after -- is one
        if argument.startswith("--"):
            key = argument.split("=", 1)[0]
            if any(option_name.startswith(key) and len(key) > 2
                   for option_name in ("--all", "--include", "--only")):
                return True
            if key in COMMIT_VALUE_LONG and "=" not in argument:
                index += 1
            continue
        for position, letter in enumerate(argument[1:], 1):
            if letter in "aio":
                return True
            if letter in COMMIT_VALUE_SHORT:
                if position == len(argument) - 1 and letter != "S" and letter != "u":
                    index += 1  # value is the next argument
                break
    return False


def sed_script_runs_commands(script: str) -> bool:
    """Detect the sed e command and the s///e flag (also w/W, which write arbitrary files)."""
    position = 0
    length = len(script)

    def skip_delimited(start: int, delimiter: str) -> int:
        while start < length and script[start] != delimiter:
            start += 2 if script[start] == "\\" else 1
        return start + 1

    while position < length:
        while position < length and script[position] in " \t\n;{}":
            position += 1
        # addresses: numbers, $, /re/, \cREc, first~step, +N, ~N, commas and !
        while position < length:
            char = script[position]
            if char in "0123456789$,~+! \t":
                position += 1
            elif char == "/":
                position = skip_delimited(position + 1, "/")
            elif char == "\\" and position + 1 < length:
                position = skip_delimited(position + 2, script[position + 1])
            else:
                break
            while position < length and script[position] in "IM":
                position += 1
        if position >= length:
            break
        command = script[position]
        position += 1
        if command in "eEwW":
            return True
        if command == "s":
            if position >= length:
                break
            delimiter = script[position]
            position = skip_delimited(skip_delimited(position + 1, delimiter), delimiter)
            while position < length and script[position] not in ";\n}":
                if script[position] in "ewW":
                    return True
                position += 1
        elif command == "y":
            if position >= length:
                break
            delimiter = script[position]
            position = skip_delimited(skip_delimited(position + 1, delimiter), delimiter)
        elif command in "aicrR:":
            position = script.find("\n", position)
            position = length if position < 0 else position
        elif command in "btT":
            while position < length and script[position] not in ";\n}":
                position += 1
    return False


def sed_policy(tokens: list[str]) -> str | None:
    scripts: list[str] = []
    index = 1
    positional: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "--":
            positional.extend(tokens[index:])
            break
        if token in {"--file", "-f"} or token.startswith("--file=") or (
                token.startswith("-f") and not token.startswith("--")):
            return "sed script files cannot be inspected; use -e with the script inline."
        if token == "--expression":
            if index < len(tokens):
                scripts.append(tokens[index])
                index += 1
        elif token.startswith("--expression="):
            scripts.append(token.split("=", 1)[1])
        elif token.startswith("-") and not token.startswith("--") and len(token) > 1:
            cluster = token[1:]
            if "e" in cluster:
                rest = cluster.split("e", 1)[1]
                if rest:
                    scripts.append(rest)
                elif index < len(tokens):
                    scripts.append(tokens[index])
                    index += 1
            elif cluster.endswith("l") and index < len(tokens):
                index += 1
        elif not token.startswith("-"):
            positional.append(token)
    if not scripts and positional:
        scripts.append(positional[0])
    if any(sed_script_runs_commands(script) for script in scripts):
        return "sed scripts using the e or w commands/flags run commands or write files and are blocked."
    return None


def readonly_sed(tokens: list[str]) -> bool:
    return ("-n" in tokens and sed_policy(tokens) is None and not any(
        token.startswith("--") and "--in-place".startswith(token.split("=", 1)[0])
        or token.startswith("--in-place=")
        or token.startswith("-") and not token.startswith("--") and "i" in token[1:]
        for token in tokens[1:]))


AWK_NAMES = {"awk", "gawk", "mawk", "nawk"}
AWK_PROGRAM_RISKS = (re.compile(r"system\s*\("), re.compile(r"\|\s*&?\s*getline"),
                     re.compile(r"getline[^;}\n]*<"), re.compile(r"(?<!\|)\|(?!\|)\s*\""),
                     re.compile(r">>?\s*\""))


def awk_policy(tokens: list[str]) -> str | None:
    for token in tokens[1:]:
        if (token in {"-f", "--file", "--exec", "-i", "--include", "-E"}
                or token.startswith(("--file=", "--exec=", "--include="))
                or token.startswith("-f") and not token.startswith("--")):
            return "awk program files and includes cannot be inspected; use an inline program."
        if any(risk.search(token) for risk in AWK_PROGRAM_RISKS):
            return "awk programs calling system(), piping to or from commands, or writing files are blocked."
    return None


def gh_policy(tokens: list[str]) -> str | None:
    """Find protected operations through inherited repository/hostname options."""
    arguments: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-R", "--repo", "--hostname"}:
            if index + 1 >= len(tokens):
                return "Incomplete GitHub CLI selector is blocked."
            index += 2
            continue
        if token.startswith(("--repo=", "--hostname=")) or (token.startswith("-R") and len(token) > 2):
            index += 1
            continue
        arguments.append(token)
        index += 1
    words = [token for token in arguments if not token.startswith("-")]
    if any(SHELL_EXPANSION.search(token) for token in words[:2]):
        return "Dynamic GitHub CLI command selection is blocked."
    if words[:2] == ["pr", "merge"]:
        return "Build agents never merge."
    if (words[:2] in (["repo", "delete"], ["repo", "edit"], ["release", "delete"])
            or words[:1] in (["secret"], ["ruleset"])):
        return "GitHub repository administration, deletion, secrets and rulesets are blocked."
    if words[:1] == ["api"]:
        for index, argument in enumerate(arguments):
            method = None
            if argument in {"-X", "--method"}:
                if index + 1 >= len(arguments):
                    return "Incomplete GitHub API method is blocked."
                method = arguments[index + 1]
            elif argument.startswith("--method="):
                method = argument.split("=", 1)[1]
            elif argument.startswith("-X") and len(argument) > 2:
                method = argument[2:].removeprefix("=")
            if method is not None and method.upper() == "DELETE":
                return "GitHub API DELETE requests are blocked."
    return None


def gh_prose_positions(tokens: list[str]) -> set[int]:
    """Only documented inline PR/issue body/title values are textual payloads.

    A body-file operand remains a real path and is checked; its contents are
    never read by the hook. Exempting prose here does not exempt any shell
    syntax, repository policy, destructive operation or permission check.
    """
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-R", "--repo", "--hostname"}:
            index += 2
        elif token.startswith(("--repo=", "--hostname=")) or token.startswith("-R") and len(token) > 2:
            index += 1
        else:
            break
    if tokens[index:index + 2] not in (
            ["pr", "create"], ["pr", "edit"], ["pr", "comment"],
            ["issue", "create"], ["issue", "edit"], ["issue", "comment"]):
        return set()
    prose: set[int] = set()
    value_flags = {
        "--body-file", "-F", "--assignee", "-a", "--label", "-l",
        "--milestone", "-m", "--project", "-p", "--reviewer", "-r",
        "--repo", "-R", "--hostname", "--base", "-B", "--head", "-H",
        "--template", "-T", "--recover", "--add-assignee", "--remove-assignee",
        "--add-label", "--remove-label", "--add-project", "--remove-project",
        "--add-reviewer", "--remove-reviewer", "--attach", "--parent", "--type",
        "--blocked-by", "--blocking", "--add-sub-issue", "--remove-sub-issue",
        "--add-blocked-by", "--remove-blocked-by", "--add-blocking", "--remove-blocking",
    }
    switches = {
        "--draft", "-d", "--editor", "-e", "--web", "-w", "--edit-last",
        "--create-if-none", "--delete-last", "--yes", "--help", "-h",
        "--fill", "-f", "--fill-first", "--fill-verbose", "--no-maintainer-edit",
        "--dry-run", "--remove-milestone", "--remove-parent", "--remove-type",
    }
    index += 2
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break  # Remaining operands are never option payloads.
        if token in {"--body", "--title", "-b", "-t"}:
            if index + 1 < len(tokens):
                prose.add(index + 1)
            index += 2
            continue
        if token in value_flags:
            index += 2  # Even option-looking values belong to this real flag.
            continue
        if token.startswith(("--body=", "--title=")) or (
                token.startswith(("-b", "-t")) and len(token) > 2
                and not token.startswith("--")):
            prose.add(index)
        elif token.split("=", 1)[0] in value_flags and "=" in token:
            pass
        elif token[:2] in value_flags and not token.startswith("--") and len(token) > 2:
            pass
        elif token.startswith("-") and token not in switches:
            return set()  # Unknown/combined options must not hide path operands.
        index += 1
    return prose


def regex_prose_positions(tokens: list[str], executable: str) -> set[int]:
    """Recognize explicit expressions and the first positional expression."""
    if executable not in {"rg", "grep", "sed"} | AWK_NAMES:
        return set()
    expressions: set[int] = set()
    has_expression = False
    options = True
    index = 1
    value_options = {"-f", "--file", "-g", "--glob", "--iglob", "-t", "--type",
                     "-T", "--type-not", "-A", "-B", "-C", "-m", "--max-count",
                     "--field-separator", "--assign"}
    if executable in AWK_NAMES:
        value_options |= {"-F", "-v"}
    switches = {"-i", "--ignore-case", "-v", "--invert-match", "-n",
                "--line-number", "-l", "--files-with-matches", "-q", "--quiet",
                "-r", "-R", "--recursive", "-E", "-F", "-P", "-w", "-x",
                "-c", "-o", "-H", "-h", "--hidden", "--no-heading",
                "--fixed-strings", "--extended-regexp", "--perl-regexp",
                "-S", "--smart-case", "-I", "-s", "-L", "-u", "-uu", "-.",
                "--no-ignore", "-z", "-U", "--multiline", "--json", "-a", "--text"}
    while index < len(tokens):
        token = tokens[index]
        if options and token == "--":
            options = False
        elif options and token in {"-e", "--regexp", "--expression"}:
            if index + 1 < len(tokens):
                expressions.add(index + 1)
            has_expression = True
            index += 1
        elif options and token.startswith(("--regexp=", "--expression=", "-e")):
            expressions.add(index)
            has_expression = True
        elif options and token in value_options:
            if token in {"-f", "--file"}:
                has_expression = True
            index += 1
        elif options and token.startswith("--") and "=" in token:
            if token.startswith("--file="):
                has_expression = True
            # Self-contained options consume no following operand.
        elif options and token.startswith("-"):
            if token not in switches:
                if token.startswith("--"):
                    return set()
                for offset, letter in enumerate(token[1:], 1):
                    option = "-" + letter
                    if option == "-e" or option in value_options:
                        if option in {"-e", "-f"}:
                            has_expression = True
                        if offset == len(token) - 1:
                            index += 1
                            if option == "-e" and index < len(tokens):
                                expressions.add(index)
                        elif option == "-e":
                            expressions.add(index)
                        break
                    if option not in switches:
                        return set()  # Unknown arity cannot identify an expression.
        else:
            if not has_expression:
                expressions.add(index)
            break
        index += 1
    return expressions


def host_command_reason(tokens: list[str], executable: str) -> str | None:
    """Block host execution and real Windows path arguments, not gh prose."""
    if ((tokens[0] != "[" and SHELL_EXPANSION.search(tokens[0])) or executable in HOST_NAMES | HOST_OPS
            or HOST_EXECUTABLE.search(tokens[0])
            or WINDOWS_PATH.search(tokens[0])):
        return "Host, Windows, and operating-system commands are blocked."
    prose = gh_prose_positions(tokens) if executable == "gh" else regex_prose_positions(tokens, executable)
    if any(WINDOWS_PATH.search(token) for index, token in enumerate(tokens[1:], 1)
           if index not in prose):
        return "Windows path arguments are blocked."
    return None


def task_state_path(path_text: str, cwd: str) -> bool:
    """True for an operand inside the PM's private task-state directory."""
    try:
        target = Path(path_text).expanduser()
        if not target.is_absolute():
            target = Path(cwd) / target
        root = (Path.home() / ".local/state/task").resolve()
        resolved = target.resolve(strict=False)
        return resolved == root or root in resolved.parents
    except (OSError, ValueError, RuntimeError):
        return ".local/state/task" in path_text.replace("\\", "/")


def task_state_write_reason(tokens: list[str], cwd: str) -> str | None:
    """Permit read commands, while protecting private state from writes."""
    unwrapped = unwrap_command(tokens)
    if isinstance(unwrapped, list) and unwrapped and Path(unwrapped[0]).name in {
            "cat", "ls", "head", "tail", "grep", "wc", "stat"} and not any(
            token in {">", ">>", ">|", "&>", "&>>", ">&", "<>"} for token in tokens):
        return None
    if any(task_state_path(token, cwd) for token in tokens if not token.startswith("-")):
        return "Writing or accessing PM task state from an agent command is blocked."
    return None


def evaluate(event: dict, approved_roots: tuple[Path, ...] | None = None) -> str | None:
    tool = event.get("tool_name")
    data = event.get("tool_input")
    if not isinstance(data, dict):
        return Denial("input.invalid", "Unrecognized tool input; blocked by policy.")
    if git_environment_override(event, data):
        return Denial("git.environment_override", "Git configuration and execution environment overrides are blocked.")
    cwd = str(event.get("cwd") or os.getcwd())
    anchor = os.environ.get("TASK_WORKTREE")
    root = root_for(anchor or cwd, approved_roots)
    if anchor and (not Path(anchor).is_absolute() or root != Path(anchor).resolve()
                   or not inside(cwd, cwd, root)):
        return Denial("workspace.boundary", "Work outside the lane task worktree is blocked.")
    if str(root) == "/__bootstrap_outside_approved_roots__":
        return Denial("workspace.boundary", "Work outside approved code and knowledge roots is blocked.")
    if tool in {"PowerShell", "Computer", "ComputerUse"}:
        return Denial("host.command_boundary", "Windows and host-computer actions are blocked.")
    if tool in {"Write", "Edit", "MultiEdit"}:
        paths = [data.get(k) for k in ("file_path", "path", "filename")
                 if isinstance(data.get(k), str)]
        if any(task_state_path(path, cwd) for path in paths):
            return Denial("task.private_state_write", "Agent writes to private task state are blocked.")
        metadata = git_metadata(root)
        if not paths or any(not writable(p, cwd, root, metadata) for p in paths):
            return Denial("filesystem.write_boundary", "File writes outside the current repository or into Git metadata are blocked.")
        return None
    if tool == "apply_patch":
        patch = data.get("patch", data.get("command", ""))
        if not isinstance(patch, str):
            return Denial("input.invalid", "Patch input is unrecognized; blocked by policy.")
        paths = re.findall(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$",
                           patch, re.MULTILINE)
        if any(task_state_path(path, cwd) for path in paths):
            return Denial("task.private_state_write", "Agent writes to private task state are blocked.")
        metadata = git_metadata(root)
        if not paths or any(not writable(p, cwd, root, metadata) for p in paths):
            return Denial("filesystem.write_boundary", "Patch writes outside the current repository or into Git metadata are blocked.")
        return None
    if tool != "Bash":
        return None
    command = data.get("command")
    if not isinstance(command, str) or not command.strip():
        return Denial("input.invalid", "Shell command is unrecognized; blocked by policy.")
    reason = evaluate_command(command, cwd, root, approved_roots)
    if not reason or isinstance(reason, Denial):
        return reason
    return Denial(command_rule(reason), reason)


def command_rule(reason: str) -> str:
    """Stable cause identifiers for the existing command denials."""
    for words, rule in (
            (("Multi-line", "Shell syntax", "Empty command", "Command chain", "Only ;"), "shell.syntax"),
            (("Shell constructs",), "shell.construct"),
            (("Inline shell", "Piping into", "Feeding a program"), "shell.interpreter"),
            (("Shell output", "Shell writes"), "filesystem.write_boundary"),
            (("Cannot identify the repository safely", "Cannot resolve folder safely"), "filesystem.folder_resolution"),
            (("Direct shell writes", "Git metadata", "repository root"), "filesystem.git_metadata"),
            (("Run git/gh network",), "command.network_chain"),
            (("A chain containing",), "command.mixed_chain"),
            (("git commit/merge",), "git.state_chain"),
            (("cd ",), "filesystem.cd_boundary"),
            (("sed ",), "command.sed"), (("awk ",), "command.awk"),
            (("sort ", "uniq "), "command.output_file"),
            (("Expanded file", "Recursive permission", "Ambiguous options"), "filesystem.operands"),
            (("env ", "Unsupported env"), "command.environment"),
            (("Unsupported timeout", "Unsupported nice", "Unsupported stdbuf", "timeout needs",
              "xargs, setsid"), "command.wrapper")):
        if any(word in reason for word in words):
            return rule
    return "command.other"


def split_command(command: str) -> tuple[list[str], list[str]] | str:
    """Split on unquoted ; && || | into segments; return a reason for anything else.

    Redirections (>&, &>, >|, 2>&1) are not separators; background & and |& are refused.
    """
    segments: list[str] = []
    operators: list[str] = []
    current = ""
    quote: str | None = None
    redirect = False
    index = 0
    while index < len(command):
        char = command[index]
        was_redirect, redirect = redirect, False
        if quote:
            if char in "\n\r":
                return "Quoted multi-line shell strings are blocked."
            current += char
            if char == "\\" and quote == '"' and index + 1 < len(command):
                if command[index + 1] in "\n\r":
                    return "Shell line continuations are blocked."
                index += 1
                current += command[index]
            elif char == quote:
                quote = None
        elif char == "\\" and index + 1 < len(command):
            if command[index + 1] in "\n\r":
                return "Shell line continuations are blocked."
            index += 1
            current += char + command[index]
        elif char in "'\"":
            quote = char
            current += char
        elif command[index:index + 3] == "<<<":
            current += "<<<"
            index += 2
        elif command[index:index + 2] == "<<":
            return "Heredocs are blocked; use the edit tool."
        elif char in "<>":
            redirect = True
            current += char
        elif char in ";&|" and (was_redirect and char in "&|"
                                or char == "&" and command[index + 1:index + 2] == ">"):
            current += char
        elif char in "\n\r":
            if current.strip():
                segments.append(current)
                operators.append(";")
                current = ""
        elif char in ";&|":
            end = index
            while end < len(command) and command[end] in ";&|":
                end += 1
            operator = command[index:end]
            if operator not in CHAIN_SEPARATORS:
                return "Only ;, &&, || and | may join commands; background and |& are blocked."
            if not current.strip():
                return "Empty command in a chain; blocked by policy."
            segments.append(current)
            operators.append(operator)
            current = ""
            index = end - 1
        else:
            current += char
        index += 1
    if quote:
        return "Shell syntax is unrecognized; blocked by policy."
    if current.strip():
        segments.append(current)
    elif operators and operators[-1] != ";":
        return "Command chain ends with an operator; blocked by policy."
    else:
        operators = operators[:-1]
    return segments, operators


def unsafe_env_name(name: str) -> bool:
    name = name.removesuffix("+").split("[", 1)[0]
    return name in UNSAFE_ENV_NAMES or name.startswith(UNSAFE_ENV_PREFIXES)


def unwrap_command(tokens: list[str]) -> list[str] | str:
    """Strip VAR=value prefixes and env/timeout/nice/stdbuf; refuse anything else that hides a command."""
    while tokens:
        if ENV_ASSIGNMENT.fullmatch(tokens[0]):
            if unsafe_env_name(tokens[0].split("=", 1)[0]):
                return Denial("command.unsafe_environment",
                              "Environment variables that change execution or Git behaviour are blocked.")
            tokens = tokens[1:]
            continue
        wrapper = Path(tokens[0]).name.lower()
        if pure_lookup(tokens):
            return tokens
        if wrapper in REFUSED_WRAPPERS:
            return "xargs, setsid, nohup and other command-hiding wrappers are blocked."
        if wrapper not in EVALUATED_WRAPPERS:
            return tokens
        if "/" in tokens[0] and Path(tokens[0]).parent not in {
                Path("/bin"), Path("/usr/bin"), Path("/usr/local/bin")}:
            return "Custom wrapper executable paths cannot be judged safely."
        index = 1
        if wrapper == "env":
            while index < len(tokens):
                token = tokens[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-i", "--ignore-environment", "-"}:
                    index += 1
                elif token == "-u" and index + 1 < len(tokens):
                    index += 2
                elif token.startswith("--unset=") or token.startswith("-u") and len(token) > 2:
                    index += 1
                elif ENV_ASSIGNMENT.fullmatch(token):
                    if unsafe_env_name(token.split("=", 1)[0]):
                        return Denial("command.unsafe_environment",
                                      "Environment variables that change execution or Git behaviour are blocked.")
                    index += 1
                elif token.startswith("-"):
                    return "Unsupported env option; blocked by policy."
                else:
                    break
            if index >= len(tokens):
                return "env without a command prints the environment; blocked by policy."
        else:
            switches, valued = WRAPPER_OPTIONS[wrapper]
            while index < len(tokens) and tokens[index].startswith("-"):
                token = tokens[index]
                key = token.split("=", 1)[0]
                if token == "--":
                    index += 1
                    break
                if token in switches or key in valued and "=" in token:
                    index += 1
                elif token in valued and index + 1 < len(tokens):
                    index += 2
                elif token[:2] in valued and not token.startswith("--") and len(token) > 2:
                    index += 1
                elif wrapper == "nice" and re.fullmatch(r"-\d+", token):
                    index += 1
                else:
                    return f"Unsupported {wrapper} option; blocked by policy."
            if wrapper == "timeout":
                if index >= len(tokens) or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", tokens[index]):
                    return "timeout needs a literal duration; blocked by policy."
                index += 1
        tokens = tokens[index:]
    return tokens


def pure_lookup(tokens: list[str]) -> bool:
    return bool(tokens) and (
        tokens[0] == "command" and len(tokens) == 3 and tokens[1] in {"-v", "-V"}
        and bool(re.fullmatch(r"[A-Za-z0-9_.+-]+", tokens[2]))
        or tokens[0] in {"type", "which"} and len(tokens) == 2
        and bool(re.fullmatch(r"[A-Za-z0-9_.+-]+", tokens[1])))


def git_subcommand(tokens: list[str], cwd: str) -> str | None:
    parsed = git_target(tokens, cwd) if Path(tokens[0]).name == "git" else None
    return parsed[1] if parsed else None


# git/gh run outside the sandbox only as a whole command; in a chain or pipe the whole
# call is sandboxed, where github.com is not reachable.
NETWORK_CHAIN_MESSAGE = ("Run git/gh network commands on their own; only then do they run "
                         "outside the sandbox. Use one call per command, e.g. "
                         "`git -C <path> push origin <branch>`, not `cd <path> && git push`.")
MIXED_CHAIN_MESSAGE = ("A chain containing git or gh may only add other git/gh commands or read-only "
                       "helpers (" + ", ".join(sorted(MIXED_CHAIN_TEXT_TOOLS - {"sed"})) +
                       "), sed -n, or command -v/-V, type and which lookups without file redirects "
                       "(stderr 2>&1 and 2>/dev/null are permitted). "
                       "Run git on its own instead: use `git -C <path> <command>` rather than "
                       "`cd <path> && git ...`, and run builds, tests and file writes as a separate call.")
STATE_CHAIN_MESSAGE = ("git commit/merge must not follow commands that may change the index or branch "
                       "in the same call, so the secret scan and default-branch guard see the real "
                       "state. Run them as separate calls, e.g. `git -C <path> add <files>` and then "
                       "`git -C <path> commit -m ...`.")
GIT_NETWORK_SUBCOMMANDS = {"fetch", "pull", "push", "ls-remote", "clone"}
GH_OFFLINE_COMMANDS = {"config", "alias", "completion", "help", "version"}


def network_command(head: str, tokens: list[str], cwd: str) -> bool:
    if head == "git":
        sub = git_subcommand(tokens, cwd)
        if sub is None:  # unparsable options: be conservative
            return any(token in GIT_NETWORK_SUBCOMMANDS for token in tokens[1:])
        return sub in GIT_NETWORK_SUBCOMMANDS
    if head == "gh":
        words = [token for token in tokens[1:] if not token.startswith("-")]
        return bool(words) and words[0] not in GH_OFFLINE_COMMANDS
    return False


SHELL_PROGRAMS = {"bash", "sh", "zsh", "dash"}
INDIRECTION_DEPTH = 8
PROGRAM_LIMIT = 128 * 1024


def program_text(path: Path) -> str:
    """Bound reads, including special files, before interpreting any source."""
    with open(path, "rb", buffering=0, opener=lambda p, flags: os.open(
            p, flags | os.O_NONBLOCK)) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = stream.read(PROGRAM_LIMIT + 1)
    if len(data) > PROGRAM_LIMIT or any(c == 127 or c < 32 and c not in (9, 10, 13) for c in data):
        raise ValueError("binary or oversized file")
    return data.decode("utf-8")


def judge_program(text: str, cwd: str, root: Path,
                  approved_roots: tuple[Path, ...] | None, depth: int,
                  allow_cd: bool = True) -> str | None:
    # This deliberately accepts a small shell language. Do not interpret another
    # language as shell, expand variables, execute a parser, or discard unknown syntax.
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        interpreter = shlex.split(lines[0][2:])
        if interpreter[:1] == ["/usr/bin/env"] and len(interpreter) == 2:
            interpreter = ["/usr/bin/" + interpreter[1]]
        if len(interpreter) != 1 or interpreter[0] not in {
                f"{directory}/{shell}" for directory in ("/bin", "/usr/bin")
                for shell in SHELL_PROGRAMS}:
            return "Script interpreter cannot be judged safely."
    body = "\n".join(line for line in lines if line.strip() and not line.lstrip().startswith("#"))
    if re.search(r"[$`*?{}\[\]#]", body) or "\\\n" in body:
        return "Script expansion or syntax cannot be judged safely."
    # source changes the caller's cwd, while recipe lines may run in separate
    # shells. Do not pretend to propagate that state across those boundaries.
    split = split_command(body)
    if isinstance(split, str):
        return split
    for segment in split[0]:
        tokens = unwrap_command(shlex.split(segment))
        if isinstance(tokens, str):
            return tokens
        if tokens and re.match(r"\d*[<>]", tokens[0]):
            return "Script leading redirections cannot be judged safely."
        if tokens and (not allow_cd and tokens[0] == "cd" or tokens[0].endswith("()")):
            return "Script cwd changes and functions cannot be judged safely."
        if tokens and tokens[0] in {
                "pushd", "popd", "hash", "rehash", "enable", "disable", "autoload",
                "emulate", "zmodload", "noglob", "nocorrect", "repeat", "foreach", "end", "-"}:
            return "Script shell state or command modifiers cannot be judged safely."
    return evaluate_command(body, cwd, root, approved_roots, depth)


def script_reason(tokens: list[str], cwd: str, root: Path,
                  approved_roots: tuple[Path, ...] | None, depth: int) -> str | None:
    head = Path(tokens[0]).name.lower()
    path = None
    if "/" in tokens[0]:
        path = Path(cwd) / tokens[0]
        try:
            resolved = path.resolve()
            # Installed commands retain their existing argument checks. Workspace
            # files with the same basename must not inherit that trust.
            if resolved.parent in {Path("/usr/bin"), Path("/usr/local/bin")}:
                info = resolved.stat()
                # Sandboxes may map root ownership to an overflow uid.
                if (info.st_uid != os.geteuid() or info.st_uid == 0) and not info.st_mode & 0o022:
                    if (path.parent not in {Path("/bin"), Path("/usr/bin"), Path("/usr/local/bin")}
                            and head != resolved.name and not {head, resolved.name} <= SHELL_PROGRAMS):
                        return "Executable aliases cannot be judged safely."
                    path = None
        except (OSError, ValueError, RuntimeError):
            return "Script cannot be read or judged safely."
    if path is None and head in SHELL_PROGRAMS:
        args = tokens[1:]
        while args and args[0] in {"-e", "-u", "-eu", "-ue", "-x", "--"}:
            option, *args = args
            if option == "--":
                break
        if not args or args[0].startswith("-"):
            return "Shell program requires a readable literal script and supported options."
        path = Path(cwd) / args[0]
    elif path is None and (head == "source" or tokens[0] == "."):
        if len(tokens) != 2 or "/" not in tokens[1]:
            return "Sourced files require one explicit literal path."
        path = Path(cwd) / tokens[1]
    if path is None:
        return None
    if depth >= INDIRECTION_DEPTH:
        return "Script/recipe recursion limit reached."
    if SHELL_EXPANSION.search(str(path)):
        return "Script path must be literal."
    try:
        return judge_program(program_text(path), cwd, root, approved_roots, depth + 1,
                             allow_cd=head != "source" and tokens[0] != ".")
    except (OSError, ValueError, RuntimeError):
        return "Script cannot be read or judged safely."


def recipe_base(root: Path) -> str:
    """Prefer origin's default, then local defaults, then the lane's source branch."""
    candidates = []
    try:
        candidates.append(_read_git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"))
    except subprocess.CalledProcessError:
        pass
    candidates.extend(("refs/heads/main", "refs/heads/master"))
    try:
        current = _read_git(root, "symbolic-ref", "--short", "HEAD")
        messages = _read_git(root, "reflog", "show", "--format=%gs", f"refs/heads/{current}")
        for message in reversed(messages.splitlines()):
            if message.startswith("branch: Created from "):
                source = message.removeprefix("branch: Created from ")
                if source != "HEAD":
                    candidates.append(source if source.startswith("refs/heads/")
                                      else f"refs/heads/{source}")
                else:
                    checkout = _read_git(root, "reflog", "show", "--format=%gs", "HEAD")
                    for move in reversed(checkout.splitlines()):
                        match = re.fullmatch(r"checkout: moving from (.+) to (.+)", move)
                        if match and match[2] == current:
                            candidates.append(f"refs/heads/{match[1]}")
                            break
                break
    except subprocess.CalledProcessError:
        pass
    for candidate in candidates:
        try:
            base = _read_git(root, "merge-base", "HEAD", candidate)
            if re.fullmatch(r"[0-9a-f]{40,64}", base):
                return base
        except subprocess.CalledProcessError:
            continue
    raise ValueError("No recipe base found: need origin/HEAD, local main/master, or the worktree source branch.")


def recipe_changed(path: Path, root: Path) -> bool:
    """Compare the working file (including untracked files) to the merge base."""
    relative = path.relative_to(root).as_posix()
    base = recipe_base(root)
    entry = _read_git(root, "ls-tree", base, "--", relative)
    if not entry:
        return True
    if not entry.startswith(("100644 blob ", "100755 blob ")):
        raise ValueError("recipe is not a regular tracked file")
    result = policy_subprocess(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--quiet", base, "--", relative],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=3)
    if result.returncode not in {0, 1}:
        raise ValueError("cannot compare recipe")
    return result.returncode == 1


def trusted_script_changes(text: str, cwd: str, root: Path,
                           approved_roots: tuple[Path, ...] | None, depth: int,
                           seen: frozenset[Path] = frozenset()) -> str | None:
    """Inspect literal script inputs of trusted bodies, including gate wrappers.

    Trust in existing shell syntax is retained; it does not transfer to changed
    literal script operands. Scan wrapper arguments too (e.g. `run bash ci/x.sh`).
    This is not a parser for dynamically constructed paths or other languages.
    """
    if depth >= INDIRECTION_DEPTH:
        return "Script/recipe recursion limit reached."
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    words = list(lexer)
    for index, word in enumerate(words):
        head = Path(word).name
        if word == "cd" and index + 1 < len(words):
            target = words[index + 1]
            if target == "--" and index + 2 < len(words):
                target = words[index + 2]
            if not SHELL_EXPANSION.search(target) and not target.startswith("-"):
                cwd = str((Path(cwd) / target).resolve())
        shell = head in SHELL_PROGRAMS | {"source", "."}
        interpreter = shell or head in INTERPRETERS or bool(re.fullmatch(r"python[0-9.]+", head))
        operand = None
        if interpreter:
            rest = words[index + 1:]
            # The options themselves are trusted here, but must not hide a
            # literal script operand (e.g. python3 -I ci/check.py).
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            if rest and not rest[0].startswith("-"):
                operand = rest[0]
        elif ("/" in word or Path(word).suffix in {".sh", ".py", ".js", ".mjs", ".cjs"}) \
                and not word.startswith("-"):
            operand = word
        if operand is None or SHELL_EXPANSION.search(operand):
            continue
        path = Path(cwd) / operand
        if not path.is_file():
            if interpreter and ("/" in operand or Path(operand).suffix in {".sh", ".py", ".js"}):
                return "Trusted recipe script cannot be read safely."
            continue
        resolved = path.resolve()
        if not interpreter:
            if resolved.parent in {Path("/usr/bin"), Path("/usr/local/bin")}:
                continue
            if path.suffix not in {".sh", ".py", ".js", ".mjs", ".cjs"} and not os.access(path, os.X_OK):
                continue
        if not resolved.is_relative_to(root):
            return "Trusted recipe script must stay inside the repository."
        if resolved in seen:
            continue
        changed = path.is_symlink() or recipe_changed(path, root)
        body = program_text(path)
        if changed:
            if interpreter and not shell or not interpreter and path.suffix in {
                    ".py", ".js", ".mjs", ".cjs"}:
                return "Changed non-shell script cannot be judged safely."
            reason = judge_program(body, cwd, root, approved_roots, depth + 1,
                                   allow_cd=head not in {"source", "."})
        elif shell or body.startswith(("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash",
                                       "#!/usr/bin/env sh")):
            reason = trusted_script_changes(body, cwd, root, approved_roots, depth + 1,
                                            seen | {resolved})
        else:
            reason = None
        if reason:
            return reason
    return None


def recipe_reason(tokens: list[str], cwd: str, root: Path,
                  approved_roots: tuple[Path, ...] | None, depth: int) -> str | None:
    runner = Path(tokens[0]).name.lower()
    if runner in {"bunx"}:
        return "Fetch-and-run package commands are blocked."
    if runner == "npx":
        if depth >= INDIRECTION_DEPTH:
            return "Script/recipe recursion limit reached."
        args = tokens[1:]
        if not args or args[0] not in {"--no-install", "--no"}:
            return "npx requires explicit --no-install or --no and an installed local binary."
        args = args[1:]
        if not args or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", args[0]):
            return "npx requires a literal local binary without runner options."
        path = Path(cwd) / "node_modules" / ".bin" / args[0]
        if not path.is_file() or not os.access(path, os.X_OK):
            return "npx binary is not already installed in node_modules/.bin."
        if not path.resolve().is_relative_to(Path(cwd).resolve() / "node_modules"):
            return "npx binary must resolve within local node_modules."
        return evaluate_segment([str(path), *args[1:]], cwd, root, approved_roots, depth + 1)
    names = {"just": ("justfile", "Justfile", ".justfile"),
             "make": ("GNUmakefile", "makefile", "Makefile"),
             "gmake": ("GNUmakefile", "makefile", "Makefile"),
             "npm": ("package.json",), "pnpm": ("package.json",),
             "yarn": ("package.json",), "bun": ("package.json",)}
    if runner not in names:
        return None
    args = tokens[1:]
    package_runner = runner in {"npm", "pnpm", "yarn", "bun"}
    if package_runner:
        if args and args[0] in {"exec", "dlx", "x", "create"}:
            return "Fetch-and-run package commands are blocked."
        if args and args[0] in {"outdated", "audit", "list", "ls", "view", "info",
                               "--version", "--help"}:
            return None
        if not args or args[0] not in {"run", "run-script", "test", "start", "stop", "restart",
                                      "install", "ci", "add", "remove", "uninstall", "update"}:
            return "Package runner shorthand and global options cannot be judged reliably."
        # All script values are checked below, including install/prepare hooks.
        args = args[1:]
    if any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", arg) or arg.startswith("-") for arg in args):
        return "Recipe options, overrides and parameters cannot be judged safely."
    if depth >= INDIRECTION_DEPTH:
        return "Script/recipe recursion limit reached."
    try:
        directory = Path(cwd).resolve()
        while True:
            candidates = [directory / name for name in names[runner] if (directory / name).exists()]
            if (candidates or runner not in {"just", "npm", "yarn", "bun"} or directory == root
                    or root not in directory.parents):
                break
            directory = directory.parent
        if len(candidates) != 1:
            return "Cannot identify one recipe definition safely."
        path = candidates[0]
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            return "Recipe definition must be a regular file in the current repository."
        text = program_text(path)
        # Local runner settings can replace the shell or load executable hooks,
        # independently of an unchanged recipe file.
        config_names = (".npmrc", ".pnpmfile.cjs", "pnpm-workspace.yaml",
                        ".yarnrc", ".yarnrc.yml", "bunfig.toml") if package_runner else (
                            ".env",) if runner == "just" else ()
        config_directory = directory
        has_config = False
        while True:
            for name in config_names:
                config = config_directory / name
                if config.exists():
                    has_config = True
                    if config.is_symlink() or recipe_changed(config, root):
                        return "Changed runner configuration cannot be judged reliably."
            if config_directory == root or root not in config_directory.parents:
                break
            config_directory = config_directory.parent
        # These can import definitions that changed independently of this file.
        if runner == "just" and re.search(r"(?m)^\s*(?:import|mod)\b", text):
            return "Just imports and modules cannot be judged reliably."
        if runner in {"make", "gmake"} and re.search(r"(?m)^\s*(?:-?include|sinclude)\b", text):
            return "Make includes cannot be judged reliably."
        changed = recipe_changed(path, root)
        if not changed:
            if package_runner:
                package = json.loads(text)
                scripts = package.get("scripts", {})
                if not isinstance(scripts, dict) or not all(isinstance(b, str) for b in scripts.values()):
                    return "Package scripts cannot be judged safely."
                text = "\n".join(scripts.values())
            return trusted_script_changes(text, str(directory), root, approved_roots, depth)
        if has_config:
            return "Changed recipes with runner configuration cannot be judged reliably."
        if runner in {"make", "gmake"}:
            return "Changed Make definitions cannot be judged reliably."
        if package_runner:
            package = json.loads(text)
            scripts = package.get("scripts", {}) if isinstance(package, dict) else None
            if not isinstance(scripts, dict) or not all(isinstance(body, str) for body in scripts.values()):
                return "Package scripts cannot be judged safely."
            bodies = list(scripts.values())  # Includes lifecycle hooks, not just the named script.
        else:
            bodies = []
            body = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                if line[0].isspace():
                    if not bodies:
                        return "Just recipe body without a literal header."
                    command = line.lstrip().removeprefix("@")
                    if command.startswith(("-", "+", "@")):
                        return "Just command prefixes cannot be judged reliably."
                    body.append(command)
                elif line.startswith("#"):
                    continue
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*:\s*(?:[A-Za-z_][A-Za-z0-9_-]*\s*)*", line):
                    body = []
                    bodies.append(body)
                else:
                    return "Changed Just syntax cannot be judged reliably."
            bodies = ["\n".join(body) for body in bodies]
        for body in bodies:
            reason = judge_program(body, str(directory), root, approved_roots, depth + 1,
                                   allow_cd=False)
            if reason:
                return reason
    except ValueError as exc:
        if str(exc).startswith("No recipe base found:"):
            return str(exc)
        return "Recipe definition or merge base cannot be read safely."
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return "Recipe definition or merge base cannot be read safely."
    return None


def evaluate_command(command: str, cwd: str, root: Path,
                     approved_roots: tuple[Path, ...] | None, depth: int = 0) -> str | None:
    repeat = re.fullmatch(
        r"for [A-Za-z_][A-Za-z0-9_]* in (?:\$\(seq 1 [0-9]+\)|\{1\.\.[0-9]+\});\s*do (.+?);\s*done\s*",
        command)
    if repeat:
        return evaluate_command(repeat[1], cwd, root, approved_roots, depth)
    if "$(" in command or "`" in command:
        return "Dynamic ($(...), backtick) shell commands are blocked."
    loop = re.fullmatch(r"for ([A-Za-z_][A-Za-z0-9_]*) in (.+?);\s*do (.+?);\s*done\s*", command)
    if loop:
        variable, values, body = loop.groups()
        try:
            operands = shlex.split(values)
            body_tokens = shlex.split(body)
        except ValueError:
            operands, body_tokens = [], []
        if (operands and body_tokens and body_tokens[0] in {
                "cat", "ls", "head", "tail", "grep", "wc", "stat"}
                and all(re.fullmatch(r"[A-Za-z0-9_./:+-]+", value) for value in operands)
                and not any(char in body for char in ";&|<>")
                and all(not SHELL_EXPANSION.search(token.replace("${" + variable + "}", "").replace(
                    "$" + variable, "")) for token in body_tokens)):
            for value in operands:
                expanded = body.replace("${" + variable + "}", shlex.quote(value)).replace(
                    "$" + variable, shlex.quote(value))
                reason = evaluate_command(expanded, cwd, root, approved_roots, depth)
                if reason:
                    return reason
            return None
    split = split_command(command)
    if isinstance(split, str):
        return split
    segments, operators = split
    plain_chain = all(operator in {";", "&&"} for operator in operators)
    effective = cwd
    all_tokens: list[list[str]] = []
    for text in segments:
        try:
            lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            lexer.escape = ""
            raw_tokens = unwrap_command(list(lexer))
            if isinstance(raw_tokens, list) and raw_tokens:
                reason = host_command_reason(raw_tokens, Path(raw_tokens[0]).name.lower())
                if reason:
                    return Denial("host.command_boundary", reason)
            lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
            if tokens and Path(tokens[0]).name not in {"git", "gh"}:
                # Keep the original text intact. Only discard shlex tokens
                # proven to start at an unquoted, unescaped new word.
                drop = set()
                quote = None
                escaped = False
                word_start = True
                for offset, char in enumerate(text):
                    if escaped:
                        escaped = False
                        continue
                    if char == "\\" and quote != "'":
                        escaped = True
                        word_start = False
                        continue
                    if quote:
                        if char == quote:
                            quote = None
                        continue
                    if char in {"'", '"'}:
                        quote = char
                        word_start = False
                        continue
                    if word_start and char == "2":
                        target = offset + 2
                        while target < len(text) and text[target].isspace():
                            target += 1
                        for spelling, expected in (
                                (text[offset:target] + "/dev/null",
                                 ["2", ">", "/dev/null"]),
                                ("2>&1", ["2", ">&", "1"])):
                            end = offset + len(spelling)
                            if (text.startswith("2>", offset)
                                    and text.startswith(spelling, offset)
                                    and (end == len(text) or text[end].isspace())):
                                prefix = shlex.shlex(text[:offset], posix=True,
                                                     punctuation_chars=";&|<>")
                                prefix.whitespace_split = True
                                prefix.commenters = ""
                                index = len(list(prefix))
                                if tokens[index:index + len(expected)] == expected:
                                    drop.update(range(index, index + len(expected)))
                    word_start = char.isspace()
                tokens = [token for index, token in enumerate(tokens) if index not in drop]
            all_tokens.append(tokens)
        except ValueError:
            return "Shell syntax is unrecognized; blocked by policy."
    for tokens in all_tokens:
        state_reason = task_state_write_reason(tokens, cwd)
        if state_reason:
            return Denial("task.private_state_write", state_reason)
    had_chain = len(all_tokens) > 1
    while all_tokens and all_tokens[0][:1] == ["set"]:
        options = all_tokens[0][1:]
        valid = bool(options)
        while options and valid:
            option, *options = options
            if re.fullmatch(r"-[eu]+", option):
                continue
            if re.fullmatch(r"-[eu]*o", option) and options[:1] == ["pipefail"]:
                options = options[1:]
                continue
            valid = False
        if not valid or not operators or operators[0] not in {";", "&&"}:
            return "Shell constructs: unsupported set options."
        all_tokens.pop(0)
        operators.pop(0)
    # Recognize a bounded single-line conditional, then inspect every branch in
    # order. Keeping one list preserves the conservative index-state chain guard.
    conditional_else = None
    conditional = all_tokens and all_tokens[0][:1] == ["if"] and "\n" not in command
    if conditional:
        if (len(all_tokens) < 3 or all_tokens[-1] != ["fi"]
                or any(op != ";" for op in (operators[0], operators[-1]))):
            return "Shell constructs: unsupported conditional."
        all_tokens[0] = all_tokens[0][1:]
        if all_tokens[1][:1] != ["then"]:
            return "Shell constructs: conditional needs then."
        all_tokens[1] = all_tokens[1][1:]
        seen_else = False
        for index in range(2, len(all_tokens) - 1):
            if all_tokens[index][:1] == ["else"]:
                if seen_else or operators[index - 1] != ";":
                    return "Shell constructs: unsupported else."
                seen_else = True
                conditional_else = index
                all_tokens[index] = all_tokens[index][1:]
        all_tokens.pop()
        operators.pop()
        if any(not tokens for tokens in all_tokens):
            return "Shell constructs: empty conditional part."
    if had_chain:
        heads: list[tuple[str, list[str]]] = []
        for tokens in all_tokens:
            unwrapped = unwrap_command(tokens)
            if isinstance(unwrapped, str):
                return unwrapped
            heads.append((Path(unwrapped[0]).name.lower() if unwrapped else "", unwrapped))
        for head, unwrapped in heads:
            if network_command(head, unwrapped, effective):
                return NETWORK_CHAIN_MESSAGE
        if any(head in {"git", "gh"} for head, _ in heads):
            # Sequential segments get their ordinary checks below. Preserve the
            # command-hiding helper guards and the stricter pipeline checks.
            for head, unwrapped in heads:
                if head in {"git", "gh"}:
                    continue
                writes = any(token in {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
                             and (i + 1 >= len(unwrapped) or unwrapped[i + 1] not in NULL_DEVICES)
                             for i, token in enumerate(unwrapped))
                find_writes = head == "find" and any(
                    token.startswith(("-exec", "-ok", "-fprint"))
                    or token in {"-delete", "-fls"}
                    for token in unwrapped[1:])
                rg_executes = head == "rg" and any(
                    token == "--pre" or token.startswith("--pre=")
                    for token in unwrapped[1:])
                file_writes = head == "file" and any(
                    token == "--compile" or (token.startswith("-")
                                             and not token.startswith("--") and "C" in token)
                    for token in unwrapped[1:])
                sed_writes = head == "sed" and not readonly_sed(unwrapped)
                if (find_writes or rg_executes or file_writes or not plain_chain and (
                        (head not in MIXED_CHAIN_TEXT_TOOLS | {"git", "gh"}
                         and not pure_lookup(unwrapped)) or writes or sed_writes)):
                    return MIXED_CHAIN_MESSAGE
    seen_changing = False
    branch_cwd = effective
    for position, tokens in enumerate(all_tokens):
        if conditional and position == 1:
            branch_cwd = effective
        if position == conditional_else:
            effective = branch_cwd
        piped = position > 0 and operators[position - 1] == "|"
        unwrapped = unwrap_command(tokens)
        if isinstance(unwrapped, str):
            return unwrapped
        head = Path(unwrapped[0]).name.lower() if unwrapped else ""
        if head == "cd":
            if not plain_chain or len(unwrapped) != 2 or unwrapped[1].startswith("-") \
                    or SHELL_EXPANSION.search(unwrapped[1]) or unwrapped != tokens:
                return "cd is allowed only as cd <literal path> joined by ; or &&."
            try:
                effective = str((Path(effective) / unwrapped[1]).resolve())
            except (OSError, ValueError, RuntimeError):
                return "Cannot resolve cd path safely; blocked by policy."
            if not inside(effective, effective, root):
                return "cd outside the current repository is blocked."
            continue
        if (piped and head in INTERPRETERS | {"python3"} or piped and re.fullmatch(r"python[0-9.]+", head)):
            return "Piping into a shell or interpreter is blocked."
        if (unwrapped and head == "git" and position > 0
                and git_subcommand(unwrapped, effective) in STATE_DEPENDENT_GIT and seen_changing):
            return STATE_CHAIN_MESSAGE
        indirect = head in SHELL_PROGRAMS | {"source", ".", "", "just", "make", "gmake", "npm", "pnpm",
                                           "yarn", "bun", "bunx", "npx"}
        if unwrapped and "/" in unwrapped[0]:
            try:
                indirect |= (Path(effective) / unwrapped[0]).resolve().parent not in {
                    Path("/usr/bin"), Path("/usr/local/bin")}
            except (OSError, ValueError, RuntimeError):
                return "Script path cannot be resolved safely."
        if seen_changing and indirect:
            return "Run scripts and recipes separately from commands that may change their definitions."
        reason = evaluate_segment(tokens, effective, root, approved_roots, depth)
        if reason:
            return reason
        if unwrapped:
            sub = git_subcommand(unwrapped, effective) if head == "git" else None
            output_ops = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
            redirects = any(token in output_ops
                            and (i + 1 >= len(tokens) or tokens[i + 1] not in NULL_DEVICES)
                            for i, token in enumerate(tokens))
            redirect_only = len(unwrapped) % 2 == 0 and all(
                unwrapped[i] in output_ops for i in range(0, len(unwrapped), 2))
            if redirects or not (redirect_only or head in READ_ONLY_COMMANDS
                    or head == "sed" and readonly_sed(unwrapped)
                    or sub in READ_ONLY_GIT or pure_lookup(unwrapped)):
                seen_changing = True
    return None


def evaluate_segment(tokens: list[str], cwd: str, root: Path,
                     approved_roots: tuple[Path, ...] | None, depth: int = 0) -> str | None:
    """Apply the single-command rules to one chain segment (wrappers already vetted)."""
    unwrapped = unwrap_command(tokens)
    if isinstance(unwrapped, str):
        return unwrapped
    tokens = unwrapped
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    if executable in SHELL_CONSTRUCTS - {"source", "."} or tokens[0].startswith(("(", "{")) or any(
            tokens[i] in {"<", ">", ">>"} and tokens[i + 1].startswith("(")
            for i in range(len(tokens) - 1)):
        return "Shell constructs (eval, source, export, subshells, loops, process substitution) are blocked."
    if any(token in {"<", "<<", "<<<"} for token in tokens[1:]) and (
            executable in INTERPRETERS or re.fullmatch(r"python[0-9.]+", executable)):
        return "Feeding a program to a shell or interpreter on stdin is blocked."
    if (executable in INTERPRETERS or re.fullmatch(r"python[0-9.]+", executable)) and any(
        t in {"--eval", "--print"} or INLINE_FLAG.fullmatch(t) and any(c in t for c in "ce")
        for t in tokens[1:]
    ):
        return "Inline shell and interpreter programs are blocked."
    host_reason = host_command_reason(tokens, executable)
    if host_reason:
        return Denial("host.command_boundary", host_reason)
    if executable == "sed":
        reason = sed_policy(tokens)
        if reason:
            return reason
    if executable in AWK_NAMES:
        reason = awk_policy(tokens)
        if reason:
            return reason
    if executable == "sort" and any(
            t.startswith("--o") and "--output".startswith(t.split("=", 1)[0])
            or t.startswith("-") and not t.startswith("--") and t[1:].isalpha() and "o" in t
            for t in tokens[1:]):
        return "sort -o/--output writes a file; redirect inside the repository instead."
    if executable == "uniq" and len([t for t in tokens[1:] if not t.startswith("-")]) > 1:
        return "uniq with an output file operand is blocked; redirect inside the repository instead."
    output_operators = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
    metadata = git_metadata(root) if executable in WRITERS | {"sed"} or any(
        token in output_operators for token in tokens) else None
    for i, token in enumerate(tokens):
        if token in output_operators and i + 1 < len(tokens) and tokens[i + 1] in NULL_DEVICES:
            continue
        if token in output_operators and (i + 1 >= len(tokens) or SHELL_EXPANSION.search(tokens[i + 1])
                or not writable(tokens[i + 1], cwd, root, metadata)):
            return "Shell output outside the current repository or into Git metadata is blocked."
    # Redirect targets were just validated; they are not arguments of the command.
    command_tokens: list[str] = []
    skip = False
    for i, token in enumerate(tokens):
        if skip:
            skip = False
        elif token in output_operators:
            skip = True
        else:
            command_tokens.append(token)
    tokens = command_tokens
    if not tokens:
        return None
    reason = script_reason(tokens, cwd, root, approved_roots, depth)
    if reason:
        return Denial("command.script", reason)
    reason = recipe_reason(tokens, cwd, root, approved_roots, depth)
    if reason:
        return Denial("command.recipe", reason)
    args = [t for t in tokens[1:] if not t.startswith("-")]
    targets: list[str] = []
    inspected: list[str] = []
    if executable in {"cp", "mv", "install"}:
        transfer = transfer_paths(tokens, executable, cwd)
        if transfer is None and executable == "cp":
            transfer = work_copy_paths(tokens, cwd, root, metadata)
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
        if executable in {"rm", "chmod", "chown"}:
            if any(SHELL_EXPANSION.search(t) for t in targets):
                return "Expanded file operands are blocked; use explicit paths."
            folders = [t for t in targets if (Path(cwd) / t).is_dir() and not (Path(cwd) / t).is_symlink()]
            if folders and executable != "rm":
                return "Recursive permission changes are blocked; use explicit file paths."
            for folder in folders:
                reason = folder_delete_reason(Path(cwd) / folder, root, metadata)
                if reason:
                    return reason
    if executable == "sed" and any(t == "-i" or t.startswith("-i") or t.startswith("--in-place")
                                  for t in tokens[1:]):
        targets = args[-1:]
        inspected = args
    if any(SHELL_EXPANSION.search(t) or metadata is None or protected_git_path(t, cwd, metadata)
           for t in inspected):
        return "Direct shell writes to Git metadata or expanded file operands are blocked."
    if any(SHELL_EXPANSION.search(t) or not (
            writable(t, cwd, root, metadata)
            or executable == "tee" and metadata is not None
            and inside(t, cwd, Path("/tmp"))
            and not protected_git_path(t, cwd, metadata)) for t in targets):
        return "Shell writes outside the current repository or into Git metadata are blocked."
    if executable == "git":
        reason = git_policy(tokens, cwd, root, approved_roots)
        return Denial("git.command_policy", reason) if reason else None
    if executable == "gh":
        reason = gh_policy(tokens)
        return Denial("github.command_policy", reason) if reason else None
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
    split = split_command(command)
    if isinstance(split, str) or len(split[0]) != 1:
        return "Chained commands may not leave the sandbox; send one Git or GitHub CLI command."
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "Unrecognized approval command; blocked by policy."
    if not tokens or tokens[0] not in {"git", "gh", "/usr/bin/git", "/usr/bin/gh"}:
        return "Only Git and GitHub CLI may request network or sandbox escalation."
    # No hook grant: native allow rules cover routine Git/gh; destructive prompt
    # rules still require the user's approval. Forbidden operations fail above.
    return None


def first_command_category(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unparsed"
    index = 0
    while index < len(tokens) and ENV_ASSIGNMENT.fullmatch(tokens[index]):
        index += 1
    if index < len(tokens) and Path(tokens[index]).name == "env":
        index += 1
        while index < len(tokens):
            if ENV_ASSIGNMENT.fullmatch(tokens[index]):
                index += 1
            elif tokens[index] == "-u" and index + 1 < len(tokens):
                index += 2
            elif tokens[index].startswith("-"):
                index += 1
            else:
                break
    return Path(tokens[index]).name if index < len(tokens) else ""


def event_vendor(event: dict, vendor: str | None = None) -> str:
    return vendor if vendor in {"claude", "codex"} else "unknown"


def audit_text(value: str) -> str:
    value = str(value)
    def redact_run(match):
        token_start = re.split(r"\s", value[:match.start()])[-1]
        token = token_start + match[0]
        path = (token.startswith(("./", "../", "~/", "$HOME/", "/home/", "/tmp/", "/usr/",
                                  "/opt/", "/etc/", "/var/", "/mnt/", "/proc/", "/run/",
                                  "/srv/", "/root/"))
                or token.startswith("/") and any(re.search(r"[._-]", segment)
                                                  for segment in token.split("/")))
        return match[0] if path else "[REDACTED]"

    # Redact before truncating: a token may cross the audit length boundary.
    value = re.sub(
        r"(?i)(?:ghp_|ghs_|ghu_|ghr_|github_pat_|gho_|\bsk-or-|\bsk-|\bxox[a-z]-)[A-Za-z0-9_-]+"
        r"|AKIA[A-Z0-9]+|Bearer\s+[^\s\"']+"
        , "[REDACTED]", value)
    value = re.sub(r"[A-Za-z0-9+/=_-]{32,}",
                   redact_run, value)
    value = re.sub(
        r"(?i)((?:token|key|secret|password|passwd|authorization)\b\s*[=:]\s*"
        r"|--[\w-]*(?:token|key|secret|password|passwd)\b\s+)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s]+)", r"\1[REDACTED]", value)
    return value[:200]


def log_denial(event: dict, denial: str | None = None, vendor: str | None = None):
    """Best-effort denial audit with bounded, redacted context."""
    try:
        data = event.get("tool_input", {})
        command = data.get("command", "") if isinstance(data, dict) else ""
        category = first_command_category(command) if isinstance(command, str) else ""
        cwd = Path(str(event.get("cwd") or os.getcwd()))
        repo = ""
        try:
            result = policy_subprocess(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, timeout=2)
            if result.returncode == 0:
                repo = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        vendor = event_vendor(event, vendor)
        if not denial:
            denial = evaluate(event) or Denial("policy.command_evaluation", "Blocked by policy.")
        rule_id = getattr(denial, "rule_id", "policy.command_evaluation")
        entry = {"time": datetime.now(timezone.utc).isoformat(), "vendor": vendor, "repo": repo,
                 "tool": str(event.get("tool_name", "")), "rule_id": rule_id,
                 "category": category, "reason": audit_text(str(denial or "")),
                 "command": audit_text(command if command else str(next((
                     data[key] for key in ("file_path", "path", "filename") if key in data), "")))}
        state_dir = os.environ.get("AGENT_POLICY_STATE_DIR")
        target = (Path(state_dir) if state_dir else Path.home() / ".local/state/agent-policy") / "denials.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        # Audit failure must never change the policy decision.
        return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--vendor", choices=("claude", "codex"))
    options = argparse.Namespace(vendor=None)
    try:
        options, _unknown = parser.parse_known_args(argv)
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError
    except (Exception, SystemExit) as error:
        reason = (Denial("hook.input", "Hook input invalid; blocked by policy.")
                  if isinstance(error, (json.JSONDecodeError, ValueError)) else
                  Denial("hook.error", f"policy hook error: {type(error).__name__}: {audit_text(str(error))}"))
        print(json.dumps(deny(reason, {}, options.vendor), separators=(",", ":")))
        return 0
    permission_request = event.get("hook_event_name") == "PermissionRequest"
    try:
        reason = permission_reason(event) if permission_request else evaluate(event)
    except Exception as error:
        reason = Denial("hook.error", f"policy hook error: {type(error).__name__}: {audit_text(str(error))}")
    if reason:
        if permission_request and not isinstance(reason, Denial):
            reason = Denial("permission.policy", reason)
        response = deny(reason, event, options.vendor)
        print(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
