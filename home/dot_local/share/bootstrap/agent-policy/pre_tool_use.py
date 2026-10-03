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
              "wsl", "wsl.exe", "wslconfig.exe", "diskpart", "diskpart.exe", "schtasks", "schtasks.exe"}
HOST_OPS = {"sudo", "su", "systemctl", "service", "apt", "apt-get", "dpkg",
            "mount", "umount", "shutdown", "reboot", "modprobe", "useradd",
            "adduser", "usermod", "passwd", "visudo"}
# Inspect paths in argv, never a substring in an entire shell/body string.
# Unquoted Windows backslashes can be consumed by shlex, so drive prefixes
# remain blocked even after that normalization.
WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/](?!/)|"
                          r"(?:^|=|^-[A-Za-z]+)[A-Z]:|/mnt/[a-z](?:/|$)|"
                          r"(?:^|=|^-[A-Za-z]+)\\(?:\\|[A-Za-z0-9_-]))")
HOST_EXECUTABLE = re.compile(r"(?i)\.(?:exe|com)$")
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
        configured = subprocess.run(
            ["git", "config", "--get-regexp", "^submodule[.]"], cwd=root,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=3)
        if configured.returncode != 1:
            return True
        modules_file = root / ".gitmodules"
        if (modules_file.exists() or modules_file.is_symlink()
                or any((directory / "modules").exists() for directory in metadata)):
            return True
        index = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=root,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, check=True, timeout=3)
        if any(record.startswith("160000 ") for record in index.stdout.split(chr(0))):
            return True
        head = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                              cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=3)
        if head.returncode == 1:
            return False  # An unborn repository has no committed gitlinks.
        if head.returncode != 0:
            return True
        tree = subprocess.run(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=root,
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
            head = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"],
                                  cwd=target, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, timeout=3)
            if head.returncode == 0:
                reference = head.stdout.strip()
                if not reference.startswith("refs/heads/"):
                    return "Cannot identify the default fetch branch safely; specify origin."
                configured = subprocess.run(
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
            group = subprocess.run(["git", "config", "--get-all", "remotes." + remote],
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
        result = subprocess.run(command, cwd=target, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return "Git remote cannot be resolved safely; use a configured GitHub remote."
    urls = result.stdout.splitlines()
    if not urls or any(not github_url(url) for url in urls):
        return "Local filesystem and non-GitHub remote destinations are blocked."
    return None



def git_default_branch_merge(arguments: list[str], target: Path) -> str | None:
    """Only merge the locally recorded GitHub origin default into a task branch."""
    try:
        def read_git(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=target, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=True, timeout=3).stdout.strip()
        current = read_git("symbolic-ref", "--quiet", "HEAD")
        default = read_git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        prefix = "refs/remotes/origin/"
        if (not current.startswith("refs/heads/") or not default.startswith(prefix)
                or default == prefix + "HEAD"):
            return "Cannot identify the current and origin default branch safely."
        name = default[len(prefix):]
        if current == "refs/heads/" + name:
            return "Merging into the default branch is blocked; update only a task branch."
        wanted = "origin/" + name
        if arguments not in ([wanted], ["--no-edit", wanted]):
            return "Only git merge [--no-edit] origin/<default branch> is allowed."
        read_git("rev-parse", "--verify", default + "^{commit}")
        if read_git("rev-parse", "--symbolic-full-name", "--verify", wanted) != default:
            return "The merge source must resolve uniquely to the origin default ref."
        # Git applies branch mergeOptions before argv, including custom strategies.
        options = subprocess.run(
            ["git", "config", "--get-all", "branch." + current[len("refs/heads/"):] + ".mergeOptions"],
            cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=3)
        if options.returncode != 1:
            return "Configured merge options or unreadable configuration are blocked."
        urls = read_git("remote", "get-url", "--all", "origin").splitlines()
        if len(urls) != 1:
            return "Origin must have one unambiguous GitHub URL."
    except (OSError, subprocess.SubprocessError):
        return "Cannot verify merge metadata; origin/HEAD and its commit must exist."
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
    cross_repo = target_root != root
    sibling_fetch = cross_repo and subcommand == "fetch"
    if sibling_fetch and arguments not in ([], ["origin"]):
        return "Sibling fetch permits only fetch or fetch origin; options/refspecs are blocked."
    if sibling_fetch and sibling_fetch_has_submodules(target_root):
        return "Sibling fetch with submodules is blocked to avoid unchecked recursive destinations."
    if cross_repo and subcommand not in READ_ONLY_GIT and not sibling_fetch:
        return "Git writes outside the current repository are blocked."
    if subcommand == "merge":
        return git_default_branch_merge(arguments, target)
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
        if subcommand in {"add", "mv", "rm", "restore"} and any(
                (target / arg).is_dir() for arg in operands):
            return "Git directory mutations are blocked; use explicit non-metadata file paths."
    if subcommand == "config" and not git_config_read_only(arguments):
        return "Agent Git configuration writes are blocked; only --get/--list reads are permitted."
    if cross_repo and not sibling_fetch:
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
        "--add-reviewer", "--remove-reviewer",
    }
    switches = {
        "--draft", "-d", "--editor", "-e", "--web", "-w", "--edit-last",
        "--create-if-none", "--delete-last", "--yes", "--help", "-h",
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


def host_command_reason(tokens: list[str], executable: str) -> str | None:
    """Block host execution and real Windows path arguments, not gh prose."""
    if (SHELL_EXPANSION.search(tokens[0]) or executable in HOST_NAMES | HOST_OPS
            or HOST_EXECUTABLE.search(tokens[0])
            or WINDOWS_PATH.search(tokens[0])):
        return "Host, Windows, and operating-system commands are blocked."
    prose = gh_prose_positions(tokens) if executable == "gh" else set()
    if any(WINDOWS_PATH.search(token) for index, token in enumerate(tokens[1:], 1)
           if index not in prose):
        return "Windows path arguments are blocked."
    return None


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
    host_reason = host_command_reason(tokens, executable)
    if host_reason:
        return host_reason
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
    if executable == "gh":
        return gh_policy(tokens)
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
    # No hook grant: native allow rules cover routine Git/gh; destructive prompt
    # rules still require the user's approval. Forbidden operations fail above.
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
