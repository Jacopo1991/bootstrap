#!/usr/bin/env python3
"""task lifecycle on real git repositories in a temporary HOME.

`just`, `backlog` and `gh` are small stand-ins on PATH so the test runs anywhere: `just check`
passes unless the worktree holds a file named FAIL; `backlog task edit` applies status, ticks and
final summary the way Backlog.md does; `gh pr list` finds no PR.
"""
import fcntl
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "home/dot_local/bin/executable_task"
REVIEW_FIXTURES = ROOT / "ci/fixtures/reviews"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
TASK_LOADER = importlib.machinery.SourceFileLoader("task_tool_for_review_test", str(TOOL))
TASK_SPEC = importlib.util.spec_from_loader(TASK_LOADER.name, TASK_LOADER)
TASK_MODULE = importlib.util.module_from_spec(TASK_SPEC)
TASK_SPEC.loader.exec_module(TASK_MODULE)

STUBS = {
    "just": """#!/bin/sh
case "$1 $2" in
  "--show sync") echo probe >> "$HOME/sync-probe"; grep -q '^sync:' justfile && { echo 'sync recipe'; exit 0; }; exit 1 ;;
  "sync ")
    if [ -x "$HOME/during-sync.sh" ]; then "$HOME/during-sync.sh" || exit $?; fi
    echo OK ;;
  "check ")
    [ -x "$HOME/during-check.sh" ] && "$HOME/during-check.sh"
    [ -e FAIL ] && { echo "FAILED (failures=1)"; exit 1; }
    echo "Ran 3 tests in 0.01s"; echo OK ;;
  "mcp-probe ") echo "probe passed" ;;
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
        for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_THREAD_ID"):
            self.env.pop(marker, None)
        self.gate_dir = self.home / "shared-gate"
        self.gate_dir.mkdir()
        self.env["TASK_GATE_DIR"] = str(self.gate_dir)
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

    def raw_task(self, *args, cwd=None, code=0, env=None):
        result = subprocess.run([sys.executable, str(TOOL), *args], env=env or self.env, cwd=cwd or self.home,
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

    def checked_lane_with_counter(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        (self.home / "during-check.sh").write_text('echo run >> "$HOME/just-count"\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.raw_task("check", "proj", "5")
        return wt

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
        commit_at = out.index("Commit on the branch")
        check_at = out.index("then run `task check`", commit_at)
        report_at = out.index("then fill `.work/report.md`", check_at)
        later_commit_at = out.index("any later commit needs a new check", report_at)
        self.assertLess(commit_at, check_at)
        self.assertLess(check_at, report_at)
        self.assertLess(report_at, later_commit_at)
        for line in ("three lines: `changed: ...`, `verified: ...`, and `not done: ...`",
                     "followed by `report written`", "for advisory feedback",
                     "runs the trusted gate", "A PM may run `task check --detach`",
                     "PM can use `task merge --detach`"):
            self.assertIn(line, out)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "", "worktrees stay out of status")
        again = self.task("start", "proj", "5", "--builder", "codex")
        self.assertNotIn("Created", again, "starting twice reuses the worktree")
        self.task("start", "proj", "T-9", "--builder", "codex", code=1)  # no such task

    def test_real_folded_backlog_title_reaches_dispatch_and_review_prompts(self):
        source = ROOT / "ci/fixtures/t48-folded-title.md"
        task_file = next((self.kb / "backlog/tasks").glob("t-5 - *.md"))
        task_file.write_text(source.read_text())
        expected = ("Data layer phase 5 - household zone - location, module settings, "
                    "contracts, export and delete")
        out = self.raw_task("start", "proj", "5", "--builder", "codex", "--no-verify",
                            "fixture has no Verify command")
        self.assertIn(f"T-5 - {expected}", out)
        self.assertIn("on branch t5-data-layer-phase-5", out)
        record = __import__("json").loads((self.state() / "task.json").read_text())
        self.assertEqual(record["title"], expected)
        self.assertNotIn(">-", out)

        out = self.raw_task("review", "proj", "5")
        prompt = (self.state() / "review-prompt.md").read_text()
        self.assertIn(f"Outcome: {expected}", prompt)
        self.assertNotIn(">-", prompt)

    def test_frontmatter_title_reads_quoted_folded_and_literal_scalars(self):
        parser = TASK_MODULE.frontmatter_title
        self.assertEqual(parser("---\ntitle: 'Quoted: it''s a title'\n---\n"), "Quoted: it's a title")
        self.assertEqual(parser('---\ntitle: "Quoted title"\n---\n'), "Quoted title")
        self.assertEqual(parser("---\ntitle: >-\n  folded title\n  continues here\nstatus: Done\n---\n"),
                         "folded title continues here")
        self.assertEqual(parser("---\ntitle: >\n  folded title\n  continues here\nstatus: Done\n---\n"),
                         "folded title continues here")
        self.assertEqual(parser("---\ntitle: |-\n  literal title\n  continues here\nstatus: Done\n---\n"),
                         "literal title\ncontinues here")
        self.assertEqual(parser("---\ntitle: |\n  literal title\n  continues here\nstatus: Done\n---\n"),
                         "literal title\ncontinues here")

    def test_start_adopts_existing_branch_without_task_worktree(self):
        self.git(self.repo, "branch", "t5-preexisting", "main")
        out = self.task("start", "proj", "5", "--builder", "codex")
        wt = self.repo / ".worktrees/t5"
        self.assertIn("on the existing branch t5-preexisting", out)
        self.assertEqual(self.git(wt, "branch", "--show-current"), "t5-preexisting")
        self.assertTrue((wt / "a.txt").is_file())

    def test_start_reports_existing_branch_checked_out_elsewhere(self):
        external = self.home / "lane-worktree"
        self.git(self.repo, "worktree", "add", "-q", "--no-track", "-b", "t5-external",
                 str(external), "main")
        out = self.task("start", "proj", "5", "--builder", "codex", code=1)
        self.assertIn(f"Branch t5-external is already checked out at {external}", out)
        self.assertNotIn("fatal:", out)
        self.assertFalse((self.repo / ".worktrees/t5").exists())

    def test_start_adopts_slash_branch_with_sanitized_worktree_name(self):
        self.git(self.repo, "branch", "codex/direct-request", "main")
        out = self.raw_task("start", "proj", "codex/direct-request", "--builder", "codex")
        wt = self.repo / ".worktrees/codex-direct-request"
        self.assertIn("on the existing branch codex/direct-request", out)
        self.assertIn(".worktrees/codex-direct-request on branch codex/direct-request", out)
        self.assertEqual(self.git(wt, "branch", "--show-current"), "codex/direct-request")
        self.assertTrue((self.state("codex-direct-request") / "task.json").is_file())

    def test_direct_request_without_backlog_uses_two_part_report_and_request_review(self):
        self.git(self.repo, "branch", "codex/direct-request", "main")
        self.raw_task("start", "proj", "codex/direct-request", "--builder", "codex")
        wt = self.repo / ".worktrees/codex-direct-request"
        self.commit_in(wt, "direct-request.txt")
        (wt / ".work/report.md").write_text(
            "# Task report\n\n## What changed\n\nImplemented the direct request.\n\n"
            "## How I verified\n\n`task check` passed.\n")
        self.raw_task("check", "proj", "codex/direct-request")
        prompt = self.raw_task("review", "proj", "codex/direct-request")
        self.assertIn("Outcome: Direct request for branch codex/direct-request", prompt)
        self.assertIn("Criteria: none; judge the stated request above against the diff.", prompt)
        self.assertIn("judge the stated request above against the diff", prompt)
        self.assertIn("Diff range:", prompt)
        review = self.home / "direct-request-review.txt"
        head = self.git(wt, "rev-parse", "HEAD")
        review.write_text(f"Verdict: PASS\nCommit: {head}\nReviewer: claude reviewer-model\n")
        self.raw_task("review", "proj", "codex/direct-request", "--record", str(review))
        merged = self.raw_task("merge", "proj", "codex/direct-request")
        self.assertIn("Merged codex/direct-request into main", merged)

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

    def test_check_with_read_only_state_is_advisory_and_writes_local_result(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        state = self.state()
        state.chmod(0o555)
        try:
            out = self.raw_task("check", "proj", "5")
            self.assertIn("advisory lane check (not evidence for merge)", out)
            self.assertIn("PASS", out)
            evidence = __import__("json").loads((wt / ".work/check.json").read_text())
            self.assertEqual(evidence["result"], "passed")
            self.assertTrue(evidence["advisory"])
            self.assertFalse(evidence["trusted"])
            self.assertTrue((wt / ".work/check.log").is_file())
            self.assertFalse((state / "check.json").exists())
        finally:
            state.chmod(0o755)

    def test_agent_advisory_check_returns_real_failure_code(self):
        wt, _ = self.started()
        self.commit_in(wt, "FAIL")
        env = dict(self.env, CLAUDECODE="1")
        out = self.raw_task("check", "proj", "5", env=env, code=1)
        self.assertIn("advisory lane check (not evidence for merge)", out)
        self.assertIn("FAIL", out)
        evidence = __import__("json").loads((wt / ".work/check.json").read_text())
        self.assertEqual(evidence["result"], "failed")
        self.assertTrue(evidence["advisory"])

    def test_agent_markers_skip_unavailable_shared_gate_and_status_separates_lane_result(self):
        (self.repo / ".task.toml").write_text("exclusive_gate = true\n")
        self.git(self.repo, "add", ".task.toml")
        self.git(self.repo, "commit", "-qm", "enable exclusive gate")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        for index, (marker, value) in enumerate((("CLAUDECODE", "1"),
                                                  ("CODEX_SANDBOX", "workspace-write"))):
            env = dict(self.env, **{marker: value},
                       TASK_GATE_DIR=str(self.home / (marker + "-missing") / "gate"))
            args = ("check", "proj", "5", "--detach") if index == 0 else ("check", "proj", "5")
            out = self.raw_task(*args, env=env)
            self.assertNotIn("Started detached", out)
            self.assertIn("advisory lane check (not evidence for merge)", out)
            self.assertNotIn("shared gate lock unavailable", out)
            self.assertIn("PASS", out)
            self.assertFalse((self.state() / "check.json").exists())
        lane_result = __import__("json").loads((wt / ".work/check.json").read_text())
        self.assertTrue(lane_result["advisory"])
        self.raw_task("check", "proj", "5")
        trusted = __import__("json").loads((self.state() / "check.json").read_text())
        self.assertTrue(trusted["trusted"])
        self.assertTrue(__import__("json").loads((wt / ".work/check.json").read_text())["advisory"])
        self.assertIn("lane check: passed (advisory)", self.raw_task("status", "proj"))

    def test_codex_marker_is_advisory_even_when_state_is_writable(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        state = self.state()
        probe = state / "writable-probe"
        probe.write_text("writable")
        probe.unlink()
        out = self.raw_task("check", "proj", "5", env=dict(self.env, CODEX_THREAD_ID="thread-test"))
        self.assertIn("advisory lane check (not evidence for merge)", out)
        self.assertTrue(__import__("json").loads((wt / ".work/check.json").read_text())["advisory"])
        self.assertFalse((state / "check.json").exists())

    def test_exclude_failure_and_flock_error_degrade_without_traceback(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        exclude_path = self.repo / ".git/info/exclude"
        exclude_path.chmod(0o444)
        out = self.raw_task("check", "proj", "5", env=dict(self.env, CLAUDECODE="1"))
        self.assertIn("advisory lane check (not evidence for merge)", out)
        self.assertNotIn("Traceback", out)

        loader = importlib.machinery.SourceFileLoader("task_tool_for_flock_test", str(TOOL))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.HOME = self.home
        module.CORTEX = self.home / "cortex"
        # A pattern that is not yet present forces a write to the read-only exclude file.
        module.exclude(self.repo, "new-pattern-not-present/")
        self.assertNotIn("new-pattern-not-present/", exclude_path.read_text())
        exclude_path.chmod(0o644)
        task = module.Task(self.repo, "T-5")
        original_flock = module.fcntl.flock
        module.fcntl.flock = lambda *_args: (_ for _ in ()).throw(OSError("probe failure"))
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                with module.exclusive_gate_lock(task):
                    pass
        finally:
            module.fcntl.flock = original_flock
        self.assertEqual(output.getvalue().count(
            "shared gate lock unavailable in this sandbox; running without it"), 1)
        self.assertNotIn("Traceback", output.getvalue())

    def test_shared_gate_symlink_is_refused(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (wt / ".task.toml").write_text("exclusive_gate = true\n")
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "enable exclusive gate")
        lock_path = self.gate_dir / "agent-task-gate.lock"
        lock_path.symlink_to(self.home / "other-file")
        (self.home / "other-file").touch()
        out = self.raw_task("check", "proj", "5")
        self.assertIn("shared gate lock unavailable in this sandbox; running without it", out)
        lock_path.unlink()

    def test_start_rejects_redundant_just_check_verify_line(self):
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        task_file.write_text(task_file.read_text().replace("Verify: `true`", "Verify: `just check   `"))
        out = self.raw_task("start", "proj", "5", "--builder", "codex", code=1)
        self.assertIn("Verify `just check` is redundant", out)
        self.assertIn("already runs it before the Verify commands", out)
        self.assertFalse((self.repo / ".worktrees/t5").exists())

    def test_merge_reuses_lane_check_when_default_has_not_moved(self):
        self.checked_lane_with_counter()
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1)
        check = __import__("json").loads((self.state() / "check.json").read_text())
        self.assertEqual(check["base_sha"], self.git(self.repo, "rev-parse", "origin/main"))
        self.assertTrue(check["trusted"])
        run_path = self.state() / "run.json"
        run_path.write_text(__import__("json").dumps({"command": "merge", "state": "running", "completed": []}))
        run_env = dict(self.env, TASK_RUN_RECORD=str(run_path), TASK_RUN_ARCHIVE=str(run_path))
        out = self.raw_task("merge", "proj", "5", env=run_env)
        self.assertIn("main unchanged since the lane's check; reusing it", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1,
                         "unchanged default should not rerun just check in a gate worktree")
        self.assertIn("gate", __import__("json").loads(run_path.read_text())["completed"])
        self.assertFalse((self.home / "sync-probe").exists(), "no dependency change should not probe or run sync")

    def test_merge_regates_when_default_moved_after_lane_check(self):
        self.checked_lane_with_counter()
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        out = self.raw_task("merge", "proj", "5")
        self.assertNotIn("main unchanged since the lane's check; reusing it", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 2,
                         "default movement requires checking the would-be merge result")

    def assert_check_record_regates(self, mutation, override=False):
        self.checked_lane_with_counter()
        check_path = self.state() / "check.json"
        check = __import__("json").loads(check_path.read_text())
        mutation(check)
        check_path.write_text(__import__("json").dumps(check))
        args = ["merge", "proj", "5"]
        if override:
            args.extend(["--override", "permit fresh gate after stale check evidence"])
        out = self.raw_task(*args)
        self.assertNotIn("main unchanged since the lane's check; reusing it", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 2,
                         "stale or failed check records must rerun the merge gate")

    def test_merge_does_not_reuse_check_with_different_head(self):
        self.assert_check_record_regates(lambda check: check.update(sha="0" * 40), override=True)

    def test_merge_does_not_reuse_check_with_different_base(self):
        self.assert_check_record_regates(lambda check: check.update(base_sha="0" * 40))

    def test_merge_does_not_reuse_failed_check_record(self):
        self.assert_check_record_regates(lambda check: check.update(result="failed"), override=True)

    def test_merge_does_not_reuse_legacy_check_without_base_sha(self):
        self.assert_check_record_regates(lambda check: check.pop("base_sha"))

    def test_merge_does_not_reuse_advisory_check(self):
        self.assert_check_record_regates(lambda check: check.update(trusted=False, advisory=True))

    def test_merge_runs_one_trusted_gate_when_no_trusted_check_exists(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        (self.state() / "check.json").unlink()
        (self.home / "during-check.sh").write_text('echo run >> "$HOME/just-count"\n')
        (self.home / "during-check.sh").chmod(0o755)
        out = self.raw_task("merge", "proj", "5")
        self.assertIn("Merged", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1)
        gate_check = __import__("json").loads((self.state() / "gate-check.json").read_text())
        self.assertTrue(gate_check["trusted"])

    def test_merge_trusted_gate_uses_the_shared_exclusive_lock(self):
        (self.repo / ".task.toml").write_text("exclusive_gate = true\n")
        self.git(self.repo, "add", ".task.toml")
        self.git(self.repo, "commit", "-qm", "enable exclusive gate")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        (self.state() / "check.json").unlink()
        lock_path = self.gate_dir / "agent-task-gate.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX)
        release = threading.Timer(0.25, lambda: os.close(fd))
        release.start()
        try:
            out = self.raw_task("merge", "proj", "5")
        finally:
            release.join(timeout=2)
        self.assertIn("Waiting for exclusive task gate held by", out)
        self.assertIn("Merged", out)

    def test_override_runs_gate_without_check_record_and_records_failed_gate(self):
        wt, _ = self.started()
        self.commit_in(wt, "FAIL")
        self.prepare_merge_evidence("5")
        (self.state() / "check.json").unlink()
        (self.home / "during-check.sh").write_text('echo run >> "$HOME/just-count"\n')
        (self.home / "during-check.sh").chmod(0o755)
        out = self.raw_task("merge", "proj", "5", "--override", "approved failing-gate exception")
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1)
        self.assertIn("FAIL (log:", out)
        self.assertIn("failed merge gate overridden", out)
        self.assertIn("Merged", out)
        exception = __import__("json").loads((self.state() / "exception.json").read_text())
        self.assertTrue(exception["gate_failed"])

    def test_merge_warns_when_dependency_change_has_no_sync_recipe(self):
        wt, _ = self.started()
        self.commit_in(wt, "requirements.txt", "package==1\n")
        self.prepare_merge_evidence("5")
        out = self.task("merge", "proj", "5")
        self.assertIn("WARNING: dependency files changed (requirements.txt)", out)
        self.assertIn("main checkout's environment is now out of date", out)

    def test_merge_runs_only_repository_sync_recipe_for_dependency_change(self):
        justfile = self.repo / "justfile"
        justfile.write_text(justfile.read_text() + "\nsync:\n    echo sync\n")
        self.git(self.repo, "add", "justfile")
        self.git(self.repo, "commit", "-qm", "add sync recipe")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        self.commit_in(wt, "pyproject.toml", "[project]\nname = 'fixture'\n")
        self.prepare_merge_evidence("5")
        (self.home / "during-sync.sh").write_text('echo sync >> "$HOME/sync-count"\n')
        (self.home / "during-sync.sh").chmod(0o755)
        out = self.task("merge", "proj", "5")
        self.assertIn("Ran `just sync` after dependency changes: pyproject.toml", out)
        self.assertEqual((self.home / "sync-count").read_text().splitlines(), ["sync"])
        self.assertNotIn("environment is now out of date", out)

    def test_merge_runs_sync_for_requirements_in_change(self):
        justfile = self.repo / "justfile"
        justfile.write_text(justfile.read_text() + "\nsync:\n    echo sync\n")
        self.git(self.repo, "add", "justfile")
        self.git(self.repo, "commit", "-qm", "add sync recipe")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        self.commit_in(wt, "requirements-dev.in", "package\n")
        self.prepare_merge_evidence("5")
        (self.home / "during-sync.sh").write_text('echo sync >> "$HOME/sync-count"\n')
        (self.home / "during-sync.sh").chmod(0o755)
        out = self.task("merge", "proj", "5")
        self.assertIn("Ran `just sync` after dependency changes: requirements-dev.in", out)
        self.assertEqual((self.home / "sync-count").read_text().splitlines(), ["sync"])

    def test_merge_reports_failed_sync_recipe_loudly(self):
        justfile = self.repo / "justfile"
        justfile.write_text(justfile.read_text() + "\nsync:\n    echo sync\n")
        self.git(self.repo, "add", "justfile")
        self.git(self.repo, "commit", "-qm", "add sync recipe")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        self.commit_in(wt, "package.json", "{}\n")
        self.prepare_merge_evidence("5")
        (self.home / "during-sync.sh").write_text('echo dependency sync failed >&2; exit 1\n')
        (self.home / "during-sync.sh").chmod(0o755)
        out = self.task("merge", "proj", "5")
        self.assertIn("WARNING: `just sync` failed", out)
        self.assertIn("package.json", out)
        self.assertIn("dependency sync failed", out)
        self.assertIn("environment may be out of date", out)

    def test_merge_says_just_missing_when_dependency_sync_cannot_be_checked(self):
        wt, _ = self.started()
        self.commit_in(wt, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        self.prepare_merge_evidence("5")
        isolated_bin = self.home / "isolated-bin"
        isolated_bin.mkdir()
        for name in ("bash", "git", "python3"):
            executable = shutil.which(name, path=self.env["PATH"])
            self.assertIsNotNone(executable, f"{name} is needed by this test")
            os.symlink(executable, isolated_bin / name)
        os.symlink(self.home / "bin/just", isolated_bin / "just")
        env = dict(self.env, PATH=str(isolated_bin))
        remover = self.home / "during-check.sh"
        remover.write_text('/bin/mv "$HOME/isolated-bin/just" "$HOME/isolated-bin/just.removed"\n')
        remover.chmod(0o755)
        out = self.raw_task("merge", "proj", "5", env=env)
        self.assertIn("dependency files changed (pnpm-lock.yaml)", out)
        self.assertIn("`just` is not installed", out)
        self.assertNotIn("no `just sync` recipe", out)

    def test_review_prints_claude_headless_command_and_persisted_prompt(self):
        wt, _ = self.started()
        out = self.raw_task("review", "proj", "5")
        prompt = self.state() / "review-prompt.md"
        reply = self.state() / "review-response.md"
        self.assertIn(f"claude -p --add-dir {self.kb} --permission-mode dontAsk --model haiku --effort high "
                      "--allowedTools Read Grep Glob 'Bash(git --no-optional-locks:*)' <", out)
        self.assertIn(f"{prompt} > {reply}", out)
        self.assertIn(f"task review proj 5 --record {reply}", out)
        self.assertIn("Worktree:", prompt.read_text())
        self.assertTrue(prompt.read_text().endswith(
            "Return exactly this final fenced block, with plain text only inside it:\n"
            "```review\nVerdict: PASS|FAIL\nCommit: <full sha>\nReviewer: <vendor> <model>\n"
            "#1 pass|fail|unclear: <one line of evidence>\n"
            "#2 pass|fail|unclear: <one line of evidence>\n```\nThen list findings.\n"))
        for command in ("git --no-optional-locks diff", "git --no-optional-locks log",
                        "git --no-optional-locks show"):
            self.assertIn(command, prompt.read_text())
        self.assertIn(f"read {wt / '.work/report.md'} alongside the diff", prompt.read_text())
        self.assertFalse(reply.exists(), "a stale response must not be presented as the next review")

    def test_review_includes_direct_request_text(self):
        self.task("start", "proj", "direct-request", "--builder", "codex")
        request = self.home / "request.md"
        request.write_text("Preserve the user's uncommitted files and explain the behavior change.")
        out = self.raw_task("review", "proj", "direct-request", "--request", str(request))
        prompt = self.state("direct-request") / "review-prompt.md"
        self.assertIn("Preserve the user's uncommitted files and explain the behavior change.", prompt.read_text())
        self.assertIn("--allowedTools Read Grep Glob", out)

    def test_review_prints_read_only_codex_command_for_claude_builder(self):
        self.raw_task("start", "proj", "5", "--builder", "claude")
        out = self.raw_task("review", "proj", "5")
        prompt = self.state() / "review-prompt.md"
        reply = self.state() / "review-response.md"
        self.assertIn(f"GIT_OPTIONAL_LOCKS=0 codex exec --sandbox read-only --add-dir {self.kb} "
                      "-m gpt-6-luna -c model_reasoning_effort=high - <", out)
        self.assertIn(f"{prompt} > {reply}", out)
        self.assertIn(f"task review proj 5 --record {reply}", out)
        for command in ("git --no-optional-locks diff", "git --no-optional-locks log",
                        "git --no-optional-locks show"):
            self.assertIn(command, prompt.read_text())

    def test_direct_review_commands_do_not_add_kb_directory_without_backlog_file(self):
        self.raw_task("start", "proj", "direct-request", "--builder", "claude")
        out = self.raw_task("review", "proj", "direct-request")
        self.assertIn("codex exec --sandbox read-only", out)
        self.assertNotIn("--add-dir", out)

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
        self.assertEqual(out.count("For long gates"), 1)
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

    def test_close_with_origin_uses_resumable_pr_and_merge_commit(self):
        origin = self.home / "knowledge-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.kb), str(origin))
        self.git(self.kb, "remote", "add", "origin", str(origin))
        self.git(self.kb, "fetch", "-q", "origin")
        self.git(self.kb, "remote", "set-head", "origin", "main")
        status = self.home / "close-pr.json"
        merge_sha = self.home / "close-merge-sha"
        (self.home / "bin/gh").write_text(f"""#!/bin/sh
set -e
case "$1 $2" in
  "pr list") [ -f {status} ] && cat {status} || echo '[]' ;;
  "pr create") echo '[{{"number":7,"state":"OPEN","mergeCommit":null,"headRefName":"task-close-t5","baseRefName":"main"}}]' > {status}; echo "$*" > {self.home}/close-pr-create ;;
  "pr merge")
    test "$4" = --merge
    test "$5" = --match-head-commit
    head=$6
    tmp=$(mktemp -d)
    git clone -q {origin} "$tmp/repo"
    git -C "$tmp/repo" fetch -q origin task-close-t5
    git -C "$tmp/repo" merge -q --no-ff -m "Merge pull request #7" FETCH_HEAD
    git -C "$tmp/repo" push -q origin HEAD:main
    git -C "$tmp/repo" rev-parse HEAD > {merge_sha}
    printf '[{{"number":7,"state":"MERGED","mergeCommit":{{"oid":"%s"}},"headRefName":"task-close-t5","baseRefName":"main"}}]\\n' "$(cat {merge_sha})" > {status}
    echo "$head" > {self.home}/close-match-head
    echo "$*" > {self.home}/close-pr-merge
    rm -rf "$tmp" ;;
  "pr view") printf '{{"state":"MERGED","mergeCommit":{{"oid":"%s"}}}}\\n' "$(cat {merge_sha})" ;;
  *) exit 1 ;;
esac
""")
        (self.home / "bin/gh").chmod(0o755)
        wt, _ = self.started()
        archive = self.state()
        (archive / "review.json").write_text('{"judgments":{"1":"pass","2":"pass"}}')
        (archive / "review.md").write_text("Verdict: PASS\\n#1 pass: reviewed\\n")
        close_dirs = self.kb / ".worktrees"
        close_dirs_existed = close_dirs.exists()
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine")
        self.assertIn("Opened close PR #7", out)
        self.assertIn("Merged close PR #7 with merge commit", out)
        self.assertIn("--merge --match-head-commit", (self.home / "close-pr-merge").read_text())
        close_head = (self.home / "close-match-head").read_text().strip()
        self.assertEqual(self.git(origin, "rev-parse", "task-close-t5"), close_head)
        self.assertNotEqual(__import__("subprocess").run(
            ["git", "-C", str(self.kb), "show-ref", "--verify", "--quiet",
             "refs/heads/task-close-t5"], capture_output=True).returncode, 0,
                         "successful close removes the merged local task-close branch")
        merge = self.git(origin, "rev-parse", "main")
        parents = self.git(origin, "rev-list", "--parents", "-n", "1", merge).split()
        self.assertEqual(len(parents), 3, "the PR must use a two-parent merge commit")
        self.assertEqual(parents[2], close_head)
        self.assertEqual(self.git(self.kb, "rev-parse", "main"), merge, "clean checkout fast-forwards")
        contents = sorted(path.name for path in close_dirs.iterdir()) if close_dirs.exists() else []
        close_paths = list(close_dirs.glob("task-close-*")) if close_dirs.exists() else []
        self.assertEqual(close_paths, [],
                         f"assert close removes its own temporary worktree; directory listing: {contents!r}\n{out}")
        self.assertEqual(close_dirs.exists(), close_dirs_existed,
                         "close removes .worktrees only when it created the now-empty folder")
        before_count = self.git(origin, "rev-list", "--count", "main")
        close_dirs.mkdir(exist_ok=True)
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine")
        self.assertIn("already merged", out)
        self.assertTrue(close_dirs.is_dir(), "close must preserve a pre-existing .worktrees folder")
        self.assertEqual(list(close_dirs.iterdir()), [], "the pre-existing folder stays empty")
        self.assertEqual(self.git(origin, "rev-list", "--count", "main"), before_count,
                         "rerunning close must not create a second commit")

    def test_close_resumes_after_pr_creation_failure_without_duplicate_commit(self):
        origin = self.home / "knowledge-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.kb), str(origin))
        self.git(self.kb, "remote", "add", "origin", str(origin))
        self.git(self.kb, "fetch", "-q", "origin")
        self.git(self.kb, "remote", "set-head", "origin", "main")
        state = self.home / "close-state"
        (self.home / "bin/gh").write_text(f"""#!/bin/sh
case "$1 $2" in
  "pr list") [ -f {state} ] && cat {state} || echo '[]' ;;
  "pr create")
    if [ ! -f {state}.failed ]; then touch {state}.failed; echo 'creation refused' >&2; exit 1; fi
    echo '[{{"number":8,"state":"OPEN","mergeCommit":null,"headRefName":"task-close-t5","baseRefName":"main"}}]' > {state} ;;
  "pr merge")
    if [ ! -f {state}.merge-failed ]; then touch {state}.merge-failed; exit 1; fi
    tmp=$(mktemp -d)
    git clone -q {origin} "$tmp/repo"
    git -C "$tmp/repo" fetch -q origin task-close-t5
    git -C "$tmp/repo" merge -q --no-ff -m "Merge pull request #8" FETCH_HEAD
    git -C "$tmp/repo" push -q origin HEAD:main
    git -C "$tmp/repo" rev-parse HEAD > {state}.merge-sha
    printf '[{{"number":8,"state":"MERGED","mergeCommit":{{"oid":"%s"}},"headRefName":"task-close-t5","baseRefName":"main"}}]\\n' "$(cat {state}.merge-sha)" > {state}
    rm -rf "$tmp" ;;
  "pr view") printf '{{"state":"MERGED","mergeCommit":{{"oid":"%s"}}}}\\n' "$(cat {state}.merge-sha)" ;;
  *) exit 1 ;;
esac
""")
        (self.home / "bin/gh").chmod(0o755)
        self.started()
        archive = self.state()
        (archive / "review.json").write_text('{"judgments":{"1":"pass","2":"pass"}}')
        (archive / "review.md").write_text("Verdict: PASS\\n#1 pass: reviewed\\n")
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine", code=1)
        self.assertIn("PR creation failed", out)
        self.assertIn("Close worktree kept at", out, "a deliberately kept worktree must be announced")
        self.assertEqual(len(list((self.kb / ".worktrees").glob("task-close-*"))), 1)
        self.assertEqual(self.git(origin, "rev-list", "--count", "task-close-t5"), "2")
        self.assertEqual(self.git(self.kb, "rev-parse", "main"), self.git(origin, "rev-parse", "main"),
                         "PR workflow leaves the main checkout alone before merge")
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine", code=1)
        self.assertIn("remains open", out)
        self.assertEqual(self.git(origin, "rev-list", "--count", "task-close-t5"), "2",
                         "resume reuses the close commit")
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine")
        self.assertIn("Merged close PR #8", out)
        self.assertFalse((self.kb / ".worktrees").exists(), "the resumed close removes the worktree it created")
        self.assertEqual(self.git(origin, "rev-list", "--count", "task-close-t5"), "2",
                         "merging after a failed attempt still reuses the same close commit")

    def test_close_pr_with_no_changes_reports_already_closed(self):
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        task_file.write_text(task_file.read_text() + "\n## Review evidence\n\nAlready reviewed.\n")
        self.git(self.kb, "add", "backlog/tasks")
        self.git(self.kb, "commit", "-qm", "record review evidence")
        origin = self.home / "knowledge-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.kb), str(origin))
        self.git(self.kb, "remote", "add", "origin", str(origin))
        self.git(self.kb, "fetch", "-q", "origin")
        self.git(self.kb, "remote", "set-head", "origin", "main")
        (self.home / "bin/backlog").write_text("#!/bin/sh\nexit 0\n")
        (self.home / "bin/backlog").chmod(0o755)
        (self.home / "bin/gh").write_text("#!/bin/sh\n"
            "if [ \"$1 $2\" = 'pr list' ]; then "
            "echo '[{\"number\":7,\"state\":\"OPEN\",\"headRefName\":\"task-close-t5\",\"baseRefName\":\"main\"}]'; "
            "else exit 1; fi\n")
        (self.home / "bin/gh").chmod(0o755)
        self.started()
        archive = self.state()
        (archive / "review.json").write_text('{"judgments":{"1":"pass","2":"pass"}}')
        (archive / "review.md").write_text("Verdict: PASS\n#1 pass: reviewed\n")
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine")
        self.assertIn("T-5 was already closed.", out)
        self.assertEqual(list((self.kb / ".worktrees").glob("task-close-*")), [])

    def test_exclusive_gate_serializes_checks_and_wait_does_not_use_timeout(self):
        lock_path = self.gate_dir / "agent-task-gate.lock"
        holder_path = self.gate_dir / "agent-task-gate.holder"
        self.addCleanup(lambda: holder_path.unlink(missing_ok=True))
        (self.repo / ".task.toml").write_text("exclusive_gate = true\ncheck_timeout = 3\n")
        self.git(self.repo, "add", ".task.toml")
        self.git(self.repo, "commit", "-qm", "enable exclusive gate")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.started()
        other = self.repo / ".worktrees/other"
        self.git(self.repo, "worktree", "add", "-q", "--no-track", "-b", "other", str(other), "main")
        (other / ".task.toml").write_text("exclusive_gate = true\ncheck_timeout = 1\n")
        self.git(other, "add", ".task.toml")
        self.git(other, "commit", "-qm", "short check timeout")
        (self.home / "during-check.sh").write_text(
            "#!/bin/sh\n"
            "if [ -e \"$HOME/gate-active\" ]; then echo overlap > \"$HOME/gate-overlap\"; exit 9; fi\n"
            "echo $$ > \"$HOME/gate-active\"\n"
            "sleep \"${TASK_CHECK_SLEEP:-0.05}\"\n"
            "rm -f \"$HOME/gate-active\"\n")
        (self.home / "during-check.sh").chmod(0o755)
        env1 = dict(self.env, TASK_CHECK_SLEEP="2.2")
        env2 = dict(self.env, TASK_CHECK_SLEEP="0.05")
        first = subprocess.Popen([sys.executable, str(TOOL), "check", "proj", "5"],
                                 cwd=self.home, env=env1, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline and not (self.home / "gate-active").exists():
            __import__("time").sleep(0.02)
        self.assertTrue((self.home / "gate-active").exists(), "first check acquired the shared gate")
        holder = __import__("json").loads(holder_path.read_text())
        self.assertEqual(holder["repo"], "proj")
        self.assertEqual(holder["task"], "T-5")
        started = __import__("time").monotonic()
        second = subprocess.Popen([sys.executable, str(TOOL), "check", "proj", "other"],
                                  cwd=self.home, env=env2, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        out1, _ = first.communicate(timeout=8)
        out2, _ = second.communicate(timeout=8)
        elapsed = __import__("time").monotonic() - started
        self.assertEqual(first.returncode, 0, out1)
        self.assertEqual(second.returncode, 0, out2)
        self.assertGreater(elapsed, 2, "the second check waited longer than its one-second check timeout")
        self.assertIn("Waiting for exclusive task gate held by task T-5 in proj, pid", out2)
        self.assertIn("since ", out2)
        self.assertFalse((self.home / "gate-overlap").exists(), "checks never overlapped")

        dead_holder = self.home / "dead-gate-holder.py"
        dead_holder.write_text(
            "import fcntl, os\n"
            f"fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o666)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n")
        subprocess.run([sys.executable, str(dead_holder)], check=True, timeout=5)
        out = self.raw_task("check", "proj", "other")
        self.assertIn("PASS", out, "kernel releases the gate lock when its process exits")

        nonexclusive = self.repo / ".worktrees/nonexclusive"
        self.git(self.repo, "worktree", "add", "-q", "--no-track", "-b", "nonexclusive",
                 str(nonexclusive), "main")
        (nonexclusive / ".task.toml").write_text("check_timeout = 1\n")
        self.git(nonexclusive, "add", ".task.toml")
        self.git(nonexclusive, "commit", "-qm", "leave gate opt-in off")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            begin = __import__("time").monotonic()
            out = self.raw_task("check", "proj", "nonexclusive")
            self.assertLess(__import__("time").monotonic() - begin, 1)
            self.assertIn("PASS", out)
            self.assertNotIn("Waiting for exclusive task gate", out)
        finally:
            os.close(fd)

    def test_exclusive_gate_unavailable_prints_notice_and_runs_check(self):
        wt, _ = self.started()
        (wt / ".task.toml").write_text("exclusive_gate = true\n")
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "enable exclusive gate")
        env = dict(self.env, TASK_GATE_DIR=str(self.home / "missing" / "gate"))
        out = self.raw_task("check", "proj", "5", env=env)
        self.assertEqual(out.count("shared gate lock unavailable in this sandbox; running without it"), 1)
        self.assertIn("PASS", out)

    def test_close_pr_worktree_is_inside_knowledge_repo(self):
        origin = self.home / "knowledge-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.kb), str(origin))
        self.git(self.kb, "remote", "add", "origin", str(origin))
        self.git(self.kb, "fetch", "-q", "origin")
        self.git(self.kb, "remote", "set-head", "origin", "main")
        (self.home / "bin/gh").write_text(f"""#!/bin/sh
case "$1 $2" in
  "pr list") echo '[]' ;;
  "pr create") echo 'creation refused' >&2; exit 1 ;;
  *) exit 1 ;;
esac
""")
        (self.home / "bin/gh").chmod(0o755)
        self.started()
        archive = self.state()
        (archive / "review.json").write_text('{"judgments":{"1":"pass","2":"pass"}}')
        (archive / "review.md").write_text("Verdict: PASS\\n#1 pass: reviewed\\n")
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine", code=1)
        self.assertIn("PR creation failed", out)
        close_worktrees = list((self.kb / ".worktrees").glob("task-close-*"))
        self.assertEqual(len(close_worktrees), 1)
        self.assertTrue(close_worktrees[0].resolve().is_relative_to(self.kb.resolve()))

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

    def test_start_repairs_missing_origin_head(self):
        self.git(self.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        self.task("start", "proj", "T-5", "--builder", "codex")
        self.assertEqual(self.git(self.repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"),
                         "origin/main")

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
        merge_sha = self.home / "merge-sha"
        (self.home / "bin/gh").write_text(f"""#!/bin/sh
set -e
case "$1 $2" in
  "pr list") [ -f {state} ] && cat {state}; exit 0 ;;
  "pr create") echo 7 > {state}; echo "$*" > {self.home}/pr-create; exit 0 ;;
  "pr merge")
    head=$6; tmp=$(mktemp -d); git clone -q {github} $tmp
    git -C $tmp merge -q --no-ff -m "Merge pull request #7" $head
    git -C $tmp push -q origin HEAD:main; git -C $tmp rev-parse HEAD > {merge_sha}
    rm -f {state}; exit 0 ;;
  "pr view") printf '{{"mergeCommit":{{"oid":"%s"}}}}\n' "$(cat {merge_sha})"; exit 0 ;;
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

    def test_github_later_push_does_not_create_false_base_exception(self):
        github = self.home / "github-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.origin), str(github))
        self.git(self.repo, "remote", "set-url", "origin", str(github))
        self.git(self.repo, "fetch", "-q", "origin")
        state = self.home / "pr-state"
        merge_sha = self.home / "merge-sha"
        (self.home / "bin/gh").write_text(f"""#!/bin/sh
set -e
case "$1 $2" in
  "pr list") [ -f {state} ] && cat {state}; exit 0 ;;
  "pr create") echo 7 > {state}; exit 0 ;;
  "pr merge")
    head=$6; tmp=$(mktemp -d); git clone -q {github} $tmp
    git -C $tmp merge -q --no-ff -m "Merge pull request #7" $head
    git -C $tmp push -q origin HEAD:main; git -C $tmp rev-parse HEAD > {merge_sha}
    echo later > $tmp/later.txt; git -C $tmp add later.txt; git -C $tmp commit -qm later
    git -C $tmp push -q origin main; rm -f {state}; exit 0 ;;
  "pr view") printf '{{"mergeCommit":{{"oid":"%s"}}}}\n' "$(cat {merge_sha})"; exit 0 ;;
esac
exit 1
""")
        (self.home / "bin/gh").chmod(0o755)
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        out = self.task("merge", "proj", "5")
        self.assertIn("Merged PR #7", out)
        self.assertNotIn("WARNING:", out)
        self.assertFalse((self.state() / "exception.json").exists())
        self.assertIn("later.txt", self.git(github, "ls-tree", "--name-only", "main"))

    def test_github_merge_base_mismatch_records_exception_without_reverting(self):
        github = self.home / "github-origin.git"
        self.git(self.home, "clone", "-q", "--bare", str(self.origin), str(github))
        self.git(self.repo, "remote", "set-url", "origin", str(github))
        self.git(self.repo, "fetch", "-q", "origin")
        state = self.home / "pr-state"
        merge_sha = self.home / "merge-sha"
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
    git -C $tmp push -q origin HEAD:main; git -C $tmp rev-parse HEAD > {merge_sha}
    rm -rf $tmp; rm -f {state}; exit 0 ;;
  "pr view") printf '{{"mergeCommit":{{"oid":"%s"}}}}\n' "$(cat {merge_sha})"; exit 0 ;;
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
        for item in ("recorded PASS review", "filled .work/report.md"):
            self.assertIn(item, out)
        self.assertNotIn("passing task check", out)
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

    def test_cancel_kills_check_descendants_that_started_new_sessions(self):
        self.started()
        child_pid_file = self.home / "setsid-child.pid"
        script = ("#!/bin/sh\n"
                  f"setsid sh -c 'echo $$ > {child_pid_file}; exec sleep 30' &\n"
                  "wait\n")
        just = self.home / "bin/just"
        just.write_text(script)
        just.chmod(0o755)
        self.raw_task("check", "--detach", "proj", "5")
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline and not child_pid_file.exists():
            __import__("time").sleep(0.02)
        self.assertTrue(child_pid_file.exists(), "the check must start its setsid child")
        child_pid = int(child_pid_file.read_text())
        run = __import__("json").loads((self.state() / "run.json").read_text())
        environment = Path(f"/proc/{child_pid}/environ").read_bytes().split(b"\0")
        self.assertIn(f"TASK_RUN_ID={run['task_run_id']}".encode(), environment)
        self.assertIn("Cancelled", self.raw_task("cancel", "proj", "5"))
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline:
            stat = Path(f"/proc/{child_pid}/stat")
            if not stat.exists() or stat.read_text().split(") ", 1)[1][0] == "Z":
                break
            __import__("time").sleep(0.05)
        stat = Path(f"/proc/{child_pid}/stat")
        self.assertTrue(not stat.exists() or stat.read_text().split(") ", 1)[1][0] == "Z",
                        "cancel must terminate the setsid descendant")

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
        self.assertIn("recorded PASS review for lane head", out)
        self.assertNotIn("passing task check for lane head", out)
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
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        task_file.write_text(task_file.read_text() +
                             "\n## Outcome\n\nA real outcome from the task.\n\n"
                             "## Scope\n\nTouch the task tool and its checks.\n")
        wt, _ = self.started()
        prompt = self.raw_task("review", "proj", "5")
        self.assertIn("Task:", prompt)
        self.assertIn("Worktree:", prompt)
        self.assertIn("Outcome: A real outcome from the task.", prompt)
        self.assertIn("Scope: Touch the task tool and its checks.", prompt)
        self.assertIn("Diff range:", prompt)
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "review.txt"
        review.write_text(f"**Verdict: PASS**\nCommit: {head[:9]}\nReviewer: claude model-x\n"
                          "- #1 pass: evidence\n- #2 pass: evidence\n")
        self.assertIn("Recorded PASS", self.raw_task("review", "proj", "5", "--record", str(review)))

    def test_review_record_accepts_bold_verdict_labels_and_backticked_sha(self):
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")[:9]
        review = self.home / "review-format.txt"
        for verdict in ("**Verdict:** PASS", "Verdict: **PASS**"):
            review.write_text(f"{verdict}\nCommit: `{head}`\nReviewer: claude format-test\n"
                              "#1 pass: reviewed source\n#2 pass: ran acceptance evidence\n")
            out = self.raw_task("review", "proj", "5", "--record", str(review))
            self.assertIn("Recorded PASS", out, verdict)

    def test_real_markdown_review_fixtures_parse_verdict_commit_and_only_stated_judgments(self):
        cases = {
            "haiku-1.md": ([1, 2, 3, 4, 5], "FAIL", "93b8888", "claude",
                           {1: "pass", 2: "pass", 3: "pass", 4: "pass", 5: "fail"}),
            "haiku-2.md": ([1, 2, 3, 4, 5], "FAIL", "298aed910b35f0da8f54052574b567ce9c03b640", "claude",
                           {1: "pass", 2: "pass", 3: "pass", 4: "pass", 5: "pass"}),
            "luna-1.md": (list(range(1, 9)), "FAIL", "c3c5dac9f337d6f5ad37bc677ecaf6c9d596a1de", "codex",
                          {2: "pass", 3: "fail", 4: "pass", 5: "pass", 6: "pass", 7: "pass", 8: "pass"}),
            "luna-2.md": ([1, 2, 3, 4, 5], "FAIL", "ec01a2e465ac66f92fabeb50f8230772194e61e1", "codex",
                          {1: "fail", 2: "pass", 3: "pass", 4: "pass", 5: "unclear"}),
        }
        for filename, (criteria, expected_verdict, expected_commit, expected_vendor, expected_judgments) in cases.items():
            with self.subTest(fixture=filename):
                source = REVIEW_FIXTURES / filename
                self.assertTrue(source.is_file(), f"missing copied fixture: {source}")
                text = source.read_text()
                verdict, commit, reviewer, judgments = TASK_MODULE.parse_review_reply(text, criteria)
                self.assertEqual(verdict, expected_verdict)
                self.assertEqual(commit, expected_commit)
                self.assertEqual(reviewer[0].lower(), expected_vendor)
                self.assertEqual({number: result for number, (result, _) in judgments.items()},
                                 expected_judgments)
                self.assertTrue(all(evidence for _, evidence in judgments.values()))

    def test_review_parser_ignores_numbered_verdicts_without_matching_task_criteria(self):
        text = """A prose introduction before the verdict.
## Verdict: **FAIL**
**Commit**: `abcdef0123456789`
**Reviewer**: codex gpt-6-luna
9. **Pass:** unrelated numbered finding
| #1 | pass | table evidence |
"""
        verdict, commit, reviewer, judgments = TASK_MODULE.parse_review_reply(text, [1, 2])
        self.assertEqual((verdict, commit, reviewer[0].lower()), ("FAIL", "abcdef0123456789", "codex"))
        self.assertEqual(judgments, {1: ("pass", "table evidence")})

    def test_review_parser_accepts_numbered_em_dash_and_unprefixed_table_rows_for_pinned_criteria(self):
        text = """Verdict: FAIL, mainly on R3.5
Commit: abcdef0123456789
Reviewer: codex gpt-6-luna
1. **Pass** — numbered evidence
| 2 | fail | table evidence |
"""
        verdict, _, _, judgments = TASK_MODULE.parse_review_reply(text, [1, 2])
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(judgments, {1: ("pass", "numbered evidence"), 2: ("fail", "table evidence")})

    def test_review_parser_does_not_map_numbered_findings_when_hash_criteria_are_present(self):
        text = """Verdict: FAIL
Commit: abcdef0123456789
Reviewer: codex gpt-6-luna
#1 pass: explicit criterion evidence
2. fail — numbered finding, not a criterion judgment
| 2 | fail | table finding, not a criterion judgment |
"""
        _, _, _, judgments = TASK_MODULE.parse_review_reply(text, [1, 2])
        self.assertEqual(judgments, {1: ("pass", "explicit criterion evidence")})

    def test_review_parser_does_not_treat_verdict_prefix_words_as_judgments(self):
        text = """Verdict: FAIL
Commit: abcdef0123456789
Reviewer: codex gpt-6-luna
1. Fail-safe default is missing
2. fail
"""
        _, _, _, judgments = TASK_MODULE.parse_review_reply(text, [1, 2])
        self.assertEqual(judgments, {2: ("fail", "")})

    def test_haiku3_fenced_fixture_records_fail_without_criteria(self):
        fixture = REVIEW_FIXTURES / "haiku-3-fenced.md"
        text = fixture.read_text()
        verdict, commit, reviewer, judgments = TASK_MODULE.parse_review_reply(text, [])
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(commit, "b85ccb9d8c26080884670ca87c2fd6cd9ae5b998")
        self.assertEqual(reviewer, ("claude", "haiku-5-5"))
        self.assertEqual(judgments, {})

        self.raw_task("start", "proj", "haiku-3", "--builder", "codex")
        wt = self.repo / ".worktrees/haiku-3"
        current_head = self.git(wt, "rev-parse", "HEAD")
        record = self.home / "haiku-3-reply.md"
        record.write_text(text.replace(commit, current_head))
        self.assertIn("Recorded FAIL", self.raw_task("review", "proj", "haiku-3", "--record", str(record)))
        metadata = __import__("json").loads((self.state("haiku-3") / "review.json").read_text())
        self.assertEqual(metadata["verdict"], "FAIL")
        self.assertEqual(metadata["commit"], current_head)
        self.assertEqual((metadata["reviewer"], metadata["model"]), reviewer)
        self.assertEqual(metadata["judgments"], {})

    def test_strict_review_fixture_preserves_free_text_evidence_and_reviewer(self):
        fixture = REVIEW_FIXTURES / "strict-free-evidence.md"
        verdict, commit, reviewer, judgments = TASK_MODULE.parse_review_reply(fixture.read_text(), [1])
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(commit, "b85ccb9d8c26080884670ca87c2fd6cd9ae5b998")
        self.assertEqual(reviewer, ("claude", "haiku-5-5_model|tag"))
        self.assertEqual(judgments, {1: ("pass", "executable_task:533 `git show` **bold** _under_ | ~tilde")})

    def test_review_record_prefers_strict_fenced_block_and_requires_plain_content(self):
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "strict-review.md"
        review.write_text(f"""Verdict: FAIL, this line is outside the block
Commit: {'0' * 40}
Reviewer: claude ignored-fallback
```review

Verdict: PASS

Commit: {head}
Reviewer: claude strict-model
#1 pass: first criterion evidence

#2 unclear: second criterion evidence
```
""".replace("Verdict: PASS\n\nCommit:", "  Verdict: PASS  \n\nCommit:", 1))
        self.raw_task("review", "proj", "5", "--record", str(review))
        metadata = __import__("json").loads((self.state() / "review.json").read_text())
        self.assertEqual(metadata["verdict"], "PASS")
        self.assertEqual(metadata["judgments"], {"1": "pass", "2": "unclear"})

        review.write_text(f"""Verdict: PASS
Commit: {head}
Reviewer: claude fallback-must-not-run
```review
**Verdict:** PASS
Commit: {head}
Reviewer: claude strict-model
#1 pass: evidence
#2 pass: evidence
```
""")
        out = self.raw_task("review", "proj", "5", "--record", str(review), code=1)
        self.assertIn("review block", out)

    def test_review_record_rejects_duplicate_judgments_with_criterion_number(self):
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "duplicate-review.md"
        review.write_text(f"Verdict: FAIL\nCommit: {head}\nReviewer: claude duplicate-test\n"
                          "#1 pass: first evidence\n#1 fail: conflicting evidence\n#2 pass: evidence\n")
        out = self.raw_task("review", "proj", "5", "--record", str(review), code=1)
        self.assertIn("duplicate judgment for criterion #1", out)

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
        run_log = (_wt / ".work/run.log").read_text()
        self.assertEqual(run_log.count("For long gates"), 1)

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

    def test_round5_status_reports_failed_cancelled_and_interrupted_merges(self):
        wt, _ = self.started()
        run_path = self.state() / "run.json"
        log_path = wt / ".work/run.log"
        log_path.write_text("gate output\n")
        run = {"command": "merge", "state": "failed", "completed": []}
        run_path.write_text(__import__("json").dumps(run))
        out = self.raw_task("status", "proj")
        self.assertIn("merge failed", out)
        self.assertIn(f"log: {log_path}", out)
        self.assertIn(f"t5: merge failed (log: {log_path})", out)
        self.assertNotIn("check=", out)
        for run_state, expected in (("cancelled", "merge cancelled"),
                                    ("interrupted", "merge interrupted")):
            run["state"] = run_state
            run_path.write_text(__import__("json").dumps(run))
            out = self.raw_task("status", "proj")
            self.assertIn(expected, out)
            self.assertIn(f"log: {log_path}", out)
            self.assertNotIn("check=", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
