#!/usr/bin/env python3
"""task start/check/merge/close on real git repositories in a temporary HOME.

`just`, `backlog` and `gh` are small stand-ins on PATH so the test runs anywhere: `just check`
passes unless the worktree holds a file named FAIL; `backlog task edit` applies status, ticks and
final summary the way Backlog.md does; `gh pr list` finds no PR.
"""
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "home/dot_local/bin/executable_task"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

STUBS = {
    "just": """#!/bin/sh
[ "$1" = check ] || exit 2
[ -e FAIL ] && { echo "FAILED (failures=1)"; exit 1; }
echo "Ran 3 tests in 0.01s"; echo OK
""",
    "gh": """#!/bin/sh
exit 0
""",
    "backlog": """#!/usr/bin/env python3
import re, sys, pathlib
args = sys.argv[1:]
assert args[:3] == ["task", "edit", args[2]] and args[2].startswith("T-"), args
number = args[2][2:]
path = next(pathlib.Path("backlog/tasks").glob(f"t-{number} - *.md"))
text = path.read_text()
i = 3
while i < len(args):
    flag, value = args[i], args[i + 1]
    if flag == "-s":
        text = re.sub(r"(?m)^status: .*$", "status: " + value, text)
    elif flag == "--check-ac":
        text = text.replace(f"- [ ] #{value} ", f"- [x] #{value} ")
    elif flag == "--final-summary":
        text += "\\n## Final Summary\\n" + value + "\\n"
    i += 2
path.write_text(text)
""",
}


class TaskTool(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            (bin_dir / name).write_text(textwrap.dedent(body))
            (bin_dir / name).chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{bin_dir}:{os.environ['PATH']}", **IDENTITY)
        self.origin = self.home / "origin.git"
        self.git(self.home, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.home / "dev_workspace/proj"
        self.repo.mkdir(parents=True)
        self.git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "justfile").write_text("check:\n    true\n")
        (self.repo / "a.txt").write_text("a\n")
        self.git(self.repo, "add", "justfile", "a.txt")
        self.git(self.repo, "commit", "-qm", "base")
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        self.git(self.repo, "remote", "set-head", "origin", "main")
        self.kb = self.home / "cortex/cortex-kb-proj"
        (self.kb / "backlog/tasks").mkdir(parents=True)
        self.git(self.kb, "init", "-q", "-b", "main")
        (self.kb / "backlog/tasks/t-5 - Add-the-b-file.md").write_text(textwrap.dedent("""\
            ---
            id: T-5
            title: Add the b file
            status: To Do
            ---
            ## Acceptance Criteria
            - [ ] #1 b exists
            - [ ] #2 checks pass
            """))
        self.git(self.kb, "add", "-A")
        self.git(self.kb, "commit", "-qm", "kb")

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], env=getattr(self, "env", None) or
                              dict(os.environ, **IDENTITY), check=True, capture_output=True,
                              text=True).stdout.strip()

    def task(self, *args, cwd=None, code=0):
        result = subprocess.run(["python3", str(TOOL), *args], env=self.env, cwd=cwd or self.home,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def started(self):
        out = self.task("start", "proj", "T-5")
        wt = self.repo / ".worktrees/t5"
        self.assertTrue(wt.is_dir(), out)
        return wt, out

    def commit_in(self, wt, name, text="x\n"):
        (wt / name).write_text(text)
        self.git(wt, "add", name)
        self.git(wt, "commit", "-qm", f"add {name}")

    def test_start_makes_worktree_and_prompt(self):
        wt, out = self.started()
        self.assertEqual(self.git(wt, "branch", "--show-current"), "t5-add-the-b-file")
        self.assertIn("work only in .worktrees/t5 on branch t5-add-the-b-file", out)
        self.assertIn("t-5 - Add-the-b-file.md", out)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "", "worktrees stay out of status")
        again = self.task("start", "proj", "5")
        self.assertNotIn("Created", again, "starting twice reuses the worktree")
        self.task("start", "proj", "T-9", code=1)  # no such task

    def test_check_passes_fails_and_refuses_dirty(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        out = self.task("check", "proj", "t5")
        self.assertIn("PASS", out)
        self.assertTrue((wt / ".work/check.log").is_file())
        self.assertEqual(self.git(wt, "status", "--porcelain"), "", ".work stays out of status")
        self.assertIn("PASS", self.task("check", cwd=wt), "inside the worktree no arguments are needed")
        (wt / "a.txt").write_text("changed\n")
        self.assertIn("Uncommitted changes", self.task("check", "proj", "5", code=1))
        self.git(wt, "checkout", "--", "a.txt")
        self.commit_in(wt, "FAIL")
        self.assertIn("FAIL", self.task("check", "proj", "5", code=1))

    def test_merge_brings_main_in_merges_and_cleans_up(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        # main moves on in the meantime (another task merged)
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        out = self.task("merge", "proj", "T-5")
        self.assertIn("Merged origin/main into t5-add-the-b-file", out)
        self.assertIn("PASS", out)
        tip = self.git(self.origin, "rev-parse", "main")
        parents = self.git(self.origin, "rev-list", "--parents", "-n", "1", tip).split()
        self.assertEqual(len(parents), 3, "a merge commit with two parents")
        self.assertIn("Merge T-5: Add the b file", self.git(self.origin, "log", "-1", "--format=%B", tip))
        files = self.git(self.origin, "ls-tree", "--name-only", tip).split()
        self.assertEqual(sorted(files), ["a.txt", "b.txt", "c.txt", "justfile"])
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), tip, "main checkout fast-forwarded")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(wt.exists(), out)
        self.assertNotIn("t5-add-the-b-file", self.git(self.repo, "branch"))

    def test_failed_check_merges_nothing(self):
        wt, _ = self.started()
        self.commit_in(wt, "FAIL")
        before = self.git(self.origin, "rev-parse", "main")
        self.assertIn("nothing merged", self.task("merge", "proj", "5", code=1))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), before)
        self.assertTrue(wt.exists())

    def test_merge_local_only_repo_and_main_checkout_on_other_branch(self):
        self.git(self.repo, "remote", "remove", "origin")
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.git(self.repo, "switch", "-q", "-c", "someone-else")
        self.task("merge", "proj", "5")
        self.assertEqual(self.git(self.repo, "show", "main:b.txt"), "x")
        self.assertEqual(self.git(self.repo, "branch", "--show-current"), "someone-else", "checkout untouched")

    def test_conflict_is_reported_not_forced(self):
        wt, _ = self.started()
        self.commit_in(wt, "a.txt", "lane\n")
        self.commit_in(self.repo, "a.txt", "main\n")
        self.git(self.repo, "push", "-q", "origin", "main")
        out = self.task("merge", "proj", "5", code=1)
        self.assertIn("does not merge cleanly", out)
        self.assertEqual(self.git(wt, "status", "--porcelain"), "", "merge aborted cleanly")

    def test_close_marks_done_ticks_and_commits(self):
        out = self.task("close", "proj", "T-5", "-m", "Built b; check green.")
        self.assertIn("2 criteria ticked", out)
        text = next((self.kb / "backlog/tasks").glob("t-5 *")).read_text()
        self.assertIn("status: Done", text)
        self.assertNotIn("- [ ]", text)
        self.assertIn("Built b; check green.", text)
        self.assertEqual(self.git(self.kb, "log", "-1", "--format=%s"), "Close T-5: Add the b file")
        self.assertEqual(self.git(self.kb, "status", "--porcelain"), "")

    def test_start_shares_or_rebuilds_the_environment(self):
        venv = self.repo / ".venv/lib/python3.12/site-packages"
        venv.mkdir(parents=True)
        (self.repo / "node_modules").mkdir()
        wt, out = self.started()
        self.assertEqual(os.readlink(wt / ".venv"), str(self.repo / ".venv"), out)
        self.assertTrue((wt / "node_modules").is_symlink())
        self.commit_in(wt, "b.txt")
        self.assertEqual(self.git(wt, "status", "--porcelain"), "", "links stay out of status")
        self.task("merge", "proj", "5")
        self.assertFalse(wt.exists(), "linked worktrees are still removed after merge")
        self.assertTrue((self.repo / ".venv").is_dir(), "the shared venv survives")
        # editable install: the worktree gets its own venv built with uv
        (venv / "__editable__.proj-0.1.pth").write_text("")
        calls = self.home / "uv-calls"
        (self.home / "bin/uv").write_text(f"""#!/bin/sh
echo "$*" >> {calls}
case "$*" in "venv "*) for a; do last=$a; done; mkdir -p "$last" ;; esac
""")
        (self.home / "bin/uv").chmod(0o755)
        out = self.task("start", "proj", "tidy")
        wt2 = self.repo / ".worktrees/tidy"
        self.assertTrue((wt2 / ".venv").is_dir() and not (wt2 / ".venv").is_symlink(), out)
        log = calls.read_text()
        self.assertIn("--exclude-editable", log)
        self.assertIn(f"--no-deps -e {wt2}", log)

    def test_plain_name_without_task(self):
        out = self.task("start", "proj", "tidy-docs")
        self.assertIn("work only in .worktrees/tidy-docs on branch tidy-docs", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
