#!/usr/bin/env python3
"""Hosted synthetic metadata tests; no remote connection or PR merge."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agent_policy", ROOT / "system/agent-policy/pre_tool_use.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()

with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary)
    code = base / "dev_workspace"
    knowledge = base / "cortex"
    roots = (code, knowledge)
    for parent in roots:
        for default in ("main", "master", "trunk"):
            repo = parent / ("fixture-" + default)
            repo.mkdir(parents=True)
            git(repo, "init", "-q", "--initial-branch=" + default)
            git(repo, "config", "user.name", "CI")
            git(repo, "config", "user.email", "ci@example.invalid")
            git(repo, "remote", "add", "origin", "https://github.com/Jacopo1991/fixture.git")
            (repo / "safe.txt").write_text("safe synthetic content\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "base")
            git(repo, "update-ref", "refs/remotes/origin/" + default, "HEAD")
            git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/" + default)
            def event(command):
                return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
            def allowed(command):
                reason = policy.evaluate(event(command), roots)
                assert reason is None, command + ": " + str(reason)
            def denied(command):
                assert policy.evaluate(event(command), roots) is not None, command
            # Never merge into default, even when the merge would be a no-op.
            denied("git merge origin/" + default)
            denied("git merge --no-edit origin/" + default)
            git(repo, "switch", "-q", "-c", "task")
            for executable in ("git", "/usr/bin/git"):
                for middle in ("", "--no-edit "):
                    allowed(executable + " merge " + middle + "origin/" + default)
                for tail in ("other", "HEAD", default, "origin/HEAD", "origin/task",
                             "origin/" + default + "~1", "upstream/" + default,
                             "--abort", "--continue", "--edit origin/" + default,
                             "--squash origin/" + default, "--strategy=fixture origin/" + default,
                             "-s fixture origin/" + default, "-X fixture origin/" + default,
                             "--exec=fixture origin/" + default,
                             "--gpg-sign=fixture origin/" + default,
                             "--no-verify origin/" + default,
                             "origin/" + default + " --no-edit",
                             "origin/" + default + " origin/task"):
                    denied(executable + " merge " + tail)
                denied("GIT_SSH_COMMAND=fixture " + executable + " merge origin/" + default)
                denied(executable + " -c core.hooksPath=fixture merge origin/" + default)
                denied(executable + " --config-env=alias.merge=FIXTURE merge origin/" + default)
            # Catching up the current branch from its own origin branch: fast-forward only.
            git(repo, "update-ref", "refs/remotes/origin/task", "HEAD")
            for executable in ("git", "/usr/bin/git"):
                allowed(executable + " merge --ff-only origin/task")
                allowed(executable + " -C . merge --ff-only origin/task")
                for tail in ("origin/task", "--no-edit origin/task", "--ff origin/task",
                             "--ff-only task", "--ff-only refs/remotes/origin/task",
                             "--ff-only origin/task~1", "--ff-only origin/other",
                             "--ff-only origin/HEAD", "--ff-only upstream/task",
                             "--ff-only origin/task origin/" + default,
                             "--ff-only --no-edit origin/task",
                             "--no-edit --ff-only origin/task",
                             "--ff-only --squash origin/task",
                             "--ff-only --strategy=fixture origin/task",
                             "--ff-only -X fixture origin/task",
                             "--ff-only --no-verify origin/task",
                             "--ff-only origin/task --no-edit",
                             "--ff-only origin/*", "--ff-only origin/task^",
                             "--ff-only origin/" + default + "..origin/task"):
                    denied(executable + " merge " + tail)
                denied("GIT_SSH_COMMAND=fixture " + executable + " merge --ff-only origin/task")
                denied(executable + " -c core.hooksPath=fixture merge --ff-only origin/task")
            # The ff-only form still needs a real remote-tracking ref for this branch.
            git(repo, "update-ref", "-d", "refs/remotes/origin/task")
            denied("git merge --ff-only origin/task")
            git(repo, "update-ref", "refs/remotes/origin/task", "HEAD")
            for shadow in ("refs/tags/origin/task", "refs/heads/origin/task"):
                git(repo, "update-ref", shadow, "HEAD")
                denied("git merge --ff-only origin/task")
                git(repo, "update-ref", "-d", shadow)
            for value in ("--strategy=fixture", "--squash", ""):
                git(repo, "config", "branch.task.mergeOptions", value)
                denied("git merge --ff-only origin/task")
                git(repo, "config", "--unset-all", "branch.task.mergeOptions")
            git(repo, "remote", "set-url", "origin", "https://example.invalid/fixture.git")
            denied("git merge --ff-only origin/task")
            git(repo, "remote", "set-url", "origin", "https://github.com/Jacopo1991/fixture.git")
            allowed("git merge --ff-only origin/task")
            git(repo, "merge", "--ff-only", "origin/task")
            # Never on the default branch, and never for another branch's ref.
            git(repo, "update-ref", "refs/remotes/origin/other", "HEAD")
            denied("git merge --ff-only origin/other")
            git(repo, "switch", "-q", default)
            denied("git merge --ff-only origin/" + default)
            git(repo, "switch", "-q", "task")
            git(repo, "checkout", "-q", "--detach")
            denied("git merge --ff-only origin/task")
            git(repo, "switch", "-q", "task")
            git(repo, "update-ref", "-d", "refs/remotes/origin/task")
            git(repo, "update-ref", "-d", "refs/remotes/origin/other")
            # Short refs must not be shadowed, even at the same commit.
            for shadow in ("refs/tags/origin/", "refs/heads/origin/"):
                git(repo, "update-ref", shadow + default, "HEAD")
                denied("git merge origin/" + default)
                denied("git merge --no-edit origin/" + default)
                git(repo, "update-ref", "-d", shadow + default)
            for value in ("--strategy=fixture", "--squash", "--no-verify", ""):
                git(repo, "config", "branch.task.mergeOptions", value)
                denied("git merge origin/" + default)
                denied("git merge --no-edit origin/" + default)
                git(repo, "config", "--unset-all", "branch.task.mergeOptions")
            allowed("git merge origin/" + default)
            git(repo, "merge", "--no-edit", "origin/" + default)
            git(repo, "checkout", "-q", "--detach")
            denied("git merge origin/" + default)
            git(repo, "switch", "-q", "task")
            git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
            denied("git merge origin/" + default)
            git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/" + default)
            git(repo, "update-ref", "-d", "refs/remotes/origin/" + default)
            denied("git merge origin/" + default)
            git(repo, "update-ref", "refs/remotes/origin/" + default, "HEAD")
            for url in (str(base), "file://" + str(base), "https://example.invalid/fixture.git"):
                git(repo, "remote", "set-url", "origin", url)
                denied("git merge origin/" + default)
            git(repo, "remote", "set-url", "origin", "https://github.com/Jacopo1991/fixture.git")
            git(repo, "config", "url." + str(base) + ".insteadOf", "https://github.com/")
            denied("git merge origin/" + default)
            git(repo, "config", "--unset-all", "url." + str(base) + ".insteadOf")
            allowed("git merge origin/" + default)
            allowed("git -C . merge --no-edit origin/" + default)
            outside = base / "outside"
            outside.mkdir(exist_ok=True)
            denied("git -C " + str(outside) + " merge origin/" + default)
            sibling = parent / "sibling"
            sibling.mkdir(exist_ok=True)
            git(sibling, "init", "-q")
            denied("git -C " + str(sibling) + " merge origin/" + default)
            denied("gh pr merge 1")
            with patch.object(policy.subprocess, "run", side_effect=OSError("fixture")):
                denied("git merge origin/" + default)
    # Read-only previews take the same source as the merge itself (last fixture: default trunk).
    for preview in ("git merge-tree --write-tree HEAD origin/" + default,
                    "git merge-tree --write-tree --name-only HEAD origin/" + default):
        allowed(preview)
    for preview in ("git merge-tree HEAD origin/" + default, "git merge-tree --write-tree HEAD other",
                    "git merge-tree --write-tree task origin/" + default,
                    "git merge-tree --write-tree HEAD origin/" + default + " extra",
                    "git merge-tree --write-tree --merge-base=HEAD HEAD origin/" + default,
                    "git merge-tree --write-tree --name-only --name-only HEAD origin/" + default):
        denied(preview)
print("PASS: origin default merges allowed only on current task branches; unsafe forms denied")

# Local-only projects have no remote at all: a task branch may merge the local main, and
# preview that merge; everything else stays denied.
with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary)
    code = base / "dev_workspace"
    roots = (code, base / "cortex")
    repo = code / "local-only"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "--initial-branch=main")
    git(repo, "config", "user.name", "CI")
    git(repo, "config", "user.email", "ci@example.invalid")
    (repo / "safe.txt").write_text("safe synthetic content\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    def event(command):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    def allowed(command):
        reason = policy.evaluate(event(command), roots)
        assert reason is None, command + ": " + str(reason)
    def denied(command):
        assert policy.evaluate(event(command), roots) is not None, command
    denied("git merge main")  # on main itself
    denied("git merge-tree --write-tree HEAD main")
    git(repo, "switch", "-q", "-c", "task")
    for command in ("git merge main", "git merge --no-edit main", "/usr/bin/git merge main",
                    "git -C . merge --no-edit main", "git merge-tree --write-tree HEAD main",
                    "git merge-tree --write-tree --name-only HEAD main"):
        allowed(command)
    for tail in ("other", "HEAD", "task", "origin/main", "main~1", "refs/heads/main",
                 "--squash main", "--edit main", "-s ours main", "--strategy=ours main",
                 "-X theirs main", "--no-verify main", "--ff-only main", "main --no-edit",
                 "main other", "--abort", "--continue"):
        denied("git merge " + tail)
    denied("git -c core.hooksPath=fixture merge main")
    denied("GIT_DIR=fixture git merge main")
    for value in ("--strategy=ours", "--squash", ""):
        git(repo, "config", "branch.task.mergeOptions", value)
        denied("git merge main")
        git(repo, "config", "--unset-all", "branch.task.mergeOptions")
    # A tag named main would shadow the branch; refuse rather than guess.
    git(repo, "tag", "main")
    denied("git merge main")
    denied("git merge-tree --write-tree HEAD main")
    git(repo, "tag", "-d", "main")
    git(repo, "checkout", "-q", "--detach")
    denied("git merge main")
    git(repo, "switch", "-q", "task")
    # The local rule applies only when there is no remote at all.
    git(repo, "remote", "add", "upstream", "https://github.com/Jacopo1991/fixture.git")
    denied("git merge main")
    git(repo, "remote", "remove", "upstream")
    allowed("git merge main")
    git(repo, "merge", "--no-edit", "main")
    git(repo, "branch", "-m", "main", "trunk")
    denied("git merge main")
    denied("git merge trunk")
    with patch.object(policy.subprocess, "run", side_effect=OSError("fixture")):
        denied("git merge main")
print("PASS: local-only repositories merge only the local main into a task branch; previews read-only")
