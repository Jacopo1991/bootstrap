#!/usr/bin/env python3
"""Hosted tests for native settings and policy-hook negative controls."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
assert (ROOT / ".chezmoiroot").read_text(encoding="utf-8").strip() == "home"
codex = tomllib.loads((ROOT / "home/dot_codex/config.toml").read_text(encoding="utf-8"))
assert codex["approval_policy"] == "on-request"
assert codex["approvals_reviewer"] == "user"
assert codex["sandbox_mode"] == "workspace-write"
assert codex["sandbox_workspace_write"]["network_access"] is False
assert codex["features"]["hooks"] is True
codex_hooks = json.loads((ROOT / "home/dot_codex/hooks.json").read_text(encoding="utf-8"))
assert set(codex_hooks["hooks"]) == {"PreToolUse", "PermissionRequest"}
assert codex_hooks["hooks"]["PermissionRequest"][0]["matcher"] == "^Bash$"
assert {x["matcher"] for x in codex_hooks["hooks"]["PreToolUse"]} == {"^Bash$", "^(apply_patch|Edit|Write)$"}

claude = json.loads((ROOT / "home/dot_claude/settings.json").read_text(encoding="utf-8"))
assert claude["permissions"]["defaultMode"] == "acceptEdits"
assert "ask" not in claude["permissions"]
assert claude["permissions"]["allow"] == ["Bash(git *)", "Bash(gh *)",
    "Bash(/usr/bin/git *)", "Bash(/usr/bin/gh *)"]
assert claude["sandbox"] == {"enabled": True, "allowUnsandboxedCommands": False,
    # Bare names match only argument-less calls; keep the exact approved patterns.
    "failIfUnavailable": True,
    "excludedCommands": ["git", "gh", "git *", "gh *", "/usr/bin/git *", "/usr/bin/gh *"],
    "autoAllowBashIfSandboxed": False,
    # Package registries for `uv sync` and the one writable cache path, nothing wider.
    "network": {"allowedDomains": ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]},
    "filesystem": {"allowWrite": ["~/.cache/uv", "~/.npm"]}}
assert 'mkdir -p "$HOME/.cache/uv"' in (ROOT / "home/.chezmoitemplates/tools.sh").read_text(encoding="utf-8")
assert "Read(~/.config/gh/**)" in claude["permissions"]["deny"]
assert "Bash(git merge *)" not in claude["permissions"]["deny"]
assert "Bash(gh pr merge *)" in claude["permissions"]["deny"]
assert not any("*" in d for d in claude["sandbox"]["network"]["allowedDomains"])
# Documented Bash glob patterns cover destructive flags before and after
# ordinary remote/branch operands. Selector/quote variants are hook-tested below.
from fnmatch import fnmatchcase
def claude_denies(command):
    return any(fnmatchcase(command, entry[5:-1])
               for entry in claude["permissions"]["deny"] if entry.startswith("Bash("))
for executable in ("git", "/usr/bin/git"):
    for tail in ("push --force origin HEAD", "push origin HEAD --force-with-lease",
                 "push origin HEAD -f", "push origin -fextra",
                 "push --delete origin branch", "push origin branch --delete",
                 "push origin :branch"):
        assert claude_denies(executable + " " + tail), tail
for executable in ("gh", "/usr/bin/gh"):
    for tail in ("repo delete fixture", "repo edit fixture", "release delete fixture",
                 "secret list", "ruleset list", "api -X DELETE repos/fixture",
                 "api repos/fixture -X DELETE", "api -XDELETE repos/fixture",
                 "api repos/fixture --method DELETE", "api repos/fixture --method=DELETE"):
        assert claude_denies(executable + " " + tail), tail
for command in ("git push -u origin HEAD", "gh issue list", "gh pr create",
                "/usr/bin/git push origin HEAD", "/usr/bin/gh api repos/fixture"):
    assert not claude_denies(command), command
rules = (ROOT / "home/dot_codex/rules/default.rules").read_text(encoding="utf-8")
assert rules.count('decision = "allow"') == 4
assert rules.count('decision = "prompt"') == 4
assert rules.count('decision = "forbidden"') == 12
assert "PreToolUse" in claude["hooks"]
assert not claude.get("mcpServers") and not claude.get("plugins")

spec = importlib.util.spec_from_file_location(
    "agent_policy", ROOT / "home/dot_local/share/bootstrap/agent-policy/pre_tool_use.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    root = base / "repo"
    root.mkdir()
    outside = base / "outside.txt"
    assert policy.inside(str(root / "file.py"), str(root), root)
    assert not policy.inside(str(outside), str(root), root)
    assert policy.evaluate({"tool_name": "PowerShell", "tool_input": {}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "wsl.exe -d AgentDev"}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "sudo true"}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "touch " + str(outside)}, "cwd": str(root)}) is not None
    assert policy.evaluate({"tool_name": "Write", "tool_input": {"file_path": str(outside)}, "cwd": str(root)}) is not None
    approved = (base,)
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "git status --short"},
                            "cwd": str(root)}, approved) is None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "python3 -c 'print(1)'"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "env python3 -c 'print(1)'"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "env bash -c 'true'"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "FOO=bar python3 -c 'print(1)'"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "command python3 -c 'print(1)'"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "busybox sh -c 'true'"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "env sudo true"},
                            "cwd": str(root)}, approved) is not None
    assert policy.evaluate({"tool_name": "Bash", "tool_input": {"command": "printf safe"},
                            "cwd": str(root)}, approved) is None

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "CI Fixture"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "ci-fixture@example.invalid"], cwd=root, check=True)
    (root / "safe.txt").write_text("safe fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=root, check=True)
    assert policy.secret_scan(root) is None, "safe staged diff should pass"
    synthetic = "ghp_" + hashlib.sha256(b"bootstrap hook canary").hexdigest()[:36]
    (root / "synthetic.txt").write_text(synthetic + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "synthetic.txt"], cwd=root, check=True)
    assert policy.secret_scan(root) is not None, "synthetic staged token must block commit"


# Two approved root families, three real repositories, and a sibling root that
# merely shares a string prefix. All fixtures are hosted and contain no auth.
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    code_base = base / "dev_workspace"
    knowledge_base = base / "cortex"
    roots = (code_base, knowledge_base)
    current = code_base / "current"
    sibling = code_base / "sibling"
    knowledge = knowledge_base / "project"
    outsider = base / "dev_workspace-escape" / "repo"
    for repo in (current, sibling, knowledge, outsider):
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/Jacopo1991/ci-fixture.git"], cwd=repo, check=True)
    link = code_base / "escape-link"
    link.symlink_to(outsider, target_is_directory=True)
    nested = current / "nested"
    nested.mkdir()
    (sibling / "outside-link").symlink_to(outsider / "file")
    def event(command):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(current)}
    for target in (sibling, knowledge):
        for subcommand in ("log", "show", "diff", "status", "rev-parse", "ls-files", "grep", "blame"):
            extra = " --no-textconv" if subcommand in {"log", "show", "diff", "blame"} else ""
            if subcommand in {"log", "show", "diff"}:
                extra += " --no-ext-diff"
            command = f"git --no-pager --no-optional-locks -C {target} {subcommand}{extra}"
            assert policy.evaluate(event(command), roots) is None, command
        for subcommand in ("add", "commit", "push", "pull", "merge", "checkout", "switch", "reset", "config"):
            command = f"git -C {target} {subcommand}"
            assert policy.evaluate(event(command), roots) is not None, command
    # Fetch updates refs/objects/FETCH_HEAD, never the working tree. Its narrow
    # cross-repository exception does not require read-only pager/lock selectors.
    for executable in ("git", "/usr/bin/git"):
        for target in (current, sibling, knowledge):
            for suffix in ("fetch", "fetch origin"):
                command = f"{executable} -C {target} {suffix}"
                assert policy.evaluate(event(command), roots) is None, command
                assert policy.permission_reason(event(command), roots) is None, command
        for suffix in ("fetch", "fetch origin"):
            assert policy.evaluate(event(executable + " " + suffix), roots) is None
        for target in (sibling, knowledge):
            for suffix in ("fetch --all", "fetch --multiple origin", "fetch --update-head-ok origin",
                           "fetch origin HEAD:refs/heads/task", "fetch --refmap=refs/heads/* origin",
                           "fetch https://github.com/Jacopo1991/ci-fixture.git",
                           "fetch local-target", "fetch origin --upload-pack=fixture",
                           "pull origin", "merge fixture", "checkout fixture", "switch fixture",
                           "switch -c fixture"):
                assert policy.evaluate(event(f"{executable} -C {target} {suffix}"), roots) is not None
        for target in (outsider, link):
            assert policy.evaluate(event(f"{executable} -C {target} fetch origin"), roots) is not None
    assert policy.evaluate(event("git -C ../sibling fetch"), roots) is None
    assert policy.evaluate(event("git -C.. -Csibling fetch origin"), roots) is None
    for target in (sibling, knowledge):
        original_url = "https://github.com/Jacopo1991/ci-fixture.git"
        for unsafe_url in (str(current), "file://" + str(current), "https://example.invalid/repo"):
            subprocess.run(["git", "remote", "set-url", "origin", unsafe_url], cwd=target, check=True)
            for suffix in ("fetch", "fetch origin"):
                assert policy.evaluate(event(f"git -C {target} {suffix}"), roots) is not None
        subprocess.run(["git", "remote", "set-url", "origin", original_url], cwd=target, check=True)
        # Owner-provisioned default-remote selection cannot bypass the URL check.
        subprocess.run(["git", "remote", "add", "local-default", str(current)], cwd=target, check=True)
        head = subprocess.check_output(["git", "symbolic-ref", "--short", "HEAD"], cwd=target,
                                       text=True).strip()
        key = "branch." + head + ".remote"
        subprocess.run(["git", "config", key, "local-default"], cwd=target, check=True)
        assert policy.evaluate(event(f"git -C {target} fetch"), roots) is not None
        assert policy.evaluate(event(f"git -C {target} fetch origin"), roots) is None
        subprocess.run(["git", "config", "--unset", key], cwd=target, check=True)
        subprocess.run(["git", "config", "remotes.origin", "origin local-default"], cwd=target, check=True)
        assert policy.evaluate(event(f"git -C {target} fetch"), roots) is not None
        assert policy.evaluate(event(f"git -C {target} fetch origin"), roots) is not None
        subprocess.run(["git", "config", "--unset", "remotes.origin"], cwd=target, check=True)
        subprocess.run(["git", "config", "url." + str(current) + ".insteadOf",
                        original_url], cwd=target, check=True)
        assert policy.evaluate(event(f"git -C {target} fetch origin"), roots) is not None
        subprocess.run(["git", "config", "--unset-all", "url." + str(current) + ".insteadOf"],
                       cwd=target, check=True)
    # Populated submodules can recurse to an unchecked child URL. Deny both
    # forms, including missing .gitmodules and stale index/worktree cases.
    for target in (sibling, knowledge):
        for repo in (target, target / "unsafe-child"):
            if repo != target:
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "config", "user.name", "CI Fixture"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "ci-fixture@example.invalid"],
                           cwd=repo, check=True)
        child = target / "unsafe-child"
        subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/submodule"],
                       cwd=child, check=True)
        (child / "safe.txt").write_text("submodule fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "safe.txt"], cwd=child, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "submodule fixture"], cwd=child, check=True)
        oid = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=child, text=True).strip()
        (target / "a-before.txt").write_text("ordinary fixture\n", encoding="utf-8")
        (target / ".gitmodules").write_text(
            '[submodule "unsafe-child"]\n\tpath = unsafe-child\n'
            '\turl = https://example.invalid/submodule\n', encoding="utf-8")
        subprocess.run(["git", "add", "a-before.txt", ".gitmodules"], cwd=target, check=True)
        subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                        "160000," + oid + ",unsafe-child"], cwd=target, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "parent gitlink fixture"], cwd=target, check=True)
        for suffix in ("fetch", "fetch origin"):
            assert policy.evaluate(event(f"git -C {target} {suffix}"), roots) is not None
        (target / ".gitmodules").unlink()
        assert policy.sibling_fetch_has_submodules(target), "indexed gitlink must deny without file"
        subprocess.run(["git", "update-index", "--force-remove", "unsafe-child"], cwd=target, check=True)
        assert policy.sibling_fetch_has_submodules(target), "committed gitlink must deny without index"
        subprocess.run(["git", "update-index", "--force-remove", ".gitmodules"], cwd=target, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "remove parent gitlinks"], cwd=target, check=True)
        stored = target / ".git" / "modules"
        stored.mkdir()
        assert policy.sibling_fetch_has_submodules(target), "stored submodule metadata must deny"
        stored.rmdir()
        assert policy.evaluate(event(f"git -C {target} fetch origin"), roots) is None
        # Keep the embedded child while all files/gitlinks/stored metadata are
        # absent: retained URL or activation config can make a new tree recurse.
        for key, value in (("submodule.unsafe-child.url", "https://example.invalid/submodule"),
                           ("submodule.unsafe-child.active", "true"),
                           ("submodule.active", "unsafe-child"),
                           ("submodule.recurse", "true")):
            subprocess.run(["git", "config", key, value], cwd=target, check=True)
            for suffix in ("fetch", "fetch origin"):
                assert policy.evaluate(event(f"git -C {target} {suffix}"), roots) is not None
            subprocess.run(["git", "config", "--unset", key], cwd=target, check=True)
        included = target / "owner-submodule-config"
        included.write_text("[submodule]\n\tactive = unsafe-child\n", encoding="utf-8")
        subprocess.run(["git", "config", "include.path", str(included)], cwd=target, check=True)
        assert policy.evaluate(event(f"git -C {target} fetch origin"), roots) is not None
        subprocess.run(["git", "config", "--unset", "include.path"], cwd=target, check=True)
        assert policy.evaluate(event(f"git -C {target} fetch origin"), roots) is None
    from unittest.mock import patch
    with patch.object(policy, "git_metadata", return_value=None):
        assert policy.sibling_fetch_has_submodules(sibling), "metadata failure must deny"
    print("PASS: sibling fetch excludes populated/indexed/committed/stored submodules in both roots")
    print("PASS: sibling fetch/default remote allowed in both roots; checkout/pull/merge/switch and unsafe fetches denied")

    assert policy.evaluate(event("git --no-pager --no-optional-locks -C ../sibling status"), roots) is None
    assert policy.evaluate(event("git --no-pager --no-optional-locks -C.. -Csibling status"), roots) is None
    assert policy.evaluate(event("git --no-pager --no-optional-locks -C ../sibling log --no-textconv --no-ext-diff"), roots) is None
    for command in (
        f"git -C {outsider} status", f"git -C {link} status",
        "git -C nested -C ../../sibling add file",
        "git -C ../sibling --work-tree=. status",
        "git --git-dir=../sibling/.git status",
        "git -c alias.status=add -C ../sibling status",
        "git --config-env=alias.status=FOO status",
        "git -C ../sibling diff --output=/tmp/no-write",
        "git -C ../sibling log --output /tmp/no-write",
        "git -C ../sibling diff --ext-diff",
        "git -C ../sibling diff --textconv",
        "git -C ../sibling diff --no-index /tmp/a /tmp/b",
        "git -C ../sibling grep needle ../../outside",
        "git -C ../sibling status > ../sibling/result",
        "git merge branch", "gh pr merge 1",
        "gh --repo owner/repo pr merge 1", "gh pr --repo owner/repo merge 1",
        "gh -Rowner/repo pr merge 1", "gh --hostname github.com pr merge 1",
        "git config --global review.fixture value",
        "git config --glob review.fixture value",
        "git config --file ../sibling/.git/config review.fixture value",
        "git config -f../sibling/.git/config review.fixture value",
        "git clone . ../created", "git worktree add ../created",
        "git bundle create /tmp/out HEAD", "git format-patch -o /tmp/out HEAD",
        "git diff --out=/tmp/out", "git checkout ../sibling/file",
        "git push sibling.git HEAD", "git push child/../../sibling HEAD",
        "git push -o origin local-target HEAD", "git push --push-option origin local-target HEAD",
        "git push", "git fetch --all",
        "git status > '$OUT'", "git status > ~/result",
        f"gh issue list &> {outsider}/result",
        "git status &> '$OUT'", "git status &>> ~/result",
        "git --no-pager --no-optional-locks -C ../sibling grep -nf/etc/file needle",
        "git --no-pager --no-optional-locks -C ../sibling grep -nOcat needle",
        "git --no-pager --no-optional-locks -C ../sibling ls-files -zX/etc/file",
        "git --no-pager --no-optional-locks -C ../sibling blame --no-textconv -wS/tmp/revisions file",
        "git --no-pager --no-optional-locks -C ../sibling diff --no-textconv --no-ext-diff --ext-diff",
        "git --no-pager --no-optional-locks -C ../sibling diff --no-textconv --no-ext-diff --textconv",
        "git --no-pager --no-optional-locks -C ../sibling diff --no-textconv",
        "git --no-pager --no-optional-locks -C ../sibling grep -f/etc/file needle",
        "git --no-pager --no-optional-locks -C ../sibling grep -foutside-link needle",
        "git --no-pager --no-optional-locks -C ../sibling ls-files -X/etc/file",
        "git --no-pager --no-optional-locks -C ../sibling ls-files --exclude-from=outside-link",
        "git --no-pager --no-optional-locks -C ../sibling blame --no-textconv --contents=outside-link file",
        "git --no-pager --no-optional-locks -C ../sibling blame --no-textconv --ignore-revs-file=outside-link file",
        "git --no-pager --no-optional-locks -C ../sibling show --no-textconv --no-ext-diff outside-link",
        "git --no-pager --no-optional-locks -C ../sibling log -p",
        "git push ../sibling HEAD", "git push file:///tmp/repo HEAD",
        "git -C ~ status", "git -C '$HOME' status", "git -C ../* status",
        "git -C ../sibling status",
        f"git -C --no-pager -C --no-optional-locks -C {sibling} status",
        "git --no-pager -C ../sibling status",
        "git --no-pager --no-optional-locks -C ../sibling diff",
        "git --no-pager --no-optional-locks -C ../sibling diff --no-textconv --no-ext-diff --out=/tmp/no-write",
        "git --no-pager --no-optional-locks -C ../sibling grep -Ocat needle",
        "git --no-pager --no-optional-locks -C ../sibling grep --open-files-in-pager=cat needle",
        "git --no-pager --no-optional-locks -C ../sibling grep --open=cat needle",
        "git --no-pager --no-optional-locks -C ../sibling blame --no-textconv -S/tmp/revisions file",
        "git --no-pager --no-optional-locks -C ../sibling blame --no-textconv --contents=/tmp/file file",
    ):
        assert policy.evaluate(event(command), roots) is not None, command
    for command in ("git add file", "git push origin branch", "git push -u origin branch", "git fetch --depth=1 origin",
                    "git -C . add file", "git config --local --get remote.origin.url",
                    "git config --get remote.origin.url", "git config --list",
                    "gh --repo owner/repo pr create", "gh pr --repo owner/repo create"):
        assert policy.evaluate(event(command), roots) is None, command
    # Owner/CI provisions identity and remote configuration. Agent calls cannot
    # modify configuration, even for apparently harmless local keys.
    for key in ("core.hooksPath", "core.fsmonitor", "core.sshCommand",
                "alias.x", "filter.x.clean", "filter.x.smudge", "filter.x.process",
                "diff.x.command", "review.fixture"):
        for option in ("", "--local ", "--add ", "--replace-all "):
            command = f"git config {option}{key} fixture-command"
            assert policy.evaluate(event(command), roots) is not None, command
    for command in ("git config --unset core.hooksPath", "git config --edit",
                    "git config --remove-section core", "git config core.hooksPath",
                    "git config --get core.hooksPath fixture-command",
                    "git config --list --add core.hooksPath fixture-command",
                    "git -c core.hooksPath=fixture status",
                    "git -ccore.fsmonitor=fixture status",
                    "git --config-env=core.sshCommand=FIXTURE status",
                    "git status --config-env=core.sshCommand=FIXTURE",
                    "FOO=fixture git status", "FOO=fixture gh issue list",
                    "GIT_CONFIG_COUNT=0 git status", "GIT_CONFIG_PARAMETERS=fixture git status",
                    "GIT_DIR=.git git status", "GIT_EXEC_PATH=. git status",
                    "env GIT_DIR=.git git status", "env FOO=fixture gh issue list"):
        assert policy.evaluate(event(command), roots) is not None, command

    import unittest.mock
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                 "GIT_CONFIG", "GIT_DIR", "GIT_EXEC_PATH", "GIT_SSH_COMMAND"):
        # Refuse overrides before any helper Git subprocess runs.
        with unittest.mock.patch.dict(policy.os.environ, {name: "fixture"}), \
             unittest.mock.patch.object(policy.subprocess, "run",
                                        side_effect=AssertionError("Git must not run with overrides")):
            assert policy.evaluate(event("git status"), roots) is not None, name
        for location in ("event", "input"):
            payload = event("git status")
            (payload if location == "event" else payload["tool_input"])["env"] = {name: "fixture"}
            with unittest.mock.patch.object(policy.subprocess, "run",
                                            side_effect=AssertionError("Git must not run with overrides")):
                assert policy.evaluate(payload, roots) is not None, (name, location)

    # Metadata is protected through native edit/patch tools, shell outputs,
    # file transfers, symlink aliases, and resolved directory-copy destinations.
    (current / "metadata-alias").symlink_to(current / ".git", target_is_directory=True)
    (current / "pre-commit").write_text("fixture\n", encoding="utf-8")
    (current / "safe.txt").write_text("safe fixture\n", encoding="utf-8")
    (current / "docs").mkdir()
    (current / "docs/pre-commit").symlink_to(current / ".git/hooks/pre-commit")
    for filename in (".git/config", ".git/hooks/pre-commit", "metadata-alias/hooks/pre-commit",
                     str(current / ".git/hooks/pre-commit"), ".git",
                     "docs/pre-commit", ".git/../safe.txt"):
        for tool in ("Write", "Edit", "MultiEdit"):
            payload = {"tool_name": tool, "tool_input": {"file_path": filename}, "cwd": str(current)}
            assert policy.evaluate(payload, roots) is not None, (tool, filename)
        payload = {"tool_name": "apply_patch",
                   "tool_input": {"patch": f"*** Begin Patch\n*** Add File: {filename}\n+fixture\n*** End Patch"},
                   "cwd": str(current)}
        assert policy.evaluate(payload, roots) is not None, filename
    payload = {"tool_name": "apply_patch",
               "tool_input": {"patch": "*** Begin Patch\n*** Update File: safe.txt\n*** Move to: .git/hooks/pre-commit\n@@\n-fixture\n+fixture\n*** End Patch"},
               "cwd": str(current)}
    assert policy.evaluate(payload, roots) is not None, "patch rename into metadata"
    for command in (
        "printf fixture > .git/hooks/pre-commit", "printf fixture >> .git/config",
        "printf fixture &> .git/config", "printf fixture &>> metadata-alias/config",
        "printf fixture >| .git/config", "printf fixture >& .git/config", "cat <> .git/config",
        "cp safe.txt .git/hooks/pre-commit", "mv safe.txt .git/config",
        "tee .git/hooks/pre-commit", "tee -a metadata-alias/config",
        "cp -t .git/hooks pre-commit", "cp -t.git/hooks pre-commit",
        "cp --target-directory=.git/hooks pre-commit",
        "mv -t .git/hooks pre-commit", "cp pre-commit docs",
        "cp .git/config safe.txt", "mv .git/config safe.txt",
        "touch .git/config", "install -m 755 safe.txt .git/hooks/pre-commit",
        "ln .git/config alias-config", "ln -s .git metadata-copy",
        "dd of=.git/hooks/pre-commit", "sed -i s/a/b/ .git/config safe.txt",
        "rm -r .", "chmod -R 755 .", "cp -r . docs", "mv docs renamed-docs",
    ):
        assert policy.evaluate(event(command), roots) is not None, command
    for tool in ("Write", "Edit"):
        payload = {"tool_name": tool, "tool_input": {"file_path": "safe.txt"}, "cwd": str(current)}
        assert policy.evaluate(payload, roots) is None, tool
    for command in ("cp safe.txt copied.txt", "mv copied.txt moved.txt",
                    "tee safe.txt", "printf fixture > safe.txt", "cp -t docs safe.txt"):
        assert policy.evaluate(event(command), roots) is None, command

    # Git can store metadata under a name other than .git; discover that path
    # instead of relying only on path components (same protection as worktrees).
    separate = code_base / "separate"
    separate.mkdir()
    control = separate / "git-control"
    subprocess.run(["git", "init", "-q", "--separate-git-dir=" + str(control), str(separate)], check=True)
    for filename in (".git", "git-control/config", "git-control/hooks/pre-commit"):
        payload = {"tool_name": "Write", "tool_input": {"file_path": filename}, "cwd": str(separate)}
        assert policy.evaluate(payload, roots) is not None, filename
    payload = {"tool_name": "Write", "tool_input": {"file_path": "ordinary.txt"}, "cwd": str(separate)}
    assert policy.evaluate(payload, roots) is None
    with unittest.mock.patch.object(policy, "git_metadata", return_value=None):
        assert policy.evaluate(payload, roots) is not None, "metadata discovery must fail closed"

    # The enclosing repository must also protect an existing nested repo's
    # custom Git directory, even when it was not returned for the current cwd.
    nested_repo = current / "nested-owner-repo"
    nested_control = current / "nested-control"
    subprocess.run(["git", "init", "-q", "--separate-git-dir=" + str(nested_control),
                    str(nested_repo)], check=True)
    for filename in ("nested-control/config", "nested-control/hooks/pre-commit",
                     "nested-control/HEAD", "nested-owner-repo/.git"):
        payload = {"tool_name": "Write", "tool_input": {"file_path": filename}, "cwd": str(current)}
        assert policy.evaluate(payload, roots) is not None, filename
    for command in ("printf fixture > nested-control/hooks/pre-commit",
                    "tee nested-control/config",
                    "cp safe.txt nested-control/hooks/pre-commit",
                    "mv safe.txt nested-control/config"):
        assert policy.evaluate(event(command), roots) is not None, command

    (current / "-control").symlink_to(nested_control, target_is_directory=True)
    (current / "-metadata").symlink_to(nested_control, target_is_directory=True)
    reason = policy.evaluate(event("git mv -f -- safe.txt -metadata/config"), roots)
    assert reason is not None and "metadata" in reason, reason
    for command in ("git add -- ':(literal)nested-control/config'",
                    "git rm -f -- ':(literal)nested-control/config'",
                    "git add -- ':nested-control/config'", "git add .",
                    "git add -A", "git add -u", "git add",
                    "git add -A --chmod +x", "git add -A --ch +x",
                    "git add --chmod +x safe.txt", "git add --ch=+x safe.txt",
                    "git add -A --chmod=+x", "git add --chmod=-x",
                    "git add --pathspec-from=paths.txt",
                    "git mv -f -- safe.txt -control/config",
                    "git add -- -control/config",
                    "git mv -f safe.txt nested-control/config",
                    "git rm -f nested-control/config", "git restore nested-control/config",
                    "git checkout -- nested-control/config", "git add nested-control/config",
                    "git clean -ffdx nested-control", "git clean -ffdx .", "git clean -fd",
                    "git mv nested-owner-repo renamed-owner-repo",
                    "git mv safe.txt .git/hooks/pre-commit",
                    "git add --pathspec-from-file=paths.txt"):
        assert policy.evaluate(event(command), roots) is not None, command
    assert policy.evaluate(event("git mv safe.txt ordinary.txt"), roots) is None
    assert policy.evaluate(event("git mv -- safe.txt -ordinary.txt"), roots) is None
    assert policy.evaluate(event("git --literal-pathspecs add safe.txt"), roots) is None
    assert policy.evaluate(event("git add --chmod=+x safe.txt"), roots) is None
    assert policy.evaluate(event("git add --chmod=-x safe.txt"), roots) is None

    # A real current-repository branch switch/creation must pass the guard.
    # Keep global/attached configuration override variants denied.
    for executable in ("git", "/usr/bin/git"):
        for tail in ("switch task-fixture", "switch -c task-fixture", "checkout -b task-fixture"):
            assert policy.evaluate(event(executable + " " + tail), roots) is None, tail
        for tail in ("-c core.hooksPath=fixture switch task-fixture",
                     "-ccore.hooksPath=fixture switch -c task-fixture",
                     "switch --config-env=core.hooksPath=FIXTURE task-fixture",
                     "switch -c task-fixture -c extra",
                     "switch -ccore.hooksPath=fixture"):
            assert policy.evaluate(event(executable + " " + tail), roots) is not None, tail
    for target in (sibling, knowledge):
        assert policy.evaluate(event(f"git -C {target} switch task-fixture"), roots) is not None
        assert policy.evaluate(event(f"git -C {target} switch -c task-fixture"), roots) is not None

    # Literal creation start points are allowed only in the current repository.
    # Test both shared hooks, executable spellings and approved root families.
    for repo in (current, knowledge):
        def switch_event(command):
            return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
        for executable in ("git", "/usr/bin/git"):
            for create in ("-c", "--create"):
                for suffix in ("feature+fix", "feature+fix origin/topic+fix"):
                    command = executable + " switch " + create + " " + suffix
                    assert policy.evaluate(switch_event(command), roots) is None, command
                    assert policy.permission_reason(switch_event(command), roots) is None, command
                for start in ("", " origin/main", " origin/topic/nested",
                              " abcd", " a1b2c3d", " " + "a1" * 20, " " + "A1" * 20,
                              " " + "ab" * 32):
                    command = executable + " switch " + create + " task-fixture" + start
                    assert policy.evaluate(switch_event(command), roots) is None, command
                    assert policy.permission_reason(switch_event(command), roots) is None, command
                    assert policy.evaluate(switch_event(executable + " -C . switch " + create
                                                       + " task-fixture" + start), roots) is None
                for start in ("HEAD", "main", "upstream/main", "refs/remotes/origin/main",
                              "origin/main~1", "origin/main^", "origin/main..origin/topic",
                              "origin/main...", "origin/", "origin//main", "origin/.hidden",
                              "origin/main.lock", "origin/main/", "origin/main.", "origin/a..b",
                              "abc", "a" * 41, "g" * 40, "../sibling", "/tmp/fixture",
                              "--orphan", "-"):
                    command = executable + " switch " + create + " task-fixture " + start
                    assert policy.evaluate(switch_event(command), roots) is not None, command
                    assert policy.permission_reason(switch_event(command), roots) is not None, command
                for tail in ("", " -f", " --discard-changes", " --merge", " --track",
                             " --recurse-submodules", " --create extra", " -c extra", " extra"):
                    # The empty tail is an ordinary valid control, checked above.
                    if tail:
                        command = executable + " switch " + create + " task-fixture origin/main" + tail
                        assert policy.evaluate(switch_event(command), roots) is not None, command
                for prefix in ("-c core.hooksPath=fixture ", "-ccore.hooksPath=fixture ",
                               "--config-env=core.hooksPath=FIXTURE "):
                    assert policy.evaluate(switch_event(executable + " " + prefix + "switch "
                                                        + create + " task-fixture origin/main"), roots) is not None
                assert policy.evaluate(switch_event("FIXTURE=value " + executable + " switch "
                                                    + create + " task-fixture origin/main"), roots) is not None
                for env in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_EXEC_PATH"):
                    request = switch_event(executable + " switch " + create + " task-fixture origin/main")
                    request["tool_input"]["env"] = {env: "fixture"}
                    assert policy.evaluate(request, roots) is not None, env
                for target in (sibling, outsider, link):
                    assert policy.evaluate(switch_event(executable + " -C " + str(target) + " switch "
                                                        + create + " task-fixture origin/main"), roots) is not None
            for tail in ("-ctask-fixture origin/main", "--create=task-fixture origin/main",
                         "-C task-fixture origin/main", "--force-create task-fixture origin/main",
                         "--cre task-fixture origin/main", "-q -c task-fixture origin/main",
                         "-c task-fixture --config-env=core.hooksPath=FIXTURE",
                         "-c -option origin/main", "-c /bad origin/main", "-c 'task fixture' origin/main"):
                assert policy.evaluate(switch_event(executable + " switch " + tail), roots) is not None, tail
    print("PASS: literal switch creation start points allowed; options/overrides/other refs/siblings denied")

    # Host checks apply to executable/path argv, never GitHub prose. These
    # requests reach native permission rules without an unconditional grant.
    for executable in ("gh", "/usr/bin/gh"):
        for tail in (
                "pr create --body 'Attribution: https://claude.com/claude-code'",
                "issue create --title 'Discuss wsl.exe' --body 'C:\\Windows\\System32 and /mnt/c are documentation'",
                "pr edit 7 --body 'sudo'",
                "pr comment 7 -b 'wsl.exe'",
                "issue comment 7 --body='powershell.exe at C:\\Windows\\System32'",
                "pr create -b'https://claude.com/claude-code /mnt/c/example'",
                "issue edit 7 -t'cmd.exe findings' -b'\\\\host\\share is prose'",
                "-R Jacopo1991/ci-fixture pr create --body 'diskpart.exe C:\\Windows\\System32'",
                "--hostname github.com issue comment 7 --body '/mnt/c/example'",
                "pr create --assignee fixture --label fixture --body 'wsl.exe C:\\Windows\\System32'",
                "pr create --fill --body 'C:\\Windows\\System32 is documentation'",
                "pr create -f --body 'C:\\Windows\\System32 is documentation'",
                "pr create --fill-first --body 'C:\\Windows\\System32 is documentation'",
                "pr create --fill-verbose --body 'C:\\Windows\\System32 is documentation'",
                "pr create --no-maintainer-edit --body 'C:\\Windows\\System32 is documentation'",
                "pr create --dry-run --body 'C:\\Windows\\System32 is documentation'",
                "pr edit 7 --remove-milestone --body 'C:\\Windows\\System32 is documentation'",
                "issue edit 7 --remove-parent --remove-type --body '/mnt/c is documentation'",
                "issue create --type Bug --parent 100 --body '/mnt/c is documentation'",
                "pr create --attach image.png --body 'C:\\Windows\\System32 is documentation'",
                "pr create -F body.com --body 'wsl.exe C:\\Windows\\System32'",
                "pr create --body-file body.com",
                "issue comment 7 --body-file body.exe"):
            command = executable + " " + tail
            assert policy.evaluate(event(command), roots) is None, command
            assert policy.permission_reason(event(command), roots) is None, command
        for tail in (
                "pr create --body 'safe' --body-file 'C:\\Windows\\body.md'",
                "issue comment 7 --body-file /mnt/c/body.md",
                "pr create --body-file='C:\\Windows\\body.md'",
                "pr create -F'C:\\Windows\\body.md'",
                "pr create -FC:\\Windows\\body.md",
                "pr create --body-file \\\\host\\share\\body.md",
                "pr create -F'\\\\host\\share\\body.md'",
                "pr create --body-file '-bC:\\Windows\\body.md'",
                "pr create -F '-tC:\\Windows\\body.md'",
                "pr create --body-file=-bC:\\Windows\\body.md",
                "pr create --attach '-bC:\\Windows\\file.png'",
                "pr create --unknown --body 'C:\\Windows\\System32'",
                "pr create -dt'C:\\Windows\\System32'",
                "pr create -- --body 'C:\\Windows\\System32'",
                "pr create --body-file '\\\\host\\share\\with spaces.md'",
                "pr create --body-file C:\\Windows\\body.md",
                "issue comment 7 --body-file '\\\\host\\share\\body.md'",
                "pr create --body 'safe' > /mnt/c/body.md",
                "pr create --body 'safe'; wsl.exe",
                "pr create --body 'safe' && fixture.com",
                "pr create --body \"$(wsl.exe)\"",
                "pr create --body 'safe' --repo 'C:\\Windows\\repo'",
                "pr merge 7 --body 'wsl.exe'",
                "repo delete fixture --body 'safe'"):
            command = executable + " " + tail
            assert policy.evaluate(event(command), roots) is not None, command
            assert policy.permission_reason(event(command), roots) is not None, command
    for command in (
            "wsl.exe -d AgentDev", "/usr/bin/wsl.exe -d AgentDev",
            "wsl${empty}", "wsl$empty", "fixture${empty}.com", "diskpart", "schtasks",
            "git -CC:\\Windows\\repo status",
            "fixture.com", "/tmp/fixture.COM", "'/tmp/fixture.exe'",
            "'C:\\Windows\\System32\\cmd.exe' /c echo safe",
            "C:\\Windows\\System32\\cmd.exe /c echo safe",
            "'\\\\host\\share\\fixture.com'", "sudo true",
            "cat 'C:\\Windows\\file.md'", "cat /mnt/c/file.md",
            "cat '\\\\host\\share\\file.md'", "cp safe.txt 'C:\\Windows\\file.md'",
            "env fixture.com", "command fixture.exe", "bash -c 'fixture.com'"):
        assert policy.evaluate(event(command), roots) is not None, command
    # A Linux data filename or prose argument is not a Windows executable.
    for command in ("cat fixture.com", "cat fixture.exe", "printf 'wsl.exe'",
                    "printf 'sudo'", "cat /mnt/customer/file.md", "git grep '\\.com'"):
        assert policy.evaluate(event(command), roots) is None, command
    body_file = current / "body.com"
    body_file.write_text("wsl.exe C:\\Windows\\System32 https://claude.com/claude-code\n",
                         encoding="utf-8")
    with unittest.mock.patch.object(Path, "read_text", side_effect=AssertionError("no body-file reads")):
        assert policy.evaluate(event("gh pr create --body-file body.com"), roots) is None
        assert policy.permission_reason(event("gh pr create --body-file body.com"), roots) is None
    print("PASS: gh prose/body files accepted; real host execution and Windows paths denied")

    # Founder decision: ordinary Git/gh has native allow rules; forbidden
    # operations are denied independently of flag position or inherited selectors.
    for executable in ("git", "/usr/bin/git"):
        for tail in ("push --force origin HEAD", "push origin HEAD --force",
                     "push origin HEAD --force-with-lease=refs/heads/task:fixture",
                     "push origin HEAD --force-if-includes", "push origin HEAD -f",
                     "push origin HEAD -fextra", "push origin HEAD -uf",
                     "push --delete origin task", "push origin task --delete",
                     "push origin task -d", "push origin :task",
                     "push origin +HEAD:task", "push --mirror origin", "push origin --prune",
                     "-C . push origin HEAD --force", "-C . push origin :task"):
            command = executable + " " + tail
            assert policy.evaluate(event(command), roots) is not None, command
            assert policy.permission_reason(event(command), roots) is not None, command
        for tail in ("reset --hard", "rebase --abort", "cherry-pick --abort", "revert --abort",
                     "stash pop", "stash drop", "stash clear"):
            # Native literal prefixes request approval; hook must not grant it.
            command = executable + " " + tail
            assert policy.evaluate(event(command), roots) is None, command
            assert policy.permission_reason(event(command), roots) is None, command
            for prefix in ("-C . ", "--no-pager "):
                assert policy.evaluate(event(executable + " " + prefix + tail), roots) is not None
        for tail in ("push -u origin HEAD", "status --short", "stash list"):
            assert policy.evaluate(event(executable + " " + tail), roots) is None, tail
    for executable in ("gh", "/usr/bin/gh"):
        for tail in ("repo delete fixture", "repo edit fixture", "release delete fixture",
                     "secret list", "ruleset list", "-R owner/repo repo delete",
                     "repo --repo owner/repo edit", "--hostname github.com release delete fixture",
                     "--repo=owner/repo secret list", "-Rowner/repo ruleset list",
                     "api -X DELETE repos/fixture", "api repos/fixture -X DELETE",
                     "api -XDELETE repos/fixture", "api repos/fixture -X=DELETE",
                     "api --method DELETE repos/fixture", "api repos/fixture --method=DELETE",
                     "-R owner/repo api repos/fixture --method=delete"):
            command = executable + " " + tail
            assert policy.evaluate(event(command), roots) is not None, command
            assert policy.permission_reason(event(command), roots) is not None, command
        for tail in ("issue list", "pr create", "auth status", "repo view", "release list",
                     "api repos/fixture", "api repos/fixture --method GET"):
            assert policy.evaluate(event(executable + " " + tail), roots) is None, tail

    # A real local add/commit completes after passing the hook; GitHub push is
    # permission-tested against its named HTTPS remote, with no authentication
    # or network push from CI.
    subprocess.run(["git", "config", "user.name", "CI Fixture"], cwd=current, check=True)
    subprocess.run(["git", "config", "user.email", "ci-fixture@example.invalid"], cwd=current, check=True)
    assert policy.evaluate(event("git add safe.txt"), roots) is None
    subprocess.run(["git", "add", "safe.txt"], cwd=current, check=True)
    assert policy.evaluate(event("git commit -m 'CI safe workflow'"), roots) is None
    subprocess.run(["git", "commit", "-q", "-m", "CI safe workflow"], cwd=current, check=True)
    assert policy.evaluate(event("git switch -c ci-switch-fixture"), roots) is None
    subprocess.run(["git", "switch", "-q", "-c", "ci-switch-fixture"], cwd=current, check=True)
    subprocess.run(["git", "branch", "ci-existing-fixture"], cwd=current, check=True)
    assert policy.evaluate(event("git switch ci-existing-fixture"), roots) is None
    subprocess.run(["git", "switch", "-q", "ci-existing-fixture"], cwd=current, check=True)
    # Execute the allowed forms against two different synthetic commits so the
    # explicit start point, not current HEAD, determines each created branch.
    first = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=current, text=True).strip()
    (current / "start-fixture.txt").write_text("second synthetic commit\n")
    subprocess.run(["git", "add", "start-fixture.txt"], cwd=current, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second fixture"], cwd=current, check=True)
    second = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=current, text=True).strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/start-fixture", first], cwd=current, check=True)
    for index, (create, start, expected) in enumerate((
            ("-c", first, first), ("--create", second, second),
            ("-c", first[:7], first), ("--create", "origin/start-fixture", first), ("-c", first, first))):
        name = "ci-start-fixture-" + str(index) + ("+fix" if index == 4 else "")
        command = "git switch " + create + " " + name + " " + start
        assert policy.evaluate(event(command), roots) is None, command
        assert policy.permission_reason(event(command), roots) is None, command
        subprocess.run(["git", "switch", create, name, start], cwd=current, check=True)
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=current, text=True).strip() == expected
        assert subprocess.check_output(["git", "symbolic-ref", "--short", "HEAD"],
                                       cwd=current, text=True).strip() == name
    print("PASS: real switch creation selects the explicit commit or origin ref")
    assert policy.evaluate(event("git push -u origin HEAD"), roots) is None
    assert policy.permission_reason(event("git push -u origin HEAD"), roots) is None
    print("PASS: agent config/metadata/override attempts denied; real add/commit and GitHub push approval permitted")

    subprocess.run(["git", "remote", "add", "local-target", str(sibling)], cwd=current, check=True)
    subprocess.run(["git", "remote", "add", "local-file", "file://" + str(sibling)], cwd=current, check=True)
    for command in ("git push local-target HEAD", "git push local-file HEAD",
                    "git fetch local-target", "git pull local-target",
                    "git push https://example.invalid/repo HEAD"):
        assert policy.evaluate(event(command), roots) is not None, command
    # Also check an existing named remote redirected through pushurl/insteadOf.
    subprocess.run(["git", "config", "remote.origin.pushurl", str(sibling)], cwd=current, check=True)
    assert policy.evaluate(event("git push origin HEAD"), roots) is not None
    subprocess.run(["git", "config", "--unset", "remote.origin.pushurl"], cwd=current, check=True)
    subprocess.run(["git", "config", "url." + str(sibling) + ".insteadOf",
                    "https://github.com/Jacopo1991/ci-fixture.git"], cwd=current, check=True)
    assert policy.evaluate(event("git push origin HEAD"), roots) is not None
    assert policy.evaluate(event("git push https://github.com/Jacopo1991/ci-fixture.git HEAD"), roots) is not None
    subprocess.run(["git", "config", "--unset-all", "url." + str(sibling) + ".insteadOf"], cwd=current, check=True)
    subprocess.run(["git", "config", "url." + str(sibling) + ".pushInsteadOf",
                    "https://github.com/Jacopo1991/ci-fixture.git"], cwd=current, check=True)
    assert policy.evaluate(event("git push origin HEAD"), roots) is not None
    assert policy.evaluate(event("git push https://github.com/Jacopo1991/ci-fixture.git HEAD"), roots) is not None
    subprocess.run(["git", "config", "--unset-all", "url." + str(sibling) + ".pushInsteadOf"], cwd=current, check=True)
    assert policy.permission_reason(event("git push origin branch"), roots) is None
    assert policy.permission_reason(event("gh issue list"), roots) is None
    assert policy.permission_reason(event("/usr/bin/gh pr create"), roots) is None
    for command in ("curl https://github.com", "wget https://github.com",
                    "python3 script.py", "env gh issue list", "gh issue list && curl example.com"):
        assert policy.permission_reason(event(command), roots) is not None, command
    # Exercise hook output shape, not just evaluate(): allowed network requests
    # produce no permission grant; denied escalations use PermissionRequest JSON.
    previous_root_for = policy.root_for
    policy.root_for = lambda cwd, approved_roots=None: previous_root_for(cwd, roots)
    try:
        import contextlib
        import io
        import sys
        for command, blocked in (("gh issue list", False), ("curl https://github.com", True),
                                 ("git config core.hooksPath fixture-command", True),
                                 ("git config core.fsmonitor fixture-command", True),
                                 ("git config core.sshCommand fixture-command", True),
                                 ("git config alias.x fixture-command", True),
                                 ("git config filter.x.clean fixture-command", True),
                                 ("printf fixture > .git/hooks/pre-commit", True)):
            payload = event(command) | {"hook_event_name": "PermissionRequest"}
            previous_stdin = sys.stdin
            output = io.StringIO()
            try:
                sys.stdin = io.StringIO(json.dumps(payload))
                with contextlib.redirect_stdout(output):
                    assert policy.main() == 0
            finally:
                sys.stdin = previous_stdin
            if blocked:
                response = json.loads(output.getvalue())
                assert response["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
                assert response["hookSpecificOutput"]["decision"]["behavior"] == "deny"
            else:
                assert output.getvalue() == "", "hook must defer to native allow/prompt rules"
    finally:
        policy.root_for = previous_root_for
print("PASS: founder-approved routine Git/gh, destructive denies and canonical native approval controls")

for path in ("home/dot_codex/AGENTS.md", "home/dot_claude/CLAUDE.md"):
    rules = (ROOT / path).read_text(encoding="utf-8").lower()
    for phrase in ("slim-workflow", "never merge", "read the affected", "secret values",
                   "approved workspace roots", "scheduled project jobs remain deferred",
                   "customerharness and typo3"):
        assert phrase in rules, (path, phrase)
print("PASS: native settings, hook schema, and policy negative controls")
