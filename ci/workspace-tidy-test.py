#!/usr/bin/env python3
"""workspace-tidy removes only finished, clean, unlocked, old worktrees and merged branches.

Real git fixtures in a temporary HOME: new, dirty, unmerged, locked and in-use worktrees must
survive; finished Codex app worktrees may be removed, and strays and undeclared project-data are
reported. Report mode changes nothing.
"""
import os
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "home/dot_local/bin/executable_workspace-tidy"
TIDY_LOADER = importlib.machinery.SourceFileLoader("workspace_tidy_test", str(TOOL))
TIDY_SPEC = importlib.util.spec_from_loader(TIDY_LOADER.name, TIDY_LOADER)
TIDY_MODULE = importlib.util.module_from_spec(TIDY_SPEC)
sys.dont_write_bytecode = True
TIDY_SPEC.loader.exec_module(TIDY_MODULE)
OLD = time.time() - 10 * 86400
OLD_DATE = f"@{int(OLD)} +0000"


def run(cwd, *args, old=False):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    if old:
        env.update(GIT_AUTHOR_DATE=OLD_DATE, GIT_COMMITTER_DATE=OLD_DATE)
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


def age(path: Path):
    """Make a worktree look created long ago (.git file and HEAD log)."""
    os.utime(path / ".git", (OLD, OLD))
    gitdir = Path(run(path, "rev-parse", "--absolute-git-dir").stdout.strip())
    if (gitdir / "logs/HEAD").exists():
        os.utime(gitdir / "logs/HEAD", (OLD, OLD))


class Tidy(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ws = self.home / "dev_workspace"
        self.repo = ws / "proj"
        self.repo.mkdir(parents=True)
        run(self.repo, "init", "-q", "-b", "main", old=True)
        (self.repo / "a.txt").write_text("a\n")
        run(self.repo, "add", "a.txt", old=True)
        run(self.repo, "commit", "-qm", "base", old=True)
        wt = self.repo / ".worktrees"
        # finished: merged, clean, old -> removed
        run(self.repo, "worktree", "add", "-q", "-b", "done", str(wt / "done"), old=True)
        # recent: merged, clean, created now -> kept
        run(self.repo, "worktree", "add", "-q", "-b", "fresh", str(wt / "fresh"))
        # unmerged: has its own commit -> kept
        run(self.repo, "worktree", "add", "-q", "-b", "open", str(wt / "open"), old=True)
        (wt / "open/b.txt").write_text("b\n")
        run(wt / "open", "add", "b.txt", old=True)
        run(wt / "open", "commit", "-qm", "work", old=True)
        # dirty: merged but with uncommitted changes -> kept
        run(self.repo, "worktree", "add", "-q", "-b", "dirty", str(wt / "dirty"), old=True)
        (wt / "dirty/a.txt").write_text("changed\n")
        # locked (a running Claude Code session) -> kept
        run(self.repo, "worktree", "add", "-q", "-b", "locked", str(wt / "locked"), old=True)
        run(self.repo, "worktree", "lock", str(wt / "locked"))
        # sibling outside the repo, unmerged -> kept and reported
        run(self.repo, "worktree", "add", "-q", "-b", "sibling", str(ws / "proj-wt"), old=True)
        (ws / "proj-wt/c.txt").write_text("c\n")
        run(ws / "proj-wt", "add", "c.txt", old=True)
        run(ws / "proj-wt", "commit", "-qm", "sib", old=True)
        # Detached Codex worktree at a default-branch commit -> eligible for removal.
        self.codex = self.home / ".codex/worktrees/ab12"
        run(self.repo, "worktree", "add", "-q", "--detach", str(self.codex), old=True)
        # A recent clean Codex worktree is finished but still inside the one-day grace period.
        self.codex_fresh = self.home / ".codex/worktrees/ij90/proj"
        run(self.repo, "worktree", "add", "-q", "--detach", str(self.codex_fresh))
        # A clean merged Codex branch is eligible once its one-day grace period has elapsed.
        self.codex_branch = self.home / ".codex/worktrees/cd34/proj"
        run(self.repo, "worktree", "add", "-q", "-b", "codex-done", str(self.codex_branch))
        # Uncommitted Codex work is retained and explicitly reported.
        self.codex_dirty = self.home / ".codex/worktrees/ef56/proj"
        run(self.repo, "worktree", "add", "-q", "-b", "codex-dirty", str(self.codex_dirty))
        (self.codex_dirty / "a.txt").write_text("unsaved Codex work\n")
        self.codex_open = self.home / ".codex/worktrees/gh78/proj"
        run(self.repo, "worktree", "add", "-q", "-b", "codex-open", str(self.codex_open), old=True)
        (self.codex_open / "open.txt").write_text("committed but unmerged\n")
        run(self.codex_open, "add", "open.txt", old=True)
        run(self.codex_open, "commit", "-qm", "unmerged Codex work", old=True)
        for name in ("done", "open", "dirty", "locked"):
            age(wt / name)
        age(ws / "proj-wt")
        age(self.codex)
        age(self.codex_branch)
        age(self.codex_dirty)
        # branches: merged+old -> deleted; merged+recent -> kept; unmerged -> kept
        run(self.repo, "branch", "merged-old", old=True)
        run(self.repo, "branch", "merged-new")
        run(self.repo, "branch", "unmerged", "open", old=True)
        # strays and project-data
        (ws / "scratch").mkdir()
        run(ws, "clone", "-q", str(self.repo), "proj-copy")
        (self.home / "project-data/proj/live").mkdir(parents=True)
        (self.home / "project-data/proj/junk").mkdir()
        (self.home / "project-data/old-archive").mkdir()
        (self.repo / ".project-data").write_text("proj/live  # live state that must survive worktrees\n")


    def tidy(self, *args):
        env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        env.pop("CODEX_HOME", None)
        result = subprocess.run(["python3", str(TOOL), *args], env=env, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def branches(self):
        return set(run(self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout.split())

    def test_report_mode_changes_nothing(self):
        before = (self.branches(), sorted(p.name for p in (self.repo / ".worktrees").iterdir()))
        out = self.tidy()
        self.assertIn("finished worktree", out)
        self.assertIn(f"Codex worktree {self.codex} (detached) is finished and clean; would be removed", out)
        self.assertIn(f"Codex worktree {self.codex_branch} (codex-done) is finished and clean; would be removed", out)
        self.assertIn(f"Codex worktree {self.codex_fresh} (detached) kept: within one-day grace period", out)
        self.assertIn(f"Codex worktree {self.codex_dirty} (codex-dirty) kept: uncommitted or untracked changes", out)
        self.assertIn(f"Codex worktree {self.codex_open} (codex-open) kept: branch is not merged", out)
        self.assertIn("merged branch merged-old would be deleted", out)
        self.assertEqual(before, (self.branches(), sorted(p.name for p in (self.repo / ".worktrees").iterdir())))
        self.assertTrue((self.home / ".local/state/workspace-tidy/report.txt").is_file())

    def test_apply_removes_only_what_is_finished(self):
        out = self.tidy("--apply")
        wt = self.repo / ".worktrees"
        self.assertFalse((wt / "done").exists(), out)
        for kept in ("fresh", "open", "dirty", "locked"):
            self.assertTrue((wt / kept).exists(), f"{kept} must survive:\n{out}")
        self.assertTrue((self.repo.parent / "proj-wt").exists())
        self.assertFalse(self.codex.exists(), out)
        self.assertFalse(self.codex_branch.exists(), out)
        self.assertTrue(self.codex_fresh.exists(), out)
        self.assertTrue(self.codex_dirty.exists(), out)
        self.assertTrue(self.codex_open.exists(), out)
        self.assertIn("removed finished Codex worktree", out)
        self.assertEqual((self.codex_dirty / "a.txt").read_text(), "unsaved Codex work\n")
        branches = self.branches()
        self.assertNotIn("merged-old", branches)
        self.assertNotIn("done", branches, "the finished worktree's merged branch goes too")
        for kept in ("main", "merged-new", "unmerged", "fresh", "open", "dirty", "locked", "sibling",
                     "codex-done", "codex-dirty", "codex-open"):
            self.assertIn(kept, branches, f"{kept} must survive:\n{out}")
        self.assertEqual((wt / "dirty/a.txt").read_text(), "changed\n")

    def test_strays_and_project_data_are_reported_never_removed(self):
        out = self.tidy("--apply")
        self.assertIn("worktree outside the repository", out)
        self.assertIn("proj-wt", out)
        self.assertIn("dev_workspace/scratch: not a project repository", out)
        self.assertIn("dev_workspace/proj-copy: second checkout of", out)
        self.assertIn("project-data/proj: undeclared junk", out)
        self.assertIn("project-data/old-archive: not declared by any project", out)
        self.assertNotIn("live", out.split("undeclared", 1)[1].splitlines()[0])
        for path in ("dev_workspace/scratch", "dev_workspace/proj-copy", "project-data/proj/junk",
                     "project-data/old-archive"):
            self.assertTrue((self.home / path).exists(), path)

    def test_project_data_reports_only_new_folders(self):
        self.tidy("--apply")
        self.assertNotIn("project-data/", self.tidy("--apply"))
        (self.home / "project-data/new-folder").mkdir()
        out = self.tidy("--apply")
        self.assertIn("project-data/new-folder: not declared", out)
        self.assertNotIn("project-data/old-archive", out)



class Safety(unittest.TestCase):
    """Each case the 2026-10-09 review reproduced as data loss must now be kept and reported."""

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ws = self.home / "dev_workspace"
        self.repo = self.ws / "proj"
        self.repo.mkdir(parents=True)
        run(self.repo, "init", "-q", "-b", "main", old=True)
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / ".gitignore").write_text(".work/\nnode_modules/\n")
        run(self.repo, "add", "a.txt", ".gitignore", old=True)
        run(self.repo, "commit", "-qm", "base", old=True)

    def worktree(self, name, *extra):
        path = self.repo / ".worktrees" / name
        run(self.repo, "worktree", "add", "-q", *extra, str(path), old=True)
        return path

    def tidy(self, apply=True):
        env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        env.pop("CODEX_HOME", None)
        result = subprocess.run(["python3", str(TOOL), *(["--apply"] if apply else [])], env=env, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_work_evidence_is_archived_caches_are_not(self):
        state = self.worktree("state", "--detach")
        (state / ".work").mkdir()
        (state / ".work/report.md").write_text("evidence")
        cache = self.worktree("cache", "--detach")
        (cache / "node_modules").mkdir()
        (cache / "node_modules/x.js").write_text("x")
        age(state); age(cache)
        out = self.tidy()
        self.assertFalse(state.exists(), out)
        archived = list((self.home / '.local/state/task/archive/proj/state').glob('*/.work/report.md'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), 'evidence')
        self.assertFalse(cache.exists(), out)

    def test_hidden_or_flagged_changes_are_kept(self):
        hidden = self.worktree("hidden", "--detach")
        run(self.repo, "config", "status.showUntrackedFiles", "no")
        (hidden / "notes.md").write_text("mine")
        flagged = self.worktree("flagged", "--detach")
        (flagged / "a.txt").write_text("edited")
        run(flagged, "update-index", "--assume-unchanged", "a.txt")
        merging = self.worktree("merging", "--detach")
        gitdir = Path(run(merging, "rev-parse", "--absolute-git-dir").stdout.strip())
        (gitdir / "MERGE_HEAD").write_text(run(self.repo, "rev-parse", "HEAD").stdout)
        for path in (hidden, flagged, merging):
            age(path)
        out = self.tidy()
        self.assertEqual((hidden / "notes.md").read_text(), "mine", out)
        self.assertEqual((flagged / "a.txt").read_text(), "edited", out)
        self.assertTrue(merging.exists(), out)

    def test_merged_session_files_archived_databases_and_work_caches_removed(self):
        run(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD", old=True)
        paths = []
        for name in ("session", "archived", "caches", "dirty-archived", "open-archived"):
            path = self.worktree(name, "-b", name)
            paths.append(path)
            if name == "session":
                (path / ".claude").mkdir()
                (path / ".claude/settings.local.json").write_text("{}")
            elif name == "caches":
                (path / ".work/.cache/tools").mkdir(parents=True)
                (path / ".work/.cache/tools/download").write_text("cache")
            else:
                (path / ".work").mkdir()
                (path / ".work/live.sqlite").write_bytes(b"x" * 2_000_000)
                archive = self.home / ".local/state/task/archive/proj" / name / "snapshot/.work"
                archive.mkdir(parents=True)
                (archive / "report.md").write_text("archived")
            if name == "dirty-archived":
                (path / "a.txt").write_text("dirty")
            if name == "open-archived":
                (path / "a.txt").write_text("unmerged")
                run(path, "commit", "-qam", "unmerged", old=True)
            age(path)
        out = self.tidy()
        for path in paths[:3]:
            self.assertFalse(path.exists(), out)
        for path in paths[3:]:
            self.assertTrue(path.exists(), out)
        self.assertTrue((self.home / ".local/state/task/archive/proj/archived/snapshot/.work/report.md").exists())

    def test_backlog_archive_uses_task_folder_instead_of_branch(self):
        task = self.worktree("t28", "-b", "t28-clean-up-workspaces")
        (task / ".work").mkdir()
        (task / ".work/state.sqlite").write_text("task state")
        age(task)
        (task / '.work/report.md').write_text('current evidence')
        out = self.tidy(apply=False)
        self.assertTrue(task.exists(), out)
        self.assertIn('would archive .work', out)
        archive = self.home / '.local/state/task/archive/proj/t28'
        self.assertFalse(archive.exists())
        out = self.tidy()
        self.assertFalse(task.exists(), out)
        reports = list(archive.glob('*/.work/report.md'))
        self.assertEqual(len(reports), 1, out)
        self.assertEqual(reports[0].read_text(), 'current evidence')

    def test_archive_failure_keeps_worktree_and_evidence(self):
        path = self.worktree('archive-failure', '-b', 'archive-failure')
        (path / '.work').mkdir()
        (path / '.work/report.md').write_text('must survive')
        age(path)
        archive = self.home / '.local/state/task/archive'
        archive.mkdir(parents=True)
        (archive / 'proj').write_text('obstruct archive directory')
        out = self.tidy()
        self.assertIn('cannot archive .work', out)
        self.assertEqual((path / '.work/report.md').read_text(), 'must survive')

    def test_moved_worktree_is_not_pruned(self):
        moved = self.worktree("moved", "--detach")
        (moved / "z.txt").write_text("z")
        run(moved, "add", "z.txt", old=True)
        run(moved, "commit", "-qm", "unmerged", old=True)
        target = self.home / "elsewhere"
        moved.rename(target)
        out = self.tidy()
        self.assertIn("worktree folder missing", out)
        self.assertIn("prunable", run(self.repo, "worktree", "list", "--porcelain").stdout)
        self.assertEqual(run(target, "status", "--porcelain").returncode, 0)

    def test_commits_only_in_a_reflog_are_kept(self):
        detached = self.worktree("detached", "--detach")
        (detached / "y.txt").write_text("y")
        run(detached, "add", "y.txt", old=True)
        run(detached, "commit", "-qm", "only here", old=True)
        run(detached, "checkout", "-q", "--detach", "main", old=True)
        age(detached)
        tree = run(self.repo, "rev-parse", "HEAD^{tree}").stdout.strip()
        orphan = run(self.repo, "commit-tree", tree, "-p", "HEAD", "-m", "x", old=True).stdout.strip()
        run(self.repo, "update-ref", "refs/heads/rf", orphan, old=True)
        run(self.repo, "update-ref", "refs/heads/rf", "main", old=True)
        out = self.tidy()
        self.assertTrue(detached.exists(), out)
        self.assertIn("rf", run(self.repo, "branch", "--format=%(refname:short)").stdout.split(), out)

    def test_finished_worktree_outside_the_repository_is_kept(self):
        sibling = self.ws / "proj-sib"
        run(self.repo, "worktree", "add", "-q", "--detach", str(sibling), old=True)
        age(sibling)
        out = self.tidy()
        self.assertTrue(sibling.exists(), out)
        self.assertIn("worktree outside the repository", out)

    def test_worktree_in_use_is_kept(self):
        busy = self.worktree("busy", "--detach")
        age(busy)
        sleeper = subprocess.Popen(["sleep", "60"], cwd=busy)
        try:
            out = self.tidy()
        finally:
            sleeper.kill()
            sleeper.wait()
        self.assertTrue(busy.exists(), out)
        self.assertIn("in use by a running process", out)

    def test_codex_worktree_with_missing_head_is_kept(self):
        codex = self.home / ".codex/worktrees/missing-head"
        run(self.repo, "worktree", "add", "-q", "--detach", str(codex), old=True)
        entry = {"worktree": str(codex), "detached": True}
        report, done = [], []
        TIDY_MODULE.tidy_codex_worktree(self.repo, entry, codex, TIDY_MODULE.default_refs(self.repo),
                                        True, time.time(), [], report, done)
        self.assertTrue(codex.exists())
        self.assertEqual(done, [])
        self.assertIn("kept: HEAD is missing", report[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
