#!/usr/bin/env python3
"""task lifecycle on real git repositories in a temporary HOME.

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
case "$1" in
  check)
    [ -x "$HOME/during-check.sh" ] && "$HOME/during-check.sh"
    [ -e FAIL ] && { echo "FAILED (failures=1)"; exit 1; }
    echo "Ran 3 tests in 0.01s"; echo OK ;;
  mcp-probe) echo "probe passed" ;;
  *) exit 2 ;;
esac
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
            ## Verification and tools
            Verify: `true`
            """))
        self.git(self.kb, "add", "-A")
        self.git(self.kb, "commit", "-qm", "kb")

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], env=getattr(self, "env", None) or
                              dict(os.environ, **IDENTITY), check=True, capture_output=True,
                              text=True).stdout.strip()

    def task(self, *args, cwd=None, code=0):
        if args and args[0] == "start" and "--builder" not in args:
            args = (*args, "--builder", "codex")
        if args and args[0] == "merge":
            self.prepare_merge_evidence(args[2])
        return self.raw_task(*args, cwd=cwd, code=code)

    def raw_task(self, *args, cwd=None, code=0):
        result = subprocess.run(["python3", str(TOOL), *args], env=self.env, cwd=cwd or self.home,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def started(self):
        out = self.task("start", "proj", "T-5", "--builder", "codex")
        wt = self.repo / ".worktrees/t5"
        self.assertTrue(wt.is_dir(), out)
        return wt, out

    def state(self, name="t5"):
        return self.home / ".local/state/task/proj" / name

    def prepare_merge_evidence(self, ident):
        path = self.repo / ".worktrees" / ("t" + ident.lower().removeprefix("t-").removeprefix("t"))
        state = self.state("t" + ident.lower().removeprefix("t-").removeprefix("t"))
        if not path.is_dir() or not (state / "task.json").exists():
            return
        content = "# Task report\n\n## What changed\n\nImplemented the task.\n\n## How I verified\n\n`task check` passed.\n\n## Criteria\n\n"
        content += "\n".join(f"#{n} met: implemented and verified" for n in (1, 2))
        content += "\n\n## Noticed, not done\n\n(none)\n"
        (path / ".work/report.md").write_text(content)
        import json
        record = json.loads((state / "task.json").read_text())
        head = self.git(path, "rev-parse", "HEAD")
        state.mkdir(parents=True, exist_ok=True)
        (state / "check.json").write_text(json.dumps({"sha": head, "record_revision": record["revision"],
                                                       "result": "passed", "steps": [], "time": "fixture"}))
        (state / "review.md").write_text("Verdict: PASS\nReviewer: claude test\nEvidence: reviewed\n")
        judgments = {str(item["number"]): "pass" for item in record["criteria"]}
        (state / "review.json").write_text(json.dumps({"verdict": "PASS", "commit": head,
                                                        "reviewer": "claude", "model": "test",
                                                        "time": "fixture-time",
                                                        "judgments": judgments,
                                                        "record_revision": record["revision"]}))

    def commit_in(self, wt, name, text="x\n"):
        (wt / name).write_text(text)
        self.git(wt, "add", name)
        self.git(wt, "commit", "-qm", f"add {name}")

    def start_slow_detached_gate(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        (self.home / "bin/just").write_text("#!/bin/sh\nsleep 30\necho OK\n")
        (self.home / "bin/just").chmod(0o755)
        out = self.raw_task("merge", "proj", "5", "--detach")
        self.assertIn("Started detached merge", out)
        deadline = __import__("time").monotonic() + 10
        gates = []
        while __import__("time").monotonic() < deadline:
            gates = list((self.repo / ".worktrees").glob(".task-gate-*"))
            if gates:
                break
            __import__("time").sleep(0.02)
        self.assertTrue(gates, "detached merge should create a temporary gate worktree")
        return wt, gates[0]

    def test_start_makes_worktree_and_prompt(self):
        wt, out = self.started()
        self.assertEqual(self.git(wt, "branch", "--show-current"), "t5-add-the-b-file")
        self.assertIn("work only in .worktrees/t5 on branch t5-add-the-b-file", out)
        self.assertIn("t-5 - Add-the-b-file.md", out)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "", "worktrees stay out of status")
        again = self.task("start", "proj", "5", "--builder", "codex")
        self.assertNotIn("Created", again, "starting twice reuses the worktree")
        self.task("start", "proj", "T-9", "--builder", "codex", code=1)  # no such task

    def test_check_passes_fails_and_refuses_dirty(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        out = self.task("check", "proj", "t5")
        self.assertIn("PASS", out)
        self.assertTrue((wt / ".work/check.log").is_file())
        self.assertEqual(self.git(wt, "status", "--porcelain"), "", ".work stays out of status")
        self.assertIn("PASS", self.task("check", cwd=wt), "inside the worktree no arguments are needed")
        (wt / "a.txt").write_text("changed\n")
        self.assertIn("Uncommitted", self.task("check", "proj", "5", code=1))
        self.git(wt, "checkout", "--", "a.txt")
        self.commit_in(wt, "FAIL")
        self.assertIn("FAIL", self.task("check", "proj", "5", code=1))

    def test_merge_gates_merge_result_without_changing_lane_and_is_idempotent(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        lane_head = self.git(wt, "rev-parse", "HEAD")
        # main moves on in the meantime (another task merged)
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        base_tip = self.git(other, "rev-parse", "HEAD")
        out = self.task("merge", "proj", "T-5")
        self.assertIn("Merged t5-add-the-b-file into main", out)
        self.assertIn("use `task merge proj T-5 --detach`", out)
        self.assertIn("Recorded review: claude test at fixture-time", out)
        self.assertEqual(self.git(self.origin, "rev-parse", "main~1"), base_tip,
                         "first parent is the gated main tip")
        self.assertIn("PASS", out)
        tip = self.git(self.origin, "rev-parse", "main")
        parents = self.git(self.origin, "rev-list", "--parents", "-n", "1", tip).split()
        self.assertEqual(len(parents), 3, "a merge commit with two parents")
        self.assertEqual(parents[2], lane_head, "second parent is the unchanged reviewed lane head")
        self.assertIn("Merge T-5: Add the b file", self.git(self.origin, "log", "-1", "--format=%B", tip))
        files = self.git(self.origin, "ls-tree", "--name-only", tip).split()
        self.assertEqual(sorted(files), ["a.txt", "b.txt", "c.txt", "justfile"])
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), tip, "main checkout fast-forwarded")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(wt.exists(), out)
        self.assertNotIn("t5-add-the-b-file", self.git(self.repo, "branch"))
        self.assertIn("already merged", self.raw_task("merge", "proj", "5"))

    def test_failed_check_merges_nothing(self):
        wt, _ = self.started()
        self.commit_in(wt, "FAIL")
        before = self.git(self.origin, "rev-parse", "main")
        self.assertIn("nothing was published", self.task("merge", "proj", "5", code=1))
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
        lane_head = self.git(wt, "rev-parse", "HEAD")
        self.commit_in(self.repo, "a.txt", "main\n")
        self.git(self.repo, "push", "-q", "origin", "main")
        out = self.task("merge", "proj", "5", code=1)
        self.assertIn("main changed a.txt that this task also changed; ask the lane to merge main into its branch, then `task check` and a new review", out)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), lane_head, "lane head remains untouched")
        self.assertEqual(self.git(wt, "status", "--porcelain"), "", "merge aborted cleanly")

    def test_close_marks_done_ticks_and_commits(self):
        wt, _ = self.started()
        record = __import__("json").loads((self.state() / "task.json").read_text())
        archive = self.state()
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "review.json").write_text(__import__("json").dumps({"judgments": {"1": "pass"}}))
        (archive / "review.md").write_text("Verdict: PASS\n#1 pass: evidence inspected\n")
        (archive / "report.md").write_text("## Noticed, not done\n\n- Slow startup could be improved\n")
        out = self.task("close", "proj", "T-5", "-m", "Built b; check green.", "--retro",
                        "went well: evidence / went wrong: none / change: keep it", "--partial")
        self.assertIn("1 criteria ticked", out)
        text = next((self.kb / "backlog/tasks").glob("t-5 *")).read_text()
        self.assertIn("status: In Progress", text)
        self.assertIn("- [x] #1", text)
        self.assertIn("- [ ] #2", text)
        self.assertIn("Built b; check green.", text)
        self.assertIn("## Review evidence", text)
        self.assertIn("#1 pass: evidence inspected", text)
        self.assertEqual(self.git(self.kb, "log", "-1", "--format=%s"), "Close T-5: Add the b file")
        self.assertIn("Slow startup", (self.kb / "improvements.md").read_text())
        self.assertIn("improvements.md", self.git(self.kb, "show", "--name-only", "--format=", "HEAD"))
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
        out = self.task("start", "proj", "tidy", "--builder", "codex")
        wt2 = self.repo / ".worktrees/tidy"
        self.assertTrue((wt2 / ".venv").is_dir() and not (wt2 / ".venv").is_symlink(), out)
        log = calls.read_text()
        self.assertIn("--exclude-editable", log)
        self.assertIn(f"--no-deps -e {wt2}", log)

    def during_check(self, script):
        hook = self.home / "during-check.sh"
        hook.write_text("#!/bin/sh\nset -e\n" + script)
        hook.chmod(0o755)

    def other_clone_pushes(self, name):
        other = self.home / "other"
        if not other.exists():
            self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, name)
        self.git(other, "push", "-q", "origin", "main")

    def test_review_main_moving_during_gate_is_regated(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.during_check(f"cd {other} && echo c > c.txt && git add c.txt && git commit -qm c "
                          f"&& git push -q origin main && git -C {self.repo} fetch -q origin\n")
        out = self.task("merge", "proj", "5")
        self.assertIn("re-gating", out)
        self.assertIn("c.txt", self.git(self.origin, "ls-tree", "--name-only", "main"))
        (self.home / "during-check.sh").unlink()
        self.assertFalse(wt.exists())
        files = self.git(self.origin, "ls-tree", "--name-only", "main").split()
        self.assertTrue({"b.txt", "c.txt"} <= set(files), files)

    def test_lane_commit_during_gate_is_not_published(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        before = self.git(self.origin, "rev-parse", "main")
        reviewed_head = self.git(wt, "rev-parse", "HEAD")
        self.during_check(f"cd {wt} && touch FAIL && git add FAIL && git commit -qm late\n")
        out = self.task("merge", "proj", "5", code=1)
        self.assertIn("lane branch changed while the gate ran", out)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD~1"), reviewed_head)

    def test_review_untracked_files_block_check_and_merge(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (wt / "helper.py").write_text("x\n")
        self.assertIn("untracked", self.task("check", "proj", "5", code=1))
        self.assertIn("untracked", self.task("merge", "proj", "5", code=1))

    def test_review_folder_that_is_not_a_worktree_is_refused(self):
        self.git(self.repo, "switch", "-q", "-c", "someone-wip")
        self.commit_in(self.repo, "wip.txt")
        (self.repo / ".worktrees/foo").mkdir(parents=True)
        (self.repo / ".worktrees/foo/justfile").write_text("check:\n    true\n")
        before = self.git(self.origin, "rev-parse", "main")
        for command in (("merge", "proj", "foo"), ("check", "proj", "foo"), ("start", "proj", "foo")):
            self.assertIn("not a worktree", self.task(*command, code=1))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(self.git(self.repo, "log", "-1", "--format=%s"), "add wip.txt")

    def test_review_ignored_results_keep_the_worktree(self):
        wt, _ = self.started()
        self.commit_in(wt, ".gitignore", "results/\n")
        (wt / "results").mkdir()
        (wt / "results/data.csv").write_text("1\n")
        (wt / "__pycache__").mkdir()
        out = self.task("merge", "proj", "5")
        self.assertIn("Kept", out)
        self.assertTrue((wt / "results/data.csv").exists())

    def test_review_unpushed_local_main_is_not_pushed(self):
        self.commit_in(self.repo, "local-only.txt")
        wt, out = self.started()
        self.assertIn("not on origin", out)
        self.assertFalse((wt / "local-only.txt").exists())
        self.commit_in(wt, "b.txt")
        self.task("merge", "proj", "5")
        self.assertNotIn("local-only.txt", self.git(self.origin, "ls-tree", "--name-only", "main"))
        self.assertTrue((self.repo / "local-only.txt").exists(), "local main is left as it was")

    def test_review_pth_pointing_into_repo_counts_as_editable(self):
        site = self.repo / ".venv/lib/python3.12/site-packages"
        site.mkdir(parents=True)
        (site / "proj.pth").write_text(str(self.repo / "src") + "\n")
        (self.home / "bin/uv").write_text("#!/bin/sh\ncase \"$*\" in \"venv \"*) for a; do last=$a; done; mkdir -p \"$last\" ;; esac\n")
        (self.home / "bin/uv").chmod(0o755)
        wt, _ = self.started()
        self.assertFalse((wt / ".venv").is_symlink())

    def test_review_failed_check_leaves_branch_as_lane_left_it(self):
        wt, _ = self.started()
        self.commit_in(wt, "FAIL")
        lane_head = self.git(wt, "rev-parse", "HEAD")
        self.other_clone_pushes("c.txt")
        self.assertIn("nothing was published", self.task("merge", "proj", "5", code=1))
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), lane_head)

    def test_review_close_commits_only_its_own_task_file(self):
        other = self.kb / "backlog/tasks/t-6 - Other.md"
        other.write_text("---\nid: T-6\ntitle: Other\nstatus: To Do\n---\n")
        self.git(self.kb, "add", "-A")
        self.git(self.kb, "commit", "-qm", "t6")
        other.write_text(other.read_text() + "edit in progress\n")
        wt, _ = self.started()
        record = __import__("json").loads((self.state() / "task.json").read_text())
        archive = self.state()
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "review.json").write_text(__import__("json").dumps({"judgments": {"1": "pass", "2": "pass"}}))
        self.task("close", "proj", "5", "-m", "done", "--retro", "all routine")
        self.assertEqual(self.git(self.kb, "show", "--name-only", "--format=", "HEAD"),
                         "backlog/tasks/t-5 - Add-the-b-file.md")
        self.assertIn("t-6", self.git(self.kb, "status", "--porcelain"))

    def test_review_main_checkout_blocked_is_reported_not_fatal(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (self.repo / "b.txt").write_text("someone's own file\n")
        out = self.task("merge", "proj", "5")
        self.assertIn("not updated", out)
        self.assertEqual((self.repo / "b.txt").read_text(), "someone's own file\n")

    def test_review_default_branch_is_not_a_task_name(self):
        self.assertIn("default branch", self.task("start", "proj", "main", "--builder", "codex", code=1))

    def test_review_merged_branch_is_removed_even_if_local_main_is_blocked(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (self.repo / "b.txt").write_text("someone's own file\n")
        self.task("merge", "proj", "5")
        self.assertNotIn("t5-add-the-b-file", self.git(self.repo, "branch"))

    def test_check_gives_an_older_worktree_its_environment(self):
        self.git(self.repo, "worktree", "add", "-q", "--no-track", "-b", "t5-old", str(self.repo / ".worktrees/t5"), "main")
        (self.repo / ".venv/lib/python3.12/site-packages").mkdir(parents=True)
        self.task("start", "proj", "5", "--refresh", "--builder", "codex")
        self.assertIn("PASS", self.task("check", "proj", "5"))
        self.assertTrue((self.repo / ".worktrees/t5/.venv").is_symlink())

    def test_merge_gate_prepares_lane_environment(self):
        (self.repo / ".venv").mkdir()
        (self.repo / "node_modules").mkdir()
        (self.home / "bin/just").write_text(
            "#!/bin/sh\n[ -d .venv ] && [ -d node_modules ] || exit 9\n"
            "echo 'Ran 1 test'; echo OK\n")
        (self.home / "bin/just").chmod(0o755)
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.assertIn("PASS", self.raw_task("check", "proj", "5"))
        out = self.task("merge", "proj", "5")
        self.assertIn("PASS", out)
        self.assertIn(str(self.state() / "gate-check.log"), out)
        self.assertFalse(any(line.startswith("PASS (log: ") and ".task-gate-" in line
                             for line in out.splitlines()))

    def test_github_repo_merges_through_a_pull_request(self):
        github = self.home / "github-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.origin), str(github))
        self.git(self.repo, "remote", "set-url", "origin", str(github))
        self.git(self.repo, "fetch", "-q", "origin")
        state = self.home / "pr-state"
        (self.home / "bin/gh").write_text(f"""#!/bin/sh
set -e
case "$1 $2" in
  "pr list") [ -f {state} ] && cat {state}; exit 0 ;;
  "pr create") echo 7 > {state}; echo "$*" > {self.home}/pr-create; exit 0 ;;
  "pr merge")
    head=$6; tmp=$(mktemp -d); git clone -q {github} $tmp
    git -C $tmp merge -q --no-ff -m "Merge pull request #7" $head
    git -C $tmp push -q origin HEAD:main; rm -f {state}; exit 0 ;;
esac
exit 1
""")
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        out = self.task("merge", "proj", "5")
        self.assertIn("Opened PR #7", out)
        self.assertIn("--title T-5: Add the b file", (self.home / "pr-create").read_text())
        self.assertIn("b.txt", self.git(github, "ls-tree", "--name-only", "main"))
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), self.git(github, "rev-parse", "main"))
        self.assertFalse(wt.exists(), out)

    def test_github_merge_base_mismatch_records_exception_without_reverting(self):
        github = self.home / "github-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.origin), str(github))
        self.git(self.repo, "remote", "set-url", "origin", str(github))
        self.git(self.repo, "fetch", "-q", "origin")
        state = self.home / "pr-state"
        (self.home / "bin/gh").write_text(f"""#!/bin/sh
set -e
case "$1 $2" in
  "pr list") [ -f {state} ] && cat {state}; exit 0 ;;
  "pr create") echo 7 > {state}; exit 0 ;;
  "pr merge")
    head=$6; tmp=$(mktemp -d); git clone -q {github} $tmp
    echo moved > $tmp/late.txt; git -C $tmp add late.txt; git -C $tmp commit -qm moved
    git -C $tmp push -q origin main
    git -C $tmp merge -q --no-ff -m "Merge pull request #7" $head
    git -C $tmp push -q origin HEAD:main; rm -rf $tmp; rm -f {state}; exit 0 ;;
esac
exit 1
""")
        (self.home / "bin/gh").chmod(0o755)
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        out = self.task("merge", "proj", "5")
        self.assertIn("WARNING: GitHub merged PR", out)
        exception = __import__("json").loads((self.state() / "exception.json").read_text())
        self.assertTrue(exception["publication_mismatch"])
        self.assertNotEqual(exception["actual_first_parent"], exception["gated_base"])
        self.assertIn("b.txt", self.git(github, "ls-tree", "--name-only", "main"))

    def test_plain_name_without_task(self):
        out = self.task("start", "proj", "tidy-docs", "--builder", "codex")
        self.assertIn("work only in .worktrees/tidy-docs on branch tidy-docs", out)

    def test_acceptance_1_merge_gate_lists_missing_evidence_and_override_is_visible(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        out = self.raw_task("merge", "proj", "5", code=1)
        for item in ("passing task check", "recorded PASS review", "filled .work/report.md"):
            self.assertIn(item, out)
        out = self.raw_task("merge", "proj", "5", "--override", "urgent approved exception")
        self.assertIn("exception", out)
        self.assertIn("Needs attention", self.raw_task("status", "proj"))

    def test_acceptance_2_verify_is_required_pinned_and_runs_after_gate(self):
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        original = task_file.read_text().replace("Verify: `true`", "Verify: `false`")
        task_file.write_text(original)
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        task_file.write_text(original.replace("Verify: `false`", "Verify: `true`"))
        out = self.task("check", "proj", "5", code=1)
        self.assertIn("FAIL", out)
        check = __import__("json").loads((self.state() / "check.json").read_text())
        self.assertEqual(check["steps"][-1]["command"], ["bash", "-c", "false"])
        self.assertEqual(check["steps"][-1]["exit"], 1)

    def test_acceptance_2_no_verify_requires_recorded_reason(self):
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        task_file.write_text(task_file.read_text().replace("Verify: `true`\n", ""))
        out = self.raw_task("start", "proj", "5", "--builder", "codex", code=1)
        self.assertIn("has no `Verify:", out)
        self.raw_task("start", "proj", "5", "--builder", "codex", "--no-verify", "manual smoke test")
        record = __import__("json").loads((self.state() / "task.json").read_text())
        self.assertEqual(record["no_verify"], "manual smoke test")

    def test_acceptance_2_t22_shaped_verification_line_is_pinned(self):
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        task_file.write_text(task_file.read_text().replace(
            "Verify: `true`", "Verify: `just check && just mcp-probe`"))
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        record = __import__("json").loads((self.state() / "task.json").read_text())
        self.assertEqual(record["verify"], ["just check && just mcp-probe"])
        task_file.write_text(task_file.read_text().replace("just check && just mcp-probe", "false"))
        self.assertIn("PASS", self.task("check", "proj", "5"))
        check = __import__("json").loads((self.state() / "check.json").read_text())
        self.assertEqual(check["steps"][-1]["command"], ["bash", "-c", "just check && just mcp-probe"])

    def test_acceptance_3_review_requires_head_other_vendor_and_every_criterion(self):
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        cases = [
            f"Verdict: PASS\nCommit: {head}\nReviewer: claude test\n#1 pass: evidence\n",
            f"Verdict: PASS\nCommit: {'0' * 40}\nReviewer: claude test\n#1 pass: evidence\n#2 pass: evidence\n",
            f"Verdict: PASS\nCommit: {head}\nReviewer: codex test\n#1 pass: evidence\n#2 pass: evidence\n",
        ]
        review_file = self.home / "review.txt"
        for content, expected in zip(cases, ("every pinned criterion", "Commit does not match", "vendor")):
            review_file.write_text(content)
            out = self.raw_task("review", "proj", "5", "--record", str(review_file), code=1)
            self.assertIn(expected, out)

    def test_acceptance_7_partial_close_requires_three_part_retro(self):
        wt, _ = self.started()
        import json
        record = json.loads((self.state() / "task.json").read_text())
        archive = self.state()
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "review.json").write_text(json.dumps({"judgments": {"1": "pass"}}))
        out = self.raw_task("close", "proj", "5", "-m", "partial", "--partial", "--retro",
                            "brief note", code=1)
        self.assertIn("three-part retro", out)
        self.assertIn("status: To Do", next((self.kb / "backlog/tasks").glob("t-5 *")).read_text())

    def test_acceptance_5_timeout_kills_process_group_and_repo_timeout_wins(self):
        wt, _ = self.started()
        sleeper = self.home / "sleeper.pid"
        (self.home / "bin/just").write_text(f"#!/bin/sh\nsleep 30 & echo $! > {sleeper}; wait\n")
        (self.home / "bin/just").chmod(0o755)
        (wt / ".task.toml").write_text("check_timeout = 1\n")
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "set task timeout")
        out = self.task("check", "proj", "5", code=1)
        self.assertIn("ran over 1s", out)
        pid = int(sleeper.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_acceptance_6_detached_check_lock_cancel_and_interrupted_status(self):
        wt, _ = self.started()
        (self.home / "bin/just").write_text("#!/bin/sh\nsleep 30\n")
        (self.home / "bin/just").chmod(0o755)
        out = self.raw_task("check", "--detach", "proj", "5")
        self.assertIn("Started detached", out)
        out = self.raw_task("check", "--detach", "proj", "5", code=1)
        self.assertIn("already live", out)
        run = __import__("json").loads((self.state() / "run.json").read_text())
        self.assertEqual(run["state"], "running")
        self.assertIn("Cancelled", self.raw_task("cancel", "proj", "5"))
        self.assertEqual(__import__("json").loads((self.state() / "run.json").read_text())["state"], "cancelled")
        self.raw_task("check", "--detach", "proj", "5")
        run = __import__("json").loads((self.state() / "run.json").read_text())
        os.killpg(run["pgid"], 9)
        self.assertIn("interrupted", self.raw_task("status", "proj"))

    def test_acceptance_6_detached_merge_finishes_and_rerun_observes_merge(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        out = self.raw_task("merge", "proj", "5", "--detach")
        self.assertIn("Started detached merge", out)
        archive_run = self.state() / "run.json"
        deadline = __import__("time").monotonic() + 10
        while __import__("time").monotonic() < deadline:
            if archive_run.is_file():
                run = __import__("json").loads(archive_run.read_text())
                if run.get("state") in {"passed", "failed"}:
                    break
            __import__("time").sleep(0.05)
        self.assertEqual(run["state"], "passed", archive_run.read_text())
        self.assertIn("already merged", self.raw_task("merge", "proj", "5"))

    def test_round2_private_state_and_tracked_work_refusal(self):
        wt, _ = self.started()
        self.assertTrue((self.state() / "task.json").is_file())
        self.assertEqual(sorted(p.name for p in (wt / ".work").iterdir()), ["report.md"])
        self.assertFalse((wt / ".work/task.json").exists())
        (wt / ".work/leak.json").write_text("{}")
        self.git(wt, "add", "-f", ".work/leak.json")
        self.git(wt, "commit", "-qm", "track private evidence")
        self.assertIn("Tracked files under .work/", self.raw_task("check", "proj", "5", code=1))

    def test_round2_moved_main_other_files_merges_but_same_file_stops_for_re_review(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        out = self.task("merge", "proj", "5")
        self.assertIn("Merged", out)

        (self.repo / "a.txt").write_text("a\nmiddle\nbase2\n")
        self.git(self.repo, "add", "a.txt")
        self.git(self.repo, "commit", "-qm", "prepare two line file")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        (wt / "a.txt").write_text("lane\nmiddle\nbase2\n")
        self.git(wt, "add", "a.txt")
        self.git(wt, "commit", "-qm", "lane edits first line")
        (self.repo / "a.txt").write_text("a\nmiddle\nmain\n")
        self.git(self.repo, "add", "a.txt")
        self.git(self.repo, "commit", "-qm", "main edits second line")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.prepare_merge_evidence("5")
        lane_head = self.git(wt, "rev-parse", "HEAD")
        out = self.raw_task("merge", "proj", "5", code=1)
        self.assertIn("main changed a.txt that this task also changed; ask the lane to merge main into its branch, then `task check` and a new review", out)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), lane_head)

    def test_round2_refused_push_is_resumable_without_override(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        real_git = __import__("shutil").which("git")
        wrapper = self.home / "bin/git"
        wrapper.write_text(f"#!/bin/sh\ncase \" $* \" in *\" push \"*) "
                           f"if [ ! -e \"$HOME/push-refused\" ]; then "
                           "touch \"$HOME/push-refused\"; echo refused >&2; exit 1; fi;; esac\n"
                           f"exec {real_git} \"$@\"\n")
        wrapper.chmod(0o755)
        out = self.raw_task("merge", "proj", "5", code=1)
        self.assertIn("Push to main was refused", out)
        lane_head = self.git(wt, "rev-parse", "HEAD")
        self.assertEqual(lane_head, __import__("json").loads((self.state() / "review.json").read_text())["commit"])
        wrapper.unlink()
        out = self.raw_task("merge", "proj", "5")
        self.assertIn("Recorded review: claude test at fixture-time", out)
        self.assertIn("Merged", out)
        self.assertEqual(self.git(self.origin, "rev-parse", "main^2"), lane_head)
        self.assertFalse(wt.exists())

    def test_round3_unreviewed_main_integration_into_lane_is_rejected(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        self.git(wt, "fetch", "-q", "origin")
        self.git(wt, "merge", "--no-edit", "origin/main")
        integrated = self.git(wt, "rev-parse", "HEAD")
        out = self.raw_task("merge", "proj", "5", code=1)
        self.assertIn("passing task check for lane head", out)
        self.assertIn("recorded PASS review for lane head", out)
        self.assertNotEqual(integrated, __import__("json").loads((self.state() / "review.json").read_text())["commit"])

    def test_round3_lane_integrated_main_then_checked_and_reviewed_merges(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        self.git(wt, "fetch", "-q", "origin")
        self.git(wt, "merge", "--no-edit", "origin/main")
        self.raw_task("check", "proj", "5")
        self.prepare_merge_evidence("5")
        head = self.git(wt, "rev-parse", "HEAD")
        evidence = self.home / "review.txt"
        evidence.write_text(f"Verdict: PASS\nCommit: {head[:9]}\nReviewer: claude test\n"
                            "#1 pass: checked b\n#2 pass: checks pass\n")
        self.raw_task("review", "proj", "5", "--record", str(evidence))
        out = self.raw_task("merge", "proj", "5")
        self.assertIn("Merged", out)
        self.assertEqual(self.git(self.origin, "rev-parse", "main^2"), head)


    def test_round2_override_close_allows_done_but_requires_retro_and_names_exception(self):
        self.started()
        state = self.state()
        (state / "exception.json").write_text('{"reason":"approved emergency"}')
        out = self.raw_task("close", "proj", "5", "-m", "Closed under exception", "--retro",
                            "went well: contained / went wrong: no review / change: review next time")
        self.assertIn("0 criteria ticked", out)
        text = next((self.kb / "backlog/tasks").glob("t-5 *")).read_text()
        self.assertIn("status: Done", text)
        self.assertIn("Exception: merge override — approved emergency", text)
        self.assertIn("- [ ] #1", text)

    def test_round3_failed_check_requires_three_part_retro(self):
        self.started()
        state = self.state()
        (state / "review.json").write_text('{"judgments": {}}')
        (state / "check.json").write_text('{"result":"failed"}')
        out = self.raw_task("close", "proj", "5", "-m", "Close failed attempt", "--retro", "routine",
                            "--partial", code=1)
        self.assertIn("three-part retro", out)
        out = self.raw_task("close", "proj", "5", "-m", "Close failed attempt", "--retro",
                            "went well: isolated / went wrong: check failed / change: investigate first", "--partial")
        self.assertIn("0 criteria ticked", out)

    def test_round3_status_lists_attention_and_uses_check_event_for_idle(self):
        wt, _ = self.started()
        self.raw_task("check", "proj", "5")
        state = self.state()
        (state / "exception.json").write_text('{"reason":"approved"}')
        (state / "review.json").write_text('{"verdict":"FAIL","commit":"old-head"}')
        (self.kb / "improvements.md").write_text("- 2026-10-10 | T-5 | report | item | open\n")
        out = self.raw_task("status", "proj")
        self.assertIn("idle=0m", out)
        self.assertIn("t5: exception", out)
        self.assertIn("t5: FAIL review", out)
        self.assertIn("open improvements: 1", out)
        (state / "review.json").write_text('{"verdict":"PASS","commit":"old-head"}')
        self.assertIn("t5: stale review", self.raw_task("status", "proj"))

    def test_round2_refresh_uses_merge_base_and_preserves_filled_report(self):
        wt, _ = self.started()
        self.commit_in(wt, "lane.txt")
        report = wt / ".work/report.md"
        report.write_text("# Task report\n\n## What changed\n\nChanged one file.\n\n"
                          "## How I verified\n\nChecks passed.\n\n## Criteria\n\n"
                          "#1 met: done\n#2 met: checked\n\n## Noticed, not done\n\nNone.\n")
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "main.txt")
        self.git(other, "push", "-q", "origin", "main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--refresh")
        record = __import__("json").loads((self.state() / "task.json").read_text())
        self.assertEqual(record["base_sha"], self.git(wt, "merge-base", "HEAD", "origin/main"))
        self.assertIn("Changed one file", report.read_text())
        self.assertFalse((self.state() / "exception.json").exists())
        self.assertFalse((self.state() / "review.json").exists())

    def test_round3_refresh_clears_stale_exception_and_review(self):
        wt, _ = self.started()
        state = self.state()
        (state / "exception.json").write_text('{"reason":"old record"}')
        (state / "review.json").write_text('{"verdict":"PASS"}')
        (state / "review.md").write_text("old review")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--refresh")
        self.assertFalse((state / "exception.json").exists())
        self.assertFalse((state / "review.json").exists())
        self.assertFalse((state / "review.md").exists())

    def test_round2_preflight_blocks_before_worktree_for_missing_command_and_allows_builtin(self):
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        task_file.write_text(task_file.read_text().replace("Verify: `true`", "Verify: `not-installed-task-cmd`"))
        out = self.raw_task("start", "proj", "5", "--builder", "codex", code=1)
        self.assertIn("not installed", out)
        self.assertFalse((self.repo / ".worktrees/t5").exists())
        task_file.write_text(task_file.read_text().replace("not-installed-task-cmd", "cd . && true"))
        self.started()

    def test_round2_review_prompt_and_parser_accept_normal_markdown_and_unique_short_sha(self):
        wt, _ = self.started()
        prompt = self.raw_task("review", "proj", "5")
        self.assertIn("Task:", prompt)
        self.assertIn("Worktree:", prompt)
        self.assertIn("Diff range:", prompt)
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "review.txt"
        review.write_text(f"**Verdict: PASS**\nCommit: {head[:9]}\nReviewer: claude model-x\n"
                          "- #1 pass: evidence\n- #2 pass: evidence\n")
        self.assertIn("Recorded PASS", self.raw_task("review", "proj", "5", "--record", str(review)))

    def test_round2_cancel_uses_archive_and_merge_mentions_detach(self):
        self.started()
        run = self.state() / "run.json"
        run.write_text(__import__("json").dumps({"state": "running", "pid": 99999999,
                                                  "pgid": 99999999, "completed": []}))
        out = self.raw_task("cancel", "proj", "5")
        self.assertIn("Cleared interrupted run lock", out)
        self.assertIn("--detach", self.raw_task("merge", "proj", "5", code=1))

    def test_round2_foreground_run_holds_task_lock(self):
        self.started()
        (self.home / "bin/just").write_text("#!/bin/sh\nsleep 1\necho 'Ran 1 test'; echo OK\n")
        (self.home / "bin/just").chmod(0o755)
        process = subprocess.Popen(["python3", str(TOOL), "check", "proj", "5"],
                                   cwd=self.home, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        lock = self.state() / "run.lock"
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline and not lock.exists():
            __import__("time").sleep(0.02)
        self.assertTrue(lock.exists(), "foreground check must acquire the task lock")
        out = self.raw_task("check", "proj", "5", code=1)
        self.assertIn("already live", out)
        stdout, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stdout)

    def test_round3_sighup_marks_foreground_run_interrupted_and_cancel_clears_lock(self):
        self.started()
        (self.home / "bin/just").write_text("#!/bin/sh\nsleep 30\n")
        (self.home / "bin/just").chmod(0o755)
        process = subprocess.Popen(["python3", str(TOOL), "check", "proj", "5"],
                                   cwd=self.home, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        lock = self.state() / "run.lock"
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline and not lock.exists():
            __import__("time").sleep(0.02)
        self.assertTrue(lock.exists(), "foreground check must acquire the task lock")
        process.send_signal(__import__("signal").SIGHUP)
        stdout, _ = process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0, stdout)
        run = __import__("json").loads((self.state() / "run.json").read_text())
        self.assertEqual(run["state"], "interrupted")
        self.assertFalse(lock.exists())
        status = self.raw_task("status", "proj")
        self.assertIn("interrupted", status)
        self.assertIn("Needs attention", status)
        self.assertIn("Cleared interrupted run lock", self.raw_task("cancel", "proj", "5"))

    def test_round4_status_skips_live_gate_and_cancel_removes_it(self):
        _wt, gate = self.start_slow_detached_gate()
        out = self.raw_task("status", "proj")
        self.assertIn("t5:", out)
        self.assertNotIn("task status:", out)
        self.assertTrue(gate.exists())
        out = self.raw_task("cancel", "proj", "5")
        self.assertIn("Cancelled", out)
        self.assertFalse(gate.exists(), f"gate worktree survived cancel: {gate}")
        self.assertFalse(list((self.repo / ".worktrees").glob(".task-gate-*")))

    def test_round4_merge_prunes_killed_gate_worktree(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        dead_pid = 999999999
        stale = self.repo / ".worktrees" / f".task-gate-t5-{dead_pid}"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(stale), "main")
        self.assertTrue(stale.is_dir())
        out = self.task("merge", "proj", "5")
        self.assertIn("Removed stale gate worktree", out)
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
