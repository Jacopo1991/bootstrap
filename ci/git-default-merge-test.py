#!/usr/bin/env python3
"""Hosted synthetic metadata tests; no remote connection or PR merge."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "agent_policy", ROOT / "home/dot_local/share/bootstrap/agent-policy/pre_tool_use.py")
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
                assert policy.evaluate(event(command), roots) is None, command
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
                denied("FOO=fixture " + executable + " merge origin/" + default)
                denied(executable + " -c core.hooksPath=fixture merge origin/" + default)
                denied(executable + " --config-env=alias.merge=FIXTURE merge origin/" + default)
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
print("PASS: origin default merges allowed only on current task branches; unsafe forms denied")
