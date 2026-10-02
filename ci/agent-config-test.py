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
assert claude["permissions"]["defaultMode"] == "default"
assert "allow" not in claude["permissions"]
assert claude["sandbox"] == {"enabled": True, "allowUnsandboxedCommands": False,
    "failIfUnavailable": True, "excludedCommands": ["git", "gh"],
    "autoAllowBashIfSandboxed": False}
assert set(claude["permissions"]["ask"]) == {"Bash(git *)", "Bash(gh *)",
    "Bash(/usr/bin/git *)", "Bash(/usr/bin/gh *)"}
assert "Read(~/.config/gh/**)" in claude["permissions"]["deny"]
assert "Bash(git merge *)" in claude["permissions"]["deny"]
assert "Bash(gh pr merge *)" in claude["permissions"]["deny"]
assert "network" not in claude["sandbox"]
rules = (ROOT / "home/dot_codex/rules/default.rules").read_text(encoding="utf-8")
assert rules.count('decision = "prompt"') == 4
assert 'decision = "allow"' not in rules
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
        for subcommand in ("add", "commit", "push", "fetch", "checkout", "reset", "config"):
            command = f"git -C {target} {subcommand}"
            assert policy.evaluate(event(command), roots) is not None, command
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

    # A real local add/commit completes after passing the hook; GitHub push is
    # permission-tested against its named HTTPS remote, with no authentication
    # or network push from CI.
    subprocess.run(["git", "config", "user.name", "CI Fixture"], cwd=current, check=True)
    subprocess.run(["git", "config", "user.email", "ci-fixture@example.invalid"], cwd=current, check=True)
    assert policy.evaluate(event("git add safe.txt"), roots) is None
    subprocess.run(["git", "add", "safe.txt"], cwd=current, check=True)
    assert policy.evaluate(event("git commit -m 'CI safe workflow'"), roots) is None
    subprocess.run(["git", "commit", "-q", "-m", "CI safe workflow"], cwd=current, check=True)
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
                assert output.getvalue() == "", "normal native user approval must remain"
    finally:
        policy.root_for = previous_root_for
print("PASS: cross-repository Git reads, current-repository writes and git/gh-only approvals")

for path in ("home/dot_codex/AGENTS.md", "home/dot_claude/CLAUDE.md"):
    rules = (ROOT / path).read_text(encoding="utf-8").lower()
    for phrase in ("claim work", "never merge", "read the affected", "secret values",
                   "approved workspace roots", "scheduled project jobs remain deferred",
                   "customerharness and typo3"):
        assert phrase in rules, (path, phrase)
print("PASS: native settings, hook schema, and policy negative controls")
