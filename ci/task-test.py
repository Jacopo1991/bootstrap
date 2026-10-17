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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("TASK_TEST_TOOL", ROOT / "home/dot_local/bin/executable_task"))
REVIEW_FIXTURES = ROOT / "ci/fixtures/reviews"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
TASK_LOADER = importlib.machinery.SourceFileLoader("task_tool_for_review_test", str(TOOL))
TASK_SPEC = importlib.util.spec_from_loader(TASK_LOADER.name, TASK_LOADER)
TASK_MODULE = importlib.util.module_from_spec(TASK_SPEC)
sys.dont_write_bytecode = True
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
    "pre-commit": """#!/bin/sh
env > "$HOME/pre-commit-env"
printf '%s\\n' "$@" > "$HOME/pre-commit-args"
if [ "${SIMULATE_READ_ONLY_GIT_DIR:-}" = 1 ]; then
  case "${GIT_INDEX_FILE:-}" in
    "$PWD"/.work/pre-commit-index-*/index) ;;
    *) echo "fatal: Unable to create git index.lock: Read-only file system" >&2; exit 1 ;;
  esac
  git write-tree >/dev/null || exit $?
  git update-index --assume-unchanged a.txt || exit $?
fi
[ "${PRE_COMMIT_FAIL:-}" = 1 ] && exit 1
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
        assert value in {"Done", "In Progress", "To Do"}, value
        text = re.sub(r"(?m)^status: .*$", "status: " + value, text)
    elif flag == "--check-ac":
        text = text.replace(f"- [ ] #{value} ", f"- [x] #{value} ")
    elif flag == "--final-summary":
        text += "\\n## Final Summary\\n" + value + "\\n"
    i += 2
path.write_text(text)
""",
}

MODEL_STUB = """#!/usr/bin/env python3
import json, os, pathlib, sys, time
work = pathlib.Path.cwd() / '.work'
prompt = sys.stdin.read()
(work / 'model-call.json').write_text(json.dumps({
    'args': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': prompt,
    'env_names': sorted(os.environ),
    'state_home': os.environ.get('XDG_STATE_HOME'),
    'reason': os.environ.get('SECRETSPEC_REASON'),
}))
release = os.environ.get('LANE_STUB_RELEASE')
if release:
    import subprocess
    descendant = subprocess.Popen(['sleep', '30'])
    (work / 'descendant.pid').write_text(str(descendant.pid))
deadline = time.monotonic() + 10
while release and not pathlib.Path(release).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if release:
    descendant.terminate()
    descendant.wait()
if pathlib.Path(sys.argv[0]).name == 'codex':
    final = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
    final.write_text('codex-final: ' + prompt)
    print('codex-log')
else:
    print('claude-final: ' + prompt, end='')
    print('claude-log', file=sys.stderr)
sys.exit(int(os.environ.get('LANE_STUB_EXIT', '0')))
"""

REVIEW_STUB = """#!/usr/bin/env python3
import json, os, pathlib, sys, time
work = pathlib.Path.cwd() / '.work'
(work / 'reviewer-call.json').write_text(json.dumps({
    'args': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': sys.stdin.read(),
    'git_optional_locks': os.environ.get('GIT_OPTIONAL_LOCKS'),
    'env_names': sorted(os.environ),
}))
if os.environ.get('REVIEW_STUB_POLICY'):
    import importlib.util, subprocess
    spec = importlib.util.spec_from_file_location('review_policy', os.environ['REVIEW_STUB_POLICY'])
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    command = ['git', '--no-optional-locks', 'diff', 'origin/main..HEAD']
    denial = policy.evaluate({'tool_name': 'Bash', 'cwd': os.getcwd(),
                              'tool_input': {'command': ' '.join(command)}},
                             approved_roots=(pathlib.Path.cwd(),))
    if denial:
        print(denial, file=sys.stderr)
        sys.exit(1)
    diff = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    (work / 'reviewer-diff.txt').write_text(diff)
counter = work / 'reviewer-count'
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
reply = os.environ.get('REVIEW_STUB_REPLY', '')
if count == 1:
    reply = os.environ.get('REVIEW_STUB_FIRST_REPLY', reply)
print(reply, end='', flush=True)
time.sleep(float(os.environ.get('REVIEW_STUB_DELAY', '0')))
sys.exit(int(os.environ.get('REVIEW_STUB_EXIT', '0')))
"""


class TaskTool(unittest.TestCase):
    def test_reliability_override_records_timed_out_gate_step(self):
        import json
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        (self.state() / 'check.json').unlink()
        script = self.home / 'during-check.sh'
        script.write_text('#!/bin/sh\nsleep 3\n')
        script.chmod(0o755)
        out = self.raw_task('merge', 'proj', '5', '--override', 'approved timeout',
                            env=dict(self.env, TASK_CHECK_TIMEOUT='1'))
        check = json.loads((self.state() / 'gate-check.json').read_text())
        self.assertIsNone(check['steps'][0]['exit'])
        self.assertTrue(check['steps'][0]['timeout'])
        exception = json.loads((self.state() / 'exception.json').read_text())
        self.assertEqual(exception['waived'], [
            {'step': 'check:just check', 'reason': 'approved timeout'}])
        self.assertIn('check:just check waived: approved timeout', out)

    def test_reliability_cancel_lane_already_stopped(self):
        import json
        from types import SimpleNamespace
        wt, _ = self.started()
        lane = wt / '.work/lane.json'
        for error in (ProcessLookupError, PermissionError):
            with self.subTest(error=error):
                lane.write_text(json.dumps({'state': 'running', 'pid': 99999999}))
                output = io.StringIO()
                with mock.patch.object(TASK_MODULE, 'resolve_repo', return_value=self.repo), \
                        mock.patch.object(TASK_MODULE, 'prune_stale_gate_worktrees'), \
                        mock.patch.object(TASK_MODULE, 'Task', return_value=SimpleNamespace(path=wt)), \
                        mock.patch.object(TASK_MODULE, 'pid_alive', return_value=True), \
                        mock.patch.object(TASK_MODULE.os, 'killpg', side_effect=error), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(TASK_MODULE.cancel(SimpleNamespace(repo='proj', id='5')), 0)
                message = 'could not stop (permission denied)' if error is PermissionError else 'already stopped'
                self.assertIn(message, output.getvalue())
                if error is PermissionError:
                    self.assertNotIn('already stopped', output.getvalue())
                record = json.loads(lane.read_text())
                self.assertEqual(record['state'], 'interrupted')
                self.assertTrue(record['ended_at'])

    def test_reliability_repair_dead_lane_status_read_only(self):
        import json
        wt, _ = self.started()
        lane = wt / '.work/lane.json'
        for alive in (True, False):
            with self.subTest(alive=alive):
                lane.write_text(json.dumps({'state': 'running',
                                            'pid': os.getpid() if alive else 99999999}))
                before = lane.read_bytes()
                self.raw_task('status', 'proj')
                self.assertEqual(lane.read_bytes(), before)
                self.raw_task('repair', 'proj')
                if alive:
                    self.assertEqual(lane.read_bytes(), before)
                else:
                    record = json.loads(lane.read_text())
                    self.assertEqual(record['state'], 'interrupted')
                    self.assertTrue(record['ended_at'])

    def test_reliability_override_names_every_missing_item(self):
        import json
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        (self.state() / 'review.json').unlink()
        (wt / '.work/report.md').unlink()
        out = self.raw_task('merge', 'proj', '5', '--override', 'approved emergency')
        waived = json.loads((self.state() / 'exception.json').read_text())['waived']
        self.assertEqual({item['step'] for item in waived}, {
            'recorded PASS review for lane head with every criterion pass by the other vendor',
            'filled .work/report.md'})
        for item in waived:
            self.assertIn(item['step'] + ' waived: approved emergency', out)

    def test_reliability_merged_partial_pr_then_final_close(self):
        import json
        origin = self.home / 'knowledge-origin.git'
        self.git(self.home, 'clone', '-q', '--bare', str(self.kb), str(origin))
        self.git(self.kb, 'remote', 'add', 'origin', str(origin))
        self.git(self.kb, 'fetch', '-q', 'origin')
        self.git(self.kb, 'remote', 'set-head', 'origin', 'main')
        stub = self.home / 'bin/gh'
        stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, tempfile
root = pathlib.Path(os.environ['HOME'])
store = root / 'close-prs.json'
prs = json.loads(store.read_text()) if store.exists() else []
args = sys.argv[1:]
def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()
if args[:2] == ['pr', 'list']:
    branch = args[args.index('--head') + 1]
    print(json.dumps([pr for pr in prs if pr['headRefName'] == branch]))
elif args[:2] == ['pr', 'create']:
    prs.append(dict(number=len(prs)+1, state='OPEN', headRefName=args[args.index('--head')+1], baseRefName='main'))
elif args[:2] == ['pr', 'merge']:
    pr = prs[int(args[2])-1]
    with tempfile.TemporaryDirectory() as folder:
        subprocess.check_call(['git', 'clone', '-q', str(root / 'knowledge-origin.git'), folder])
        git(folder, 'merge', '-q', '--no-ff', '-m', 'Merge close PR', 'origin/' + pr['headRefName'])
        git(folder, 'push', '-q', 'origin', 'HEAD:main')
        pr.update(state='MERGED', mergeCommit={'oid': git(folder, 'rev-parse', 'HEAD')})
elif args[:2] == ['pr', 'view']:
    print(json.dumps(prs[int(args[2])-1]))
else:
    sys.exit(1)
store.write_text(json.dumps(prs))
''')
        stub.chmod(0o755)
        wt, _ = self.started()
        self.prepare_merge_evidence('5')
        retro = 'went well: verified / went wrong: partial / change: finish'
        self.raw_task('close', 'proj', '5', '-m', 'partial', '--partial', '--retro', retro)
        task_file = next((self.kb / 'backlog/tasks').glob('t-5 *'))
        self.assertIn('status: In Progress', task_file.read_text())
        self.raw_task('close', 'proj', '5', '-m', 'final', '--retro', retro, '--no-merge', 'abandoned')
        self.assertIn('status: Done\n', task_file.read_text())
        self.assertIn('not merged: abandoned', task_file.read_text())
        prs = json.loads((self.home / 'close-prs.json').read_text())
        self.assertEqual([p['state'] for p in prs], ['MERGED', 'MERGED'])
        self.assertEqual(prs[1]['headRefName'], 'task-close-t5-final')

    def test_reliability_help_commands_and_flags(self):
        out = self.raw_task('--help')
        for name in ('wait', 'repair', 'cancel', 'lane', 'close'):
            self.assertIn(name, out)
        for command, flags in {'lane': ['--timeout'], 'wait': ['--max'],
                               'check': ['--lock-wait', '--detach'],
                               'merge': ['--lock-wait', '--detach', '--override'],
                               'close': ['--no-merge', '--partial']}.items():
            out = self.raw_task(command, '--help')
            for flag in flags:
                self.assertIn(flag, out)

    def test_reliability_repair_state_only_records(self):
        import json
        self.started()
        for name, locked, alive in [('orphan-run', False, False), ('orphan-lock', True, False),
                                    ('live', True, True)]:
            state = self.state(name)
            state.mkdir()
            pid = os.getpid() if alive else 99999999
            (state / 'run.json').write_text(json.dumps({'state': 'running', 'pid': pid}))
            if locked:
                (state / 'run.lock').write_text(json.dumps({'pid': pid}))
        before = {p: p.read_bytes() for p in self.state().parent.rglob('*') if p.is_file()}
        self.raw_task('status', 'proj')
        self.assertEqual(before, {p: p.read_bytes() for p in self.state().parent.rglob('*') if p.is_file()})
        self.raw_task('repair', 'proj')
        for name in ('orphan-run', 'orphan-lock'):
            self.assertEqual(json.loads((self.state(name) / 'run.json').read_text())['state'], 'interrupted')
            self.assertFalse((self.state(name) / 'run.lock').exists())
        self.assertTrue((self.state('live') / 'run.lock').exists())

    def test_reliability_wait_observes_transition_while_blocked(self):
        import json
        self.started()
        file = self.state() / 'run.json'
        file.write_text(json.dumps({'state': 'running', 'pid': os.getpid()}))
        waiter = subprocess.Popen([sys.executable, str(TOOL), 'wait', 'proj', '5', '--max', '0.03'],
                                  cwd=self.home, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        import time
        time.sleep(0.3)
        self.assertIsNone(waiter.poll(), 'wait must still be blocked before the transition')
        temporary = file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'state': 'passed'}))
        temporary.replace(file)
        out, err = waiter.communicate(timeout=5)
        self.assertEqual(waiter.returncode, 0, err)
        self.assertIn(b'passed', out)

    def test_reliability_detached_lock_timeout_cleanup(self):
        import json
        import time
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        (self.repo / '.task.toml').write_text('exclusive_gate = true\n')
        (wt / '.task.toml').write_text('exclusive_gate = true\n')
        self.git(wt, 'add', '.task.toml')
        self.git(wt, 'commit', '-qm', 'exclusive gate')
        self.prepare_merge_evidence('5')
        self.git(self.repo, 'add', '.task.toml')
        self.git(self.repo, 'commit', '-qm', 'exclusive gate')
        lock = self.gate_dir / 'agent-task-gate.lock'
        holder = self.gate_dir / 'agent-task-gate.holder'
        with lock.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            holder.write_text(json.dumps({'repo': 'proj', 'task': 'T-9', 'pid': 99999999, 'since': 'today'}))
            for command in ('check', 'merge'):
                self.raw_task(command, 'proj', '5', '--detach', '--lock-wait', '0')
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    run = json.loads((self.state() / 'run.json').read_text())
                    if run['state'] != 'running' and not (self.state() / 'run.lock').exists():
                        break
                    time.sleep(0.02)
                self.assertEqual(run['state'], 'failed')
                self.assertFalse((self.state() / 'run.lock').exists())
                self.assertIn('gate busy: held by proj/T-9 since today', (wt / '.work/run.log').read_text())
                self.assertTrue(TASK_MODULE.gate_holder_alive(lock))
                self.assertEqual(json.loads(holder.read_text())['pid'], 99999999)

    def test_reliability_review_diff_excludes_integrated_main(self):
        wt, _ = self.started()
        self.commit_in(wt, 'lane.txt')
        self.commit_in(self.repo, 'main-only.txt')
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        base = self.git(wt, 'merge-base', 'origin/main', 'HEAD').strip()
        self.assertNotEqual(base, self.git(wt, 'rev-parse', 'origin/main').strip())
        self.assertIn('Diff range: ' + base + '...' + self.git(wt, 'rev-parse', 'HEAD').strip(),
                      self.raw_task('review', 'proj', '5'))
        self.git(wt, 'merge', '--no-edit', 'main')
        self.commit_in(wt, 'after-integration.txt')
        prompt = self.raw_task('review', 'proj', '5')
        import re
        match = re.search(r'Diff range: (\S+)', prompt)
        self.assertIsNotNone(match, prompt)
        base = self.git(wt, 'merge-base', 'origin/main', 'HEAD').strip()
        self.assertEqual(match[1], base + '...' + self.git(wt, 'rev-parse', 'HEAD').strip())
        names = self.git(wt, 'diff', '--name-only', match[1]).splitlines()
        self.assertEqual(set(names), {'lane.txt', 'after-integration.txt'})

    def assert_group_stopped(self, pgid):
        import time
        def live_members():
            members = []
            for entry in Path('/proc').glob('[0-9]*/stat'):
                try:
                    fields = entry.read_text().split(') ', 1)[1].split()
                    if int(fields[2]) == pgid and fields[0] != 'Z':
                        members.append(entry.parent.name)
                except (OSError, ValueError):
                    pass
            return members
        deadline = time.monotonic() + 5
        while live_members() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(live_members(), [], 'the whole lane process group must stop')

    def test_reliability_lane_timeout_cancel_and_restart(self):
        import json
        import time
        wt, _ = self.started()
        self.stub_models()
        (wt / ".work/prompt.md").write_text("do the task")
        env = dict(self.env, LANE_STUB_RELEASE=str(self.home / "release"))
        self.raw_task("lane", "proj", "5", "--timeout", "0.002", env=env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = json.loads((wt / ".work/lane.json").read_text())
            if run["state"] == "timed_out":
                break
            time.sleep(0.02)
        self.assertEqual(run["state"], "timed_out")
        self.assertIsNone(run['exit_code'])
        self.assertIn('lane timed_out', self.raw_task('status', 'proj').split('Needs attention:')[1])
        self.assertTrue((wt / '.work/descendant.pid').exists())
        self.assert_group_stopped(run['pid'])
        self.assertTrue(wt.exists())
        (wt / '.work/descendant.pid').unlink()
        self.raw_task("lane", "proj", "5", env=env)
        self.wait_for_lane(wt, finished=False)
        deadline = time.monotonic() + 5
        while not (wt / '.work/descendant.pid').exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((wt / '.work/descendant.pid').exists())
        cancelled = json.loads((wt / '.work/lane.json').read_text())
        out = self.raw_task("cancel", "proj", "5")
        self.assertIn("process group", out)
        self.assertEqual(json.loads((wt / ".work/lane.json").read_text())["state"], "cancelled")
        self.assert_group_stopped(cancelled['pid'])
        self.raw_task("lane", "proj", "5")
        self.wait_for_lane(wt)

    def test_reliability_wait_lane_and_detached_state(self):
        import json
        wt, _ = self.started()
        file = wt / ".work/lane.json"
        file.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        self.assertIn("still running", self.raw_task("wait", "proj", "5", "--max", "0", code=3))
        file.write_text(json.dumps({"state": "timed_out"}))
        self.assertIn("timed_out", self.raw_task("wait", "proj", "5"))
        run = self.state() / "run.json"
        run.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        self.raw_task("wait", "proj", "5", "--max", "0", code=3)
        run.write_text(json.dumps({"state": "passed"}))
        self.assertIn("passed", self.raw_task("wait", "proj", "5"))

    def test_reliability_status_does_not_write_and_repair_does(self):
        import json
        self.started()
        run = self.state() / "run.json"
        lock = self.state() / "run.lock"
        run.write_text(json.dumps({"state": "running", "pid": 99999999}))
        lock.write_text(json.dumps({"pid": 99999999}))
        before = {p: p.read_bytes() for p in self.state().iterdir() if p.is_file()}
        self.sandbox_state()
        self.assertIn("repair available", self.raw_task("status", "proj"))
        self.assertEqual(before, {p: p.read_bytes() for p in self.state().iterdir() if p.is_file()})
        (self.home / ".local/state/task").chmod(0o755)
        self.assertIn("Repaired", self.raw_task("repair", "proj"))
        self.assertFalse(lock.exists())
        self.assertEqual(json.loads(run.read_text())["state"], "interrupted")

    def test_reliability_context_replaces_symlinks(self):
        from types import SimpleNamespace
        (self.kb / 'INTENT.md').write_text('intent')
        wt, _ = self.started()
        context = wt / '.work/context'
        outside = self.home / 'outside-context'
        outside.mkdir()
        victim = outside / 'INTENT.md'
        victim.write_text('outside untouched')
        mode = victim.stat().st_mode
        shutil.rmtree(context)
        context.symlink_to(outside, target_is_directory=True)
        task = SimpleNamespace(path=wt, kb=self.kb, file=None)
        TASK_MODULE.refresh_context(task)
        self.assertFalse(context.is_symlink())
        self.assertEqual((context / 'INTENT.md').read_text(), 'intent')
        (context / 'INTENT.md').unlink()
        (context / 'INTENT.md').symlink_to(victim)
        (context / 'stale-link').symlink_to(victim)
        (context / 'nested').symlink_to(outside, target_is_directory=True)
        TASK_MODULE.refresh_context(task)
        self.assertFalse((context / 'INTENT.md').is_symlink())
        self.assertEqual((context / 'INTENT.md').read_text(), 'intent')
        self.assertFalse((context / 'stale-link').exists())
        self.assertFalse((context / 'nested').exists())
        self.assertEqual(victim.read_text(), 'outside untouched')
        self.assertEqual(victim.stat().st_mode, mode)
        self.assertEqual(list(outside.iterdir()), [victim])

    def test_reliability_context_and_review_range(self):
        (self.kb / "INTENT.md").write_text("intent")
        (self.kb / "STATUS.md").write_text("status")
        decisions = self.kb / "backlog/decisions"
        decisions.mkdir(parents=True, exist_ok=True)
        decision = decisions / "decision-1 - scope.md"
        decision.write_text("binding decision")
        other = decisions / 'decision-2 - other.md'
        other.write_text('other decision')
        task_file = next((self.kb / "backlog/tasks").glob("t-5 *"))
        task_file.write_text(task_file.read_text() +
                             '\n[scope](../decisions/decision-1%20-%20scope.md)\n[[decision-2 - other]]\n')
        wt, out = self.started()
        self.assertIn("Context: .work/context/", out)
        context = wt / ".work/context"
        self.assertEqual((context / "INTENT.md").read_text(), "intent")
        self.assertEqual((context / "backlog/decisions" / decision.name).read_text(), "binding decision")
        self.assertEqual((context / 'backlog/decisions' / other.name).read_text(), 'other decision')
        self.assertEqual((context / "INTENT.md").stat().st_mode & 0o222, 0)
        (self.kb / "STATUS.md").write_text("updated")
        self.stub_models()
        (wt / ".work/prompt.md").write_text("task")
        self.raw_task("lane", "proj", "5")
        self.wait_for_lane(wt)
        self.assertEqual((context / "STATUS.md").read_text(), "updated")
        self.assertIn('.work/context/', __import__('json').loads((wt / '.work/model-call.json').read_text())['prompt'])
        decision.unlink()
        (self.kb / 'STATUS.md').unlink()
        out = self.raw_task('lane', 'proj', '5')
        self.wait_for_lane(wt)
        self.assertIn('Context source removed: STATUS.md', out)
        self.assertFalse((context / 'STATUS.md').exists())
        self.assertFalse((context / 'backlog/decisions' / decision.name).exists())
        self.assertIn("...", self.raw_task("review", "proj", "5"))

    def test_reliability_special_files_archive_manifest_and_publication(self):
        import json
        import socket
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        try:
            sock = socket.socket(socket.AF_UNIX)
        except PermissionError:
            self.skipTest('lane sandbox forbids Unix socket creation; PM trusted check must run unsandboxed')
        self.addCleanup(sock.close)
        sock.bind(str(wt / ".work/service.sock"))
        os.mkfifo(wt / ".work/pipe")
        self.task("merge", "proj", "5")
        self.assertTrue((self.state() / "merge.json").exists())
        manifests = list(self.work_archives().glob(f"{wt.name}-*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(set(json.loads(manifests[0].read_text())["skipped_special_files"]),
                         {"service.sock", "pipe"})
        self.assertFalse((manifests[0].parent / ".work/service.sock").exists())
        self.assertFalse((manifests[0].parent / ".work/pipe").exists())

    def test_reliability_fifo_only_archive_manifest_and_publication(self):
        import json
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        work = wt / ".work"
        (work / "nested").mkdir()
        os.mkfifo(work / "pipe")
        os.mkfifo(work / "nested/pipe")
        (work / "nested/evidence.txt").write_text("preserved")
        (work / "pipe-link").symlink_to("pipe")
        self.task("merge", "proj", "5")
        self.assertTrue((self.state() / "merge.json").exists())
        manifests = list(self.work_archives().glob(f"{wt.name}-*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(set(json.loads(manifests[0].read_text())["skipped_special_files"]),
                         {"pipe", "nested/pipe"})
        saved = manifests[0].parent / ".work"
        self.assertFalse((saved / "pipe").exists())
        self.assertFalse((saved / "nested/pipe").exists())
        self.assertEqual((saved / "nested/evidence.txt").read_text(), "preserved")
        self.assertTrue((saved / "pipe-link").is_symlink())
        self.assertEqual(os.readlink(saved / "pipe-link"), "pipe")

    def test_reliability_setup_ids_are_atomic(self):
        import json
        processes = [subprocess.Popen([sys.executable, str(TOOL), "note", "proj", "-", "setup", str(i)],
                                      env=self.env, cwd=self.home, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) for i in range(2)]
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, (out, err))
        events = (self.home / ".local/state/task/setup-requests.jsonl").read_text().splitlines()
        self.assertEqual(sorted(json.loads(line)["id"] for line in events), [1, 2])

    def test_reliability_lock_timeout_uses_kernel_holder(self):
        import json
        self.started()
        lock = self.gate_dir / "agent-task-gate.lock"
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            holder = self.gate_dir / "agent-task-gate.holder"
            holder.write_text(json.dumps({"repo": "proj", "task": "T-9", "pid": 99999999,
                                          "since": "2026-01-01"}))
            with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)), \
                    mock.patch.object(TASK_MODULE, "GATE_LOCK_WAIT", 0):
                self.assertTrue(TASK_MODULE.gate_holder_alive(lock))
                output = io.StringIO()
                stdout = io.StringIO()
                with contextlib.redirect_stderr(output), contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as stop:
                    with TASK_MODULE.exclusive_gate_lock(TASK_MODULE.Task(self.repo, "5")):
                        self.fail("must never enter unlocked")
                self.assertEqual(stop.exception.code, 3)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn("gate busy: held by proj/T-9 since 2026-01-01", output.getvalue())
        self.assertFalse(TASK_MODULE.gate_holder_alive(lock))

    def test_reliability_start_clears_stale_run_locks(self):
        import json
        import time
        self.started()
        for detach in (False, True):
            for malformed in (False, True):
                with self.subTest(detach=detach, malformed=malformed):
                    lock = self.state() / 'run.lock'
                    lock.write_text('unreadable' if malformed else json.dumps({'pid': 99999999}))
                    os.utime(lock, (time.time() - 60, time.time() - 60))
                    self.raw_task('check', 'proj', '5', *(['--detach'] if detach else []))
                    deadline = time.monotonic() + 5
                    while lock.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(lock.exists())
                    self.assertEqual(json.loads((self.state() / 'check.json').read_text())['result'], 'passed')

    def test_reliability_lane_timeout_must_be_positive(self):
        for value in ('0', '-1'):
            out = self.raw_task('lane', 'proj', '5', '--timeout', value, code=2)
            self.assertIn('--timeout must be greater than zero', out)

    def test_reliability_claude_streams_before_exit(self):
        import time
        wt, _ = self.started()
        binary = self.home / 'bin/claude'
        binary.write_text('#!/usr/bin/env python3\nimport pathlib, time\nprint("early-output", flush=True)\nwhile not pathlib.Path(".work/release").exists(): time.sleep(0.02)\nprint("late-output", flush=True)\n')
        binary.chmod(0o755)
        (wt / '.work/prompt.md').write_text('stream')
        self.raw_task('lane', 'proj', '5', '--builder', 'claude', '--timeout', '1')
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                final = wt / '.work/final.md'
                if final.exists() and 'early-output' in final.read_text():
                    break
                time.sleep(0.02)
            self.assertIn('early-output', final.read_text())
            self.assertIn('early-output', (wt / '.work/lane.log').read_text())
            self.assertNotIn('late-output', final.read_text())
            self.assertEqual(__import__('json').loads((wt / '.work/lane.json').read_text())['state'], 'running')
        finally:
            (wt / '.work/release').touch()
        self.wait_for_lane(wt)
        self.assertIn('late-output', final.read_text())

    def test_reliability_claude_timeout_kills_group(self):
        import json
        import time
        wt, _ = self.started()
        self.stub_models()
        (wt / '.work/prompt.md').write_text('timeout')
        self.raw_task('lane', 'proj', '5', '--builder', 'claude', '--timeout', '0.005',
                      env=dict(self.env, LANE_STUB_RELEASE=str(self.home / 'release')))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = json.loads((wt / '.work/lane.json').read_text())
            if run['state'] == 'timed_out':
                break
            time.sleep(0.02)
        self.assertEqual(run['state'], 'timed_out')
        self.assertIsNone(run['exit_code'])
        self.assertTrue((wt / '.work/descendant.pid').exists())
        self.assert_group_stopped(run['pid'])

    def test_reliability_close_without_merge_records_notice(self):
        wt, _ = self.started()
        self.prepare_merge_evidence("5")
        command = [sys.executable, str(TOOL), "close", "proj", "5", "-m", "done", "--retro", "fine"]
        result = subprocess.run(command, cwd=self.home, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('merge not recorded', result.stdout)
        closed = next((self.kb / "backlog/tasks").glob("t-5 *")).read_text()
        self.assertIn('status: Done\n', closed)
        self.assertIn('merge not recorded', closed)

    def test_no_merge_close_accepts_missing_or_failed_review(self):
        import json
        self.started()
        review = self.state() / "review.json"
        for verdict in (None, "fail"):
            with self.subTest(verdict=verdict):
                if verdict is None:
                    review.unlink(missing_ok=True)
                else:
                    review.write_text(json.dumps({"judgments": {"1": verdict}}))
                self.raw_task("close", "proj", "5", "--no-merge", "superseded by new task",
                              "-m", "superseded", "--retro", "routine")
                closed = next((self.kb / "backlog/tasks").glob("t-5 *")).read_text()
                self.assertIn("status: Done\n", closed)
                self.assertIn("not merged: superseded by new task", closed)
                self.assertNotIn("- [x]", closed)

    def test_gate_defaults_and_advisory_compatibility(self):
        path = self.home / "gate-config"
        path.mkdir()
        for config, trusted, advisory in ((None, True, False), ("", True, False),
                ("exclusive_gate = false\n", False, False),
                ("exclusive_gate = true\n", True, True)):
            with self.subTest(config=config):
                file = path / ".task.toml"
                if config is None:
                    file.unlink(missing_ok=True)
                else:
                    file.write_text(config)
                self.assertEqual(TASK_MODULE.exclusive_gate_enabled(path), trusted)
                self.assertEqual(TASK_MODULE.exclusive_gate_enabled(path, advisory=True), advisory)

    def test_gate_os_errors_fall_back_only_without_explicit_opt_in(self):
        config_path = self.home / "gate-config"
        config_path.mkdir()
        task = TASK_MODULE.Task(self.repo, "5")
        for explicit in (False, True):
            (config_path / ".task.toml").write_text("exclusive_gate = true\n" if explicit else "")
            for operation in ("open", "flock"):
                with self.subTest(explicit=explicit, operation=operation):
                    target = TASK_MODULE.os if operation == "open" else TASK_MODULE.fcntl
                    output = io.StringIO()
                    ran = []
                    with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)), \
                            mock.patch.object(target, operation, side_effect=PermissionError("permission denied")), \
                            contextlib.redirect_stdout(output):
                        if explicit:
                            with self.assertRaisesRegex(TASK_MODULE.Stop, "permission denied"):
                                with TASK_MODULE.exclusive_gate_lock(task, config_path=config_path):
                                    ran.append(True)
                        else:
                            with TASK_MODULE.exclusive_gate_lock(task, config_path=config_path):
                                ran.append(True)
                    self.assertEqual(ran, [] if explicit else [True])
                    if not explicit:
                        self.assertEqual(len(output.getvalue().splitlines()), 1)
                        self.assertIn("running trusted gate unlocked", output.getvalue())

    def test_skills_setup_repairs_verify_ui_before_updating_existing_install(self):
        stamp = self.home / ".local/state/bootstrap/skills-installed"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        environment = self.home / "skills-test-env.sh"
        environment.write_text(
            'id() { echo agent; }\n'
            'gh() { echo "gh $*" >> "$HOME/skills-calls"; }\n'
            'gh-skill-update() { echo update >> "$HOME/skills-calls"; }\n')
        result = subprocess.run(["bash", str(ROOT / "home/dot_local/share/bootstrap/executable_skills-setup.sh")],
                                env=dict(self.env, BASH_ENV=str(environment), XDG_STATE_HOME=str(self.home / ".local/state"),
                                         XDG_RUNTIME_DIR=str(self.home / "no-bus")),
                                capture_output=True, text=True)
        self.assertIn("No user systemd bus", result.stderr)
        self.assertEqual((self.home / "skills-calls").read_text().splitlines(), [
            "gh auth status",
            "gh skill install Jacopo1991/cortex-core verify-ui --agent claude-code --scope user --force",
            "gh skill install Jacopo1991/cortex-core verify-ui --agent codex --scope user --force",
            "update"])

    def test_wait_message_uses_kernel_pid_instead_of_namespaced_record(self):
        import json
        lock = self.gate_dir / "agent-task-gate.lock"
        holder = self.gate_dir / "agent-task-gate.holder"
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            holder.write_text(json.dumps({"repo": "proj", "task": "T-9", "pid": 2, "since": "today"}))
            self.assertEqual(TASK_MODULE.gate_holder_pid(lock), os.getpid())
            output = io.StringIO()
            def release(_):
                fcntl.flock(held, fcntl.LOCK_UN)
            with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)), \
                    mock.patch.object(TASK_MODULE.time, "sleep", side_effect=release), \
                    mock.patch.object(TASK_MODULE, "pid_alive", side_effect=AssertionError("record PID used")), \
                    contextlib.redirect_stdout(output):
                with TASK_MODULE.exclusive_gate_lock(TASK_MODULE.Task(self.repo, "5")):
                    pass
            self.assertIn(f"pid {os.getpid()} since today", output.getvalue())
        self.assertFalse(TASK_MODULE.gate_holder_alive(lock))

    def test_reliability_noticed_import_lists_only_and_deduplicates(self):
        self.started()
        self.prepare_merge_evidence("5")
        (self.state() / "report.md").write_text(
            "## Noticed\nprose\n- real improvement\n```sh\n- shell line\necho bad\n```\n"
            "- existing improvement\n1. numbered dot\n2) numbered paren\n3. real improvement\n- real improvement\n## Other\n- unrelated\n")
        (self.kb / "improvements.md").write_text("- old | T-1 | report | existing improvement | open\n")
        self.raw_task("close", "proj", "5", "-m", "done", "--retro", "fine")
        text = (self.kb / "improvements.md").read_text()
        self.assertEqual(text.count("real improvement"), 1)
        self.assertEqual(text.count("existing improvement"), 1)
        self.assertEqual(text.count("numbered dot"), 1)
        self.assertEqual(text.count("numbered paren"), 1)
        self.assertNotIn("shell line", text)
        self.assertNotIn("unrelated", text)

    def test_flatten_review_links_handles_windows_drive_and_unc_targets(self):
        self.assertEqual(TASK_MODULE.flatten_review_links(
            r"[drive](C:\Users\agent\report.md) [slash](d:/reports/final.md) "
            r"[unc](\\server\share\review.md)"),
            "drive slash unc")

    def test_flatten_review_links_keeps_protocol_relative_web_target(self):
        text = "[web](//example.com/review.md)"
        self.assertEqual(TASK_MODULE.flatten_review_links(text), text)

    def test_flatten_review_links_flattens_local_reference_and_autolinks(self):
        self.assertEqual(TASK_MODULE.flatten_review_links(
            "[1]: /home/agent/report.md\n<file:///tmp/review.md>"),
            "[1]: report.md\nreview.md")

    def test_flatten_review_links_flattens_local_images_to_alt_text(self):
        self.assertEqual(TASK_MODULE.flatten_review_links(
            "![alt](/home/agent/x.png)"), "alt")

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

    def sandbox_state(self):
        """Simulate the sandbox's private-state mount, independently of vendor markers."""
        state = self.home / ".local/state/task"
        state.mkdir(parents=True, exist_ok=True)
        state.chmod(0o555)
        self.addCleanup(state.chmod, 0o755)
        return state

    def stub_models(self):
        for name in ("codex", "claude"):
            binary = self.home / "bin" / name
            binary.write_text(MODEL_STUB)
            binary.chmod(0o755)

    def stub_reviewers(self):
        for name in ("codex", "claude"):
            binary = self.home / "bin" / name
            binary.write_text(REVIEW_STUB)
            binary.chmod(0o755)

    def wait_for_lane(self, wt, finished=True):
        import json
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if finished:
                record = json.loads((wt / ".work/lane.json").read_text())
                if record["state"] == "finished":
                    return record
            elif (wt / ".work/model-call.json").exists():
                return json.loads((wt / ".work/model-call.json").read_text())
            time.sleep(0.02)
        self.fail("lane did not reach the expected state")

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

    def work_archives(self):
        return self.home / ".local/state/task/archive/proj/t5"

    def test_archive_runtime_failures_and_deep_tree(self):
        wt, _ = self.started()
        work = wt / '.work'
        for name in TASK_MODULE.WORK_CACHES:
            (work / name).mkdir()
            (work / name / 'state').write_text('runtime')
        (work / 'outside').symlink_to(self.home)
        (work / 'unreadable').write_text('private')
        (work / 'unreadable').chmod(0)
        self.addCleanup(lambda: (work / 'unreadable').chmod(0o600) if work.exists() else None)
        os.mkfifo(work / 'pipe')
        deep = work / 'deep'
        for _ in range(400):
            deep = deep / 'd'
            deep.mkdir(parents=True, exist_ok=True)
        (deep / 'request.md').write_text('keep')
        (work / 'big').mkdir()
        with (work / 'big/data').open('wb') as stream:
            stream.truncate(201 * 1024 ** 2)
        (work / 'big/report.md').write_text('keep report')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(TASK_MODULE.Task(self.repo, '5'), wt)
        manifest = next(self.work_archives().glob('*/manifest.json'))
        record = json.loads(manifest.read_text())
        skipped = {entry['path']: entry for entry in record['skipped']}
        self.assertIn('pipe', skipped)
        self.assertIn('unreadable', skipped)
        self.assertEqual(skipped['big']['size'], 201 * 1024 ** 2 + len('keep report'))
        saved = manifest.parent / '.work'
        self.assertEqual((saved / 'big/report.md').read_text(), 'keep report')
        self.assertFalse((saved / 'big/data').exists())
        self.assertTrue((saved / 'outside').is_symlink())
        self.assertEqual((saved / deep.relative_to(work) / 'request.md').read_text(), 'keep')
        self.assertFalse(list(self.work_archives().glob('.pending-*')))
        for name in TASK_MODULE.WORK_CACHES:
            self.assertFalse((saved / name).exists())

    def test_archive_copy_error_finishes_manifest(self):
        wt, _ = self.started()
        (wt / '.work/disappearing').write_text('runtime')
        original = TASK_MODULE.shutil.copy2

        def copy(source, target):
            if source.name == 'disappearing':
                raise FileNotFoundError('runtime removed during archive')
            return original(source, target)

        with mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(TASK_MODULE.shutil, 'copy2', side_effect=copy):
            TASK_MODULE.preserve_work(TASK_MODULE.Task(self.repo, '5'), wt)
        manifest = next(self.work_archives().glob('*/manifest.json'))
        self.assertIn('disappearing', [entry['path'] for entry in json.loads(manifest.read_text())['skipped']])
        self.assertFalse(list(self.work_archives().glob('.pending-*')))

    def test_repair_published_merge_and_failed_run(self):
        wt, _ = self.started()
        self.commit_in(wt, 'published.txt')
        head = self.git(wt, 'rev-parse', 'HEAD')
        self.git(self.repo, 'merge', '--no-ff', '-m', 'published', head)
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        (wt / '.work/report.md').write_text('evidence')
        (self.state() / 'run.json').write_text(json.dumps({'command': 'merge', 'state': 'failed'}))
        out = self.raw_task('repair', 'proj')
        self.assertIn('Repaired published merge', out)
        self.assertFalse(wt.exists())
        self.assertEqual(json.loads((self.state() / 'merge.json').read_text())['head'], head)
        self.assertEqual(json.loads((self.state() / 'run.json').read_text())['state'], 'passed')
        self.assertTrue(list(self.work_archives().glob('*/.work/report.md')))
        (self.state() / 'run.json').write_text(json.dumps({'command': 'merge', 'state': 'failed'}))
        self.raw_task('repair', 'proj')
        self.assertEqual(json.loads((self.state() / 'run.json').read_text())['state'], 'passed')

    def test_prune_archive_dry_run_and_preserves_evidence(self):
        root = self.work_archives()
        saved = root / 't5-finished'
        (saved / '.work/fakehome').mkdir(parents=True)
        (saved / '.work/fakehome/state').write_bytes(b'x' * 100)
        (saved / '.work/fakehome/gateway.log').write_text('keep nested log')
        (saved / '.work/large').mkdir()
        with (saved / '.work/large/data').open('wb') as stream:
            stream.truncate(201 * 1024 ** 2)
        (saved / '.work/large/merge.json').write_text('{}')
        (saved / '.work/report.md').write_text('report')
        (saved / '.work/gateway.log').write_text('log')
        (saved / 'manifest.json').write_text('{}')
        pending = root / '.pending-old'
        pending.mkdir()
        (pending / 'partial').write_text('partial')
        stale = root / '.task-gate-t5-99999999-copy'
        stale.mkdir()
        (stale / 'cache').write_text('cache')
        orphan = root / 'other/.pending-unfinished'
        orphan.mkdir(parents=True)
        out = self.raw_task('repair', '--prune-archive', '--dry-run', 'proj')
        self.assertIn('Would free', out)
        self.assertTrue(pending.exists())
        self.assertTrue((saved / '.work/fakehome').exists())
        out = self.raw_task('repair', '--prune-archive', 'proj')
        self.assertIn('Freed', out)
        self.assertFalse(pending.exists())
        self.assertFalse(stale.exists())
        self.assertTrue(orphan.exists())
        self.assertFalse((saved / '.work/fakehome/state').exists())
        self.assertTrue((saved / '.work/fakehome/gateway.log').exists())
        self.assertFalse((saved / '.work/large/data').exists())
        self.assertTrue((saved / '.work/large/merge.json').exists())
        for name in ('.work/report.md', '.work/gateway.log', 'manifest.json'):
            self.assertTrue((saved / name).is_file())

    def test_merge_preserves_all_work_including_large_data_and_harness(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        work = wt / ".work"
        for number in range(81):
            run = work / "heldout" / str(number)
            run.mkdir(parents=True)
            (run / "transcript.json").write_text(f'{{"run": {number}}}\n')
        (work / "product-harness").mkdir()
        (work / "product-harness/patch.py").write_text("# paid-run patch\n")
        # Results named build, dist, .cache, etc. are not disposable inside .work.
        (work / "build").mkdir()
        (work / "build/result.bin").write_bytes(b"result" * (2 * 1024 * 1024))
        (work / "link").symlink_to("heldout/0/transcript.json")
        (work / "dangling").symlink_to("missing")
        for cache in ("__pycache__", ".pytest_cache", ".venv"):
            (work / "product-harness" / cache).mkdir()
            (work / "product-harness" / cache / "regenerable").write_text("cache")
        self.prepare_merge_evidence("5")
        expected_report = (work / "report.md").read_bytes()
        out = self.raw_task("merge", "proj", "5")
        self.assertFalse(wt.exists(), out)
        snapshots = list(self.work_archives().glob("*/.work"))
        self.assertEqual(len(snapshots), 2, "lane and temporary gate both preserve their .work")
        saved = next(path for path in snapshots if (path / "heldout").exists())
        self.assertEqual(len(list((saved / "heldout").glob("*/transcript.json"))), 81)
        self.assertEqual((saved / "heldout/80/transcript.json").read_text(), '{"run": 80}\n')
        self.assertEqual((saved / "product-harness/patch.py").read_text(), "# paid-run patch\n")
        self.assertEqual((saved / "build/result.bin").read_bytes(), b"result" * (2 * 1024 * 1024))
        self.assertEqual((saved / "report.md").read_bytes(), expected_report)
        self.assertEqual(os.readlink(saved / "link"), "heldout/0/transcript.json")
        self.assertEqual(os.readlink(saved / "dangling"), "missing")
        for cache in ("__pycache__", ".pytest_cache", ".venv"):
            self.assertFalse((saved / "product-harness" / cache).exists())
        self.assertIn(str(saved), out)
        self.assertRegex(out, r"Preserved .* \(\d+ bytes\)")

    def test_merge_preservation_failure_keeps_worktree_and_data(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        self.raw_task("check", "proj", "5")  # Reuse the trusted check: exercise lane cleanup.
        (wt / ".work/paid-transcript").write_text("irreplaceable")
        archive_root = self.home / ".local/state/task/archive"
        archive_root.write_text("not a directory")
        out = self.raw_task("merge", "proj", "5", code=1)
        self.assertIn("refusing to remove", out)
        self.assertTrue(wt.is_dir())
        self.assertEqual((wt / ".work/paid-transcript").read_text(), "irreplaceable")

    def test_stale_gate_cleanup_removes_copy_even_when_archive_unavailable(self):
        self.started()
        gate = self.repo / ".worktrees/.task-gate-t5-99999999"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(gate), "main")
        (gate / ".work").mkdir()
        (gate / ".work/paid-transcript").write_text("gate evidence")
        archive_root = self.home / ".local/state/task/archive"
        archive_root.write_text("not a directory")
        out = self.raw_task("status", "proj")
        self.assertIn("repair available", out)
        out = self.raw_task("repair", "proj")
        self.assertFalse(gate.exists(), out)
        archive_root.unlink()

    def test_close_cleanup_preserves_work_and_repeated_snapshots(self):
        self.started()
        task = TASK_MODULE.Task(self.repo, "5")
        branch = "task-close-t5"
        close_path = TASK_MODULE.close_worktree_path(self.kb, branch)
        for content in ("first", "second"):
            self.git(self.kb, "worktree", "add", "-q", "-B", branch, str(close_path), "main")
            (close_path / ".work").mkdir()
            (close_path / ".work/evidence").write_text(content)
            with mock.patch.object(TASK_MODULE, "HOME", self.home):
                TASK_MODULE.remove_completed_close_worktree(self.kb, branch, task=task)
            self.assertFalse(close_path.exists())
        saved = list(self.work_archives().glob("*/.work/evidence"))
        self.assertEqual(sorted(path.read_text() for path in saved), ["first", "second"])

    def test_close_cleanup_copy_failure_keeps_registered_worktree(self):
        self.started()
        task = TASK_MODULE.Task(self.repo, "5")
        branch = "task-close-t5"
        path = TASK_MODULE.close_worktree_path(self.kb, branch)
        self.git(self.kb, "worktree", "add", "-q", "-b", branch, str(path), "main")
        (path / ".work").mkdir()
        (path / ".work/evidence").write_text("irreplaceable")

        with mock.patch.object(TASK_MODULE, "HOME", self.home), \
                mock.patch.object(TASK_MODULE, "atomic_json", side_effect=OSError("No space left on device")):
            with self.assertRaisesRegex(TASK_MODULE.Stop, "refusing to remove"):
                TASK_MODULE.remove_completed_close_worktree(self.kb, branch, task=task)
        self.assertEqual((path / ".work/evidence").read_text(), "irreplaceable")
        self.assertIn(str(path), self.git(self.kb, "worktree", "list", "--porcelain"))

    def test_cancel_cleanup_preserves_temporary_gate_work(self):
        _wt, gate = self.start_slow_detached_gate()
        (gate / ".work").mkdir(exist_ok=True)
        (gate / ".work/paid-transcript").write_text("gate evidence")
        out = self.raw_task("cancel", "proj", "5")
        self.assertIn("Cancelled", out)
        self.assertFalse(gate.exists(), out)
        saved = list(self.work_archives().glob("*/.work/paid-transcript"))
        self.assertTrue(saved, out)
        self.assertTrue(all(path.read_text() == "gate evidence" for path in saved))

    def test_start_makes_worktree_and_prompt(self):
        wt, out = self.started()
        self.assertEqual(self.git(wt, "branch", "--show-current"), "t5-add-the-b-file")
        self.assertIn(f"Work only in {wt} on branch t5-add-the-b-file.", out)
        self.assertIn("`task lane proj T-5`", out)
        self.assertIn(str(wt / ".work/prompt.md"), out)
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

    def test_setup_requests_are_pm_intake_and_status_attention(self):
        out = self.raw_task("note", "proj", "5", "setup", "Please configure the new provider")
        self.assertIn("Recorded setup request #1", out)
        path = self.home / ".local/state/task/setup-requests.jsonl"
        import json
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(entries[0]["repo"], "proj")
        self.assertEqual(entries[0]["task"], "T-5")
        self.assertEqual(entries[0]["text"], "Please configure the new provider")
        self.assertTrue(entries[0]["time"])
        status = self.raw_task("status")
        self.assertIn("Needs attention:", status)
        self.assertIn("setup request #1 (task T-5): Please configure the new provider", status)
        self.raw_task("note", "proj", "-", "setup", "Repository-level request")
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(entries[1]["task"], "-")
        self.raw_task("note", "-", "-", "setup-done", "1")
        status = self.raw_task("status", "proj")
        self.assertNotIn("setup request #1", status)
        self.assertIn("setup request #2 (task -): Repository-level request", status)
        self.raw_task("note", "-", "-", "setup-done", "2")
        self.assertNotIn("setup request", self.raw_task("status"))
        self.assertIn('"action":"setup-done","id":1', path.read_text())
        self.raw_task("note", str(self.kb), "-", "setup", "Cortex repository setup")
        status = self.raw_task("status")
        self.assertIn("Needs attention (setup requests outside dev_workspace):", status)
        self.assertIn("setup request #3 (cortex-kb-proj, task -): Cortex repository setup", status)

    def test_status_skips_corrupt_setup_request_line_once(self):
        self.raw_task("note", "proj", "-", "setup", "Valid setup request")
        path = self.home / ".local/state/task/setup-requests.jsonl"
        with path.open("a") as output:
            output.write("{broken json\n")
        status = self.raw_task("status")
        self.assertIn("setup request #1 (task -): Valid setup request", status)
        warning = "warning: setup request log line 2 is invalid JSON; skipped"
        self.assertIn(warning, status)
        self.assertEqual(status.count(warning), 1)

    def test_real_folded_backlog_title_reaches_dispatch_and_review_prompts(self):
        source = ROOT / "ci/fixtures/t48-folded-title.md"
        task_file = next((self.kb / "backlog/tasks").glob("t-5 - *.md"))
        task_file.write_text(source.read_text())
        expected = ("Data layer phase 5 - household zone - location, module settings, "
                    "contracts, export and delete")
        out = self.raw_task("start", "proj", "5", "--builder", "codex", "--no-verify",
                            "fixture has no Verify command")
        self.assertIn(f"T-5 - {expected}", out)
        self.assertIn("on branch t5-data-layer-phase-5.", out)
        record = __import__("json").loads((self.state() / "task.json").read_text())
        self.assertEqual(record["title"], expected)
        self.assertNotIn(">-", out)

        out = self.raw_task("review", "proj", "5")
        prompt = (self.state() / "review-prompt.md").read_text()
        self.assertIn(f"Outcome: {expected}", prompt)
        self.assertIn(
            "Verdict FAIL only when a criterion above, or the stated request, is not met, "
            "or the change breaks something that worked.", prompt)
        self.assertIn("Split findings into 'Blocking' (each tied to a criterion or a breakage) and 'Notes'.", prompt)
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
        self.assertIn(".worktrees/codex-direct-request on branch codex/direct-request.", out)
        self.assertIn("`task lane proj codex/direct-request`", out)
        self.assertEqual(self.git(wt, "branch", "--show-current"), "codex/direct-request")
        self.assertTrue((self.state("codex-direct-request") / "task.json").is_file())

    def test_direct_request_without_backlog_uses_two_part_report_and_request_review(self):
        self.git(self.repo, "branch", "codex/direct-request", "main")
        self.raw_task("start", "proj", "codex/direct-request", "--builder", "codex")
        wt = self.repo / ".worktrees/codex-direct-request"
        lane_prompt = "Implement the requested change from the lane prompt."
        (wt / ".work/prompt.md").write_text(lane_prompt)
        self.commit_in(wt, "direct-request.txt")
        (wt / ".work/report.md").write_text(
            "# Task report\n\n## What changed\n\nImplemented the direct request.\n\n"
            "## How I verified\n\n`task check` passed.\n")
        self.raw_task("check", "proj", "codex/direct-request")
        prompt = self.raw_task("review", "proj", "codex/direct-request")
        self.assertIn("Outcome: Direct request for branch codex/direct-request", prompt)
        self.assertIn("Original request:\n" + lane_prompt, prompt)
        self.assertIn("Criteria: none; judge the stated request above against the diff.", prompt)
        self.assertIn("judge the stated request above against the diff", prompt)
        self.assertIn("Diff range:", prompt)
        review = self.home / "direct-request-review.txt"
        head = self.git(wt, "rev-parse", "HEAD")
        review.write_text(f"Verdict: PASS\nCommit: {head}\nReviewer: claude reviewer-model\n")
        self.raw_task("review", "proj", "codex/direct-request", "--record", str(review),
                      "--reviewer", "claude", "reviewer-model")
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
        self.sandbox_state()
        env = dict(self.env, CLAUDECODE="1")
        out = self.raw_task("check", "proj", "5", env=env, code=1)
        self.assertIn("advisory lane check (not evidence for merge)", out)
        self.assertIn("FAIL", out)
        evidence = __import__("json").loads((wt / ".work/check.json").read_text())
        self.assertEqual(evidence["result"], "failed")
        self.assertTrue(evidence["advisory"])

    def test_advisory_pre_commit_uses_private_index_for_committed_range(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (wt / ".pre-commit-config.yaml").write_text("repos: []\n")
        self.git(wt, "add", ".pre-commit-config.yaml")
        self.git(wt, "commit", "-qm", "add pre-commit config")
        real_index = Path(self.git(wt, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        index_before = real_index.read_bytes()
        original_worktree = (wt / "a.txt").read_bytes()
        self.sandbox_state()
        env = dict(self.env, CLAUDECODE="1", SIMULATE_READ_ONLY_GIT_DIR="1")

        out = self.raw_task("check", "proj", "5", env=env)

        self.assertIn("PASS", out)
        self.assertEqual(real_index.read_bytes(), index_before, "advisory hooks must not write the real index")
        self.assertEqual((wt / "a.txt").read_bytes(), original_worktree,
                         "advisory hooks must not alter the worktree")
        captured = dict(line.split("=", 1) for line in
                        (self.home / "pre-commit-env").read_text().splitlines() if "=" in line)
        private_index = Path(captured["GIT_INDEX_FILE"])
        self.assertEqual(private_index.parent.parent, wt / ".work")
        self.assertFalse(private_index.parent.exists(), "private index storage should be disposed after the step")
        self.assertEqual((self.home / "pre-commit-args").read_text().splitlines(),
                         ["run", "--from-ref", self.git(self.repo, "rev-parse", "origin/main"),
                          "--to-ref", "HEAD"], "pre-commit must check the committed range")

        failed = self.raw_task("check", "proj", "5",
                               env=dict(env, PRE_COMMIT_FAIL="1"), code=1)
        self.assertIn("FAIL", failed)
        evidence = __import__("json").loads((wt / ".work/check.json").read_text())
        self.assertEqual(evidence["steps"][-1]["command"],
                         ["pre-commit", "run", "--from-ref", self.git(self.repo, "rev-parse", "origin/main"),
                          "--to-ref", "HEAD"])
        self.assertEqual(evidence["steps"][-1]["exit"], 1)
        self.assertEqual(real_index.read_bytes(), index_before, "a failing hook must not write the real index")
        self.assertEqual((wt / "a.txt").read_bytes(), original_worktree)

    def test_trusted_pre_commit_keeps_default_index_environment(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (wt / ".pre-commit-config.yaml").write_text("repos: []\n")
        self.git(wt, "add", ".pre-commit-config.yaml")
        self.git(wt, "commit", "-qm", "add pre-commit config")

        out = self.raw_task("check", "proj", "5")

        self.assertIn("PASS", out)
        captured = dict(line.split("=", 1) for line in
                        (self.home / "pre-commit-env").read_text().splitlines() if "=" in line)
        self.assertNotIn("GIT_INDEX_FILE", captured, "trusted pre-commit must retain its existing environment")
        self.assertEqual((self.home / "pre-commit-args").read_text().splitlines(),
                         ["run", "--from-ref", self.git(self.repo, "rev-parse", "origin/main"),
                          "--to-ref", "HEAD"])

    def test_advisory_check_holds_exclusive_gate_lock_during_run(self):
        wt, _ = self.started()
        (wt / ".task.toml").write_text("exclusive_gate = true\n")
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "enable shared check gate")
        (self.home / "during-check.sh").write_text(
            'test -s "$TASK_GATE_DIR/agent-task-gate.holder" || exit 7\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.sandbox_state()
        out = self.raw_task("check", "proj", "5", env=dict(self.env, CLAUDECODE="1"))
        self.assertIn("PASS", out)
        self.assertFalse((self.gate_dir / "agent-task-gate.holder").exists(),
                         "the shared gate holder should be released after the advisory run")

    def test_bare_just_check_does_not_expand_unset_proxy_bypass_vars(self):
        justfile = (ROOT / "justfile").read_text()
        self.assertIn("check:\n", justfile, "inspect the repository's real justfile")
        self.assertIn("bash ci/lint.sh", justfile, "the check recipe should call ci/lint.sh")
        for path in (ROOT / "justfile", ROOT / "ci/lint.sh"):
            source = path.read_text()
            for expansion in ("$NO_PROXY", "${NO_PROXY", "$no_proxy", "${no_proxy"):
                self.assertNotIn(expansion, source,
                                 f"{path.relative_to(ROOT)} must not expand unset {expansion}")

    def test_check_steps_append_localhost_proxy_bypasses(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (wt / ".pre-commit-config.yaml").write_text("repos: []\n")
        self.git(wt, "add", ".pre-commit-config.yaml")
        self.git(wt, "commit", "-qm", "add pre-commit config")
        (self.home / "during-check.sh").write_text('env > "$HOME/just-env"\n')
        (self.home / "during-check.sh").chmod(0o755)
        task_record_path = self.state() / "task.json"
        import json
        record = json.loads(task_record_path.read_text())
        record["verify"] = [f'env > "{self.home}/verify-env"']
        record["revision"] = TASK_MODULE.record_revision(record)
        task_record_path.write_text(json.dumps(record))
        self.sandbox_state()
        env = dict(self.env, CLAUDECODE="1", HTTP_PROXY="http://127.0.0.1:8123",
                   HTTPS_PROXY="http://127.0.0.1:8123", ALL_PROXY="socks5://127.0.0.1:8123",
                   http_proxy="http://127.0.0.1:8124", https_proxy="http://127.0.0.1:8124",
                   all_proxy="socks5://127.0.0.1:8124", NO_PROXY="existing.example",
                   no_proxy="lower.example")
        out = self.raw_task("check", "proj", "5", env=env)
        self.assertIn("PASS", out)
        for name in ("just", "verify", "pre-commit"):
            captured = dict(line.split("=", 1) for line in
                            (self.home / f"{name}-env").read_text().splitlines() if "=" in line)
            self.assertEqual(captured["NO_PROXY"], "existing.example,localhost,127.0.0.1,::1")
            self.assertEqual(captured["no_proxy"], "lower.example,localhost,127.0.0.1,::1")
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                self.assertEqual(captured[key], env[key], f"{key} must be preserved for {name}")

    def test_concurrent_advisory_check_is_refused_without_overwriting_log(self):
        wt, _ = self.started()
        self.commit_in(wt, "FAIL")
        (self.home / "during-check.sh").write_text(
            '#!/bin/sh\necho active > "$HOME/advisory-active"\nsleep 1\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.sandbox_state()
        env = dict(self.env, CLAUDECODE="1")
        first = subprocess.Popen([sys.executable, str(TOOL), "check", "proj", "5"],
                                 cwd=self.home, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline and not (self.home / "advisory-active").exists():
            __import__("time").sleep(0.02)
        self.assertTrue((self.home / "advisory-active").exists(), "first advisory check should be running")
        second = self.raw_task("check", "proj", "5", env=env, code=1)
        first_out, _ = first.communicate(timeout=8)
        self.assertEqual(first.returncode, 1, first_out)
        self.assertIn("Advisory check already running", second)
        self.assertIn(str(wt), second)
        self.assertNotIn("PASS", second)
        self.assertIn("FAILED (failures=1)", (wt / ".work/check.log").read_text())
        self.assertIn("FAIL (log:", first_out)

    def test_read_only_state_skips_unavailable_shared_gate_and_separates_lane_result(self):
        (self.repo / ".task.toml").write_text("exclusive_gate = true\n")
        self.git(self.repo, "add", ".task.toml")
        self.git(self.repo, "commit", "-qm", "enable exclusive gate")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        read_only = self.sandbox_state()
        for index, (marker, value) in enumerate((("CLAUDECODE", "1"),
                                                  ("CODEX_SANDBOX", "workspace-write"))):
            env = dict(self.env, **{marker: value},
                       TASK_GATE_DIR=str(self.home / (marker + "-missing") / "gate"))
            args = ("check", "proj", "5", "--detach") if index == 0 else ("check", "proj", "5")
            out = self.raw_task(*args, env=env)
            self.assertNotIn("Started detached", out)
            self.assertIn("Cannot open shared gate lock", out)
            self.assertIn("PASS", out)
            checked = __import__("json").loads((wt / ".work/check.json").read_text())
            self.assertTrue(checked["advisory"])
            self.assertEqual(checked["result"], "passed")
            self.assertFalse((self.state() / "check.json").exists())
        self.raw_task("check", "proj", "5")
        lane_result = __import__("json").loads((wt / ".work/check.json").read_text())
        self.assertTrue(lane_result["advisory"])
        read_only.chmod(0o755)
        self.raw_task("check", "proj", "5")
        trusted = __import__("json").loads((self.state() / "check.json").read_text())
        self.assertTrue(trusted["trusted"])
        self.assertTrue(__import__("json").loads((wt / ".work/check.json").read_text())["advisory"])
        self.assertIn("lane check: passed (advisory)", self.raw_task("status", "proj"))

    def test_vendor_markers_do_not_make_writable_state_advisory(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        state = self.state()
        probe = state / "writable-probe"
        probe.write_text("writable")
        probe.unlink()
        for markers in ({"CODEX_THREAD_ID": "thread-test"}, {"CODEX_SANDBOX": "workspace-write"},
                        {"CODEX_SANDBOX": "read-only"}, {"CLAUDECODE": "1"},
                        {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            with self.subTest(markers=markers):
                out = self.raw_task("check", "proj", "5", env=dict(self.env, **markers))
                self.assertNotIn("advisory lane check", out)
                checked = __import__("json").loads((state / "check.json").read_text())
                self.assertTrue(checked["trusted"])
                self.assertFalse(checked["advisory"])
        self.assertFalse((wt / ".work/check.json").exists())

    def test_read_only_state_refuses_mutating_commands_without_writes(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        read_only = self.sandbox_state()
        before = {str(path.relative_to(self.home)): path.read_bytes()
                  for path in self.home.rglob("*") if path.is_file()}
        commands = (("start", "proj", "5", "--builder", "codex", "--refresh"),
                    ("review", "proj", "5"),
                    ("review", "proj", "5", "--run"),
                    ("review", "proj", "5", "--record", "missing-review.md"),
                    ("merge", "proj", "5"), ("merge", "proj", "5", "--detach"),
                    ("close", "proj", "5", "-m", "done", "--retro", "tested"),
                    ("cancel", "proj", "5"), ("lane", "proj", "5"),
                    ("note", "proj", "5", "improve", "idea"))
        for markers in ({}, {"CODEX_THREAD_ID": "thread-test"}, {"CODEX_SANDBOX": "workspace-write"},
                        {"CODEX_SANDBOX": "read-only"}, {"CLAUDECODE": "1"},
                        {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            for command in commands:
                with self.subTest(markers=markers, command=command):
                    out = self.raw_task(*command, code=1, env=dict(self.env, **markers))
                    self.assertIn("run this from the PM shell or a batch runner", out)
        after = {str(path.relative_to(self.home)): path.read_bytes()
                 for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(after, before, "a denied PM command must write nothing")
        read_only.chmod(0o755)

    def test_writable_runner_can_start_check_record_merge_and_close(self):
        self.env.update(CODEX_THREAD_ID="runner-test")
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.raw_task("check", "proj", "5")
        self.assertTrue(__import__("json").loads((self.state() / "check.json").read_text())["trusted"])
        self.prepare_merge_evidence("5")
        self.raw_task("review", "proj", "5")
        review = self.home / "review.md"
        review.write_text(f"```review\nVerdict: PASS\nCommit: {self.git(wt, 'rev-parse', 'HEAD')}\n"
                          "#1 pass: b exists\n#2 pass: checked\n```\n")
        self.raw_task("review", "proj", "5", "--record", str(review))
        self.assertIn("Merged", self.raw_task("merge", "proj", "5"))
        self.assertIn("Closed", self.raw_task("close", "proj", "5", "-m", "done", "--retro", "tested"))
        task_text = next((self.kb / "backlog/tasks").glob("t-5 *")).read_text()
        self.assertIn("status: Done", task_text)

    def test_codex_lane_is_detached_sandboxed_clean_and_visible_in_status(self):
        self.stub_models()
        wt, _ = self.started()
        prompt = "Build only the approved task.\n"
        (wt / ".work/prompt.md").write_text(prompt)
        release = self.home / "lane-release"
        self.env.update(GIT_PAGER="cat", GIT_EXTERNAL_DIFF="blocked-helper", GIT_CONFIG_COUNT="0",
                        TASK_RUN_RECORD=str(self.home / "runner-record.json"), TASK_RUN_ID="runner-id",
                        LANE_STUB_RELEASE=str(release))
        out = self.raw_task("lane", "proj", "5", "--model", "gpt-6.1-sol")
        self.assertRegex(out, r"pid \d+")
        self.assertIn(str(wt / ".work/lane.log"), out)
        call = self.wait_for_lane(wt, finished=False)
        try:
            self.assertEqual(call["cwd"], str(wt))
            self.assertEqual(call["prompt"], 'Read the pinned task context in .work/context/ before working.\n\n' + prompt)
            self.assertEqual(call["args"], ["exec", "--sandbox", "workspace-write", "--model", "gpt-6.1-sol",
                                            "--output-last-message", str(wt / ".work/final.md"), "-"])
            self.assertFalse(any(name in TASK_MODULE.GIT_OVERRIDE_ENV or
                                 name.startswith(("GIT_CONFIG", "TASK_RUN_")) for name in call["env_names"]))
            self.assertIn("GIT_AUTHOR_NAME", call["env_names"])
            self.assertIn("GIT_COMMITTER_EMAIL", call["env_names"])
            self.assertIn("lane: running", self.raw_task("status", "proj"))
            self.assertIn("Lane already running", self.raw_task("lane", "proj", "5", code=1))
        finally:
            release.touch()
            record = self.wait_for_lane(wt)
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual((wt / ".work/final.md").read_text(), "codex-final: Read the pinned task context in .work/context/ before working.\n\n" + prompt)
        self.assertIn("codex-log", (wt / ".work/lane.log").read_text())
        self.assertIn("lane: finished (exit 0)", self.raw_task("status", "proj"))
        self.assertFalse((self.home / "runner-record.json").exists())

    def test_claude_lane_uses_pinned_builder_and_logs_final_and_failure(self):
        self.stub_models()
        self.raw_task("start", "proj", "5", "--builder", "claude")
        wt = self.repo / ".worktrees/t5"
        prompt = "Approved Claude task.\n"
        (wt / ".work/prompt.md").write_text(prompt)
        self.env["LANE_STUB_EXIT"] = "3"
        self.raw_task("lane", "proj", "5", "--builder", "claude")
        record = self.wait_for_lane(wt)
        call = __import__("json").loads((wt / ".work/model-call.json").read_text())
        self.assertEqual(call["args"], ["-p", "--model", "sonnet", "--effort", "high",
                                       "--permission-mode", "acceptEdits", "--allowedTools",
                                       "Bash Read Write Edit Glob Grep"])
        self.assertEqual(call["cwd"], str(wt))
        self.assertEqual(call["prompt"], 'Read the pinned task context in .work/context/ before working.\n\n' + prompt)
        self.assertEqual(record["exit_code"], 3)
        self.assertEqual(record["model"], "sonnet")
        self.assertEqual((wt / ".work/final.md").read_text(), "claude-final: Read the pinned task context in .work/context/ before working.\n\n" + prompt)
        log = (wt / ".work/lane.log").read_text()
        self.assertIn("claude-log", log)
        self.assertIn("claude-final", log)
        self.assertIn("lane: finished (exit 3)", self.raw_task("status", "proj"))

    def test_claude_lane_accepts_opus_and_records_selected_model(self):
        self.stub_models()
        self.raw_task("start", "proj", "5", "--builder", "claude")
        wt = self.repo / ".worktrees/t5"
        (wt / ".work/prompt.md").write_text("Approved Claude task.\n")
        self.raw_task("lane", "proj", "5", "--builder", "claude", "--model", "opus")
        record = self.wait_for_lane(wt)
        call = __import__("json").loads((wt / ".work/model-call.json").read_text())
        self.assertEqual(call["args"][:3], ["-p", "--model", "opus"])
        self.assertEqual(call["args"][3:], ["--effort", "high", "--permission-mode", "acceptEdits",
                                            "--allowedTools", "Bash Read Write Edit Glob Grep"])
        self.assertEqual(record["model"], "opus")

    def test_lane_rejects_model_from_other_builder(self):
        self.stub_models()
        self.raw_task("start", "proj", "5", "--builder", "claude")
        wt = self.repo / ".worktrees/t5"
        (wt / ".work/prompt.md").write_text("task\n")
        out = self.raw_task("lane", "proj", "5", "--builder", "claude", "--model", "gpt-6-luna", code=1)
        self.assertIn("not available for the claude builder", out)
        self.assertFalse((wt / ".work/lane.json").exists())

    def test_lane_refuses_missing_prompt_and_invalid_model(self):
        self.stub_models()
        wt, _ = self.started()
        self.assertIn("Missing lane prompt", self.raw_task("lane", "proj", "5", code=1))
        self.assertFalse((wt / ".work/lane.json").exists())
        self.assertFalse((wt / ".work/lane.log").exists())
        (wt / ".work/prompt.md").write_text("task\n")
        self.assertIn("choose one of", self.raw_task("lane", "proj", "5", "--model", "other", code=1))
        self.assertFalse((wt / ".work/model-call.json").exists())

    def test_lane_full_model_ids_and_builder_override_review_route(self):
        import json
        self.stub_models()
        wt, _ = self.started()
        (wt / ".work/prompt.md").write_text("task\n")
        for model, cli in TASK_MODULE.MODELS["claude"].items():
            out = self.raw_task("lane", "proj", "5", "--builder", "claude", "--model", model)
            record = self.wait_for_lane(wt)
            self.assertEqual((record["model"], record["cli_model"]), (model, cli))
            call = json.loads((wt / ".work/model-call.json").read_text())
            self.assertEqual(call["args"][:3], ["-p", "--model", cli])
        self.assertEqual(json.loads((self.state() / "task.json").read_text())["builder"], "claude")
        self.stub_reviewers()
        head = self.git(wt, "rev-parse", "HEAD")
        reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n```\n"
        self.raw_task("review", "proj", "5", "--run", env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertEqual(json.loads((self.state() / "review-routing.json").read_text())["vendor"], "codex")

    def test_lane_preserves_original_request_and_sets_local_state_and_reason(self):
        import json
        self.stub_models()
        wt, _ = self.started()
        prompt = wt / ".work/prompt.md"
        prompt.write_text("Original outcome\n")
        self.raw_task("lane", "proj", "5", env=dict(self.env, SECRETSPEC_REASON=""))
        self.wait_for_lane(wt)
        call = json.loads((wt / ".work/model-call.json").read_text())
        self.assertEqual(call["state_home"], str(wt / ".work/state"))
        self.assertEqual(call["reason"], "lane proj/T-5")
        self.assertTrue((wt / ".work/state").is_dir())
        prompt.write_text("Re-run, fix, commit\n")
        self.raw_task("lane", "proj", "5", env=dict(self.env, SECRETSPEC_REASON="specific reason"))
        self.wait_for_lane(wt)
        self.assertEqual((wt / ".work/request.md").read_text(), "Original outcome\n")
        call = json.loads((wt / ".work/model-call.json").read_text())
        self.assertEqual(call["reason"], "specific reason")
        self.raw_task("review", "proj", "5")
        review = (self.state() / "review-prompt.md").read_text()
        self.assertTrue(review.startswith("You are the REVIEWER of another agent's work. Do not edit, run builds or tests, or commit. Read the diff and report, then give the verdict block.\n"))
        self.assertIn("Original request:\nOriginal outcome", review)
        self.assertIn("Latest fix-round instructions to the builder (context only; judge the whole change against the original request):\nRe-run, fix, commit", review)

    def test_review_run_retries_invalid_reply_once_and_can_recover(self):
        self.stub_reviewers()
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: source\n#2 pass: evidence\n```\n"
        for first in ("I am the builder", reply.replace(head, "a" * 40)):
            (wt / ".work/reviewer-count").unlink(missing_ok=True)
            out = self.raw_task("review", "proj", "5", "--run",
                                env=dict(self.env, REVIEW_STUB_FIRST_REPLY=first, REVIEW_STUB_REPLY=reply))
            self.assertEqual((wt / ".work/reviewer-count").read_text(), "2")
            self.assertIn("Recorded PASS", out)
            self.assertNotIn("I am the builder", out)

    def test_review_run_records_unfenced_reply_on_first_try(self):
        self.stub_reviewers()
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        reply = f"Verdict: PASS\nCommit: {head[:12]}\n#1 pass: source\n#2 pass: evidence\n"
        out = self.raw_task("review", "proj", "5", "--run",
                            env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertEqual((wt / ".work/reviewer-count").read_text(), "1")
        self.assertIn("Recorded PASS", out)
        review = __import__("json").loads((self.state() / "review.json").read_text())
        self.assertEqual(review["commit"], head)
        self.assertEqual((self.state() / "review.md").read_text(), reply)

    def test_review_run_without_verdict_retries_once_then_stops(self):
        self.stub_reviewers()
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        reply = f"Commit: {head}\n#1 pass: source\n#2 pass: evidence\n"
        out = self.raw_task("review", "proj", "5", "--run", code=1,
                            env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertEqual((wt / ".work/reviewer-count").read_text(), "2")
        self.assertIn("review produced no verdict", out)
        self.assertNotIn(reply, out)
        self.assertFalse((self.state() / "review.json").exists())
        self.assertFalse((self.state() / "review.md").exists())

    def test_report_criteria_list_markers_merge_and_check(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        report = wt / ".work/report.md"
        content = report.read_text()
        for marker in ("- ", "* ", "1. "):
            report.write_text(content.replace("#1 met:", marker + "#1 met:").replace("#2 met:", marker + "#2 met:"))
            self.raw_task("check", "proj", "5")
        self.raw_task("merge", "proj", "5")

    def test_start_seeds_files_directories_existing_worktree_and_refuses_escape(self):
        source = self.repo / ".work"
        source.mkdir(exist_ok=True)
        (source / "pinned").mkdir()
        (source / "pinned/code").write_text("original")
        (source / "single").write_text("file")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", "pinned", "--seed", "single")
        wt = self.repo / ".worktrees/t5"
        self.assertEqual((wt / ".work/pinned/code").read_text(), "original")
        (source / "single").write_text("changed")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", "single")
        self.assertEqual((wt / ".work/single").read_text(), "file")
        (source / "escape").symlink_to(self.home)
        for name in ("..", "../outside", str(self.home), "escape"):
            self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", name, code=1)

    def test_runner_check_models_ids_and_blocked_constructs(self):
        runner = self.home / "runner.md"
        (self.kb / "backlog/tasks").mkdir(parents=True, exist_ok=True)
        (self.kb / "backlog/tasks/task-5.md").write_text("---\ntitle: Task five\n---\n")
        (self.repo / ".work").mkdir(exist_ok=True)
        (self.repo / ".work/pinned").write_text("seed")
        runner.write_text(
            "1. 5: builder claude, model claude-fable-5-1\n"
            "2. docs: builder codex, model gpt-6.1-sol, seed pinned, after: 5\n"
            "```text\n"
            "task start <repo> <id> --builder codex|claude\n"
            "task lane <repo> <id> --model <model>\n"
            "nohup task check\n"
            "```\n")
        self.assertIn("runner file OK (2 tasks)", self.raw_task("runner-check", "proj", str(runner)))
        for contents, expected in (
            ("1. docs: builder codex, model sonnet\n", "not available for the codex builder"),
            ("1. docs: builder codex, model gpt-6-luna, seed absent\n", "Seed does not exist"),
            ("1. docs: builder codex, model gpt-6-luna, seed ..\n", "Invalid seed name"),
            ("1. docs: builder codex, model gpt-6-luna, seed ../README.md\n", "Invalid seed name"),
            ("1. docs: builder codex, model gpt-6-luna, after: second\n"
             "2. second: builder codex, model gpt-6-luna\n", "must name an earlier task"),
            ("task lane <repo> <id> --model <model>\n", "no task lines found"),
        ):
            runner.write_text(contents)
            self.assertIn(expected, self.raw_task("runner-check", "proj", str(runner), code=1))
        for line in ("1. plain: builder codex, model gpt-6-luna, after: later\n",
                     "task start proj 999 --builder codex", "task start proj ../bad --builder codex",
                     "task lane proj plain --builder codex --model sonnet",
                     "task lane proj plain --builder claude --model unknown",
                     "cat <<EOF", "cp -a source target", "cp -r source target",
                     "cp -ra", "cp -aR x y", "cp -va x y",
                     "cp --recursive source target", "cp --archive source target",
                     "nohup task check", "setsid task check", "xargs task", "sudo ls"):
            runner.write_text(line + "\n")
            self.assertIn("line 1:", self.raw_task("runner-check", "proj", str(runner), code=1))
        runner.write_text("1. docs: builder codex, model gpt-6-luna\ncp -v x y\n")
        self.assertIn("runner file OK (1 tasks)", self.raw_task("runner-check", "proj", str(runner)))

    def test_agent_environment_removes_only_hook_blocked_git_and_runner_ownership(self):
        blocked = {name: "blocked" for name in TASK_MODULE.GIT_OVERRIDE_ENV}
        blocked.update(GIT_CONFIG="blocked", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="override",
                       GIT_CONFIG_VALUE_0="value", GIT_CONFIG_FUTURE="blocked", TASK_RUN_ID="runner")
        kept = {"GIT_AUTHOR_NAME": "builder", "GIT_AUTHOR_EMAIL": "builder@example.invalid",
                "GIT_COMMITTER_NAME": "builder", "GIT_COMMITTER_EMAIL": "builder@example.invalid",
                "GIT_OPTIONAL_LOCKS": "0", "GIT_FUTURE_VARIABLE": "value",
                "CODEX_THREAD_ID": "runner", "PATH": "tools"}
        with mock.patch.dict(os.environ, dict(blocked, **kept), clear=True):
            self.assertEqual(TASK_MODULE.agent_environment(), kept)

    def test_lane_git_override_set_matches_the_hook(self):
        import ast
        tree = ast.parse((ROOT / "system/agent-policy/pre_tool_use.py").read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "GIT_OVERRIDE_ENV"
                                  for target in node.targets))
        self.assertEqual(TASK_MODULE.GIT_OVERRIDE_ENV, ast.literal_eval(assignment.value))

    def test_exclude_failure_and_flock_error_degrade_without_traceback(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        exclude_path = self.repo / ".git/info/exclude"
        exclude_path.chmod(0o444)
        self.sandbox_state()
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
        (self.repo / ".task.toml").write_text("exclusive_gate = true\n")
        original_flock = module.fcntl.flock
        module.fcntl.flock = lambda *_args: (_ for _ in ()).throw(OSError("probe failure"))
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                with module.exclusive_gate_lock(task, silent_unavailable=True):
                    print('advisory ran')
            with contextlib.redirect_stdout(output), self.assertRaisesRegex(module.Stop, "probe failure"):
                with module.exclusive_gate_lock(task):
                    pass
        finally:
            module.fcntl.flock = original_flock
        self.assertIn("running advisory check unlocked", output.getvalue())
        self.assertIn("advisory ran", output.getvalue())
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
        out = self.raw_task("check", "proj", "5", code=1)
        self.assertIn("Cannot open shared gate lock", out)
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
        self.assertIn("check:just check waived: approved failing-gate exception", out)
        self.assertIn("Merged", out)
        exception = __import__("json").loads((self.state() / "exception.json").read_text())
        self.assertTrue(exception["gate_failed"])
        self.assertEqual(exception["waived"], [{"step": "check:just check", "reason": "approved failing-gate exception"}])

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
        self.assertEqual(__import__("json").loads((self.state() / "review-routing.json").read_text()),
                         {"vendor": "claude", "model": "haiku"})
        self.assertTrue(prompt.read_text().endswith(
            "Return exactly this final fenced block, with plain text only inside it:\n"
            "Verdict FAIL only when a criterion above, or the stated request, is not met, "
            "or the change breaks something that worked. Everything else is a non-blocking "
            "note: list it under 'Notes', never as a reason to FAIL.\n"
            "Split findings into 'Blocking' (each tied to a criterion or a breakage) and 'Notes'.\n"
            "```review\nVerdict: PASS|FAIL\nCommit: <full sha>\n"
            "#1 pass|fail|unclear: <one line of evidence>\n"
            "#2 pass|fail|unclear: <one line of evidence>\n```\n"
            "After the fenced block, list findings under 'Blocking' and 'Notes'. Every "
            "Blocking finding must identify its criterion or the breakage. Put all other "
            "observations under Notes.\n"))
        for command in ("git --no-optional-locks diff", "git --no-optional-locks log",
                        "git --no-optional-locks show"):
            self.assertIn(command, prompt.read_text())
        self.assertIn(f"read {wt / '.work/report.md'} alongside the diff", prompt.read_text())
        self.assertFalse(reply.exists(), "a stale response must not be presented as the next review")

    def assert_review_run(self, builder, verdict):
        import json
        import shlex
        self.stub_reviewers()
        self.raw_task("start", "proj", "5", "--builder", builder)
        wt = self.repo / ".worktrees/t5"
        head = self.git(wt, "rev-parse", "HEAD")
        prepared = self.raw_task("review", "proj", "5")
        self.assertFalse((wt / ".work/reviewer-call.json").exists())
        prompt = (self.state() / "review-prompt.md").read_text()
        expected_command = next(line.strip() for line in prepared.splitlines()
                                if " < " in line and " > " in line)
        expected_args = shlex.split(expected_command.split(" < ")[0])
        if expected_args[0] == "GIT_OPTIONAL_LOCKS=0":
            expected_args.pop(0)
        reply = (f"```review\nVerdict: {verdict}\nCommit: {head}\n"
                 f"Reviewer: {builder} wrong-model\n"
                 f"#1 {'pass' if verdict == 'PASS' else 'fail'}: inspected b\n"
                 "#2 pass: inspected check evidence\n```\n")
        out = self.raw_task("review", "proj", "5", "--run",
                            env=dict(self.env, CODEX_THREAD_ID="runner-test", REVIEW_STUB_REPLY=reply))
        call = json.loads((wt / ".work/reviewer-call.json").read_text())
        self.assertEqual(call["args"], expected_args[1:])
        self.assertEqual(call["cwd"], str(wt))
        self.assertEqual(call["prompt"], prompt)
        if builder == "claude":
            self.assertEqual(call["git_optional_locks"], "0")
        self.assertEqual((self.state() / "review-response.md").read_text(), reply)
        self.assertEqual((self.state() / "review.md").read_text(), reply)
        metadata = json.loads((self.state() / "review.json").read_text())
        self.assertEqual((metadata["reviewer"], metadata["model"]),
                         ("claude", "haiku") if builder == "codex" else ("codex", "gpt-6-luna"))
        self.assertEqual(metadata["verdict"], verdict)
        self.assertEqual(metadata["commit"], head)
        self.assertIn("--- reviewer reply ---", out)
        self.assertIn("inspected b", out)
        self.assertIn("inspected check evidence", out)
        self.assertNotIn(prompt, out)
        self.assertEqual(out.rstrip().splitlines()[-1], f"Review recorded: {verdict}")
        self.raw_task("review", "proj", "5", "--record", str(self.state() / "review-response.md"))
        manual = json.loads((self.state() / "review.json").read_text())
        self.assertEqual({k: v for k, v in metadata.items() if k != "time"},
                         {k: v for k, v in manual.items() if k != "time"})

    def test_review_run_claude_records_pass_like_manual_record(self):
        self.assert_review_run("codex", "PASS")

    def test_review_run_codex_records_fail_like_manual_record(self):
        self.assert_review_run("claude", "FAIL")

    def test_review_run_cleans_inherited_environment_before_hook_and_diff(self):
        import json
        self.stub_reviewers()
        for builder in ("codex", "claude"):
            with self.subTest(builder=builder):
                name = f"clean-{builder}"
                self.raw_task("start", "proj", name, "--builder", builder)
                wt = self.repo / ".worktrees" / name
                self.commit_in(wt, "b.txt")
                head = self.git(wt, "rev-parse", "HEAD")
                reply = f"```review\nVerdict: PASS\nCommit: {head}\n```\n"
                env = dict(self.env, GIT_PAGER="cat", GIT_EXTERNAL_DIFF="blocked-helper",
                           GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.pager",
                           GIT_CONFIG_VALUE_0="cat", GIT_CONFIG_FUTURE="blocked",
                           GIT_OPTIONAL_LOCKS="1", TASK_RUN_ID="runner",
                           REVIEW_STUB_POLICY=str(ROOT / "system/agent-policy/pre_tool_use.py"),
                           REVIEW_STUB_REPLY=reply)
                self.raw_task("review", "proj", name, "--run", env=env)
                call = json.loads((wt / ".work/reviewer-call.json").read_text())
                self.assertFalse(any(key in TASK_MODULE.GIT_OVERRIDE_ENV or
                                     key.startswith(("GIT_CONFIG", "TASK_RUN_"))
                                     for key in call["env_names"]))
                self.assertIn("GIT_AUTHOR_NAME", call["env_names"])
                self.assertIn("GIT_COMMITTER_EMAIL", call["env_names"])
                self.assertEqual(call["git_optional_locks"], "0" if builder == "claude" else "1")
                self.assertIn("diff --git a/b.txt b/b.txt", (wt / ".work/reviewer-diff.txt").read_text())
                self.assertEqual(json.loads((self.state(name) / "review.json").read_text())["verdict"],
                                 "PASS")

    def test_review_run_direct_request_has_no_kb_and_records_without_criteria(self):
        import json
        self.stub_reviewers()
        self.raw_task("start", "proj", "direct-request", "--builder", "claude")
        wt = self.repo / ".worktrees/direct-request"
        request = self.home / "request with spaces.md"
        request.write_text("Preserve user files.")
        reply = f"```review\nVerdict: PASS\nCommit: {self.git(wt, 'rev-parse', 'HEAD')}\n```\n"
        self.raw_task("review", "proj", "direct-request", "--run", "--request", str(request),
                      env=dict(self.env, REVIEW_STUB_REPLY=reply))
        call = json.loads((wt / ".work/reviewer-call.json").read_text())
        self.assertIn(request.read_text(), call["prompt"])
        self.assertNotIn("--add-dir", call["args"])
        self.assertEqual(json.loads((self.state("direct-request") / "review.json").read_text())["judgments"], {})

    def test_review_run_errors_do_not_record_or_replace_previous_review(self):
        self.stub_reviewers()
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        valid_reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: b\n#2 pass: checked\n```\n"
        cases = (({"REVIEW_STUB_REPLY": " \n\t"}, "review produced no verdict; rerun `task review`"),
                 ({"REVIEW_STUB_REPLY": valid_reply, "REVIEW_STUB_DELAY": "10",
                   "TASK_REVIEW_TIMEOUT": "1"}, "timed out after 1s"),
                 ({"REVIEW_STUB_REPLY": valid_reply, "REVIEW_STUB_EXIT": "7"}, "status 7"),
                 ({"REVIEW_STUB_REPLY": "Finding: parser rejected this reply\nnot a verdict"}, "review produced no verdict; rerun `task review`"),
                 ({"REVIEW_STUB_REPLY": valid_reply.replace(head, "a" * 40)}, "review produced no verdict; rerun `task review`"))
        for previous in (False, True):
            if previous:
                self.prepare_merge_evidence("5")
            before = {name: (self.state() / name).read_bytes() if (self.state() / name).exists() else None
                      for name in ("review.json", "review.md", "events.json")}
            for env, message in cases:
                with self.subTest(previous=previous, message=message):
                    out = self.raw_task("review", "proj", "5", "--run", code=1, env=dict(self.env, **env))
                    self.assertIn(message, out)
                    if message.startswith("review produced no verdict"):
                        self.assertNotIn("--- reviewer reply ---", out)
                        self.assertNotIn("Finding: parser rejected this reply", out)
                    after = {name: (self.state() / name).read_bytes() if (self.state() / name).exists() else None
                             for name in before}
                    self.assertEqual(after, before)

    def test_review_run_and_record_are_mutually_exclusive(self):
        out = self.raw_task("review", "proj", "5", "--run", "--record", "reply.md", code=2)
        self.assertIn("not allowed with argument --run", out)
        self.assertFalse(self.state().exists())

    def test_record_uses_routed_identity_despite_openai_gpt6_reply_line(self):
        wt, _ = self.started()
        self.raw_task("review", "proj", "5")
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "misnamed-openai.md"
        review.write_text(f"Verdict: PASS\nCommit: {head}\nReviewer: OpenAI GPT-6\n"
                          "#1 pass: evidence\n#2 pass: evidence\n")
        self.raw_task("review", "proj", "5", "--record", str(review))
        metadata = __import__("json").loads((self.state() / "review.json").read_text())
        self.assertEqual((metadata["reviewer"], metadata["model"]), ("claude", "haiku"))

    def test_record_uses_codex_route_despite_gpt6_1_sol_reply_line(self):
        self.raw_task("start", "proj", "5", "--builder", "claude")
        wt = self.repo / ".worktrees/t5"
        self.raw_task("review", "proj", "5")
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "misnamed-codex.md"
        review.write_text(f"Verdict: PASS\nCommit: {head}\nReviewer: codex gpt-6.1-sol\n"
                          "#1 pass: evidence\n#2 pass: evidence\n")
        self.raw_task("review", "proj", "5", "--record", str(review))
        metadata = __import__("json").loads((self.state() / "review.json").read_text())
        self.assertEqual((metadata["reviewer"], metadata["model"]), ("codex", "gpt-6-luna"))

    def test_manual_record_requires_reviewer_and_checks_vendor_rule(self):
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "manual-review.md"
        review.write_text(f"Verdict: PASS\nCommit: {head}\n#1 pass: evidence\n#2 pass: evidence\n")
        out = self.raw_task("review", "proj", "5", "--record", str(review), code=1)
        self.assertIn("no stored reviewer routing", out)
        self.raw_task("review", "proj", "5", "--record", str(review), "--reviewer", "claude", "manual")
        meta = __import__("json").loads((self.state() / "review.json").read_text())
        self.assertEqual((meta["reviewer"], meta["model"]), ("claude", "manual"))
        out = self.raw_task("review", "proj", "5", "--record", str(review),
                            "--reviewer", "codex", "manual", code=1)
        self.assertIn("reviewer vendor must differ from builder", out)

    def test_builder_vendor_in_reply_line_warns_but_routed_identity_is_recorded(self):
        wt, _ = self.started()
        self.raw_task("review", "proj", "5")
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "builder-identity.md"
        review.write_text(f"Verdict: PASS\nCommit: {head}\nReviewer: codex gpt-6.1-sol\n"
                          "#1 pass: evidence\n#2 pass: evidence\n")
        out = self.raw_task("review", "proj", "5", "--record", str(review))
        self.assertIn("WARNING: reply Reviewer: line names builder vendor 'codex'", out)
        meta = __import__("json").loads((self.state() / "review.json").read_text())
        self.assertEqual((meta["reviewer"], meta["model"]), ("claude", "haiku"))

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
        (archive / "review.md").write_text(
            "Verdict: PASS\n#1 pass: [absolute](/home/agent/report.md) "
            "[file](file:///tmp/review.md) [web](https://example.com/review) "
            "[relative](../report.md) evidence inspected\n")
        (archive / "report.md").write_text("## Noticed, not done\n\n- Slow startup could be improved\n"
                                           "- Inspect [source](/abs/path) and [report](file:///tmp/report.md) "
                                           "or [web](https://example.com).\n")
        out = self.task("close", "proj", "T-5", "-m", "Built b; check green.", "--retro",
                        "went well: evidence / went wrong: none / change: keep it", "--partial")
        self.assertIn("1 criteria ticked", out)
        text = next((self.kb / "backlog/tasks").glob("t-5 *")).read_text()
        self.assertIn("status: In Progress", text)
        self.assertIn("- [x] #1", text)
        self.assertIn("- [ ] #2", text)
        self.assertIn("Built b; check green.", text)
        self.assertIn("## Review evidence", text)
        self.assertIn("#1 pass: absolute file [web](https://example.com/review) "
                      "[relative](../report.md) evidence inspected", text)
        self.assertNotIn("/home/agent/report.md", text)
        self.assertNotIn("file:///tmp/review.md", text)
        self.assertIn("[web](https://example.com/review)", text)
        self.assertIn("[relative](../report.md)", text)
        self.assertEqual(self.git(self.kb, "log", "-1", "--format=%s"), "Close T-5: Add the b file")
        self.assertIn("Slow startup", (self.kb / "improvements.md").read_text())
        self.assertIn("Inspect `/abs/path` and `/tmp/report.md` or [web](https://example.com).",
                      (self.kb / "improvements.md").read_text())
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
        close_path = TASK_MODULE.close_worktree_path(self.kb, "task-close-t5")
        self.git(self.kb, "worktree", "add", "-q", "-b", "task-close-t5", str(close_path), "main")
        (close_path / ".work").mkdir()
        (close_path / ".work/evidence").write_text("before close reset and clean")
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine")
        self.assertIn("T-5 was already closed.", out)
        self.assertEqual(list((self.kb / ".worktrees").glob("task-close-*")), [])
        saved = list(self.work_archives().glob("*/.work/evidence"))
        self.assertEqual([path.read_text() for path in saved], ["before close reset and clean"])

    def test_exclusive_gate_serializes_checks_and_wait_does_not_use_timeout(self):
        lock_path = self.gate_dir / "agent-task-gate.lock"
        holder_path = self.gate_dir / "agent-task-gate.holder"
        self.addCleanup(lambda: holder_path.unlink(missing_ok=True))
        (self.repo / ".task.toml").write_text("check_timeout = 3\n")
        self.git(self.repo, "add", ".task.toml")
        self.git(self.repo, "commit", "-qm", "enable exclusive gate")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.started()
        other = self.repo / ".worktrees/other"
        self.git(self.repo, "worktree", "add", "-q", "--no-track", "-b", "other", str(other), "main")
        (other / ".task.toml").write_text("check_timeout = 1\n")
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
        (nonexclusive / ".task.toml").write_text("exclusive_gate = false\ncheck_timeout = 1\n")
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
        env = dict(self.env, TASK_GATE_DIR=str(self.home / "missing" / "gate"))
        out = self.raw_task("check", "proj", "5", env=env)
        self.assertEqual(out.count("Cannot open shared gate lock"), 1)
        self.assertIn("running trusted gate unlocked", out)
        self.assertIn("PASS", out)
        (wt / ".task.toml").write_text("exclusive_gate = true\n")
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "enable exclusive gate")
        env = dict(self.env, TASK_GATE_DIR=str(self.home / "missing" / "gate"))
        out = self.raw_task("check", "proj", "5", env=env, code=1)
        self.assertIn("Cannot open shared gate lock", out)
        self.assertNotIn("PASS", out)
        self.sandbox_state()
        out = self.raw_task("check", "proj", "5", env=env)
        self.assertIn("Cannot open shared gate lock", out)
        self.assertIn("running advisory check unlocked", out)
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
        self.assertIn(f"Work only in {self.repo / '.worktrees/tidy-docs'} on branch tidy-docs.", out)
        self.assertIn("`task lane proj tidy-docs`", out)

    def test_claude_dispatch_uses_task_lane(self):
        out = self.raw_task("start", "proj", "claude-lane", "--builder", "claude")
        self.assertIn(f"Work only in {self.repo / '.worktrees/claude-lane'} on branch claude-lane.", out)
        self.assertIn("`task lane proj claude-lane`", out)

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
            reviewer = ("codex", "test") if "Reviewer: codex" in content else ("claude", "test")
            out = self.raw_task("review", "proj", "5", "--record", str(review_file),
                                "--reviewer", *reviewer, code=1)
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
        self.assertIn('passed', self.raw_task('wait', 'proj', '5', '--max', '0.05'))
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
        self.assertEqual(sorted(p.name for p in (wt / ".work").iterdir()), ["context", "report.md"])
        self.assertFalse((wt / ".work/task.json").exists())
        (wt / ".work/leak.json").write_text("{}")
        self.git(wt, "add", "-f", ".work/leak.json")
        self.git(wt, "commit", "-qm", "track private evidence")
        message = self.raw_task("check", "proj", "5", code=1)
        self.assertIn(
            "Tracked files under .work/ (.work/leak.json): run `git rm --cached -r .work` "
            "and commit, then rerun `task check`. Never `git add -f` under .work/.",
            message,
        )

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
        self.raw_task("review", "proj", "5", "--record", str(evidence), "--reviewer", "claude", "test")
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
        self.assertIn("Recorded PASS", self.raw_task("review", "proj", "5", "--record", str(review),
                                                     "--reviewer", "claude", "manual"))

    def test_review_record_accepts_bold_verdict_labels_and_backticked_sha(self):
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")[:9]
        review = self.home / "review-format.txt"
        for verdict in ("**Verdict:** PASS", "Verdict: **PASS**"):
            review.write_text(f"{verdict}\nCommit: `{head}`\nReviewer: claude format-test\n"
                              "#1 pass: reviewed source\n#2 pass: ran acceptance evidence\n")
            out = self.raw_task("review", "proj", "5", "--record", str(review),
                                "--reviewer", "claude", "manual")
            self.assertIn("Recorded PASS", out, verdict)

    def test_real_markdown_review_fixtures_parse_verdict_commit_and_only_stated_judgments(self):
        cases = {
            "haiku-1.md": ([1, 2, 3, 4, 5], "FAIL", "93b8888", "claude",
                           {1: "pass", 2: "pass", 3: "pass", 4: "pass", 5: "fail"}),
            "haiku-2.md": ([1, 2, 3, 4, 5], "FAIL", "298aed910b35f0da8f54052574b567ce9c03b640", "claude",
                           {1: "pass", 2: "pass", 3: "pass", 4: "pass", 5: "pass"}),
            "luna-1.md": (list(range(1, 9)), "FAIL", "c3c5dac9f337d6f5ad37bc677ecaf6c9d596a1de", "codex",
                          {1: "pass", 2: "pass", 3: "fail", 4: "pass", 5: "pass", 6: "pass", 7: "pass", 8: "pass"}),
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
1. **Pass, per report:** numbered evidence
| 2 | fail | table evidence |
"""
        verdict, _, _, judgments = TASK_MODULE.parse_review_reply(text, [1, 2])
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(judgments, {1: ("pass", "per report: numbered evidence"),
                                     2: ("fail", "table evidence")})

    def test_review_parser_requires_exact_table_judgment_cell(self):
        text = """Verdict: FAIL
Commit: abcdef0123456789
Reviewer: codex gpt-6-luna
| 1 | Fail safe default is missing | not a judgment |
| 2 | fail | exact judgment cell |
"""
        _, _, _, judgments = TASK_MODULE.parse_review_reply(text, [1, 2])
        self.assertEqual(judgments, {2: ("fail", "exact judgment cell")})

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
3. Fail safe default is missing
4. Pass criteria are met
5. pass: colon evidence
6. fail — spaced dash evidence
"""
        _, _, _, judgments = TASK_MODULE.parse_review_reply(text, [1, 2, 3, 4, 5, 6])
        self.assertEqual(judgments, {2: ("fail", ""), 5: ("pass", "colon evidence"),
                                     6: ("fail", "spaced dash evidence")})

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
        self.assertIn("Recorded FAIL", self.raw_task("review", "proj", "haiku-3", "--record", str(record),
                                                      "--reviewer", "claude", "haiku-5-5"))
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

    def test_fenced_review_parser_accepts_no_reviewer_line_with_no_criteria(self):
        verdict, commit, reviewer, judgments = TASK_MODULE.parse_review_reply(
            "```review\nVerdict: PASS\nCommit: " + "a" * 40 + "\n```\n", [])
        self.assertEqual((verdict, commit, reviewer, judgments), ("PASS", "a" * 40, None, {}))

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
        self.raw_task("review", "proj", "5", "--record", str(review), "--reviewer", "claude", "manual")
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
        out = self.raw_task("review", "proj", "5", "--record", str(review),
                            "--reviewer", "claude", "manual", code=1)
        self.assertIn("review block", out)

    def test_review_record_rejects_duplicate_judgments_with_criterion_number(self):
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        review = self.home / "duplicate-review.md"
        review.write_text(f"Verdict: FAIL\nCommit: {head}\nReviewer: claude duplicate-test\n"
                          "#1 pass: first evidence\n#1 fail: conflicting evidence\n#2 pass: evidence\n")
        out = self.raw_task("review", "proj", "5", "--record", str(review),
                            "--reviewer", "claude", "manual", code=1)
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
