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
    link = code_base / "escape-link"
    link.symlink_to(outsider, target_is_directory=True)
    nested = current / "nested"
    nested.mkdir()
    def event(command):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(current)}
    for target in (sibling, knowledge):
        for subcommand in ("log", "show", "diff", "status", "rev-parse", "ls-files", "grep", "blame"):
            extra = " --no-textconv" if subcommand in {"show", "diff", "blame"} else ""
            command = f"git --no-pager --no-optional-locks -C {target} {subcommand}{extra}"
            assert policy.evaluate(event(command), roots) is None, command
        for subcommand in ("add", "commit", "push", "fetch", "checkout", "reset", "config"):
            command = f"git -C {target} {subcommand}"
            assert policy.evaluate(event(command), roots) is not None, command
    assert policy.evaluate(event("git --no-pager --no-optional-locks -C ../sibling status"), roots) is None
    assert policy.evaluate(event("git --no-pager --no-optional-locks -C.. -Csibling status"), roots) is None
    assert policy.evaluate(event("git --no-pager --no-optional-locks -C ../sibling log"), roots) is None
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
        "git push ../sibling HEAD", "git push file:///tmp/repo HEAD",
        "git -C ~ status", "git -C '$HOME' status", "git -C ../* status",
        "git -C ../sibling status",
        f"git -C --no-pager -C --no-optional-locks -C {sibling} status",
        "git --no-pager -C ../sibling status",
        "git --no-pager --no-optional-locks -C ../sibling diff",
        "git --no-pager --no-optional-locks -C ../sibling diff --no-textconv --out=/tmp/no-write",
        "git --no-pager --no-optional-locks -C ../sibling grep -Ocat needle",
        "git --no-pager --no-optional-locks -C ../sibling grep --open-files-in-pager=cat needle",
        "git --no-pager --no-optional-locks -C ../sibling grep --open=cat needle",
        "git --no-pager --no-optional-locks -C ../sibling blame --no-textconv -S/tmp/revisions file",
        "git --no-pager --no-optional-locks -C ../sibling blame --no-textconv --contents=/tmp/file file",
    ):
        assert policy.evaluate(event(command), roots) is not None, command
    for command in ("git add file", "git push origin branch", "git -C . add file", "git config --local review.fixture value",
                    "gh --repo owner/repo pr create", "gh pr --repo owner/repo create"):
        assert policy.evaluate(event(command), roots) is None, command
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
        for command, blocked in (("gh issue list", False), ("curl https://github.com", True)):
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
