#!/usr/bin/env python3
"""task lifecycle on real git repositories in a temporary HOME.

`just`, `backlog` and `gh` are small stand-ins on PATH so the test runs anywhere: `just check`
passes unless the worktree holds a file named FAIL; `backlog task edit` applies status, ticks and
final summary the way Backlog.md does; `gh pr list` finds no PR.
"""
import fcntl
import atexit
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import socket
import time
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from policy_test_env import isolate_policy_environment

# Keep only explicit test selection and trusted-gate socket-test semantics.
isolate_policy_environment("TASK_TEST_TOOL", "TASK_GATE_RUN_ID")

audit_state = tempfile.TemporaryDirectory(prefix="agent-policy-test-")
atexit.register(audit_state.cleanup)
os.environ["AGENT_POLICY_STATE_DIR"] = audit_state.name

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("TASK_TEST_TOOL", ROOT / "home/dot_local/bin/executable_task"))
REVIEW_FIXTURES = ROOT / "ci/fixtures/reviews"
SPARE_HELD = "spare reset held; reset at 100 %"
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
    'tmpdir': os.environ.get('TMPDIR'),
    'tmp_mode': pathlib.Path(os.environ['TMPDIR']).stat().st_mode & 0o7777,
    'tmp_owner': pathlib.Path(os.environ['TMPDIR']).stat().st_uid,
    'task_worktree': os.environ.get('TASK_WORKTREE'),
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
elif '--output-format' in sys.argv:
    reset = int(time.time()) + 3 * 86400
    window = {'utilization': 0.4, 'resetsAt': reset}
    print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
        'rateLimitType': 'seven_day', **window, 'unifiedWindows': {'five_hour': {**window, 'utilization': 0.1}, 'seven_day': window}}}))
    print(json.dumps({'type': 'result', 'result': 'claude-final: ' + prompt}))
    print('claude-log', file=sys.stderr)
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
with (work / 'reviewer-calls.jsonl').open('a') as calls:
    calls.write(json.dumps(sys.argv[1:]) + '\\n')
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
print(os.environ.get('REVIEW_STUB_STDERR', ''), end='', file=sys.stderr, flush=True)
time.sleep(float(os.environ.get('REVIEW_STUB_DELAY', '0')))
code = os.environ.get('REVIEW_STUB_EXIT', '0')
sys.exit(int(os.environ.get('REVIEW_STUB_FIRST_EXIT', code) if count == 1 else code))
"""


class TaskTool(unittest.TestCase):
    def test_adaptive_check_timeout_uses_trusted_passes_only(self):
        repo = self.home / 'dev_workspace/proj'
        worktree = repo / '.worktrees/t1'
        worktree.mkdir(parents=True)
        state = self.home / '.local/state/task/proj/t1'
        state.mkdir(parents=True)
        history = [
            {'trusted': True, 'result': 'passed', 'time': f'2026-10-21T00:0{i}:00+00:00', 'steps': [
                {'command': ['just', 'check'], 'duration': duration}]}
            for i, duration in enumerate([1100, 1000, 950, 800, 700])]
        history += [{'trusted': True, 'result': 'failed', 'time': '9', 'steps': [
            {'command': ['just', 'check'], 'duration': 9999}]},
            {'trusted': False, 'result': 'passed', 'time': '10', 'steps': [
                {'command': ['just', 'check'], 'duration': 9999}]}]
        (state / 'checks.jsonl').write_text('\n'.join(json.dumps(row) for row in history))
        (worktree / '.task.toml').write_text('check_timeout = 1200\n')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            configured, effective, used = TASK_MODULE.step_timeout(worktree, ['just', 'check'])
            self.assertEqual((configured, effective, used), (1200, 2200, [1100, 1000, 950, 800, 700]))
            self.assertIn('median 950 s', TASK_MODULE.gate_warning(worktree))
            (worktree / '.task.toml').write_text('check_timeout = 1300\n')
            self.assertEqual(TASK_MODULE.gate_warning(worktree), '')
        (state / 'checks.jsonl').unlink()
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            self.assertEqual(TASK_MODULE.step_timeout(worktree, ['just', 'check']), (1300, 1300, []))

    def test_task_check_uses_trusted_history_from_main_repo(self):
        wt, _ = self.started()
        (wt / '.task.toml').write_text('check_timeout = 1\n')
        self.git(wt, 'add', '.task.toml')
        self.git(wt, 'commit', '-qm', 'set short check timeout')
        state = self.state()
        state.mkdir(parents=True, exist_ok=True)
        rows = [{'trusted': True, 'result': 'passed', 'time': f'2026-10-21T00:0{i}:00+00:00',
                 'steps': [{'command': ['just', 'check'], 'duration': 2}]} for i in range(5)]
        (state / 'checks.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
        script = self.home / 'during-check.sh'
        script.write_text('#!/bin/sh\nsleep 1.2\n')
        script.chmod(0o755)
        out = self.raw_task('check', cwd=wt)
        self.assertIn('PASS (log:', out)
        evidence = json.loads((state / 'check.json').read_text())
        step = evidence['steps'][0]
        self.assertEqual((step['configured_timeout'], step['effective_timeout']), (1, 4))
        self.assertEqual(step['history_durations'], [2, 2, 2, 2, 2])

    def test_cumulative_report_uses_only_final_criteria_block(self):
        criteria = [{"number": 1}, {"number": 2}]
        old = "## Criteria\n#1 not met: failing check\n#2 not met: missing work\n"
        final = "## Criteria\n#1 met: regression check passes\n#2 met: seed verified\n"
        self.assertEqual(TASK_MODULE.report_criteria(old + final, criteria), final.split('\n', 1)[1])
        for tail in ("", "## Criteria", "## Criteria\n", "## Criteria\n#1 met: verified\n",
                     "## Criteria\n#1 met: verified\n#2 not met: failed\n"):
            with self.subTest(tail=tail), self.assertRaises(TASK_MODULE.Stop):
                TASK_MODULE.report_criteria(final + tail if tail else "", criteria)

    def test_main_only_catches_routing_parse_errors(self):
        with mock.patch.object(TASK_MODULE, 'foreground_lock', return_value=None), \
                mock.patch.object(TASK_MODULE, 'finish_detached'), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            with mock.patch.object(TASK_MODULE, 'status', side_effect=TASK_MODULE.RoutingParseError('bad policy')):
                self.assertEqual(TASK_MODULE.main(['status', 'proj']), 1)
                self.assertIn('bad policy', errors.getvalue())
            with mock.patch.object(TASK_MODULE, 'status', side_effect=ValueError('internal bug')):
                with self.assertRaisesRegex(ValueError, 'internal bug'):
                    TASK_MODULE.main(['status', 'proj'])

    def test_concurrent_starts_serialize_on_repository_lock(self):
        lock_path = self.repo / '.git/task-start.lock'
        children = []
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                for ident in ('parallel-one', 'parallel-two'):
                    children.append(subprocess.Popen(
                        [sys.executable, str(TOOL), 'start', 'proj', ident, '--builder', 'codex'],
                        env=self.env, cwd=self.home, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True))
                # Both real commands must wait for this repository's start lock.
                for child in children:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.communicate(timeout=0.25)
                self.assertFalse((self.repo / '.worktrees/parallel-one').exists())
                self.assertFalse((self.repo / '.worktrees/parallel-two').exists())
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            for child, ident in zip(children, ('parallel-one', 'parallel-two')):
                output, _ = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, output)
                wt = self.repo / '.worktrees' / ident
                self.assertEqual(self.git(wt, 'branch', '--show-current'), ident)
                self.assertEqual(self.git(wt, 'rev-parse', 'HEAD'), self.git(self.repo, 'rev-parse', ident))
                self.assertTrue((wt / '.work/context').is_dir())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_runner_tier_only_and_documented_dependency_example(self):
        runner = self.home / 'runner.md'
        runner.write_text('1. prepare: builder codex, tier routine\n'
                          '2. implement: builder codex, tier hard, after: prepare\n')
        self.assertIn('runner file OK (2 tasks)', self.raw_task('runner-check', 'proj', str(runner)))
        runner.write_text('1. missing: builder codex\n')
        self.assertIn('model or tier', self.raw_task('runner-check', 'proj', str(runner), code=1))

    def test_unboosted_route_notice_and_tier_only_update_message(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('Implement task')
        out = self.task('lane', 'proj', '5', '--tier', 'hard')
        self.wait_for_lane(wt)
        self.assertIn('route: hard -> codex gpt-6.1-sol', out)
        self.assertIn('Updating task tier to hard.', out)
        self.assertNotIn('Updating task builder', out)

    def test_quality_dispatch_brief_and_evidence_instruction(self):
        task = self.kb / "backlog/tasks/t-5 - Add-the-b-file.md"
        task.write_text(task.read_text() + "\n## Read first\n\na.txt and justfile\n\n"
                        "## Test against:\n\nci/fixtures/reviews/haiku-3-fenced.md\n")
        wt, dispatch = self.started()
        self.assertIn("Read first:\na.txt and justfile", dispatch)
        self.assertIn("Test against:\nci/fixtures/reviews/haiku-3-fenced.md", dispatch)
        self.assertIn("PASS/FAIL plus the log path", dispatch)
        self.assertIn("under `## Check`", dispatch)
        self.assertIn("## Check", (wt / '.work/report.md').read_text())
        direct = self.raw_task('start', 'proj', 'direct', '--builder', 'codex')
        self.assertIn('Read first:\n(PM: none given)', direct)
        self.assertIn('Test against:\n(PM: none given)', direct)
        request = self.repo / '.worktrees/direct/.work/request.md'
        request.write_text('Read first: a.txt\n  justfile\nTest against: real fixture\n')
        direct = self.raw_task('start', 'proj', 'direct', '--builder', 'codex')
        self.assertIn('Read first:\na.txt\n  justfile', direct)
        self.assertIn('Test against:\nreal fixture', direct)

    def test_quality_review_check_guard_and_override(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.assertIn('run `task check` first', self.raw_task('review', 'proj', '5', '--run', code=1))
        self.assertFalse((wt / '.work/reviewer-call.json').exists())
        self.raw_task('check', 'proj', '5')
        self.raw_task('start', 'proj', '5', '--refresh', '--builder', 'codex')
        self.assertIn('run `task check` first', self.raw_task('review', 'proj', '5', '--run', code=1))
        self.commit_in(wt, 'b.txt')
        self.assertIn('run `task check` first', self.raw_task('review', 'proj', '5', '--run', code=1))
        self.commit_in(wt, 'FAIL')
        self.raw_task('check', 'proj', '5', code=1)
        self.assertIn('run `task check` first', self.raw_task('review', 'proj', '5', '--run', code=1))
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: FAIL\nCommit: {head}\n#1 pass: b exists\n#2 fail: gate failed\n'
        self.raw_task('review', 'proj', '5', '--run', '--no-check', 'Inspect failing gate',
                      env=dict(self.env, REVIEW_STUB_REPLY=reply))
        meta = json.loads((self.state() / 'review.json').read_text())
        self.assertEqual(meta['no_check'], 'Inspect failing gate')
        self.assertEqual(json.loads((self.state() / 'review-overrides.jsonl').read_text())['head'], head)
        prompt = json.loads((wt / '.work/reviewer-call.json').read_text())['prompt']
        self.assertIn('Verdict: FAIL; head: ' + head, prompt)
        self.assertIn('FAILED (failures=1)', prompt)
        self.assertIn('file:line', prompt)
        checks = [json.loads(line) for line in (self.state() / 'checks.jsonl').read_text().splitlines()]
        self.assertEqual([item['result'] for item in checks], ['passed', 'failed'])

    def test_quality_history_rereview_and_check_precedence(self):
        wt, _ = self.started()
        advisory = dict(self.env, CODEX_SANDBOX='1')
        state_root = self.sandbox_state()
        output = self.raw_task('check', cwd=wt, env=advisory)
        state_root.chmod(0o755)
        self.assertTrue(output.rstrip().splitlines()[-1].startswith('PASS (log: '))
        self.assertIn('trusted: False', self.raw_task('review', 'proj', '5'))
        self.raw_task('check', 'proj', '5')
        self.assertIn('trusted: True', self.raw_task('review', 'proj', '5'))
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = self.home / 'review.md'
        fixture = (REVIEW_FIXTURES / 'quality-rereview.md').read_text().replace('HEAD_PLACEHOLDER', head)
        reply.write_text(fixture)
        self.raw_task('review', 'proj', '5', '--record', str(reply), '--reviewer', 'claude', 'haiku')
        first = (self.state() / 'review-1.md').read_bytes()
        self.commit_in(wt, 'b.txt')
        prompt = self.raw_task('review', 'proj', '5')
        self.assertIn('Previous review Blocking list:\n- #1: b.txt:1', prompt)
        self.assertIn('diff --git a/b.txt b/b.txt', prompt)
        self.assertIn('new non-defect findings on code unchanged', prompt)
        self.assertIn('Recorded check: missing', prompt)
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply.write_text(f'Verdict: PASS\nCommit: {head}\n#1 pass: b exists\n#2 pass: checked\n')
        self.raw_task('check', 'proj', '5')
        self.raw_task('review', 'proj', '5', '--record', str(reply), '--reviewer', 'claude', 'haiku')
        self.assertEqual((self.state() / 'review-1.md').read_bytes(), first)
        self.assertTrue((self.state() / 'review-2.md').exists())
        self.raw_task('start', 'proj', '5', '--refresh', '--builder', 'codex')
        self.assertIn('Diff since previously reviewed head', self.raw_task('review', 'proj', '5'))
        self.assertEqual(len((self.state() / 'checks.jsonl').read_text().splitlines()), 2)
        self.assertEqual(len((wt / '.work/checks.jsonl').read_text().splitlines()), 1)
        (self.state() / 'merge.json').write_text(json.dumps(dict(time=TASK_MODULE.utc_now())))
        self.assertIn('quality: collecting (1 tasks with review history since ',
                      self.raw_task('status', 'proj'))
        archive = self.home / '.local/state/task/archive/proj/t5/snapshot/.work'
        shutil.copytree(self.state(), archive)
        self.assertIn('quality: collecting (1 tasks with review history since ',
                      self.raw_task('status', 'proj'))
        shutil.rmtree(self.state())
        self.assertIn('quality: collecting (1 tasks with review history since ',
                      self.raw_task('status', 'proj'))

    def test_quality_review_gate_and_prompt_check_precedence(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.raw_task('check', 'proj', '5')
        trusted_file = self.state() / 'check.json'
        base = json.loads(trusted_file.read_text())
        head = base['sha']
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: fixture\n#2 pass: fixture\n'
        for trusted, older, newest, allowed in (
                ('passed', None, 'failed', True),
                ('failed', None, 'passed', False),
                (None, 'passed', 'failed', False),
                (None, 'failed', 'passed', True)):
            with self.subTest(trusted=trusted, older=older, newest=newest):
                trusted_file.unlink(missing_ok=True)
                if trusted:
                    trusted_file.write_text(json.dumps(dict(
                        base, result=trusted, time='2026-10-19T10:00:00+00:00',
                        summary_tail='trusted evidence')))
                advisory_history = []
                for hour, result in ((11, older), (12, newest)):
                    if result:
                        evidence = dict(base, result=result, trusted=False, advisory=True,
                                        log=str(wt / '.work/check.log'),
                                        time=f'2026-10-19T{hour}:00:00+00:00',
                                        summary_tail=f'advisory {result} evidence')
                        advisory_history.append(json.dumps(evidence))
                        (wt / '.work/check.json').write_text(json.dumps(evidence))
                (wt / '.work/checks.jsonl').write_text('\n'.join(advisory_history) + '\n')
                prompt = self.raw_task('review', 'proj', '5')
                verdict = 'PASS' if allowed else 'FAIL'
                self.assertIn(f'Verdict: {verdict}; head: {head}; trusted: {bool(trusted)}', prompt)
                self.assertIn('trusted evidence' if trusted else f'advisory {newest} evidence', prompt)
                expected_log = self.state() / 'check.log' if trusted else wt / '.work/check.log'
                self.assertIn(f'Log: {expected_log}', prompt)
                call = wt / '.work/reviewer-call.json'
                call.unlink(missing_ok=True)
                output = self.raw_task('review', 'proj', '5', '--run',
                                       code=0 if allowed else 1,
                                       env=dict(self.env, REVIEW_STUB_REPLY=reply))
                self.assertEqual(call.exists(), allowed)
                if allowed:
                    run_prompt = json.loads(call.read_text())['prompt']
                    self.assertIn(f'Verdict: PASS; head: {head}; trusted: {bool(trusted)}', run_prompt)
                else:
                    self.assertIn('run `task check` first', output)

    def quality_fixture(self, name, verdicts=(), merge='events'):
        # Mirror the supplied real layout: Markdown reviews survive without JSON
        # sidecars; events.json survives even where merge.json is absent.
        state = self.state(name)
        state.mkdir(parents=True, exist_ok=True)
        (state / 'task.json').write_text('{}')
        latest = '```review\nVerdict: PASS\nCommit: fixture-head\n```\n\n**Blocking**\n\nNone.\n'
        for filename in ('review.md', 'review-response.md', 'review-prompt.md'):
            (state / filename).write_text(latest)
        (state / 'events.json').write_text(json.dumps({
            'check': '2026-10-19T11:34:19.319446+00:00',
            'review': '2026-10-19T14:44:45.870047+00:00',
            **({'merge': '2026-10-19T15:19:59.010637+00:00'} if merge == 'events' else {})}))
        if merge == 'record':
            (state / 'merge.json').write_text(json.dumps({
                'commit': 'merged-head', 'head': 'fixture-head',
                'time': '2026-10-19T16:07:27.918904+00:00'}))
        for number, verdict in enumerate(verdicts, 1):
            (state / f'review-{number}.md').write_text(
                f'Verdict: {verdict}\nHead: fixture-head\n'
                f'Time: 2026-09-01T12:00:{number:02d}+00:00\n\n{latest}')
        if verdicts:
            (state / 'checks.jsonl').write_text('{"result":"passed"}\n' * 3)
        return state

    def quality_output(self):
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            return TASK_MODULE.quality_summary(Path('proj'))

    def test_quality_metric_legacy_only_tasks_are_excluded(self):
        for number in range(6):
            state = self.quality_fixture(f'legacy-{number}')
            (state / 'review.json').write_text(json.dumps(dict(
                time=TASK_MODULE.utc_now(), verdict='PASS', commit='latest')))
        self.assertEqual(self.quality_output(),
                         'quality: collecting (0 tasks with review history since unknown)')

    def test_quality_metric_collecting_state(self):
        for number in range(4):
            self.quality_fixture(f'numbered-{number}', ['FAIL', 'PASS'])
            self.assertEqual(self.quality_output(),
                             f'quality: collecting ({number + 1} tasks with review history since 2026-09-01)')
        state = self.quality_fixture('malformed')
        (state / 'review-1.md').write_text('Verdict: PASS\nTime: invalid\n')
        (state / 'review-1.json').write_text('{broken')
        self.assertIn('collecting (4 tasks', self.quality_output())

    def test_quality_metric_correct_rounds_count(self):
        for name, verdicts, merge in (
                ('events', ['FAIL', 'PASS'], 'events'),
                ('record', ['FAIL', 'FAIL', 'PASS'], 'record'),
                ('single', ['PASS'], 'events'),
                ('open', ['PASS'], None),
                ('before-window', ['PASS'], 'record')):
            state = self.quality_fixture(name, verdicts, merge)
        (state / 'merge.json').write_text('{"time":"2026-08-01T00:00:00+00:00"}')
        # Both merge sources and repeated snapshots must still count each task once.
        (self.state('record') / 'events.json').write_text(
            '{"merge":"2026-10-19T15:19:59+00:00"}')
        expected = ('quality (since 2026-09-01): first-review pass 3/5 (60%), '
                    'reviews per merged task 2.0')
        self.assertEqual(self.quality_output(), expected)
        for name in ('events', 'record', 'single', 'open', 'before-window'):
            for snapshot in ('snapshot-1', 'snapshot-2'):
                archive = self.home / '.local/state/task/archive/proj' / name / snapshot / '.work'
                shutil.copytree(self.state(name), archive)
        self.assertEqual(self.quality_output(), expected)
        for name in ('events', 'record', 'single', 'open', 'before-window'):
            shutil.rmtree(self.state(name))
        self.assertEqual(self.quality_output(), expected)

    def test_quality_metric_mixed_legacy_and_numbered_history(self):
        for number in range(8):
            self.quality_fixture(f'legacy-{number}')
        for number in range(5):
            state = self.quality_fixture(f'numbered-{number}',
                                         ['FAIL', 'PASS'] if number < 3 else ['PASS'], None)
            # A latest JSON pointer must not alter the numbered history.
            (state / 'review.json').write_text(json.dumps(dict(
                time='2026-08-01T00:00:00+00:00', verdict='PASS', commit='latest')))
        self.assertEqual(self.quality_output(),
                         'quality (since 2026-09-01): first-review pass 2/5 (40%), '
                         'reviews per merged task n/a')

    def test_gate_drill_signals_clean_children_worktrees_and_reservations(self):
        just = self.home / 'bin/just'
        for sig in (signal.SIGTERM, signal.SIGINT):
            for phase in ('group', 'solo'):
                with self.subTest(signal=sig, phase=phase):
                    marker = self.home / 'sleeping-gate.json'
                    marker.unlink(missing_ok=True)
                    just.write_text(f'#!{sys.executable}\n' + textwrap.dedent(f'''\
                        import json, os, pathlib, signal, subprocess, time
                        if not pathlib.Path.cwd().name.endswith('-{phase}'):
                            raise SystemExit(0)
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                        child = subprocess.Popen(['sleep', '60'])
                        pathlib.Path({str(marker)!r}).write_text(json.dumps(
                            [os.getpid(), child.pid, os.environ['TMPDIR']]))
                        time.sleep(60)
                        '''))
                    just.chmod(0o755)
                    process = subprocess.Popen([sys.executable, str(TOOL), 'gate-drill', 'proj'],
                                               env=self.env, cwd=self.home, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, text=True)
                    pids = []
                    try:
                        deadline = time.monotonic() + 10
                        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(marker.exists(), 'sleeping gate never started')
                        leader, child, temporary = json.loads(marker.read_text())
                        pids = [leader, child]
                        process.send_signal(sig)
                        output, _ = process.communicate(timeout=10)
                        self.assertNotEqual(process.returncode, 0, output)
                        self.assertIn('Drill interrupted', output)
                        for pid in pids:
                            status = Path(f'/proc/{pid}/stat')
                            self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z',
                                            f'gate process {pid} survived')
                        self.assertFalse(Path(temporary).exists())
                        self.assertEqual(list((self.repo / '.worktrees').glob('.*drill-*')), [])
                        self.assertNotIn('.drill-', self.git(self.repo, 'worktree', 'list'))
                        locks = list(self.gate_dir.glob('*.lock'))
                        self.assertTrue(locks, 'gate did not reserve a port index')
                        for lock in locks:
                            with lock.open('r') as stream:
                                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)
                        for pid in pids:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

    def test_status_bad_config_does_not_hide_other_repos(self):
        good = self.home / 'dev_workspace/z-good'
        good.mkdir()
        self.git(good, 'init', '-q', '-b', 'main')
        (good / '.task.toml').write_text('[env]\nPRODUCT_DIR = "do-not-display"\n')
        for config in (b'[env', b'[env]\nAPI_TOKEN = "do-not-display"\n', b'\xff'):
            with self.subTest(config=config):
                (self.repo / '.task.toml').write_bytes(config)
                output = self.raw_task('status')
                self.assertIn('env keys: unavailable (', output)
                self.assertIn('z-good:\n  env keys: PRODUCT_DIR', output)
                self.assertNotIn('do-not-display', output)
                if config == b'\xff':
                    self.assertIn(f'Cannot read {self.repo / ".task.toml"}', output)
        config = self.repo / '.task.toml'
        config.unlink()
        config.mkdir()  # A real OSError from the newly added configuration read.
        output = self.raw_task('status')
        self.assertIn(f'env keys: unavailable (Cannot read {config}.)', output)
        self.assertIn('z-good:\n  env keys: PRODUCT_DIR', output)

    def test_configured_environment_rejects_reserved_names(self):
        names = TASK_MODULE.GIT_OVERRIDE_ENV | {'GIT_OVERRIDE_ENV', 'GIT_CONFIG', 'GIT_CONFIG_COUNT',
                                              'TASK_RUN_ID', 'TASK_RUN_ANY', 'TMPDIR', 'TASK_GATE_SLOT',
                                              'TASK_GATE_ANY'}
        for name in names:
            with self.subTest(name=name):
                (self.repo / '.task.toml').write_text(f'[env]\n{name} = "never-print-this"\n')
                with self.assertRaisesRegex(TASK_MODULE.Stop, 'reserved'):
                    TASK_MODULE.configured_environment(self.repo)

    def test_gate_drill_seeds_and_per_repo_failures(self):
        self.commit_in(self.repo, '.task.toml', 'drill_seeds = ["pinned"]\n[env]\nPRODUCT_DIR = "{repo}/data"\n')
        self.git(self.repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        work = self.repo / '.work'
        work.mkdir(exist_ok=True)
        (work / 'pinned').mkdir()
        (work / 'pinned/source').write_text('source bytes')
        (work / 'extra').write_text('extra bytes')
        (work / 'another').write_text('another bytes')
        just = self.home / 'bin/just'
        just.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
            import os, pathlib, sys
            if sys.argv[1:] != ['check']:
                sys.exit(1)
            assert pathlib.Path('.work/pinned/source').read_text() == 'source bytes'
            assert pathlib.Path('.work/extra').read_text() == 'extra bytes'
            assert pathlib.Path('.work/another').read_text() == 'another bytes'
            assert os.environ['PRODUCT_DIR'] == str(pathlib.Path(os.environ['HOME']) / 'dev_workspace/proj/data')
            print('OK')
            '''))
        just.chmod(0o755)
        self.raw_task('gate-drill', 'proj', '--seed', 'proj:extra', '--seed', 'proj:another')
        evidence = self.home / '.local/state/task/gate-drill'
        rows = json.loads(next(evidence.glob('*/results.json')).read_text())
        self.assertEqual(rows[0]['verdict'], 'ok')
        # Evidence identity must not depend on filesystem timestamp ordering.
        initial_result = next(evidence.glob('*/results.json'))
        future = time.time() + 3600
        os.utime(initial_result, (future, future))
        bad = self.home / 'dev_workspace/bad'
        bad.mkdir()
        self.git(bad, 'init', '-q', '-b', 'main')
        for config, reason in [('drill_seeds = ["absent"]', 'missing seed absent'),
                               ('drill_seeds = ["../escape"]', 'Invalid seed name'),
                               ('drill_seeds = "pinned"', 'list of seed names'),
                               ('drill_seeds = [2]', 'list of seed names'),
                               (b'\xff', 'Cannot read'),
                               (None, 'Cannot read')]:
            with self.subTest(config=config):
                if config is None:
                    (bad / '.task.toml').unlink()
                    (bad / '.task.toml').mkdir()
                    (bad / '.task.toml/entry').write_text('directory instead of config')
                    self.git(bad, 'add', '-A', '.task.toml')
                    self.git(bad, 'commit', '-qm', 'Unreadable config')
                elif isinstance(config, bytes):
                    (bad / '.task.toml').write_bytes(config)
                    self.git(bad, 'add', '.task.toml')
                    self.git(bad, 'commit', '-qm', 'Non-UTF-8 config')
                else:
                    self.commit_in(bad, '.task.toml', config + '\n')
                output = self.raw_task('gate-drill', 'bad', 'proj', '--seed', 'proj:extra',
                                       '--seed', 'proj:another', code=1)
                self.assertIn(reason, output)
                self.assertRegex(output, r'proj \| passed .* \| passed .* \| ok')
                self.assertEqual(len(self.git(bad, 'worktree', 'list').splitlines()), 1)
                archive = Path(next(line.removeprefix('Drill evidence: ') for line in output.splitlines()
                                    if line.startswith('Drill evidence: ')))
                rows = json.loads((archive / 'results.json').read_text())
                self.assertEqual([row['repo'] for row in rows], ['bad', 'proj'])
                self.assertEqual(rows[0]['concurrent']['result'], 'failed')
                self.assertEqual(rows[0]['solo']['result'], 'failed')
                if isinstance(config, bytes) or config is None:
                    for phase in ('concurrent', 'solo'):
                        self.assertIn('.task.toml', rows[0][phase]['error'])
                self.assertEqual(rows[1]['verdict'], 'ok')
        output = self.raw_task('gate-drill', 'proj', '--seed', 'proj:missing', code=1)
        self.assertIn('missing seed missing', output)
        (work / 'escape').symlink_to(self.home)
        output = self.raw_task('gate-drill', 'proj', '--seed', 'proj:escape', code=1)
        self.assertIn('Seed symlink escapes .work', output)
        self.assertEqual(len(self.git(self.repo, 'worktree', 'list').splitlines()), 1)

    def test_gate_drill_seed_read_errors_do_not_hide_other_repos(self):
        bad = self.home / 'dev_workspace/bad'
        bad.mkdir()
        self.git(bad, 'init', '-q', '-b', 'main')
        self.commit_in(bad, '.task.toml', 'drill_seeds = ["pinned"]\n')
        (bad / '.work').mkdir()
        seed = bad / '.work/pinned'
        seed.write_text('seed contents')
        copy = shutil.copy2
        for error in (OSError('seed read failed'), ValueError('seed read failed'),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')):
            with self.subTest(error=type(error).__name__):
                def copy_seed(source, destination, *args, **kwargs):
                    if Path(source) == seed:
                        raise error
                    return copy(source, destination, *args, **kwargs)

                output = io.StringIO()
                with mock.patch.dict(os.environ, self.env), \
                        mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                        mock.patch.object(TASK_MODULE, 'WORKSPACE', self.repo.parent), \
                        mock.patch.object(shutil, 'copy2', side_effect=copy_seed), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(TASK_MODULE.gate_drill(mock.Mock(repos=['bad', 'proj'], seed=[])), 1)
                self.assertRegex(output.getvalue(), r'proj \| passed .* \| passed .* \| ok')
                archive = Path(next(line.removeprefix('Drill evidence: ') for line in output.getvalue().splitlines()
                                    if line.startswith('Drill evidence: ')))
                rows = json.loads((archive / 'results.json').read_text())
                self.assertEqual([row['repo'] for row in rows], ['bad', 'proj'])
                for phase in ('concurrent', 'solo'):
                    self.assertEqual(rows[0][phase]['result'], 'failed')
                    self.assertIn('error', rows[0][phase])
                self.assertEqual(rows[1]['verdict'], 'ok')
                for repo in (bad, self.repo):
                    self.assertEqual(len(self.git(repo, 'worktree', 'list').splitlines()), 1)

    def test_configured_gate_environment(self):
        config = '[env]\nPRODUCT_DIR = "{repo}/data"\nWORK_DIR = "{worktree}/output"\n'
        self.commit_in(self.repo, '.task.toml', config)
        wt, _ = self.started()
        (wt / '.task.toml').write_text(config)
        script = self.home / 'during-check.sh'
        script.write_text('#!/bin/sh\nprintf "PRODUCT_DIR=%s\\nWORK_DIR=%s\\n" "$PRODUCT_DIR" "$WORK_DIR" > "$PWD/.work/observed-env"\n')
        script.chmod(0o755)
        for advisory in (False, True):
            with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, 'HOME', self.home):
                self.assertTrue(TASK_MODULE.run_check(wt, '', advisory=advisory))
            observed = (wt / '.work/observed-env').read_text()
            self.assertIn(f'PRODUCT_DIR={self.repo}/data', observed)
            self.assertIn(f'WORK_DIR={wt}/output', observed)
        lane = TASK_MODULE.lane_environment(TASK_MODULE.Task(self.repo, '5'))
        self.assertEqual(lane['WORK_DIR'], f'{wt}/output')
        status = self.task('status', 'proj')
        self.assertIn('env keys: PRODUCT_DIR, WORK_DIR', status)
        self.assertNotIn(str(self.repo / 'data'), status)
        for bad in ('TOKEN', 'api_key', 'SECRET', 'PASSWORD'):
            (wt / '.task.toml').write_text(f'[env]\n{bad} = "never-print-this"\n')
            with self.assertRaisesRegex(TASK_MODULE.Stop, 'secretspec'):
                TASK_MODULE.configured_environment(wt)
        (wt / '.task.toml').write_text('[env]\nCOUNT = 2\n')
        with self.assertRaisesRegex(TASK_MODULE.Stop, 'plain strings'):
            TASK_MODULE.configured_environment(wt)

    def test_gate_matrix_environment_evidence_and_selection(self):
        config = (ROOT / 'ci/fixtures/gate-matrix.toml').read_text()
        self.commit_in(self.repo, '.task.toml', config)
        wt, _ = self.started()
        (wt / '.task.toml').write_text(config)
        script = self.home / 'during-check.sh'
        script.write_text(
            '#!/bin/sh\n'
            'printf "%s|%s|%s|%s\\n" "$PH_PRODUCT_DIR" "$PH_PRODUCT_PYTHON" '
            '"$TMPDIR" "$TASK_GATE_SLOT" >> "$PWD/.work/matrix-observed"\n'
            'test ! -e "$TMPDIR/leftover" || touch FAIL\n'
            'touch "$TMPDIR/leftover"\n')
        script.chmod(0o755)
        for advisory in (False, True):
            observed = wt / '.work/matrix-observed'
            observed.unlink(missing_ok=True)
            output = io.StringIO()
            with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                    contextlib.redirect_stdout(output):
                self.assertTrue(TASK_MODULE.run_check(wt, '', state=self.state(), advisory=advisory))
            rows = [row.split('|') for row in observed.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], f'{self.repo}/.work/madplanner-a7dc343-ph')
            self.assertEqual(rows[1][0], f'{wt}/.work/fake')
            self.assertEqual(rows[1][1], f'{wt}/.work/fake/.venv/bin/python')
            self.assertNotEqual(rows[0][2], rows[1][2])
            self.assertEqual(rows[0][3], rows[1][3])
            self.assertTrue(all(not Path(row[2]).exists() for row in rows))
            destination = wt / '.work' if advisory else self.state()
            evidence = json.loads((destination / 'check.json').read_text())
            self.assertEqual([row['name'] for row in evidence['variants']], ['real', 'fake'])
            self.assertEqual(evidence['result'], 'passed')
            history = json.loads((destination / 'checks.jsonl').read_text().splitlines()[-1])
            self.assertEqual(history['variants'], evidence['variants'])
            self.assertIn('variant real: PASS', output.getvalue())
            self.assertIn('variant fake: PASS', output.getvalue())
        with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, 'HOME', self.home):
            self.assertTrue(TASK_MODULE.run_check(wt, '', advisory=True, variant='fake'))
            evidence = json.loads((wt / '.work/check.json').read_text())
            self.assertEqual([row['name'] for row in evidence['variants']], ['fake'])
            with self.assertRaisesRegex(TASK_MODULE.Stop, 'only available'):
                TASK_MODULE.run_check(wt, '', variant='fake')
            with self.assertRaisesRegex(TASK_MODULE.Stop, 'Unknown gate variant'):
                TASK_MODULE.run_check(wt, '', advisory=True, variant='missing')
        self.assertIn('gate variants: real, fake', self.task('status', 'proj'))

    def test_gate_matrix_failure_runs_remaining_variants(self):
        self.commit_in(self.repo, '.task.toml', (ROOT / 'ci/fixtures/gate-matrix.toml').read_text())
        wt, _ = self.started()
        (wt / '.task.toml').write_text((ROOT / 'ci/fixtures/gate-matrix.toml').read_text())
        script = self.home / 'during-check.sh'
        script.write_text('#!/bin/sh\ncase "$PH_PRODUCT_DIR" in\n'
                          '*/fake) rm -f FAIL;;\n*) touch FAIL;;\nesac\n')
        script.chmod(0o755)
        for name in ('check.json', 'gate-check.json', 'drill-check.json'):
            with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, 'HOME', self.home):
                self.assertFalse(TASK_MODULE.run_check(wt, '', state=self.state(), evidence_name=name,
                                                      pre_commit_all_files=name == 'drill-check.json'))
            evidence = json.loads((self.state() / name).read_text())
            self.assertEqual(evidence['result'], 'failed')
            self.assertEqual([row['result'] for row in evidence['variants']], ['failed', 'passed'])
            self.assertIn('=== variant real ===', (self.state() / Path(name).with_suffix('.log')).read_text())
            self.assertIn('=== variant fake ===', (self.state() / Path(name).with_suffix('.log')).read_text())

    def test_gate_matrix_validation(self):
        bad_configs = [
            'gate_variant = "fake"',
            '[[gate_variant]]\nname = ""',
            '[[gate_variant]]\nname = "fake"\n[[gate_variant]]\nname = "fake"',
            '[[gate_variant]]\nname = "fake"\nenv = "invalid"',
        ]
        for key in ('TMPDIR', 'TASK_GATE_SLOT', 'API_TOKEN', 'PASSWORD', 'TASK_RUN_ID'):
            bad_configs.append(f'[[gate_variant]]\nname = "fake"\n[gate_variant.env]\n{key} = "bad"')
        bad_configs.append('[[gate_variant]]\nname = "fake"\n[gate_variant.env]\nCOUNT = 2')
        for config in bad_configs:
            with self.subTest(config=config):
                (self.repo / '.task.toml').write_text(config)
                with self.assertRaises(TASK_MODULE.Stop):
                    TASK_MODULE.gate_variants(self.repo)

    def test_gate_matrix_timeout_keeps_failure_evidence(self):
        wt, _ = self.started()
        config = (ROOT / 'ci/fixtures/gate-matrix.toml').read_text().replace('2700', '1')
        (wt / '.task.toml').write_text(config)
        script = self.home / 'during-check.sh'
        script.write_text('#!/bin/sh\ncase "$PH_PRODUCT_DIR" in\n'
                          '*/fake) :;;\n*) sleep 10;;\nesac\n')
        script.chmod(0o755)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, 'HOME', self.home):
            self.assertFalse(TASK_MODULE.run_check(wt, '', state=self.state()))
        evidence = json.loads((self.state() / 'check.json').read_text())
        self.assertEqual(evidence['result'], 'failed')
        self.assertTrue(evidence['variants'][0]['steps'][0]['timeout'])
        self.assertEqual(evidence['variants'][1]['result'], 'passed')

    def test_decision_status_only_changes(self):
        name = 'backlog/decisions/d-status.md'
        (self.kb / 'backlog/decisions').mkdir()
        original = '---\nstatus: accepted\n---\n## Decision\nKeep the data.\n'
        self.commit_in(self.kb, name, original)
        self.task('plan', 'proj', '--sign')
        for revised, counted in (
                (original.replace('accepted', 'replaced'), False),
                (original + '*Replaced by decision-2*\n', False),
                (original.replace('Keep the data.', '~~Keep the data.~~'), False),
                (original.replace('## Decision', '## Other'), True),
                (original.replace('Keep the data.', 'Replaced by decision-2'), True),
                (original.replace('Keep the data.', 'Delete the data.'), True)):
            (self.kb / name).write_text(revised)
            self.assertEqual('decisions changed:' in self.task('plan', 'proj'), counted)
        (self.kb / name).write_text(original.replace('accepted', 'replaced'))
        self.git(self.kb, 'add', name)
        subprocess.run(['git', '-C', str(self.kb), 'commit', '-qm', 'status'],
                       env=dict(self.env, GIT_COMMITTER_DATE='2099-01-01T00:00:00+00:00'), check=True)
        self.assertNotIn('decisions changed:', self.task('plan', 'proj'))
        (self.kb / 'backlog/decisions/new.md').write_text('Replaced by decision-2\n')
        self.assertIn('decisions changed:', self.task('plan', 'proj'))

    def test_status_unreadable_decision_next_to_status_edit_lists_every_repo(self):
        good = self.home / 'dev_workspace/z-good'
        good.mkdir()
        self.git(good, 'init', '-q', '-b', 'main')
        (self.kb / 'backlog/decisions').mkdir()
        status_name = 'backlog/decisions/d-status.md'
        binary_name = 'backlog/decisions/d-bytes.md'
        original = '---\nstatus: accepted\n---\nKeep the data.\n'
        self.commit_in(self.kb, status_name, original)
        self.commit_in(self.kb, binary_name, original)
        self.task('plan', 'proj', '--sign')
        (self.kb / status_name).write_text(original.replace('accepted', 'replaced'))
        (self.kb / binary_name).write_bytes(b'\xff')
        for phase in ('working tree', 'committed', 'unreadable before'):
            with self.subTest(phase=phase):
                if phase == 'committed':
                    self.git(self.kb, 'add', status_name, binary_name)
                    subprocess.run(['git', '-C', str(self.kb), 'commit', '-qm', 'decisions'],
                                   env=dict(self.env, GIT_COMMITTER_DATE='2099-01-01T00:00:00+00:00'),
                                   check=True, capture_output=True)
                elif phase == 'unreadable before':
                    (self.kb / binary_name).write_text(original)
                output = self.raw_task('status')
                self.assertIn('proj:', output)
                self.assertIn('z-good:', output)
                self.assertIn(f'decisions changed: {binary_name}', output)
                self.assertNotIn(status_name, output)

    def test_decision_read_errors_conservatively_count_as_changed(self):
        name = 'decision.md'
        (self.kb / name).write_text('Keep the data.\n')
        shown = subprocess.CompletedProcess([], 0, stdout='Keep the data.\n')
        for error in (OSError('read failed'), ValueError('read failed'),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid')):
            for source in ('before', 'after', 'working tree'):
                with self.subTest(error=type(error).__name__, source=source):
                    git_reads = [error] if source == 'before' else [shown, error]
                    with mock.patch.object(TASK_MODULE, 'run_git', side_effect=git_reads), \
                            mock.patch.object(Path, 'read_text', side_effect=error):
                        after = None if source == 'working tree' else 'HEAD'
                        self.assertTrue(TASK_MODULE.decision_changed(self.kb, name, after=after))

    def test_decision_inline_replacement_hsh_history(self):
        # Copied before/after lines from HSH d65ee46, decision-22.
        old = (
            '- **v1 category:** electricity only. Phone, internet, subscriptions, weather as a product and device advice wait until the pilot has passed. Existing phone code stays as it is, with no new work.\n'
            "- **Pilot:** first the founder's own household for 2 weeks as acceptance, then 3 invited households for 4 weeks.\n"
        )
        new = (
            '- **v1 category:** electricity only. *Replaced 2026-10-18 by decision-24: electricity first, then phone, then internet.* Phone, internet, subscriptions, weather as a product and device advice wait until the pilot has passed. Existing phone code stays as it is, with no new work.\n'
            "- **Pilot:** first the founder's own household for 2 weeks as acceptance, then 3 invited households for 4 weeks. *Replaced 2026-10-18 by decision-24: founder household (with his wife) only until three categories pass.*\n"
        )
        name = 'backlog/decisions/decision-22.md'
        (self.kb / 'backlog/decisions').mkdir()
        self.commit_in(self.kb, name, old)
        self.task('plan', 'proj', '--sign')
        (self.kb / name).write_text(new)
        self.assertNotIn('decisions changed:', self.task('plan', 'proj'))
        self.git(self.kb, 'add', name)
        subprocess.run(['git', '-C', str(self.kb), 'commit', '-qm', 'replacement'],
                       env=dict(self.env, GIT_COMMITTER_DATE='2099-01-01T00:00:00+00:00'), check=True)
        self.assertNotIn('decisions changed:', self.task('plan', 'proj'))
        for changed in (new.replace('electricity only.', 'electricity first.'),
                        new.replace('Phone, internet,', 'Phone,  internet,'),
                        new.replace('Existing phone code stays as it is, with no new work.', '')):
            with self.subTest(changed=changed):
                self.assertFalse(TASK_MODULE.decision_status_only(old, changed))

    def test_decision_marker_fragments_preserve_exact_content(self):
        old = 'Keep the data.'
        for marker in ('*Replaced 2026-10-18 by decision-24: new scope.*',
                       '(replaced by decision-24)', 'Replaced by decision-24',
                       '*rEpLaCeD by decision-24*'):
            for new in (f'{marker} {old}', f'Keep {marker} the data.',
                        f'{old} {marker}', f'~~Keep~~ {marker} the ~~data.~~',
                        f'{old}\n{marker}\n', f'{marker} {old} {marker}'):
                with self.subTest(new=new):
                    self.assertTrue(TASK_MODULE.decision_status_only(old, new))
            self.assertFalse(TASK_MODULE.decision_status_only(old, marker))
        for added in ('Replaced by decision-24 and delete the data.',
                      '*Replaced by decision-24* new policy',
                      '(replaced by decision-24) new policy',
                      'Replaced arbitrary prose', '~~new policy~~', ''):
            with self.subTest(added=added):
                self.assertFalse(TASK_MODULE.decision_status_only(old, old + '\n' + added + '\n'))
        existing = old + ' *Replaced by decision-2*'
        self.assertTrue(TASK_MODULE.decision_status_only(existing, existing + ' (replaced by decision-3)'))
        self.assertFalse(TASK_MODULE.decision_status_only(existing, old))
        self.assertFalse(TASK_MODULE.decision_status_only(
            old, 'Keep  *Replaced by decision-2* the data.'))

    def test_gate_handoff_failed_metadata_write(self):
        task = TASK_MODULE.Task(self.repo, '5')
        with mock.patch.dict(os.environ, self.env):
            lock, holder = TASK_MODULE.gate_paths('slot-0')
            holder.write_text(json.dumps({'pid': 99999999, 'repo': 'previous-holder'}))
            with mock.patch.object(TASK_MODULE, 'atomic_json', side_effect=OSError('write failed')):
                with TASK_MODULE.exclusive_gate_lock(task):
                    holders = TASK_MODULE.gate_holders()
                    self.assertEqual(holders[0][1], os.getpid())
                    self.assertIn('holder details unavailable', str(holders))
                    self.assertNotIn('previous-holder', str(holders))
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), mock.patch.object(TASK_MODULE, 'HOME', self.home):
                        TASK_MODULE.status(type('Args', (), {'repo': str(self.repo)})())
                    self.assertIn(f'pid {os.getpid()} (holder details unavailable)', output.getvalue())
                    self.assertNotIn('/? since ?', output.getvalue())
                    self.assertNotIn('previous-holder', output.getvalue())

    def test_close_update_checks_default_branch_holder_dirt(self):
        holder = self.home / 'knowledge-holder'
        self.git(self.kb, 'worktree', 'add', '-q', '-b', 'holder-main', str(holder))
        task_file = next((holder / 'backlog/tasks').glob('*.md'))
        original = task_file.read_bytes()
        self.commit_in(self.kb, 'incoming.md', 'Incoming close\n')
        incoming = self.git(self.kb, 'rev-parse', 'HEAD')
        self.git(self.kb, 'update-ref', 'refs/remotes/origin/holder-main', incoming)
        before = self.git(holder, 'rev-parse', 'HEAD')
        task_file.write_bytes(original + b'Uncommitted task change\n')
        output = TASK_MODULE.update_closed_knowledge_checkout(self.kb, 'holder-main')
        self.assertEqual(output, f'local holder-main not updated in {holder}: it has uncommitted changes')
        self.assertEqual(self.git(holder, 'rev-parse', 'HEAD'), before)
        task_file.write_bytes(original)
        (holder / 'INTENT.md').write_text('Local plan\n')
        realign = holder / 'history/realign/review.md'
        realign.parent.mkdir(parents=True)
        realign.write_text('Local review\n')
        output = TASK_MODULE.update_closed_knowledge_checkout(self.kb, 'holder-main')
        self.assertEqual(output, f'{holder} fast-forwarded')
        self.assertEqual(self.git(holder, 'rev-parse', 'HEAD'), incoming)
        self.assertEqual((holder / 'INTENT.md').read_text(), 'Local plan\n')
        self.assertEqual(realign.read_text(), 'Local review\n')

    def test_boost_state_expiry_off_and_authority(self):
        future = '2099-01-01T00:00:00+00:00'
        self.assertIn('boost: off', self.task('boost'))
        self.assertIn('future ISO', self.task('boost', '--codex-until', '2000-01-01T00:00:00+00:00', code=1))
        self.assertIn('future ISO', self.task('boost', '--codex-until', '2099-01-01T00:00:00', code=1))
        self.task('boost', '--claude-until', future, '--codex-until', future)
        self.assertIn('boost: claude until', self.task('status', 'proj'))
        self.task('boost', 'off', '--claude')
        self.assertNotIn('claude', self.task('boost'))
        self.task('boost', '--claude-until', future)
        self.task('boost', 'off', '--codex')
        self.assertNotIn('codex', self.task('boost'))
        self.task('boost', 'off')
        path = self.home / '.local/state/task/boost.json'
        path.write_text(json.dumps({'claude': '2000-01-01T00:00:00+00:00', 'codex': '2000-01-01T00:00:00+00:00'}))
        self.assertEqual(self.task('boost').strip(), 'boost: off')
        before = path.read_bytes()
        with mock.patch.object(TASK_MODULE, 'task_state_writable', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TASK_MODULE.main(['boost', '--codex-until', future]), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_boost_builder_models_opt_out_and_astra(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        self.task('boost', '--codex-until', '2099-01-01T00:00:00+00:00', '--claude-until', '2099-01-01T00:00:00+00:00')
        for builder in ('codex', 'claude'):
            for tier in ('routine', 'hard', 'design'):
                expected = {'codex': {'routine': 'gpt-6-luna', 'hard': 'gpt-6.1-sol', 'design': 'gpt-6-astra'},
                            'claude': {'routine': 'sonnet', 'hard': 'fable', 'design': 'opus'}}[builder][tier]
                (wt / '.work/model-call.json').unlink(missing_ok=True)
                (wt / '.work/final.md').unlink(missing_ok=True)
                out = self.task('lane', 'proj', '5', '--builder', builder, '--tier', tier)
                record = self.wait_for_lane(wt)
                self.assertEqual(record['model'], expected)
                self.assertIn(f'tier {tier}', out)
                explicit = 'gpt-6-luna' if builder == 'codex' else 'sonnet'
                (wt / '.work/model-call.json').unlink(missing_ok=True)
                (wt / '.work/final.md').unlink(missing_ok=True)
                self.task('lane', 'proj', '5', '--builder', builder, '--tier', tier, '--model', explicit)
                self.assertEqual(self.wait_for_lane(wt)['model'], explicit)
            (wt / '.work/model-call.json').unlink(missing_ok=True)
            (wt / '.work/final.md').unlink(missing_ok=True)
            self.task('lane', 'proj', '5', '--builder', builder, '--tier', 'hard', '--no-boost')
            no_boost_hard = 'gpt-6.1-sol' if builder == 'codex' else explicit
            self.assertEqual(self.wait_for_lane(wt)['model'], no_boost_hard)

    def test_tier_file_flag_state_and_status(self):
        task_file = next((self.kb / 'backlog/tasks').glob('t-5 *'))
        task_file.write_text(task_file.read_text() + '\nTier: hard\n')
        self.task('start', 'proj', '5', '--builder', 'codex')
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['tier'], 'hard')
        self.assertIn('tier=hard', self.task('status', 'proj'))
        self.task('start', 'proj', '5', '--refresh', '--builder', 'codex', '--tier', 'design')
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['tier'], 'design')

    def test_tier_defaults_and_override_policy(self):
        from task_routing import route, lane_limits
        for vendor, default in [('codex', 'gpt-6-luna'), ('claude', 'sonnet')]:
            for tier in ('routine', 'hard', 'design'):
                expected = default if tier == 'routine' else {'codex': 'gpt-6.1-sol', 'claude': 'opus'}[vendor]
                self.assertEqual(route(self.home, vendor, tier), expected)
                no_boost_expected = 'gpt-6.1-sol' if vendor == 'codex' and tier == 'hard' else default
                self.assertEqual(route(self.home, vendor, tier, boosted=False), no_boost_expected)
        config = self.home / '.config/task'
        config.mkdir(parents=True, exist_ok=True)
        (config / 'routing.json').write_text(json.dumps(dict(models={'claude': {'hard': 'opus'}}, extra_lanes=1)))
        self.task('boost', '--claude-until', '2099-01-01T00:00:00+00:00')
        self.assertEqual(route(self.home, 'claude', 'hard'), 'opus')
        self.assertEqual(route(self.home, 'claude', 'routine'), 'sonnet')
        self.assertEqual(route(self.home, 'claude', 'hard', 'sonnet'), 'sonnet')
        self.assertEqual(lane_limits(self.home, 3), {'codex': 1, 'claude': 2})

    def test_boost_reviewer_models_and_recorded_identity(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.task('boost', '--codex-until', '2099-01-01T00:00:00+00:00', '--claude-until', '2099-01-01T00:00:00+00:00')
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n'
        for builder, vendor, model in [('codex', 'claude', 'haiku'), ('claude', 'codex', 'gpt-6-luna')]:
            self.task('start', 'proj', '5', '--refresh', '--builder', builder)
            self.raw_task("check", "proj", '5')
            self.raw_task('review', 'proj', '5', '--run', env=dict(self.env, REVIEW_STUB_REPLY=reply))
            metadata = json.loads((self.state() / 'review.json').read_text())
            self.assertEqual((metadata['reviewer'], metadata['model']), (vendor, model))
            self.assertIn(model, json.loads((wt / '.work/reviewer-call.json').read_text())['args'])

    def test_review_limit_retries_once_without_boost_and_after_boost(self):
        self.stub_reviewers()
        wt, _ = self.started()
        for boosted in (False, True):
            with self.subTest(boosted=boosted):
                (wt / '.work/reviewer-count').unlink(missing_ok=True)
                (wt / '.work/reviewer-calls.jsonl').unlink(missing_ok=True)
                if boosted:
                    self.task('boost', '--claude-until', '2099-01-01T00:00:00+00:00')
                self.raw_task("check", "proj", '5')
                out = self.raw_task('review', 'proj', '5', '--run', code=1,
                                    env=dict(self.env, REVIEW_STUB_REPLY="You've hit your weekly limit",
                                             REVIEW_STUB_STDERR="ERROR: You've hit your usage limit.",
                                             REVIEW_STUB_EXIT='2'))
                self.assertIn('ended by usage limit', out)
                calls = [json.loads(line) for line in (wt / '.work/reviewer-calls.jsonl').read_text().splitlines()]
                self.assertEqual(len(calls), 2)
                self.assertIn('haiku', calls[0])
                self.assertIn('gpt-6-luna', calls[1])
                self.assertFalse((self.state() / 'review.json').exists())

    def test_boosted_reviewer_other_errors_do_not_fall_back(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.task('boost', '--claude-until', '2099-01-01T00:00:00+00:00')
        self.raw_task("check", "proj", '5')
        out = self.raw_task('review', 'proj', '5', '--run', code=1,
                            env=dict(self.env, REVIEW_STUB_REPLY="You've hit your Opus limit\nOther failure",
                                     REVIEW_STUB_EXIT='1'))
        self.assertIn('exited with status 1', out)
        self.assertEqual((wt / '.work/reviewer-count').read_text(), '1')
        self.assertEqual(json.loads((self.state() / 'review-routing.json').read_text())['model'], 'haiku')

    def test_boosted_codex_limit_retries_once(self):
        self.stub_reviewers()
        self.raw_task('start', 'proj', '5', '--builder', 'claude')
        wt = self.repo / '.worktrees/t5'
        self.task('boost', '--codex-until', '2099-01-01T00:00:00+00:00')
        self.raw_task("check", "proj", '5')
        out = self.raw_task('review', 'proj', '5', '--run', code=1,
                            env=dict(self.env, REVIEW_STUB_STDERR="ERROR: You've hit your usage limit.",
                                     REVIEW_STUB_REPLY="You've hit your weekly limit",
                                     REVIEW_STUB_EXIT='1'))
        self.assertIn('ended by usage limit', out)
        self.assertEqual((wt / '.work/reviewer-count').read_text(), '2')
        self.assertFalse((self.state() / 'review.json').exists())

    def test_reviewer_fallback_records_actual_route_and_stops_without_verdict(self):
        self.stub_reviewers()
        wt, _ = self.started()
        for builder, vendor, model in (('codex', 'codex', 'gpt-6-luna'), ('claude', 'claude', 'haiku')):
            self.task('start', 'proj', '5', '--refresh', '--builder', builder)
            self.raw_task('check', 'proj', '5')
            head = self.git(wt, 'rev-parse', 'HEAD')
            for reply in ('No verdict available', f'Verdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n'):
                with self.subTest(builder=builder, reply=reply):
                    (wt / '.work/reviewer-count').unlink(missing_ok=True)
                    (self.state() / 'review.json').unlink(missing_ok=True)
                    first_limit = "You've hit your weekly limit"
                    out = self.raw_task('review', 'proj', '5', '--run',
                        code=1 if reply.startswith('No') else 0,
                        env=dict(self.env, REVIEW_STUB_FIRST_REPLY=first_limit,
                                 REVIEW_STUB_STDERR="ERROR: You've hit your usage limit.", REVIEW_STUB_FIRST_EXIT='2',
                                 REVIEW_STUB_REPLY=reply, REVIEW_STUB_EXIT='0'))
                    self.assertEqual((wt / '.work/reviewer-count').read_text(), '2')
                    route = json.loads((self.state() / 'review-routing.json').read_text())
                    self.assertEqual((route['vendor'], route['model']), (vendor, model))
                    if reply.startswith('No'):
                        self.assertIn('review produced no verdict', out)
                        self.assertFalse((self.state() / 'review.json').exists())
                    else:
                        meta = json.loads((self.state() / 'review.json').read_text())
                        self.assertEqual((meta['reviewer'], meta['model']), (vendor, model))

    def test_plan_refusals_exceptions_and_resume(self):
        plan = self.kb / 'INTENT.md'
        original = plan.read_text()
        task = next((self.kb / 'backlog/tasks').glob('t-5 *'))
        for content, message in [(None, 'INTENT.md missing'),
                                 ('# Goal\ncurrent: m-1\n', 'current must name'),
                                 ('---\ntitle: Goal\n---\nBody\n', 'current must name'),
                                 ('---\ncurrent: m-1\n---\n', 'not signed'),
                                 (original + 'changed\n', 'changed since sign-off')]:
            if content is None:
                plan.unlink()
            else:
                plan.write_text(content)
            self.assertIn(message, self.task('start', 'proj', '5', code=1))
        plan.write_text(original)
        task.write_text(task.read_text().replace('milestone: m-1', 'milestone: m-2'))
        self.assertIn('milestone', self.task('start', 'proj', '5', code=1))
        self.assertIn('reason', self.task('start', 'proj', '5', '--off-plan', '', code=1))
        self.assertIn('off-plan exception: founder ask', self.task('start', 'proj', '5', '--off-plan', 'founder ask'))
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['off_plan'], 'founder ask')
        plan.unlink()
        self.started()  # Existing record and worktree resume even with no plan.
        self.assertIn('requires --refresh', self.task('start', 'proj', '5', '--off-plan', 'new reason', code=1))
        self.task('start', 'proj', '5', '--off-plan', 'new reason', '--refresh')
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['off_plan'], 'new reason')
        self.assertIn('off-plan exception', self.task('status', 'proj'))
        self.task('start', 'proj', 'direct-ask')

    def assert_start_exception(self, ident='5', reason='approved exception', refresh=False, state='t5'):
        extra = ['--refresh'] if refresh else []
        out = self.task('start', 'proj', ident, '--off-plan', reason, *extra)
        self.assertIn(f'off-plan exception: {reason}', out)
        self.assertEqual(json.loads((self.state(state) / 'task.json').read_text())['off_plan'], reason)
        status = self.task('status', 'proj')
        self.assertIn('Needs attention', status)
        self.assertIn(f'{state}: off-plan exception: {reason}', status)

    def test_start_plan_path_new_task(self):
        (self.kb / 'INTENT.md').unlink()
        self.assertIn('INTENT.md missing', self.task('start', 'proj', '5', code=1))
        self.assertFalse((self.state() / 'task.json').exists())
        self.assert_start_exception()

    def test_start_plan_path_record_without_worktree(self):
        wt, _ = self.started()
        record = self.state() / 'task.json'
        before = record.read_bytes()
        self.git(self.repo, 'worktree', 'remove', '--force', str(wt))
        (self.kb / 'INTENT.md').unlink()
        self.assertIn('requires --refresh', self.task(
            'start', 'proj', '5', '--off-plan', 'replacement', code=1))
        self.assertFalse(wt.exists())
        self.assertEqual(record.read_bytes(), before)
        self.assertIn('INTENT.md missing', self.task('start', 'proj', '5', code=1))
        self.assert_start_exception(reason='replacement', refresh=True)

    def test_start_plan_path_worktree_without_record(self):
        self.started()
        (self.state() / 'task.json').unlink()
        (self.kb / 'INTENT.md').unlink()
        self.assertIn('INTENT.md missing', self.task('start', 'proj', '5', code=1))
        self.assert_start_exception()

    def test_start_plan_path_both_present(self):
        self.started()
        (self.kb / 'INTENT.md').unlink()
        self.task('start', 'proj', '5')
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['off_plan'], '')
        self.assert_start_exception(refresh=True)
        before = (self.state() / 'task.json').read_bytes()
        self.assertIn('off-plan exception: approved exception', self.task('start', 'proj', '5'))
        self.assertIn('requires --refresh', self.task(
            'start', 'proj', '5', '--off-plan', 'replacement', code=1))
        self.assertEqual((self.state() / 'task.json').read_bytes(), before)
        self.assert_start_exception(refresh=True, reason='replacement')

    def test_start_plan_path_refresh(self):
        (self.kb / 'INTENT.md').unlink()
        self.assertIn('INTENT.md missing', self.task('start', 'proj', '5', '--refresh', code=1))
        self.assert_start_exception(refresh=True)
        # Refresh retains the recorded exception even when the plan still fails.
        out = self.task('start', 'proj', '5', '--refresh')
        self.assertIn('off-plan exception: approved exception', out)
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['off_plan'], 'approved exception')
        self.assertIn('t5: off-plan exception: approved exception', self.task('status', 'proj'))
        self.assert_start_exception(refresh=True, reason='renewed exception')

    def test_refresh_failing_plan_without_exception_refuses(self):
        self.started()
        record = self.state() / 'task.json'
        before = record.read_bytes()
        (self.kb / 'INTENT.md').unlink()
        self.assertIn('INTENT.md missing', self.task('start', 'proj', '5', '--refresh', code=1))
        self.assertEqual(record.read_bytes(), before)

    def test_refresh_passing_plan_preserves_exception_and_status(self):
        self.assert_start_exception()
        self.assertIn('plan: ok', self.task('plan', 'proj'))
        out = self.task('start', 'proj', '5', '--refresh')
        self.assertIn('off-plan exception: approved exception', out)
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['off_plan'], 'approved exception')
        status = self.task('status', 'proj')
        self.assertIn('plan: ok', status)
        self.assertIn('Needs attention', status)
        self.assertIn('t5: off-plan exception: approved exception', status)

    def test_start_plan_path_plain_name(self):
        (self.kb / 'INTENT.md').unlink()
        self.task('start', 'proj', 'direct-ask')
        self.assert_start_exception('direct-ask', refresh=True, state='direct-ask')
        self.assert_start_exception('direct-exception', state='direct-exception')

    def test_start_plan_path_no_knowledge_repo(self):
        self.kb.rename(self.home / 'unused-kb')
        # Plan exemption does not waive the separate numbered-task existence check.
        self.assertIn('No Backlog.md task', self.task('start', 'proj', '5', code=1))
        self.task('start', 'proj', 'direct-ask')
        self.assert_start_exception('direct-ask', refresh=True, state='direct-ask')
        self.assert_start_exception('direct-exception', state='direct-exception')

    def test_plan_sign_counts_decisions_and_runner_warning(self):
        plan = self.kb / 'INTENT.md'
        plan.write_text(plan.read_text().replace('current: m-1', 'current: missing'))
        self.assertIn('existing', self.task('plan', 'proj', '--sign', code=1))
        plan.write_text('---\ncurrent: m-1\nrealign_every: 1\n---\nPass test\n')
        self.assertIn('plan: ok', self.task('plan', 'proj', '--sign'))
        signed = plan.read_text()
        self.assertEqual(TASK_MODULE.plan_hash(signed), TASK_MODULE.frontmatter_value(signed, 'signed_hash'))
        runner = self.home / 'runner.md'
        runner.write_text('1. 5: builder codex, model gpt-6-luna\n2. direct: builder codex, model gpt-6-luna\n')
        self.assertIn('stop at direct', self.task('runner-check', 'proj', str(runner)))
        state = self.state('direct')
        state.mkdir(parents=True)
        (state / 'task.json').write_text(json.dumps({'id': 'direct', 'title': 'Direct ask'}))
        (state / 'merge.json').write_text(json.dumps({'time': '2099-01-01T00:00:00+00:00'}))
        self.assertIn('1/1', self.task('plan', 'proj'))
        off = self.state('t99')
        off.mkdir(parents=True)
        (off / 'task.json').write_text(json.dumps({'id': 'T-99', 'off_plan': 'approved'}))
        (off / 'merge.json').write_text(json.dumps({'time': '2099-01-01T00:00:00+00:00'}))
        self.assertIn('2/1', self.task('plan', 'proj'))
        self.assertIn('plan: stop:', self.task('status', 'proj'))
        (off / 'merge.json').write_text('invalid json')
        (off / 'task.json').write_text('invalid json')
        self.assertIn('1/1', self.task('plan', 'proj'))
        self.task('status', 'proj')
        self.assertIn('realignment due', self.task('start', 'proj', '5', code=1))
        from datetime import datetime, timezone
        (state / 'merge.json').write_text(json.dumps({'time': datetime.now(timezone.utc).isoformat()}))
        self.assertIn('0/1', self.task('plan', 'proj', '--sign'))
        decisions = self.kb / 'backlog/decisions'
        decisions.mkdir()
        decision = decisions / 'd-1.md'
        decision.write_text('New direction')
        self.assertIn('decisions changed', self.task('start', 'proj', '5', code=1))
        self.git(self.kb, 'add', str(decision))
        subprocess.run(['git', '-C', str(self.kb), 'commit', '-qm', 'decision'],
                       env=dict(self.env, GIT_COMMITTER_DATE='2099-01-01T00:00:00+00:00'), check=True)
        self.assertIn('d-1.md', self.task('plan', 'proj'))
        self.assertIn('decisions changed', self.task('start', 'proj', '5', code=1))

    def test_plan_detects_old_decision_merged_after_signoff(self):
        self.git(self.kb, 'switch', '-q', '-c', 'decision')
        decisions = self.kb / 'backlog/decisions'
        decisions.mkdir()
        (decisions / 'd-old.md').write_text('New direction')
        self.git(self.kb, 'add', 'backlog/decisions')
        subprocess.run(['git', '-C', str(self.kb), 'commit', '-qm', 'old decision'],
                       env=dict(self.env, GIT_AUTHOR_DATE='2000-01-01T00:00:00+00:00',
                                GIT_COMMITTER_DATE='2000-01-01T00:00:00+00:00'), check=True)
        self.git(self.kb, 'switch', '-q', 'main')
        self.task('plan', 'proj', '--sign')
        self.assertIn('plan: ok', self.task('plan', 'proj'))
        subprocess.run(['git', '-C', str(self.kb), 'merge', '--no-ff', '-qm', 'Merge decision', 'decision'],
                       env=dict(self.env, GIT_COMMITTER_DATE='2099-01-01T00:00:00+00:00'), check=True)
        self.assertIn('decisions changed: backlog/decisions/d-old.md', self.task('plan', 'proj'))
        # Scanning follows the default branch even when another branch is checked out.
        self.git(self.kb, 'switch', '-q', 'decision')
        self.assertIn('decisions changed: backlog/decisions/d-old.md', self.task('plan', 'proj'))

    def test_plan_scans_head_without_default_branch(self):
        self.task('plan', 'proj', '--sign')
        # No remote and no main or master: the scan walks the checked-out branch.
        self.git(self.kb, 'branch', '-m', 'main', 'trunk')
        self.assertIn('plan: ok', self.task('plan', 'proj'))
        decisions = self.kb / 'backlog/decisions'
        decisions.mkdir()
        (decisions / 'd-trunk.md').write_text('New direction')
        self.git(self.kb, 'add', 'backlog/decisions')
        subprocess.run(['git', '-C', str(self.kb), 'commit', '-qm', 'decision'],
                       env=dict(self.env, GIT_COMMITTER_DATE='2099-01-01T00:00:00+00:00'), check=True)
        self.assertIn('decisions changed: backlog/decisions/d-trunk.md', self.task('plan', 'proj'))
        # A root commit after sign-off is scanned too.
        self.git(self.kb, 'checkout', '-q', '--orphan', 'fresh')
        self.git(self.kb, 'rm', '-q', '--cached', 'backlog/decisions/d-trunk.md')
        (decisions / 'd-trunk.md').unlink()
        (decisions / 'd-root.md').write_text('Root direction')
        self.git(self.kb, 'add', '.')
        subprocess.run(['git', '-C', str(self.kb), 'commit', '-qm', 'root decision'],
                       env=dict(self.env, GIT_COMMITTER_DATE='2099-01-01T00:00:00+00:00'), check=True)
        out = self.task('plan', 'proj')
        self.assertIn('decisions changed: backlog/decisions/d-root.md', out)
        self.assertNotIn('d-trunk.md', out)

    def test_runner_warning_counts_resumed_unmerged_tasks(self):
        plan = self.kb / 'INTENT.md'
        plan.write_text('---\ncurrent: m-1\nrealign_every: 1\n---\nPass test\n')
        self.task('plan', 'proj', '--sign')
        self.started()
        self.assertTrue((self.state() / 'task.json').is_file())
        self.assertFalse((self.state() / 'merge.json').exists())
        runner = self.home / 'runner.md'
        runner.write_text('1. 5: builder codex, model gpt-6-luna\n'
                          '2. new: builder codex, model gpt-6-luna\n')
        output = self.task('runner-check', 'proj', str(runner))
        self.assertIn('warning: realignment stop at new (1 merged tasks)', output)
        self.assertIn('runner file OK (2 tasks)', output)
        # A merge record removes the resumed task from the projected count.
        (self.state() / 'merge.json').write_text(json.dumps({'time': '2000-01-01T00:00:00+00:00'}))
        self.assertNotIn('warning:', self.task('runner-check', 'proj', str(runner)))

    def test_plan_sign_preserves_intent_body_and_other_metadata(self):
        plan = self.kb / 'INTENT.md'
        body = '\n# Goal\n\n---\nsigned: body example\nsigned_hash: body hash\ncurrent: body example\nTail without newline'
        header = '---\n# Keep comment\ncurrent: m-1\nrealign_every: 7\ncustom: keep me\n'
        plan.write_text(header + '---\n' + body)
        for _ in range(2):
            self.assertIn('plan: ok', self.task('plan', 'proj', '--sign'))
            signed = plan.read_text()
            self.assertTrue(signed.startswith(header))
            self.assertEqual(signed.split('---\n', 2)[2], body)
            self.assertEqual(TASK_MODULE.frontmatter_value(signed, 'signed_hash'),
                             TASK_MODULE.plan_hash(signed))
        plan.write_text(signed.replace('signed: body example', 'signed: changed body'))
        self.assertIn('changed since sign-off', self.task('plan', 'proj'))
        plan.write_text(signed.replace('realign_every: 7', 'realign_every: 8'))
        self.assertIn('changed since sign-off', self.task('plan', 'proj'))
        plan.write_bytes((header + '---\n' + body).replace('\n', '\r\n').encode())
        self.assertIn('plan: ok', self.task('plan', 'proj', '--sign'))
        self.assertEqual(plan.read_bytes().split(b'---\r\n', 2)[2],
                         body.replace('\n', '\r\n').encode())

    def test_plan_merge_boundary_full_precision(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        fixed = datetime(2026, 10, 18, 12, 0, 0, 500000, tzinfo=timezone.utc)
        state = self.state('direct')
        state.mkdir(parents=True)
        (state / 'task.json').write_text(json.dumps({'id': 'direct'}))
        # Signing must exclude an already recorded merge in the same second.
        (state / 'merge.json').write_text(json.dumps({'time': fixed.replace(microsecond=499999).isoformat()}))
        with mock.patch.object(TASK_MODULE, 'datetime') as clock, \
                mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(TASK_MODULE, 'CORTEX', self.kb.parent), \
                mock.patch.object(TASK_MODULE, 'resolve_repo', return_value=self.repo):
            clock.now.return_value = fixed
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(TASK_MODULE.utc_now(), fixed.isoformat())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                TASK_MODULE.plan(SimpleNamespace(repo='proj', sign=True))
        self.assertIn('merged since sign-off: 0/15', output.getvalue())
        for micros, count in [(499999, 0), (500000, 0), (500001, 1)]:
            (state / 'merge.json').write_text(json.dumps({'time': fixed.replace(microsecond=micros).isoformat()}))
            self.assertIn(f'merged since sign-off: {count}/15', self.task('plan', 'proj'))

    def test_missing_task_precedes_plan_reasons(self):
        (self.kb / 'INTENT.md').unlink()
        output = self.task('start', 'proj', '999', code=1)
        self.assertIn('No Backlog.md task', output)
        self.assertNotIn('INTENT.md missing', output)

    def test_realign_read_only_prompt_and_output(self):
        (self.kb / 'STATUS.md').write_text('STATUS.md contents')
        (self.kb / 'backlog/decisions').mkdir()
        (self.kb / 'backlog/decisions/d-1 - direction.md').write_text('Decision input')
        state = self.state('direct')
        state.mkdir(parents=True)
        (state / 'task.json').write_text(json.dumps({'id': 'direct', 'title': 'Direct title', 'milestone': 'm-1'}))
        (state / 'merge.json').write_text(json.dumps({'time': '2099-01-01T00:00:00+00:00'}))
        stub = self.home / 'bin/codex'
        stub.write_text('#!/usr/bin/env python3\nimport pathlib,sys\n'
                        'pathlib.Path("prompt-copy").write_text(sys.stdin.read())\n'
                        'pathlib.Path("args-copy").write_text(" ".join(sys.argv))\n'
                        'print("Verdict: on plan")\n')
        stub.chmod(0o755)
        before = (self.kb / 'INTENT.md').read_bytes()
        self.assertIn('Verdict: on plan', self.task('realign', 'proj'))
        prompt = (self.kb / 'prompt-copy').read_text()
        self.assertEqual(prompt.count('## INTENT.md\n'), 1)
        for value in ('INTENT.md', 'Pass test: ship the goal.', 'STATUS.md contents', 'Decision input', 'pass test', 'founder', 'direct: Direct title; milestone: m-1'):
            self.assertIn(value, prompt)
        self.assertIn('--sandbox read-only', (self.kb / 'args-copy').read_text())
        self.assertEqual((self.kb / 'INTENT.md').read_bytes(), before)
        self.assertEqual(len(list((self.kb / 'history/realign').glob('realign-*.md'))), 1)
        stub.write_text('#!/usr/bin/env python3\nimport sys\nprint("partial reply")\nsys.exit(1)\n')
        for report in (self.kb / 'history/realign').iterdir():
            report.unlink()
        (self.kb / 'history/realign').rmdir()
        output = self.task('realign', 'proj', code=1)
        failed_reply = Path(output.strip().split('Reply: ', 1)[1])
        self.assertEqual(failed_reply.read_text(), 'partial reply\n')
        self.assertTrue(failed_reply.is_relative_to(self.home / '.local/state/task/proj'))
        self.assertFalse((self.kb / 'history/realign').exists())
        self.assertEqual((self.kb / 'INTENT.md').read_bytes(), before)

    def test_plan_without_knowledge_repo_is_exempt(self):
        from types import SimpleNamespace
        TASK_MODULE.check_plan(SimpleNamespace(kb=self.home / 'absent', number=5))
    def runner_fixture(self, backend='systemd'):
        work = self.repo / '.work'
        work.mkdir(exist_ok=True)
        runner = work / 'runner-b1.md'
        runner.write_text('1. docs: builder codex, model gpt-6-luna\n')
        scripts = {
            'systemctl': """#!/usr/bin/env python3
import os, pathlib, sys
home = pathlib.Path(os.environ['HOME'])
if 'stop' in sys.argv:
    (home / 'stopped').write_text(sys.argv[-1])
else:
    print('ActiveState=' + ('active' if (home / 'active').exists() else 'inactive'))
    print('ExecMainStartTimestamp=test-start')
""",
            'systemd-run': """#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ['HOME'])
(home / 'service-args').write_text(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get('SERVICE_EXIT', '0')))
""",
            'task-batch': """#!/usr/bin/env python3
import json, os, pathlib, sys, time
work = pathlib.Path.cwd() / '.work'
(work / 'runner-call').write_text(json.dumps(dict(args=sys.argv[1:], prompt=sys.stdin.read(),
    pid=os.getpid(), pgid=os.getpgrp(), sid=os.getsid(0))))
while (work / 'hold').exists():
    time.sleep(.02)
print('runner stdout')
print('runner stderr', file=sys.stderr)
if os.environ.get('RUNNER_FINAL'):
    with (work / 'runner-b1.done').open('a') as stream:
        stream.write(os.environ['RUNNER_FINAL'] + '\\n')
sys.exit(7)
"""}
        for name, body in scripts.items():
            path = self.home / 'bin' / name
            path.write_text(body)
            path.chmod(0o755)
        if backend == 'setsid':
            self.env['SERVICE_EXIT'] = '1'
        return runner

    def wait_runner_file(self, path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists(), str(path))
        return path.read_text()

    def test_runner_start_service_validation_status_cancel(self):
        runner = self.runner_fixture()
        runner.write_text('invalid\n')
        self.raw_task('runner-start', 'proj', str(runner), code=1)
        self.assertFalse((self.home / 'service-args').exists())
        runner.write_text('1. docs: builder codex, model gpt-6-luna\n')
        out = self.raw_task('runner-start', 'proj', str(runner))
        self.assertIn('task-runner-proj-b1', out)
        args = json.loads((self.home / 'service-args').read_text())
        self.assertEqual(args[:7], ['--user', '--unit', 'task-runner-proj-b1', '--collect',
                                   '--working-directory', str(self.repo), '--property=StandardInput=null'])
        self.assertIn(f'--property=StandardOutput=append:{runner.with_suffix(".log")}', args)
        self.assertIn('--property=KillMode=process', args)
        command = args[args.index(sys.executable):]
        subprocess.run(command, cwd=self.repo, env=self.env, check=False, capture_output=True)
        call = json.loads((runner.parent / 'runner-call').read_text())
        self.assertEqual(call['args'], [str(self.repo), str(runner)])
        self.assertEqual(call['prompt'], '')
        self.assertIn('code 7', runner.with_suffix('.done').read_text())
        (self.home / 'active').touch()
        self.assertIn('already active', self.raw_task('runner-start', 'proj', str(runner), code=1))
        status = self.raw_task('status', 'proj')
        self.assertIn('task-runner-proj-b1: active started=test-start', status)
        self.assertIn('last=batch: stopped: runner exited', status)
        self.raw_task('cancel', 'proj', 'runner-b1')
        self.assertEqual((self.home / 'stopped').read_text(), 'task-runner-proj-b1')
        (self.home / 'active').unlink()
        self.assertIn('task-runner-proj-b1: inactive', self.raw_task('status', 'proj'))
        for final in ('batch: finished', 'batch: stopped: deliberate'):
            for own_final in ('', final):
                with self.subTest(stale=final, own_final=own_final):
                    done = runner.with_suffix('.done')
                    stale = done.read_text() + final + '\n'
                    done.write_text(stale)
                    self.raw_task('runner-start', 'proj', str(runner))
                    self.assertIn('last=(no .done line)', self.raw_task('status', 'proj'))
                    args = json.loads((self.home / 'service-args').read_text())
                    command = args[args.index(sys.executable):]
                    subprocess.run(command, cwd=self.repo,
                                   env=dict(self.env, RUNNER_FINAL=own_final), capture_output=True)
                    current = done.read_text()[len(stale):]
                    self.assertTrue(done.read_text().startswith(stale))
                    if own_final:
                        self.assertEqual(current, own_final + '\n')
                    else:
                        self.assertEqual(current, 'batch: stopped: runner exited (code 7) '
                                         f'without a final line; see {runner.with_suffix(".log")}\n')
                    self.assertIn('last=' + current.strip(), self.raw_task('status', 'proj'))

    def test_runner_llm_flag_removed(self):
        runner = self.runner_fixture()
        out = self.raw_task('runner-start', 'proj', str(runner), '--llm', code=2)
        self.assertIn('unrecognized arguments: --llm', out)
        self.assertFalse((self.home / 'service-args').exists())

    def test_runner_fallback_survives_callers_group_and_cancel(self):
        runner = self.runner_fixture('setsid')
        hold = runner.parent / 'hold'
        hold.touch()
        # The launcher stays in the caller's session until that entire group is killed.
        caller = subprocess.Popen([sys.executable, '-c',
            'import subprocess,sys,time; subprocess.run(sys.argv[1:], check=True); time.sleep(30)',
            sys.executable, str(TOOL), 'runner-start', 'proj', str(runner)],
            env=self.env, cwd=self.home, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            call = json.loads(self.wait_runner_file(runner.parent / 'runner-call'))
            self.assertNotEqual(call['sid'], caller.pid)
            self.assertNotEqual(call['pgid'], caller.pid)
            os.killpg(caller.pid, signal.SIGKILL)
            caller.wait()
            os.kill(call['pid'], 0)
            self.assertIn('already active', self.raw_task('runner-start', 'proj', str(runner), code=1))
            self.assertIn('task-runner-proj-b1: active', self.raw_task('status', 'proj'))
            self.raw_task('cancel', 'proj', 'runner-b1')
            self.assertIn('without a final line', self.wait_runner_file(runner.with_suffix('.done')))
        finally:
            hold.unlink(missing_ok=True)
            if caller.poll() is None:
                os.killpg(caller.pid, signal.SIGKILL)
                caller.wait()

    def test_runner_fallback_exit_guard_and_logs(self):
        runner = self.runner_fixture('setsid')
        self.assertIn('falling back to setsid', self.raw_task('runner-start', 'proj', str(runner)))
        self.assertIn('code 7', self.wait_runner_file(runner.with_suffix('.done')))
        log = runner.with_suffix('.log').read_text()
        self.assertIn('runner stdout', log)
        self.assertIn('runner stderr', log)

    def test_denials_filters_and_top_commands(self):
        import json
        from datetime import datetime, timezone, timedelta
        path = self.home / '.local/state/agent-policy/denials.jsonl'
        path.parent.mkdir(parents=True)
        now = datetime.now(timezone.utc)
        base = {'time': now.isoformat(), 'rule_id': 'shell.rule', 'repo': '/work/proj'}
        rows = [dict(base, command=command) for command in ['a', 'a', 'b', 'b', 'b', 'c', 'd']]
        rows += [dict(base, command='test-only', **marker) for marker in
                 ({'test': True}, {'is_test': True}, {'session_type': 'test'})]
        rows += [dict(base, command='old', time=(now - timedelta(days=10)).isoformat()),
                 dict(base, command='other-repo', repo='/work/else'),
                 dict(base, command='other-rule', rule_id='other.rule')]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\ninvalid\n')
        out = self.task('denials', '--days', '2', '--rule', 'shell.rule', '--repo', 'proj')
        self.assertEqual(out.strip(), 'shell.rule: 7\n  3: b\n  2: a\n  1: c')
        self.assertEqual(out, self.task('denials', '--days', '2', '--rule', 'shell.rule', '--repo', '/work/proj'))
        self.assertIn('other.rule: 1', self.task('denials', '--days', '2'))
        self.assertEqual(self.task('denials', '--rule', 'missing').strip(), 'No denials in range.')

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
                log = (wt / '.work/run.log').read_text()
                self.assertIn(f'gate busy: held by pid {os.getpid()} (holder details unavailable)', log)
                self.assertNotIn('proj/T-9', log)
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

    def test_status_and_wait_identify_terminal_lane_limits(self):
        wt, _ = self.started()
        work = wt / '.work'
        for builder, diagnostic in [('codex', "ERROR: You've hit your usage limit. Try again later."),
                                    ('claude', "You've hit your weekly limit · resets 5pm (UTC)")]:
            for source in ('final.md', 'lane.log'):
                for code, state, suffix, limited in [(2, 'finished', '\n\n  \n', True),
                                                     (0, 'finished', '\n', False),
                                                     (1, 'finished', '\nOther failure', False),
                                                     (1, 'timed_out', '\n', False)]:
                    with self.subTest(builder=builder, source=source, code=code, state=state, suffix=suffix):
                        for name in ('final.md', 'lane.log'):
                            (work / name).write_text(diagnostic + suffix if name == source else '')
                        (work / 'lane.json').write_text(json.dumps(dict(builder=builder, state=state, exit_code=code)))
                        for command in [('status', 'proj'), ('wait', 'proj', '5', '--max', '0')]:
                            out = self.raw_task(*command)
                            if limited:
                                self.assertIn(f'lane: ended by usage limit ({diagnostic})', out)
                            else:
                                self.assertNotIn('ended by usage limit', out)

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
        wt, _ = self.started()
        (self.kb / 'INTENT.md').write_text('intent')
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
        self.git(self.kb, 'add', 'backlog/decisions')
        self.git(self.kb, 'commit', '-qm', 'decisions')
        self.task('plan', 'proj', '--sign')
        wt, out = self.started()
        self.assertIn("Context: .work/context/", out)
        context = wt / ".work/context"
        self.assertEqual((context / 'INTENT.md').read_bytes(), (self.kb / 'INTENT.md').read_bytes())
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
        (self.repo / '.task.toml').write_text('exclusive_gate = true\n')
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
                self.assertIn(f"gate busy: held by pid {os.getpid()} (holder details unavailable)", output.getvalue())
                self.assertNotIn('proj/T-9', output.getvalue())
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
        for config, trusted, advisory in ((None, True, True), ("", True, True),
                ("exclusive_gate = false\n", False, True),
                ("exclusive_gate = true\n", True, True)):
            with self.subTest(config=config):
                file = path / ".task.toml"
                if config is None:
                    file.unlink(missing_ok=True)
                else:
                    file.write_text(config)
                self.assertEqual(TASK_MODULE.exclusive_gate_enabled(path), trusted)
                self.assertEqual(TASK_MODULE.exclusive_gate_enabled(path, advisory=True), advisory)

    def test_gate_kernel_release_after_kill(self):
        worker = self.home / 'hold.py'
        worker.write_text(
            'import fcntl, os, sys, time\n'
            'fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n'
            'fcntl.flock(fd, fcntl.LOCK_EX)\n'
            'print("ready", flush=True)\n'
            'time.sleep(30)\n')
        lock = self.gate_dir / 'agent-task-gate.lock'
        process = subprocess.Popen([sys.executable, str(worker), str(lock)], stdout=subprocess.PIPE,
                                   text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.assertTrue(TASK_MODULE.gate_holder_alive(lock))
            (self.gate_dir / 'agent-task-gate.holder').write_text('{"pid": 99999999}')
            process.kill()
            process.wait(timeout=5)
            self.assertFalse(TASK_MODULE.gate_holder_alive(lock))
            with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)):
                with TASK_MODULE.exclusive_gate_lock(TASK_MODULE.Task(self.repo, '5')):
                    self.assertEqual(TASK_MODULE.gate_holder_pid(lock), os.getpid())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def test_gate_tmp_ignores_checkout_tmpdir_and_cleans_up_on_exception(self):
        wt, _ = self.started()
        seen = []

        def check_steps(*args, gate_environment, **kwargs):
            temporary = Path(gate_environment['TMPDIR']).resolve()
            seen.append(temporary)
            self.assertTrue(temporary.is_dir())
            self.assertFalse(temporary.is_relative_to(wt.resolve()))
            self.assertFalse(temporary.is_relative_to(self.repo.resolve()))
            self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(temporary.parent.stat().st_mode), 0o700)
            self.assertEqual([entry.name for entry in temporary.iterdir()], ['holder.pid'])
            self.assertEqual(int((temporary / 'holder.pid').read_text()), os.getpid())
            (temporary / 'leftover').write_text('gate data')
            raise RuntimeError('gate interrupted')

        with mock.patch.dict(os.environ, TMPDIR=str(wt), TASK_GATE_DIR=str(self.gate_dir)), \
                mock.patch.object(TASK_MODULE, 'run_check_steps', side_effect=check_steps):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'gate interrupted'):
                    TASK_MODULE.run_check(wt, '')
                self.assertFalse(seen[-1].exists())
        self.assertEqual(len(set(seen)), 2)

    def test_trusted_gate_allowlist_applies_to_all_steps_and_branch_env(self):
        wt, _ = self.started()
        (wt / '.pre-commit-config.yaml').write_text('repos: []\n')
        (wt / '.task.toml').write_text('[env]\nPROJECT_MODE = "from-branch"\n'
                                      'PRODUCT_DIR = "{repo}/data"\nWORK_DIR = "{worktree}/output"\n')
        self.git(wt, 'add', '.pre-commit-config.yaml', '.task.toml')
        self.git(wt, 'commit', '-qm', 'configure gate')
        (self.home / 'during-check.sh').write_text('env > "$HOME/just-env"\n')
        (self.home / 'during-check.sh').chmod(0o755)
        record_file = self.state() / 'task.json'
        record = json.loads(record_file.read_text())
        record['verify'] = ['env > "$HOME/verify-env"']
        record['revision'] = TASK_MODULE.record_revision(record)
        record_file.write_text(json.dumps(record))
        caller = dict(self.env, TMPDIR=str(wt / 'odd-tmp'), PYTHONPATH='/odd-python',
                      VIRTUAL_ENV='/odd-venv', FOO='caller-value', TASK_GATE_FAKE='caller-gate',
                      TASK_GATE_SLOT='999', HTTP_PROXY='http://caller.invalid',
                      LC_MESSAGES='C', LANG='C', TERM='dumb', SHELL='/bin/bash',
                      PROJECT_MODE='caller-mode')
        self.assertIn('PASS', self.raw_task('check', 'proj', '5', env=caller))
        environments = []
        for name in ('just', 'verify', 'pre-commit'):
            captured = dict(line.split('=', 1) for line in
                            (self.home / f'{name}-env').read_text().splitlines() if '=' in line)
            environments.append(captured)
            for key in ('PYTHONPATH', 'VIRTUAL_ENV', 'FOO', 'HTTP_PROXY', 'TASK_GATE_FAKE',
                        'TASK_GATE_DIR', 'TASK_RUN_ID', 'GIT_CONFIG_GLOBAL', 'GIT_AUTHOR_NAME'):
                self.assertNotIn(key, captured)
            for key in ('PATH', 'HOME', 'LC_MESSAGES', 'LANG', 'TERM', 'SHELL'):
                self.assertEqual(captured[key], caller[key])
            self.assertEqual(captured['PROJECT_MODE'], 'from-branch')
            self.assertEqual(captured['PRODUCT_DIR'], str(self.repo / 'data'))
            self.assertEqual(captured['WORK_DIR'], str(wt / 'output'))
            self.assertEqual(Path(captured['TMPDIR']).parent, Path('/tmp') / f'task-gate-{os.getuid()}')
            self.assertTrue(Path(captured['TMPDIR']).name.startswith('gate-'))
            self.assertNotEqual(captured['TASK_GATE_SLOT'], '999')
            self.assertEqual(int(captured['TASK_GATE_PORT_BASE']), 20000 + 200 * int(captured['TASK_GATE_SLOT']))
            self.assertTrue(captured['TASK_GATE_RUN_ID'])
            self.assertFalse(Path(captured['TMPDIR']).exists())
        self.assertEqual(len({e['TMPDIR'] for e in environments}), 1)

    def test_trusted_check_steps_direct_call_uses_isolation_and_rejects_bad_env(self):
        wt, _ = self.started()
        (self.home / 'during-check.sh').write_text('env > "$HOME/just-env"\n')
        (self.home / 'during-check.sh').chmod(0o755)
        with mock.patch.dict(os.environ, dict(self.env, FOO='caller', TMPDIR=str(wt))), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(TASK_MODULE.run_check_steps(wt, '', state=self.state()))
        captured = dict(line.split('=', 1) for line in (self.home / 'just-env').read_text().splitlines())
        self.assertNotIn('FOO', captured)
        self.assertNotEqual(captured['TMPDIR'], str(wt))
        self.assertFalse(Path(captured['TMPDIR']).exists())
        for config in ('[env]\nFOO = 1\n', '[env]\n"BAD=NAME" = "x"\n', 'env = "invalid"\n'):
            (wt / '.task.toml').write_text(config)
            with self.assertRaisesRegex(TASK_MODULE.Stop, '\\[env\\]'):
                TASK_MODULE.trusted_check_environment(wt, 'run', {})

    def test_gate_temp_sweeper_only_removes_old_dead_owned_gate_directories(self):
        root = self.home / 'gate-temp'
        root.mkdir()
        old = time.time() - 25 * 60 * 60
        for name, pid in [('gate-dead', '99999999'), ('gate-recent', '99999999'),
                          ('gate-live', str(os.getpid())), ('gate-denied', '77777777'),
                          ('gate-invalid', 'unknown'), ('gate-negative', '-1'),
                          ('gate-no-holder', None), ('unrelated', '99999999')]:
            directory = root / name
            directory.mkdir()
            (directory / 'keep').write_text('data')
            if pid is not None:
                (directory / 'holder.pid').write_text(pid)
            if name != 'gate-recent':
                os.utime(directory, (old, old))
        (root / 'gate-file').write_text('untouched')
        (root / 'gate-link').symlink_to(root / 'unrelated', target_is_directory=True)
        linked_holder = root / 'gate-linked-holder'
        linked_holder.mkdir()
        (linked_holder / 'holder.pid').symlink_to(root / 'unrelated/holder.pid')
        os.utime(linked_holder, (old, old))

        def probe(pid, signum):
            self.assertEqual(signum, 0)
            if pid == 99999999:
                raise ProcessLookupError
            if pid == 77777777:
                raise PermissionError
            self.assertEqual(pid, os.getpid())

        before = set(root.iterdir())
        with mock.patch.object(TASK_MODULE.os, 'kill', side_effect=probe):
            TASK_MODULE.sweep_gate_temporary(root)
        self.assertEqual(set(root.iterdir()), before - {root / 'gate-dead'})
        self.assertEqual((root / 'unrelated/keep').read_text(), 'data')
        self.assertTrue((root / 'gate-link').is_symlink())

    def test_gate_drill_real_concurrency_isolation_and_cleanup(self):
        repos = []
        for name in ('clean', 'clash', 'always'):
            repo = self.home / 'dev_workspace' / name
            repo.mkdir()
            self.git(repo, 'init', '-q', '-b', 'main')
            (repo / 'justfile').write_text('check:\n    true\n')
            (repo / 'kind').write_text(name)
            (repo / '.task.toml').write_text('drill_seeds = ["pinned"]\n[env]\nPRODUCT_DIR = "{repo}/data"\nWORK_DIR = "{worktree}/output"\n')
            (repo / '.pre-commit-config.yaml').write_text('repos: []\n')
            self.git(repo, 'add', '.')
            self.git(repo, 'commit', '-qm', 'stub gate')
            (repo / '.work').mkdir()
            (repo / '.work/pinned').write_text(name)
            (repo / '.work/extra').write_text(name)
            repos.append(repo)
        # The drill runs the full default gate: pre-commit over all files follows `just check`.
        # The `always` repo passes `just check` and fails only in pre-commit, so a skipped
        # pre-commit stage would wrongly report it as ok.
        pre_commit = self.home / 'bin/pre-commit'
        pre_commit.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
            import json, os, pathlib, sys
            assert not any(key in os.environ for key in ('PYTHONPATH', 'VIRTUAL_ENV', 'FOO', 'TASK_GATE_FAKE'))
            home = pathlib.Path(os.environ['HOME'])
            kind = pathlib.Path('kind').read_text()
            with (home / (kind + '-pre-commit')).open('a') as out:
                out.write(json.dumps([sys.argv[1:], os.environ['TMPDIR'], int(os.environ['TASK_GATE_SLOT'])]) + '\\n')
            sys.exit(1 if kind == 'always' else 0)
            '''))
        pre_commit.chmod(0o755)
        just = self.home / 'bin/just'
        just.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
            import json, os, pathlib, re, sys, time
            assert not any(key in os.environ for key in ('PYTHONPATH', 'VIRTUAL_ENV', 'FOO', 'TASK_GATE_FAKE'))
            if sys.argv[1:] != ['check']:
                sys.exit(1)
            home = pathlib.Path(os.environ['HOME'])
            kind = pathlib.Path('kind').read_text()
            name = pathlib.Path.cwd().name
            assert re.fullmatch(r'\\.drill-[0-9a-f]{6}-\\d+-(group|solo)', name), name
            assert len(name) < 24, name
            assert not re.search(r'sk-[A-Za-z0-9_-]{20,}|\\d{10,}', name), name
            temporary = pathlib.Path(os.environ['TMPDIR'])
            assert pathlib.Path('.work/pinned').read_text() == kind
            assert pathlib.Path('.work/extra').read_text() == kind
            assert os.environ['PRODUCT_DIR'] == str(home / 'dev_workspace' / kind / 'data')
            assert os.environ['WORK_DIR'] == str(pathlib.Path.cwd() / 'output')
            slot = int(os.environ['TASK_GATE_SLOT'])
            port = int(os.environ['TASK_GATE_PORT_BASE'])
            # Record first so a failed assertion below still leaves evidence for the test.
            with (home / (kind + '-env')).open('a') as out:
                out.write(json.dumps([str(temporary), slot, port]) + '\\n')
            assert temporary.is_dir(), temporary
            assert not temporary.resolve().is_relative_to(pathlib.Path.cwd().resolve()), temporary
            assert not temporary.resolve().is_relative_to((home / 'dev_workspace').resolve()), temporary
            assert temporary.stat().st_mode & 0o777 == 0o700, temporary
            assert temporary.parent.stat().st_mode & 0o777 == 0o700, temporary.parent
            assert [entry.name for entry in temporary.iterdir()] == ['holder.pid'], temporary
            os.kill(int((temporary / 'holder.pid').read_text()), 0)
            (temporary / 'leftover').write_text(kind)
            assert port == 20000 + 200 * slot, (slot, port)
            if kind == 'clean':
                (home / 'active').touch()
                deadline = time.monotonic() + 8
                while not (home / 'clashed').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert (home / 'clashed').exists(), 'gates did not run concurrently'
                (home / 'active').unlink()
            elif kind == 'clash' and (home / 'active').exists():
                (home / 'clashed').touch()
                sys.exit(1)
            elif kind == 'clash' and not (home / 'clashed').exists():
                deadline = time.monotonic() + 8
                while not (home / 'active').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                (home / 'clashed').touch()
                sys.exit(1)
            '''))
        just.chmod(0o755)
        out = self.raw_task('gate-drill', 'clean', 'clash', 'always',
                            '--seed', 'clean:extra', '--seed', 'clash:extra', '--seed', 'always:extra', code=1,
                            env=dict(self.env, TMPDIR=str(self.repo), PYTHONPATH='/odd-python',
                                     VIRTUAL_ENV='/odd-venv', FOO='caller', TASK_GATE_FAKE='caller'))
        self.assertIn('clash', out)
        self.assertIn('clean: solo: PASS, together: PASS, verdict: ok', out)
        self.assertIn('clash: solo: PASS, together: FAIL, verdict: clash', out)
        self.assertIn('always: solo: FAIL, together: FAIL, verdict: failing-alone (pre-existing failure, not a concurrency clash)', out)
        evidence = list((self.home / '.local/state/task/gate-drill').glob('*/results.json'))
        self.assertEqual(len(evidence), 1)
        rows = json.loads(evidence[0].read_text())
        logs = '\n'.join(log.read_text() for log in evidence[0].parent.glob('*/drill-check.log'))
        self.assertEqual([row['verdict'] for row in rows], ['ok', 'clash', 'failing-alone'], out + logs + repr(rows))
        self.assertTrue(all(row['concurrent']['duration'] > 0 for row in rows))
        concurrent_slots = []
        temporary_paths = []
        for name in ('clean', 'clash', 'always'):
            records = [json.loads(line) for line in (self.home / (name + '-env')).read_text().splitlines()]
            self.assertEqual(len(records), 2)
            concurrent_slots.append(records[0][1])
            for temporary, slot, port in records:
                temporary_paths.append(temporary)
                for repo in repos:
                    self.assertFalse(Path(temporary).resolve().is_relative_to(repo.resolve()))
                # Drill runs take no slot, so their port range starts above the 3 default slots.
                self.assertGreaterEqual(slot, 3)
                self.assertEqual(port, 20000 + 200 * slot)
                self.assertFalse(Path(temporary).exists())
            # pre-commit ran over all files after every passing `just check` (for `clash`
            # only the solo run), in the same isolated environment as that run.
            pre_commit_log = self.home / (name + '-pre-commit')
            pre_commit_records = [json.loads(line) for line in pre_commit_log.read_text().splitlines()]
            self.assertEqual([(temporary, slot) for _, temporary, slot in pre_commit_records],
                             [(temporary, slot) for temporary, slot, _ in
                              (records[1:] if name == 'clash' else records)], logs)
            self.assertEqual({tuple(arguments) for arguments, _, _ in pre_commit_records},
                             {('run', '--all-files')}, logs)
        self.assertEqual(sorted(concurrent_slots), [3, 4, 5])
        self.assertEqual(len(set(temporary_paths)), len(temporary_paths))
        self.assertIn('$ pre-commit run --all-files', logs)
        self.assertNotIn('--from-ref', logs)
        always_steps = [json.loads(path.read_text())['steps']
                        for path in sorted(evidence[0].parent.glob('2-*/drill-check.json'))]
        self.assertEqual([[(step['command'], step['exit']) for step in steps] for steps in always_steps],
                         [[(['just', 'check'], 0), (['pre-commit', 'run', '--all-files'], 1)]] * 2, always_steps)
        self.assertEqual([path for path in self.gate_dir.iterdir() if path.suffix != '.lock'], [])
        with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)):
            self.assertEqual(TASK_MODULE.gate_holders(), [])
        for repo in repos:
            self.assertEqual(list((repo / '.worktrees').iterdir()), [])
            self.assertEqual(len(self.git(repo, 'worktree', 'list').splitlines()), 1)

    def test_gate_slots_resources_exclusivity_and_status(self):
        config = self.home / '.config/task/gate.toml'
        config.parent.mkdir(parents=True, exist_ok=True)
        first = self.home / 'first-gate'
        second = self.home / 'second-gate'
        first.mkdir()
        second.mkdir()
        a = TASK_MODULE.Task(first, '1')
        b = TASK_MODULE.Task(second, '2')
        with mock.patch.dict(os.environ, HOME=str(self.home), TASK_GATE_DIR=str(self.gate_dir)), \
                mock.patch.object(TASK_MODULE, 'GATE_LOCK_WAIT', 0):
            self.assertEqual(TASK_MODULE.gate_slots(), 3)
            config.write_text('slots = 2\n')
            self.assertEqual(TASK_MODULE.gate_slots(), 2)
            with TASK_MODULE.exclusive_gate_lock(a):
                with TASK_MODULE.exclusive_gate_lock(b):
                    holders = TASK_MODULE.gate_holders()
                    self.assertEqual({row[0] for row in holders}, {'slot-0', 'slot-1'})
                (second / '.task.toml').write_text('exclusive_gate = true\n')
                with self.assertRaises(SystemExit) as stop:
                    with TASK_MODULE.exclusive_gate_lock(b):
                        self.fail('exclusive gate must wait for every slot')
                self.assertEqual(stop.exception.code, 3)
            with TASK_MODULE.exclusive_gate_lock(b):
                self.assertEqual(len(TASK_MODULE.gate_holders()), 2)
                with self.assertRaises(SystemExit):
                    with TASK_MODULE.exclusive_gate_lock(a):
                        self.fail('exclusive gate must block ordinary gates')
                (first / '.task.toml').write_text('exclusive_gate = false\ngate_resources = ["hermes"]\n')
                with TASK_MODULE.exclusive_gate_lock(a):
                    self.assertEqual(len(TASK_MODULE.gate_holders()), 2)
            for path in (first, second):
                (path / '.task.toml').write_text('gate_resources = ["z", "hermes", "a"]\n')
            with TASK_MODULE.exclusive_gate_lock(a):
                self.assertEqual([row[0] for row in TASK_MODULE.gate_holders() if row[0].startswith('resource')],
                                 ['resource-a', 'resource-hermes', 'resource-z'])
                with self.assertRaises(SystemExit):
                    with TASK_MODULE.exclusive_gate_lock(b):
                        self.fail('shared resources must serialize')
                (second / '.task.toml').write_text('gate_resources = ["other"]\n')
                with TASK_MODULE.exclusive_gate_lock(b):
                    output = io.StringIO()
                    with mock.patch.object(TASK_MODULE, 'WORKSPACE', self.home / 'dev_workspace'), \
                            contextlib.redirect_stdout(output):
                        TASK_MODULE.status(type('Args', (), {'repo': None})())
                    self.assertIn('Gate slots: 2', output.getvalue())
                    self.assertIn('gate resource-hermes:', output.getvalue())
                    self.assertIn('first-gate/T-1 since', output.getvalue())
            lock, holder = TASK_MODULE.gate_paths('slot-0')
            holder.write_text('{"pid": 99999999, "repo": "dead"}')
            with TASK_MODULE.exclusive_gate_lock(a):
                self.assertNotIn('dead', str(TASK_MODULE.gate_holders()))

    def test_gate_slot_only_from_held_slot_and_noslot_runs_get_free_port_ranges(self):
        gate = self.home / 'resource-gate'
        gate.mkdir()
        task = TASK_MODULE.Task(gate, '1')
        with mock.patch.dict(os.environ, HOME=str(self.home), TASK_GATE_DIR=str(self.gate_dir)), \
                mock.patch.object(TASK_MODULE, 'GATE_LOCK_WAIT', 0), \
                (self.gate_dir / 'agent-task-gate.lock').open('a') as slot_zero:
            fcntl.flock(slot_zero, fcntl.LOCK_EX)
            # Resource names that look like slots neither crash nor choose the slot.
            (gate / '.task.toml').write_text('gate_resources = ["slot-x", "slot-9"]\n')
            with TASK_MODULE.exclusive_gate_lock(task):
                self.assertEqual(TASK_MODULE.GATE_SLOT.get(), 1)
                with TASK_MODULE.gate_port_index(self.repo) as index:
                    self.assertEqual(index, 1)
            self.assertIsNone(TASK_MODULE.GATE_SLOT.get())
            # A trusted gate without a slot still gets a range no live gate uses.
            (gate / '.task.toml').write_text('exclusive_gate = false\n')
            with TASK_MODULE.exclusive_gate_lock(task):
                self.assertIsNone(TASK_MODULE.GATE_SLOT.get())
                with TASK_MODULE.gate_port_index(self.repo) as first, \
                        TASK_MODULE.gate_port_index(self.repo) as second:
                    self.assertEqual((first, second), (3, 4))
                    self.assertEqual(sorted(row[0] for row in TASK_MODULE.gate_holders()),
                                     ['noslot-0', 'noslot-1', 'slot-0'])
                with TASK_MODULE.gate_port_index(self.repo) as again:
                    self.assertEqual(again, 3)
        # Without a usable gate directory nothing can be reserved, so each process derives
        # its own index above the slots from its pid: two such runs never share a range.
        span = TASK_MODULE.GATE_PORT_INDEXES - 3 - 1
        missing = self.home / 'missing-gate-dir'
        with mock.patch.dict(os.environ, HOME=str(self.home), TASK_GATE_DIR=str(missing)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with TASK_MODULE.gate_port_index(self.repo) as unreserved:
                self.assertEqual(unreserved, 3 + 1 + os.getpid() % span)
        self.assertIn(f'using unreserved index {unreserved} derived from process id {os.getpid()}',
                      output.getvalue())
        probe = self.home / 'port-index-probe.py'
        probe.write_text(textwrap.dedent(f'''\
            import importlib.machinery, importlib.util, os, pathlib, sys
            loader = importlib.machinery.SourceFileLoader('task_probe', {str(TOOL)!r})
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader('task_probe', loader))
            loader.exec_module(module)
            with module.gate_port_index(pathlib.Path({str(self.repo)!r})) as index:
                print(os.getpid(), index)
            '''))
        runs = []
        env = dict(self.env, TASK_GATE_DIR=str(missing), PYTHONDONTWRITEBYTECODE='1')
        # Two processes get different ranges whenever their pids differ modulo the span of
        # free indexes; retry the second probe in the rare case the kernel hands out such a pid.
        for _attempt in range(6):
            if len(runs) == 2:
                break
            result = subprocess.run([sys.executable, '-I', str(probe)], env=env, cwd=self.home,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pid, index = (int(word) for word in result.stdout.split()[-2:])
            self.assertEqual(index, 3 + 1 + pid % span, result.stdout)
            self.assertIn(f'derived from process id {pid}', result.stdout)
            if runs and runs[0][0] % span == pid % span:
                continue
            runs.append((pid, index))
        self.assertEqual(len(runs), 2, runs)
        self.assertNotEqual(runs[0][0], runs[1][0], runs)
        self.assertNotEqual(runs[0][1], runs[1][1], runs)
        self.assertFalse(missing.exists())

    def test_wait_message_refreshes_when_holder_changes(self):
        (self.repo / '.task.toml').write_text('exclusive_gate = true\n')
        lock, holder = self.gate_dir / 'agent-task-gate.lock', self.gate_dir / 'agent-task-gate.holder'
        with lock.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            holder.write_text(json.dumps({'pid': os.getpid(), 'repo': 'old', 'task': 'T-1', 'since': 'before'}))
            calls = []
            def change(_):
                calls.append(True)
                if len(calls) == 1:
                    holder.write_text(json.dumps({'pid': os.getpid(), 'repo': 'new', 'task': 'T-2', 'since': 'after'}))
                else:
                    fcntl.flock(held, fcntl.LOCK_UN)
            output = io.StringIO()
            with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)), \
                    mock.patch.object(TASK_MODULE.time, 'sleep', side_effect=change), \
                    contextlib.redirect_stdout(output):
                with TASK_MODULE.exclusive_gate_lock(TASK_MODULE.Task(self.repo, '5')):
                    pass
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn('old/T-1', lines[0])
            self.assertIn('new/T-2', lines[1])
            self.assertNotIn('old/T-1', lines[1])

    def test_gate_os_errors_fall_back_only_without_exclusive_or_resources(self):
        # Only a plain one-slot gate may run unlocked after a lock error. A gate that is
        # exclusive or declares resources needs its isolation and stops instead.
        config_path = self.home / "gate-config"
        config_path.mkdir()
        task = TASK_MODULE.Task(self.repo, "5")
        for explicit in (False, True):
            for resource in (False, True):
                (config_path / ".task.toml").write_text(
                    ("exclusive_gate = true\n" if explicit else "") +
                    ('gate_resources = ["hermes"]\n' if resource else ''))
                for operation in ("open", "flock"):
                    self.check_gate_os_error(task, config_path, explicit, resource, operation)

    def check_gate_os_error(self, task, config_path, explicit, resource, operation):
        with self.subTest(explicit=explicit, resource=resource, operation=operation):
            stops = explicit or resource
            target = TASK_MODULE.os if operation == "open" else TASK_MODULE.fcntl
            output = io.StringIO()
            ran = []
            with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)), \
                    mock.patch.object(target, operation, side_effect=PermissionError("permission denied")), \
                    contextlib.redirect_stdout(output):
                if stops:
                    with self.assertRaisesRegex(TASK_MODULE.Stop, "permission denied"):
                        with TASK_MODULE.exclusive_gate_lock(task, config_path=config_path):
                            ran.append(True)
                else:
                    with TASK_MODULE.exclusive_gate_lock(task, config_path=config_path):
                        ran.append(True)
                self.assertEqual(TASK_MODULE.gate_holders(), [])
            self.assertEqual(ran, [] if stops else [True])
            if stops:
                self.assertNotIn("unlocked", output.getvalue())
            else:
                self.assertIn("permission denied", output.getvalue())
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
        (self.repo / '.task.toml').write_text('exclusive_gate = true\n')
        lock = self.gate_dir / "agent-task-gate.lock"
        holder = self.gate_dir / "agent-task-gate.holder"
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            holder.write_text(json.dumps({"repo": "proj", "task": "T-9", "pid": 99999999, "since": "today"}))
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
            self.assertIn(f"pid {os.getpid()} (holder details unavailable)", output.getvalue())
            self.assertNotIn('/? since ?', output.getvalue())
            self.assertIn('holder details unavailable', output.getvalue())
            self.assertNotIn('T-9', output.getvalue())
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
        # Includes AF_UNIX fixtures: nested gate TMPDIR paths exceed 108 bytes.
        socket_test = self._testMethodName == 'test_reliability_special_files_archive_manifest_and_publication'
        # Documented short AF_UNIX fallback, inside the lane's gate-temp grant.
        socket_root = TASK_MODULE.gate_temporary_root() if socket_test else None
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(
            dir=socket_root, prefix='s')))
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
            milestone: m-1
            ---
            ## Acceptance Criteria
            - [ ] #1 b exists
            - [ ] #2 checks pass
            ## Verification and tools
            Verify: `true`
            """))
        (self.kb / 'backlog/milestones').mkdir()
        (self.kb / 'backlog/milestones/m-1 - Goal.md').write_text('---\ntitle: Goal\n---\n')
        (self.kb / 'INTENT.md').write_text('---\ncurrent: m-1\n---\nPass test: ship the goal.\n')
        self.raw_task('plan', 'proj', '--sign')
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
        (work / 'locked').mkdir()
        (work / 'locked').chmod(0)
        self.addCleanup(lambda: (work / 'locked').chmod(0o700) if work.exists() else None)
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
        (work / 'big/empty/nested').mkdir(parents=True)
        (work / 'big/records').mkdir()
        (work / 'big/records/run.jsonl').write_text('{}\n')
        for name in ('prompt.txt', 'request.md', 'small.log', 'check.json',
                     'review.json', 'merge.json', 'manifest.json'):
            (work / 'big' / name).write_text('keep')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
        manifest = next(self.work_archives().glob('*/manifest.json'))
        record = json.loads(manifest.read_text())
        skipped = {entry['path']: entry for entry in record['skipped']}
        self.assertIn('pipe', skipped)
        self.assertIn('unreadable', skipped)
        self.assertIn('locked', skipped)
        self.assertFalse((manifest.parent / '.work/locked').exists())
        self.assertEqual(skipped['big']['size'], 201 * 1024 ** 2 + len('keep report') + 3 + 7 * 4)
        saved = manifest.parent / '.work'
        self.assertEqual((saved / 'big/report.md').read_text(), 'keep report')
        self.assertFalse((saved / 'big/data').exists())
        self.assertFalse((saved / 'big/empty').exists())
        self.assertEqual((saved / 'big/records/run.jsonl').read_text(), '{}\n')
        for name in ('prompt.txt', 'request.md', 'small.log', 'check.json',
                     'review.json', 'merge.json', 'manifest.json'):
            self.assertEqual((saved / 'big' / name).read_text(), 'keep')
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
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
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
        merge_time = self.git(self.repo, 'show', '-s', '--format=%cI', 'HEAD')
        self.task('plan', 'proj', '--sign')
        self.assertIn('merged since sign-off: 0/15', self.task('plan', 'proj'))
        (self.state() / 'run.json').write_text(json.dumps({'command': 'merge', 'state': 'failed'}))
        out = self.raw_task('repair', 'proj')
        self.assertIn('Repaired published merge', out)
        self.assertFalse(wt.exists())
        self.assertEqual(json.loads((self.state() / 'merge.json').read_text())['head'], head)
        self.assertEqual(json.loads((self.state() / 'merge.json').read_text())['time'], merge_time)
        self.assertIn('merged since sign-off: 0/15', self.task('plan', 'proj'))
        self.assertEqual(json.loads((self.state() / 'run.json').read_text())['state'], 'passed')
        self.assertTrue(list(self.work_archives().glob('*/.work/report.md')))
        (self.state() / 'run.json').write_text(json.dumps({'command': 'merge', 'state': 'failed'}))
        recorded_time = '2000-01-01T00:00:00.123456+00:00'
        evidence = self.state() / 'merge.json'
        evidence.write_text(json.dumps(dict(json.loads(evidence.read_text()), time=recorded_time)))
        self.raw_task('repair', 'proj')
        self.assertEqual(json.loads((self.state() / 'run.json').read_text())['state'], 'passed')
        self.assertEqual(json.loads(evidence.read_text())['time'], recorded_time)
        self.assertIn('merged since sign-off: 0/15', self.task('plan', 'proj'))

    def test_repair_skips_non_worktree_and_continues(self):
        wt, _ = self.started()
        self.commit_in(wt, 'published.txt')
        head = self.git(wt, 'rev-parse', 'HEAD')
        self.git(self.repo, 'merge', '--no-ff', '-m', 'published', head)
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        leftover = self.repo / '.worktrees/aaa-leftover'
        leftover.mkdir()
        (leftover / 'evidence.txt').write_text('keep')
        out = self.raw_task('repair', 'proj')
        self.assertIn(f'{leftover}: not a worktree; left as is', out)
        self.assertEqual((leftover / 'evidence.txt').read_text(), 'keep')
        self.assertFalse((self.state('aaa-leftover') / 'merge.json').exists())
        self.assertIn('Repaired published merge', out)
        self.assertEqual(json.loads((self.state() / 'merge.json').read_text())['head'], head)
        self.assertFalse(wt.exists())

    def test_repair_kept_worktree_and_reuses_finished_head_archive(self):
        wt, _ = self.started()
        self.commit_in(wt, 'published.txt')
        head = self.git(wt, 'rev-parse', 'HEAD')
        self.git(self.repo, 'merge', '--no-ff', '-m', 'published', head)
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
        original = list(self.work_archives().glob('*/manifest.json'))
        (wt / 'leftover.txt').write_text('keep worktree')
        for _ in range(2):
            self.raw_task('repair', 'proj')
            self.assertTrue(wt.exists())
            self.assertEqual(list(self.work_archives().glob('*/manifest.json')), original)
        (wt / 'leftover.txt').unlink()
        out = self.raw_task('repair', 'proj')
        self.assertIn('Reusing archive', out)
        self.assertFalse(wt.exists())
        self.assertEqual(list(self.work_archives().glob('*/manifest.json')), original)

    def test_gate_cleanup_warns_for_undeletable_subdirectory(self):
        self.started()
        gate = self.repo / '.worktrees/.task-gate-t5-99999999'
        (gate / 'locked').mkdir(parents=True)
        (gate / 'locked/file').write_text('held')
        (gate / 'locked').chmod(0o500)
        self.addCleanup(lambda: (gate / 'locked').chmod(0o700))
        failed = subprocess.CompletedProcess([], 1)
        warning = io.StringIO()
        with mock.patch.object(TASK_MODULE, 'run_git', return_value=failed), \
                contextlib.redirect_stderr(warning):
            TASK_MODULE.remove_gate_worktree(TASK_MODULE.Task(self.repo, '5'), gate)
        self.assertIn('Gate cleanup warning:', warning.getvalue())
        self.assertTrue((gate / 'locked/file').exists())

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
        locked = saved / '.work/large/locked'
        locked.mkdir()
        (locked / 'evidence.txt').write_text('keep inaccessible evidence')
        locked.chmod(0)
        self.addCleanup(lambda: locked.chmod(0o700))
        pending = root / '.pending-old'
        pending.mkdir()
        (pending / 'partial').write_text('partial')
        # A live numeric PID alone is not a live kernel gate holder.
        stale = root / f'.task-gate-t5-{os.getpid()}-copy'
        stale.mkdir()
        (stale / 'cache').write_text('cache')
        orphan = root / 'other/.pending-unfinished'
        orphan.mkdir(parents=True)
        for copy in (pending, stale, orphan):
            (copy / '.work/records').mkdir(parents=True)
            (copy / '.work/check.log').write_text('check evidence')
            (copy / '.work/report.md').write_text('report evidence')
            (copy / '.work/records/run.jsonl').write_text('{}\n')
            if copy != orphan:
                (copy / 'manifest.json').write_text('{}')
        out = self.raw_task('repair', '--prune-archive', '--dry-run', 'proj')
        self.assertIn('Would free', out)
        self.assertTrue(pending.exists())
        self.assertTrue(stale.exists())
        self.assertTrue(orphan.exists())
        self.assertIn('Would finish', out)
        self.assertTrue((saved / '.work/fakehome').exists())
        self.assertIn('Skipped ' + str(locked), out)
        self.assertEqual((saved / 'manifest.json').read_text(), '{}')
        out = self.raw_task('repair', '--prune-archive', 'proj')
        self.assertIn('Freed', out)
        self.assertFalse(pending.exists())
        self.assertFalse(stale.exists())
        self.assertFalse(orphan.exists())
        recovered = orphan.with_name('other-unfinished')
        manifest = json.loads((recovered / 'manifest.json').read_text())
        self.assertEqual(manifest['note'], 'finished by repair')
        self.assertIn('.work/report.md', [entry['path'] for entry in manifest['contents']])
        duplicate = recovered.parent / '.pending-later'
        duplicate.mkdir()
        (duplicate / 'partial').write_text('duplicate')
        self.raw_task('repair', '--prune-archive', 'proj')
        self.assertFalse(duplicate.exists())
        for name in ('.work/check.log', '.work/report.md', '.work/records/run.jsonl', 'manifest.json'):
            self.assertTrue((recovered / name).is_file())
        self.assertFalse((saved / '.work/fakehome/state').exists())
        self.assertFalse((saved / '.work/fakehome').exists())
        self.assertFalse((saved / '.work/large/data').exists())
        self.assertEqual((saved / '.work/large/merge.json').read_text(), '{}')
        self.assertTrue(locked.exists())
        record = json.loads((saved / 'manifest.json').read_text())
        self.assertIn('.work/large/locked', [entry['path'] for entry in record['skipped']])
        locked.chmod(0o700)
        self.assertEqual((locked / 'evidence.txt').read_text(), 'keep inaccessible evidence')
        for name in ('.work/report.md', '.work/gateway.log', 'manifest.json'):
            self.assertTrue((saved / name).is_file())

    def test_prune_archive_retains_gate_with_kernel_holder(self):
        gate = self.work_archives() / '.task-gate-t5-99999999-copy'
        (gate / '.work/records').mkdir(parents=True)
        (gate / '.work/check.log').write_text('check')
        (gate / '.work/report.md').write_text('report')
        (gate / '.work/records/run.jsonl').write_text('{}\n')
        with (self.gate_dir / 'agent-task-gate.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.raw_task('repair', '--prune-archive', 'proj')
            self.assertTrue((gate / '.work/check.log').exists())
        self.raw_task('repair', '--prune-archive', 'proj')
        self.assertFalse(gate.exists())

    def test_archive_and_prune_named_virtualenv_and_runtime(self):
        wt, _ = self.started()
        venv = wt / '.work/t8venv'
        (venv / 'lib').mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text('home = /usr/bin\n')
        (venv / 'lib/package.py').write_text('runtime')
        (venv / 'report.md').write_text('venv evidence')
        for name in ('node_modules', '.pytest_cache'):
            (wt / '.work' / name).mkdir()
        (wt / '.work/node_modules/.package-lock.json').write_text('{}')
        (wt / '.work/node_modules/package.json').write_text('{}')
        (wt / '.work/.pytest_cache/README.md').write_text('cache')
        (venv / 'lib/README.md').write_text('package readme')
        (venv / 'lib/direct_url.json').write_text('{}')
        for name in ('report.md', 'check.log', 'merge.json'):
            (wt / '.work' / name).write_text('keep')
        for name in ('large', 'reports'):
            directory = wt / '.work' / name
            directory.mkdir()
            with (directory / 'large.log').open('wb') as stream:
                stream.truncate(201 * 1024 ** 2)
            (directory / 'report.md').write_text('report')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
        archived = next(self.work_archives().glob('*/.work'))
        self.assertFalse((archived / 't8venv/lib').exists())
        self.assertFalse((archived / 't8venv/pyvenv.cfg').exists())
        for name in ('t8venv', 'node_modules', '.pytest_cache'):
            self.assertFalse((archived / name).exists())
        self.assertEqual((archived / 'large/report.md').read_text(), 'report')
        self.assertFalse((archived / 'large/large.log').exists())
        self.assertEqual((archived / 'reports/report.md').read_text(), 'report')
        for name in ('report.md', 'check.log', 'merge.json'):
            self.assertEqual((archived / name).read_text(), 'keep')

        root = self.work_archives()
        finished = root / 'old-finished'
        pending = root / 'other/.pending-runtime'
        for container in (finished, pending):
            work = container / '.work'
            (work / 't8venv/lib').mkdir(parents=True)
            (work / 't8venv/pyvenv.cfg').write_text('home = /usr/bin\n')
            (work / 't8venv/lib/package.py').write_text('runtime')
            (work / 't8venv/lib/README.md').write_text('package readme')
            (work / 't8venv/lib/direct_url.json').write_text('{}')
            (work / 'node_modules').mkdir()
            (work / 'node_modules/.package-lock.json').write_text('{}')
            (work / 'node_modules/package.json').write_text('{}')
            (work / '.pytest_cache').mkdir()
            (work / '.pytest_cache/README.md').write_text('cache')
            (work / 'reports').mkdir()
            with (work / 'reports/large.log').open('wb') as stream:
                stream.truncate(201 * 1024 ** 2)
            for name in ('report.md', 'check.log', 'merge.json'):
                (work / name).write_text('keep')
                (work / 'reports' / name).write_text('keep report')
            (work / 'runtime').mkdir()
            with (work / 'runtime/data').open('wb') as stream:
                stream.truncate(201 * 1024 ** 2)
            for name in ('report.md', 'prompt.md', 'request.md', 'check.json',
                         'review.json', 'merge.json', 'small.log', 'manifest.json', 'run.jsonl'):
                (work / 'runtime' / name).write_text('evidence')
            (work / 'records').mkdir()
            (work / 'records/run.jsonl').write_text('{}\n')
            with (work / 'records/data').open('wb') as stream:
                stream.truncate(201 * 1024 ** 2)
            (work / 't8venv/request.md').write_text('keep nested evidence')
        (finished / 'manifest.json').write_text('{"head": "original"}')
        before = {path: (path.stat().st_size,
                         path.read_bytes() if path.stat().st_size < 1024 else None)
                  for path in root.rglob('*') if path.is_file()}
        out = self.raw_task('repair', '--prune-archive', '--dry-run', 'proj')
        self.assertIn(str(pending / '.work/t8venv'), out)
        self.assertIn(str(finished / '.work/runtime'), out)
        for path, (size, content) in before.items():
            self.assertEqual(path.stat().st_size, size)
            if content is not None:
                self.assertEqual(path.read_bytes(), content)
        self.raw_task('repair', '--prune-archive', 'proj')
        recovered = pending.with_name('other-runtime')
        for container in (finished, recovered):
            self.assertFalse((container / '.work/t8venv/lib').exists())
            self.assertFalse((container / '.work/t8venv/pyvenv.cfg').exists())
            self.assertFalse((container / '.work/runtime/data').exists())
            for name in ('t8venv', 'node_modules', '.pytest_cache'):
                self.assertFalse((container / '.work' / name).exists())
            for name in ('report.md', 'prompt.md', 'request.md', 'check.json',
                         'review.json', 'merge.json', 'small.log', 'manifest.json', 'run.jsonl'):
                self.assertEqual((container / '.work/runtime' / name).read_text(), 'evidence')
            self.assertEqual((container / '.work/records/run.jsonl').read_text(), '{}\n')
            self.assertFalse((container / '.work/records/data').exists())
            for name in ('report.md', 'check.log', 'merge.json'):
                self.assertEqual((container / '.work' / name).read_text(), 'keep')
                self.assertEqual((container / '.work/reports' / name).read_text(), 'keep report')
            record = json.loads((container / 'manifest.json').read_text())
            self.assertTrue(any(line.startswith('pruned: .work/runtime/data ')
                                for line in record['pruned']))
            self.assertTrue(any(line.startswith('pruned: .work/t8venv ')
                                for line in record['pruned']))
        self.assertEqual(json.loads((finished / 'manifest.json').read_text())['head'], 'original')
        manifest = (finished / 'manifest.json').read_bytes()
        self.raw_task('repair', '--prune-archive', 'proj')
        self.assertEqual((finished / 'manifest.json').read_bytes(), manifest)

    def test_oversized_directory_keeps_broad_evidence_in_archive_and_repair(self):
        wt, _ = self.started()
        finished = self.work_archives() / 'old-finished'
        pending = self.work_archives() / 'other/.pending-broad'
        names = ['report.json', 'report.txt', 'run.json', 'exception.json',
                 'task.json', 'gate-check.json', 'arbitrary.json']
        names += ['notes' + suffix for suffix in
                  ('.md', '.jsonl', '.txt', '.html', '.toml', '.yaml', '.yml', '.csv', '.log')]
        names += [word + '.dat' for word in
                  ('report', 'prompt', 'request', 'check', 'review', 'merge',
                   'manifest', 'run', 'task', 'exception', 'gate')]
        for container in (wt, finished, pending):
            directory = container / '.work/big'
            directory.mkdir(parents=True)
            for name in names:
                (directory / name).write_text('evidence')
            for name, megabytes in (('payload.bin', 150), ('state.sqlite', 100)):
                with (directory / name).open('wb') as stream:
                    stream.truncate(megabytes * 1024 ** 2)
            (directory / 'empty').mkdir()
        (finished / 'manifest.json').write_text('{}')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
        archived = next(path.parent.parent for path in self.work_archives().glob('*/.work/big')
                        if path.parent.parent != finished)
        self.raw_task('repair', '--prune-archive', 'proj')
        for container in (archived, finished, pending.with_name('other-broad')):
            directory = container / '.work/big'
            for name in names:
                self.assertEqual((directory / name).read_text(), 'evidence')
            for name in ('payload.bin', 'state.sqlite', 'empty'):
                self.assertFalse((directory / name).exists())

    def test_archive_runtime_detects_socket_without_suffix(self):
        # The lane sandbox prohibits socket creation; use its lstat mode directly.
        info = mock.Mock(st_mode=stat.S_IFSOCK | 0o600)
        self.assertTrue(TASK_MODULE.archive_runtime(Path('gateway'), info))

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

    def test_gate_cleanup_preserves_numeric_task_name_and_live_pid(self):
        self.started()
        name = "pm-task-fixes-2"
        live = self.repo / f".worktrees/.task-gate-{name}-{os.getpid()}"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(live), "main")
        for suffix in ("", "-abc_1234"):
            gate = self.repo / f".worktrees/.task-gate-{name}-99999999{suffix}"
            self.git(self.repo, "worktree", "add", "-q", "--detach", str(gate), "main")
            (gate / ".work").mkdir()
            (gate / ".work/report.md").write_text(f"evidence{suffix}")
        self.raw_task("repair", "proj")
        self.assertTrue(live.is_dir())
        gates = list((self.repo / ".worktrees").glob(".task-gate-*"))
        self.assertEqual(gates, [live])
        root = self.home / f".local/state/task/archive/proj/{name}"
        reports = list(root.glob("*/.work/report.md"))
        self.assertEqual({p.read_text() for p in reports}, {"evidence", "evidence-abc_1234"})

    def test_offline_repair_fixes_locks_lanes_and_uses_local_default(self):
        wt, _ = self.started()
        lane = wt / ".work/lane.json"
        lane.write_text(json.dumps({"state": "running", "pid": 99999999}))
        lock = self.state() / "run.lock"
        lock.write_text(json.dumps({"pid": 99999999}))
        run = self.state() / "run.json"
        run.write_text(json.dumps({"state": "running", "pid": 99999999}))
        self.git(self.repo, "remote", "set-url", "origin", str(self.home / "missing-remote"))
        out = self.raw_task("repair", "proj")
        self.assertEqual(out.count("Repair warning:"), 1)
        self.assertFalse(lock.exists())
        self.assertEqual(json.loads(run.read_text())["state"], "interrupted")
        self.assertEqual(json.loads(lane.read_text())["state"], "interrupted")
        # The local default may contain published work absent from origin/main.
        self.commit_in(wt, "offline-published.txt")
        head = self.git(wt, "rev-parse", "HEAD")
        self.git(self.repo, "merge", "--no-ff", "-m", "published offline", head)
        out = self.raw_task("repair", "proj")
        self.assertIn("Repaired published merge", out)
        self.assertFalse(wt.exists())
        self.assertEqual(json.loads((self.state() / "merge.json").read_text())["head"], head)

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
        report_at = out.index("in `.work/report.md`", check_at)
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

    def test_status_lists_idle_unmerged_worktrees_and_never_removes_them(self):
        old = int(time.time()) - 10 * 86400
        stamp = {'GIT_AUTHOR_DATE': f'@{old} +0000', 'GIT_COMMITTER_DATE': f'@{old} +0000'}
        for name, idle in (('stale', True), ('recent', False)):
            path = self.repo / '.worktrees' / name
            self.git(self.repo, 'worktree', 'add', '-q', '-b', name, str(path))
            (path / f'{name}.txt').write_text(name)
            self.git(path, 'add', f'{name}.txt')
            subprocess.run(['git', '-C', str(path), 'commit', '-qm', name], check=True,
                           env=dict(self.env, **(stamp if idle else {})), capture_output=True)
            if idle:
                gitdir = Path(self.git(path, 'rev-parse', '--absolute-git-dir'))
                for marker in (path / '.git', gitdir / 'logs/HEAD'):
                    os.utime(marker, (old, old))
        merged = self.repo / '.worktrees/merged'
        self.git(self.repo, 'worktree', 'add', '-q', '-b', 'merged', str(merged))
        for marker in (merged / '.git', Path(self.git(merged, 'rev-parse', '--absolute-git-dir')) / 'logs/HEAD'):
            os.utime(marker, (old, old))
        for args in ((), ('proj',)):
            attention = self.raw_task('status', *args).split('Needs attention:', 1)[1]
            self.assertIn(f"idle worktree: proj {self.repo / '.worktrees/stale'} (branch stale, idle 10 days, "
                          "unmerged): finish, adopt or delete", attention)
            self.assertNotIn('recent', attention)
            self.assertNotIn('.worktrees/merged', attention)
        for name in ('stale', 'recent', 'merged'):
            self.assertTrue((self.repo / '.worktrees' / name).is_dir())

    def test_status_lists_locked_idle_unmerged_worktrees_with_the_lock_reason(self):
        old = int(time.time()) - 10 * 86400
        stamp = {'GIT_AUTHOR_DATE': f'@{old} +0000', 'GIT_COMMITTER_DATE': f'@{old} +0000'}
        for name, reason in (('held', ['--reason', 'session 42 running']), ('bare', [])):
            path = self.repo / '.worktrees' / name
            self.git(self.repo, 'worktree', 'add', '-q', '-b', name, str(path))
            (path / f'{name}.txt').write_text(name)
            self.git(path, 'add', f'{name}.txt')
            subprocess.run(['git', '-C', str(path), 'commit', '-qm', name], check=True,
                           env=dict(self.env, **stamp), capture_output=True)
            for marker in (path / '.git', Path(self.git(path, 'rev-parse', '--absolute-git-dir')) / 'logs/HEAD'):
                os.utime(marker, (old, old))
            self.git(self.repo, 'worktree', 'lock', *reason, str(path))
        for args in ((), ('proj',)):
            attention = self.raw_task('status', *args).split('Needs attention:', 1)[1]
            self.assertIn(f"idle worktree: proj {self.repo / '.worktrees/held'} (branch held, idle 10 days, "
                          "unmerged, locked: session 42 running): finish, adopt or delete", attention)
            self.assertIn(f"idle worktree: proj {self.repo / '.worktrees/bare'} (branch bare, idle 10 days, "
                          "unmerged, locked): finish, adopt or delete", attention)
        for name in ('held', 'bare'):
            self.assertTrue((self.repo / '.worktrees' / name).is_dir())

    def test_status_lists_idle_unmerged_codex_worktrees(self):
        old = int(time.time()) - 10 * 86400
        stamp = {'GIT_AUTHOR_DATE': f'@{old} +0000', 'GIT_COMMITTER_DATE': f'@{old} +0000'}
        codex = self.home / '.codex/worktrees/ab12/proj'
        recent = self.home / '.codex/worktrees/cd34/proj'
        for path, branch, idle in ((codex, 'codex-idle', True), (recent, 'codex-recent', False)):
            self.git(self.repo, 'worktree', 'add', '-q', '-b', branch, str(path))
            (path / f'{branch}.txt').write_text(branch)
            self.git(path, 'add', f'{branch}.txt')
            subprocess.run(['git', '-C', str(path), 'commit', '-qm', branch], check=True,
                           env=dict(self.env, **(stamp if idle else {})), capture_output=True)
            if idle:
                for marker in (path / '.git', Path(self.git(path, 'rev-parse', '--absolute-git-dir')) / 'logs/HEAD'):
                    os.utime(marker, (old, old))
        for args in ((), ('proj',)):
            attention = self.raw_task('status', *args).split('Needs attention:', 1)[1]
            self.assertIn(f"idle worktree: proj {codex} (branch codex-idle, idle 10 days, "
                          "unmerged): finish, adopt or delete", attention)
            self.assertNotIn('codex-recent', attention)
        self.assertTrue(codex.is_dir() and recent.is_dir())

    def test_status_lists_idle_unmerged_worktrees_outside_the_managed_folders(self):
        old = int(time.time()) - 10 * 86400
        stamp = {'GIT_AUTHOR_DATE': f'@{old} +0000', 'GIT_COMMITTER_DATE': f'@{old} +0000'}
        elsewhere = self.home / 'elsewhere/lane'
        recent = self.home / 'elsewhere/recent'
        for path, branch, idle in ((elsewhere, 'elsewhere-idle', True), (recent, 'elsewhere-recent', False)):
            self.git(self.repo, 'worktree', 'add', '-q', '-b', branch, str(path))
            (path / f'{branch}.txt').write_text(branch)
            self.git(path, 'add', f'{branch}.txt')
            subprocess.run(['git', '-C', str(path), 'commit', '-qm', branch], check=True,
                           env=dict(self.env, **(stamp if idle else {})), capture_output=True)
            if idle:
                for marker in (path / '.git', Path(self.git(path, 'rev-parse', '--absolute-git-dir')) / 'logs/HEAD'):
                    os.utime(marker, (old, old))
        for args in ((), ('proj',)):
            attention = self.raw_task('status', *args).split('Needs attention:', 1)[1]
            self.assertIn(f"idle worktree: proj {elsewhere} (branch elsewhere-idle, idle 10 days, "
                          "unmerged): finish, adopt or delete", attention)
            self.assertNotIn('elsewhere-recent', attention)
        self.assertTrue(elsewhere.is_dir() and recent.is_dir())

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
        task_file.write_text(source.read_text().replace('id:', 'milestone: m-1\nid:', 1))
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
        self.assertTrue((self.state() / "check.log").is_file())
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

    def test_advisory_check_holds_one_slot_and_resources_during_run(self):
        wt, _ = self.started()
        (wt / ".task.toml").write_text('exclusive_gate = true\ngate_resources = ["shared"]\n')
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "enable shared check gate")
        (self.home / "during-check.sh").write_text(
            'test -s "$TASK_GATE_DIR/agent-task-gate.holder" || exit 7\n'
            'test -s "$TASK_GATE_DIR/agent-task-gate-resource-shared.holder" || exit 8\n'
            'test ! -e "$TASK_GATE_DIR/agent-task-gate-slot-1.holder" || exit 9\n'
            'test ! -e "$TASK_GATE_DIR/agent-task-gate-slot-2.holder" || exit 10\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.sandbox_state()
        out = self.raw_task("check", "proj", "5", env=dict(self.env, CLAUDECODE="1"))
        self.assertIn("PASS", out)
        self.assertFalse((self.gate_dir / "agent-task-gate.holder").exists(),
                         "the shared gate holder should be released after the advisory run")

    def test_advisory_resources_exclude_other_gates_but_not_other_slots(self):
        self.commit_in(self.repo, '.task.toml', 'exclusive_gate = true\ngate_resources = ["shared"]\n')
        other = self.home / 'other'
        other.mkdir()
        task = TASK_MODULE.Task(self.repo, '5')
        second = TASK_MODULE.Task(other, '6')
        with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)), \
                mock.patch.object(TASK_MODULE, 'GATE_LOCK_WAIT', 0):
            with TASK_MODULE.exclusive_gate_lock(task, advisory=True):
                with TASK_MODULE.exclusive_gate_lock(second):
                    self.assertEqual(TASK_MODULE.GATE_SLOT.get(), 1)
                (other / '.task.toml').write_text('gate_resources = ["shared"]\n')
                with self.assertRaises(SystemExit) as caught:
                    with TASK_MODULE.exclusive_gate_lock(second):
                        self.fail('shared resource was not locked')
                self.assertEqual(caught.exception.code, 3)

    def test_drill_cleanup_old_and_new_names_preserves_live_and_unrelated(self):
        parent = self.repo / '.worktrees'
        parent.mkdir(exist_ok=True)
        stale = ['.task-drill-1792000000000000000-99999999-0-concurrent',
                 '.drill-abcdef-0-group', '.drill-abcdef-0-solo']
        kept = [f'.task-drill-1792000000000000000-{os.getpid()}-0-solo',
                '.drill-fedcba-0-group', '.drill-unrelated']
        for name in stale + kept:
            path = parent / name
            self.git(self.repo, 'worktree', 'add', '-q', '--detach', str(path), 'HEAD')
            if name == '.drill-fedcba-0-group':
                (path / '.work').mkdir(exist_ok=True)
                (path / '.work/drill.pid').write_text(str(os.getpid()))
        with mock.patch.dict(os.environ, self.env):
            TASK_MODULE.prune_drill_worktrees(self.repo)
        self.assertEqual(sorted(path.name for path in parent.iterdir()), sorted(kept))

    def test_bare_just_check_does_not_expand_unset_proxy_bypass_vars(self):
        justfile = (ROOT / "justfile").read_text()
        self.assertIn("check:\n", justfile, "inspect the repository's real justfile")
        self.assertIn("bash ci/lint.sh", justfile, "the check recipe should call ci/lint.sh")
        for path in (ROOT / "justfile", ROOT / "ci/lint.sh"):
            source = path.read_text()
            for expansion in ("$NO_PROXY", "${NO_PROXY", "$no_proxy", "${no_proxy"):
                self.assertNotIn(expansion, source,
                                 f"{path.relative_to(ROOT)} must not expand unset {expansion}")

    def test_advisory_check_tmpdir_is_outside_the_checkout(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        (wt / ".work/tmp").mkdir(parents=True, exist_ok=True)
        outside = self.home / "lane-tmp"
        outside.mkdir()
        guard = f'case "$(realpath "$TMPDIR")" in "{wt.resolve()}"*) exit 1;; esac; test -d "$TMPDIR"'
        (self.home / "during-check.sh").write_text(f'{guard} || exit 7\necho "$TMPDIR" >> "$HOME/gate-tmp"\n')
        (self.home / "during-check.sh").chmod(0o755)
        task_record_path = self.state() / "task.json"
        record = json.loads(task_record_path.read_text())
        record["verify"] = [f'{guard} && echo "$TMPDIR" >> "$HOME/verify-tmp"']
        record["revision"] = TASK_MODULE.record_revision(record)
        task_record_path.write_text(json.dumps(record))
        self.sandbox_state()
        (wt / ".work/lane.json").write_text(json.dumps({"state": "running", "temporary": str(outside)}))
        out = self.raw_task("check", "proj", "5", env=dict(self.env, CLAUDECODE="1", TMPDIR=str(wt / ".work/tmp")))
        self.assertIn("PASS", out)
        for name in ("gate", "verify"):
            seen = Path((self.home / f"{name}-tmp").read_text().splitlines()[-1])
            self.assertEqual(seen.parent, outside, "the lane's recorded temporary directory is the parent")
            self.assertFalse(seen.exists(), "the advisory check TMPDIR is removed afterwards")
        shared = self.home / f"task-gate-{os.getuid()}"
        shared.mkdir()
        for record in (None, {"temporary": str(self.home / "gone")}, {"temporary": str(wt / ".work/tmp")}):
            for inherited in (wt / ".work/tmp", shared):
                with self.subTest(record=record, inherited=inherited):
                    if record is None:
                        (wt / ".work/lane.json").unlink(missing_ok=True)
                    else:
                        (wt / ".work/lane.json").write_text(json.dumps(record))
                    with mock.patch.dict(os.environ, TMPDIR=str(inherited)):
                        self.assertIsNone(TASK_MODULE.advisory_temporary_parent(wt),
                                          "no inherited TMPDIR is ever adopted as the parent")

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
            self.assertEqual(call["tmpdir"], str(wt / ".work/tmp"))
            self.assertEqual(call["task_worktree"], str(wt))
            self.assertTrue(Path(call["tmpdir"]).is_dir())
            recorded = Path(json.loads((wt / ".work/lane.json").read_text())["temporary"])
            self.assertRegex(recorded.name, r"^lane-[0-9a-f]{8}$")
            self.assertTrue(recorded.is_relative_to(Path("/tmp") / f"claude-{os.getuid()}"))
            self.assertTrue(recorded.is_dir())
            self.assertEqual(TASK_MODULE.advisory_temporary_parent(wt), str(recorded))
            self.assertEqual(call["prompt"], 'Read the pinned task context in .work/context/ before working.\n\n' + prompt)
            self.assertEqual(call["args"], ["exec", "--sandbox", "workspace-write", "--model", "gpt-6.1-sol",
                                            "--output-last-message", str(wt / ".work/final.md"), "-"])
            self.assertFalse(any(name in TASK_MODULE.GIT_OVERRIDE_ENV or
                                 name.startswith(("GIT_CONFIG", "TASK_RUN_")) for name in call["env_names"]))
            self.assertIn("GIT_AUTHOR_NAME", call["env_names"])
            self.assertIn("GIT_COMMITTER_EMAIL", call["env_names"])
            self.assertIn("lane: running", self.raw_task("status", "proj"))
            self.task('boost', '--codex-until', '2099-01-01T00:00:00+00:00')
            refused = self.raw_task("lane", "proj", "5", code=1)
            self.assertIn("Lane already running", refused)
            self.assertNotIn("boost:", refused)
        finally:
            release.touch()
            record = self.wait_for_lane(wt)
        self.assertEqual(record["exit_code"], 0)
        self.assertFalse(recorded.exists(), "the Codex lane's recorded temporary directory is removed")
        self.assertEqual((wt / ".work/final.md").read_text(), "codex-final: Read the pinned task context in .work/context/ before working.\n\n" + prompt)
        self.assertIn("codex-log", (wt / ".work/lane.log").read_text())
        self.assertIn("lane: finished (exit 0)", self.raw_task("status", "proj"))
        self.assertFalse((self.home / "runner-record.json").exists())

    def assert_claude_temporary_path(self, temporary, call=None):
        root = Path("/tmp") / f"claude-{os.getuid()}"
        # Locate the outer grant from the fixture's actual location, independently
        # of the TMPDIR passed to the product under test.
        parents = [candidate for candidate in root.glob("lane-????????")
                   if self.home.resolve().is_relative_to(candidate)]
        self.assertLessEqual(len(parents), 1)
        parent = parents[0] if parents else root
        trusted = parent != root
        self.assertEqual(temporary.parent, parent)
        self.assertRegex(temporary.name, r"^lane-[0-9a-f]{8}$")
        # PM-approved bounds: top-level <40, nested <54 bytes.
        self.assertLess(len(os.fsencode(temporary)), 54 if trusted else 40)
        self.assertLess(len(os.fsencode(temporary / "srt-mux-3583008-1.sock")), 108)
        if call is None:
            self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o700)
            self.assertEqual(temporary.stat().st_uid, os.getuid())
        else:
            self.assertEqual(call["tmp_mode"], 0o700)
            self.assertEqual(call["tmp_owner"], os.getuid())

    def test_claude_temporary_parent_trust_and_cleanup(self):
        root = self.home / "claude-root"
        root.mkdir(mode=0o700)
        outer = root / "lane-1234abcd"
        outer.mkdir(mode=0o700)
        unrelated = self.home / "unrelated"
        unrelated.mkdir(mode=0o700)
        alias = self.home / "outer-link"
        alias.symlink_to(outer)
        fixture = outer / "policy-fixture-example"
        fixture.mkdir(mode=0o700)
        task = TASK_MODULE.Task(self.repo, "5")
        real_lstat = Path.lstat
        for candidate, mode, owner, nested in (
                (outer, 0o700, os.getuid(), True),
                (alias, 0o700, os.getuid(), True),
                (fixture, 0o700, os.getuid(), True),
                (unrelated, 0o700, os.getuid(), False),
                (outer, 0o755, os.getuid(), False),
                (outer, 0o700, os.getuid() + 1, False)):
            with self.subTest(candidate=candidate, mode=mode, owner=owner):
                outer.chmod(mode)

                def lane_stat(path, *args, **kwargs):
                    info = real_lstat(path, *args, **kwargs)
                    if path == outer:
                        return mock.Mock(st_mode=info.st_mode, st_uid=owner)
                    return info

                with mock.patch.object(TASK_MODULE, "claude_temporary_root", return_value=root), \
                        mock.patch.dict(os.environ, TMPDIR=str(candidate)), \
                        mock.patch.object(Path, "lstat", lane_stat):
                    env = TASK_MODULE.lane_environment(task, builder="claude")
                    temporary = Path(env["TMPDIR"])
                    self.assertEqual(temporary.parent, outer if nested else root)
                    self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o700)
                    self.assertEqual(temporary.stat().st_uid, os.getuid())
                    (temporary / "probe").write_text("private")
                    # Cancellation must validate the recorded path independently
                    # of the cancelling process's environment.
                    with mock.patch.dict(os.environ, TMPDIR=str(unrelated)):
                        TASK_MODULE.cleanup_lane_temporary(str(temporary))
                    self.assertFalse(temporary.exists())
                    self.assertTrue(outer.is_dir())
        outer.chmod(0o700)
        with mock.patch.object(TASK_MODULE, "claude_temporary_root", return_value=root):
            with self.assertRaises(TASK_MODULE.Stop):
                TASK_MODULE.cleanup_lane_temporary(str(unrelated))
            linked = root / "lane-deadbeef"
            linked.symlink_to(unrelated)
            with self.assertRaises(TASK_MODULE.Stop):
                TASK_MODULE.cleanup_lane_temporary(str(linked / "lane-12345678"))
        self.assertTrue(unrelated.is_dir())

    def test_fixture_subprocess_uses_isolated_tmpdir(self):
        fixture = Path(self.env['TMPDIR'])
        self.assertTrue(fixture.name.startswith('policy-fixture-'))
        script = self.home / 'temporary-probe.py'
        script.write_text('import tempfile\nfrom pathlib import Path\n'
                          'with tempfile.TemporaryDirectory() as temp:\n'
                          '    print(Path(temp).parent)\n')
        result = subprocess.run([sys.executable, str(script)], env=self.env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), str(fixture))

    def test_nested_claude_cleanup_after_outer_removed(self):
        root = self.home / "claude-root"
        root.mkdir(mode=0o700)
        outer = root / "lane-1234abcd"
        outer.mkdir(mode=0o700)
        inner = outer / "lane-abcdef12"
        inner.mkdir(mode=0o700)
        shutil.rmtree(outer)
        with mock.patch.object(TASK_MODULE, "claude_temporary_root", return_value=root):
            TASK_MODULE.cleanup_lane_temporary(str(inner))
        self.assertFalse(outer.exists())

    def test_cancel_nested_claude_after_outer_removed(self):
        wt, _ = self.started()
        root = self.home / "claude-root"
        root.mkdir(mode=0o700)
        outer = root / "lane-1234abcd"
        outer.mkdir(mode=0o700)
        inner = outer / "lane-abcdef12"
        inner.mkdir(mode=0o700)
        lane = wt / ".work/lane.json"
        lane.write_text(json.dumps(dict(state="running", pid=123456, temporary=str(inner))))
        shutil.rmtree(outer)
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(TASK_MODULE, "HOME", self.home), \
                mock.patch.object(TASK_MODULE, "claude_temporary_root", return_value=root), \
                mock.patch.object(TASK_MODULE, "pid_alive", return_value=True), \
                mock.patch.object(TASK_MODULE.os, "killpg"):
            TASK_MODULE.cancel(SimpleNamespace(repo=str(self.repo), id="5"))
        self.assertEqual(json.loads(lane.read_text())["state"], "cancelled")

    def test_trusted_check_preserves_advisory_log(self):
        wt, _ = self.started()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, "HOME", self.home):
            self.assertTrue(TASK_MODULE.run_check(wt, "", advisory=True))
            log = wt / ".work/check.log"
            before = log.read_bytes()
            (wt / "FAIL").touch()
            with mock.patch.dict(os.environ, TASK_RUN_ID="trusted-test"):
                self.assertFalse(TASK_MODULE.run_check(wt, "", state=self.state()))
        self.assertEqual(log.read_bytes(), before)
        evidence = json.loads((self.state() / "check.json").read_text())
        self.assertEqual(evidence["log"], str(self.state() / "check.log"))
        self.assertIn("exit 1", Path(evidence["log"]).read_text())
        status = self.raw_task("status", "proj")
        self.assertIn(str(log), status)
        self.assertIn(evidence["log"], status)

    def test_batch_tail_uses_actual_check_log(self):
        loader = importlib.machinery.SourceFileLoader(
            "batch_log_test", str(ROOT / "home/dot_local/bin/executable_task-batch"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        batch = importlib.util.module_from_spec(spec)
        loader.exec_module(batch)
        wt, _ = self.started()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, "HOME", self.home):
            self.assertTrue(TASK_MODULE.run_check(wt, "", advisory=True))
            advisory = (wt / ".work/check.log").read_text()
            (wt / "FAIL").touch()
            self.assertFalse(TASK_MODULE.run_check(wt, "", state=self.state()))
            evidence = json.loads((self.state() / "check.json").read_text())
            self.assertTrue(evidence["trusted"])
            self.assertEqual(batch.check_output(self.state(), wt / ".work"),
                             Path(evidence["log"]).read_text())
            self.assertNotEqual(batch.check_output(self.state(), wt / ".work"), advisory)
            (wt / "FAIL").unlink()
            self.assertTrue(TASK_MODULE.run_check(wt, "", advisory=True))
            self.assertEqual(batch.check_output(self.state(), wt / ".work"),
                             (wt / ".work/check.log").read_text())

    def test_repair_published_preserves_trusted_check_log(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.task("check", "proj", "5")
        evidence = json.loads((self.state() / "check.json").read_text())
        self.assertTrue(evidence["trusted"])
        trusted = Path(evidence["log"]).read_bytes()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, "HOME", self.home):
            (wt / "FAIL").touch()
            self.assertFalse(TASK_MODULE.run_check(wt, "", advisory=True))
        advisory = (wt / ".work/check.log").read_bytes()
        head = self.git(wt, "rev-parse", "HEAD")
        self.git(self.repo, "merge", "--no-ff", "-m", "Published fixture", head)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, "HOME", self.home):
            TASK_MODULE.repair_published(TASK_MODULE.Task(self.repo, "5"), head)
        self.assertEqual(Path(evidence["log"]).read_bytes(), trusted)
        self.assertEqual((self.state() / "advisory-check.log").read_bytes(), advisory)

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
        self.assertEqual(call["args"], ["-p", "--output-format", "stream-json", "--verbose",
                                       "--model", "sonnet", "--effort", "high",
                                       "--permission-mode", "acceptEdits", "--allowedTools",
                                       "Bash Read Write Edit Glob Grep",
                                       "--settings", str(wt / ".work/claude-settings.json")])
        settings = json.loads((wt / ".work/claude-settings.json").read_text())
        allowed = settings["sandbox"]["filesystem"]["allowWrite"]
        self.assert_claude_temporary_path(Path(call["tmpdir"]), call)
        self.assertFalse(Path(call["tmpdir"]).exists())
        self.assertEqual(call["task_worktree"], str(wt))
        self.assertEqual(allowed, [call["tmpdir"], str(self.gate_dir),
                                   f"/tmp/task-gate-{os.getuid()}"])
        self.assertNotIn("/tmp", allowed)
        # Reproduce the read-only /tmp boundary: only the generated exceptions
        # and the checkout are writable. Exercise actual tempfile and lock opens.
        real_open = os.open
        real_mkdir = os.mkdir

        def writable(path):
            resolved = Path(path).resolve()
            if not any(resolved.is_relative_to(Path(base)) for base in [str(wt), *allowed]):
                raise PermissionError("Claude sandbox: read-only /tmp")

        def sandbox_open(path, flags, *args, **kwargs):
            writable(path)
            return real_open(path, flags, *args, **kwargs)

        def sandbox_mkdir(path, *args, **kwargs):
            writable(path)
            return real_mkdir(path, *args, **kwargs)

        task = TASK_MODULE.Task(self.repo, "5")
        with mock.patch.dict(os.environ, HOME=str(self.home), TASK_GATE_DIR=str(self.gate_dir)), \
                mock.patch.object(TASK_MODULE.os, "open", side_effect=sandbox_open), \
                mock.patch.object(TASK_MODULE.os, "mkdir", side_effect=sandbox_mkdir):
            with self.assertRaises(PermissionError):
                os.open("/tmp/unlisted-lane-probe", os.O_CREAT | os.O_RDWR, 0o600)
            with tempfile.TemporaryDirectory(
                    prefix="gate-", dir=TASK_MODULE.gate_temporary_root()) as gate:
                with tempfile.TemporaryFile(dir=gate) as probe:
                    probe.write(b"writable")
            with TASK_MODULE.exclusive_gate_lock(task, silent_unavailable=True, advisory=True):
                self.assertIsNotNone(TASK_MODULE.GATE_SLOT.get())
                lock, _ = TASK_MODULE.gate_paths("slot-0")
                with lock.open("r+") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(call["cwd"], str(wt))
        self.assertEqual(call["prompt"], 'Read the pinned task context in .work/context/ before working.\n\n' + prompt)
        self.assertEqual(record["exit_code"], 3)
        self.assertEqual(record["model"], "sonnet")
        self.assertEqual((wt / ".work/final.md").read_text(), "claude-final: Read the pinned task context in .work/context/ before working.\n\n" + prompt + "\n")
        events = [json.loads(line) for line in (wt / ".work/lane.jsonl").read_text().splitlines()]
        self.assertEqual([event["type"] for event in events], ["rate_limit_event", "result"])
        self.assertEqual(json.loads((self.home / ".local/state/task/usage/claude.json").read_text())["seven_day"]["utilization"], 0.4)
        log = (wt / ".work/lane.log").read_text()
        self.assertIn("claude-log", log)
        self.assertIn("claude-final", log)
        self.assertIn("lane: finished (exit 3)", self.raw_task("status", "proj"))

    def readings(self, claude=None, codex=None):
        """Fixture usage readings in the temporary HOME: (weekly used %, hours until the weekly reset)."""
        now = datetime.now(timezone.utc)
        if claude:
            event = {"rate_limit_info": {"unifiedWindows": {"seven_day": {
                "utilization": claude[0] / 100, "resetsAt": int(now.timestamp() + claude[1] * 3600)}}}}
            TASK_MODULE.task_usage.record_claude(self.home, event, now)
        if codex:
            limits = {"primary": {"used_percent": codex[0], "window_minutes": 10080,
                                  "resets_at": int(now.timestamp() + codex[1] * 3600)}}
            path = self.home / ".codex/sessions/2026/10/20/rollout-fixture.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"timestamp": now.isoformat(), "payload": {"rate_limits": limits}}) + "\n")

    def pace_lane(self, *options, code=0, builder="claude"):
        self.stub_models()
        self.raw_task("start", "proj", "5", "--builder", builder)
        wt = self.repo / ".worktrees/t5"
        (wt / ".work/prompt.md").write_text("Approved task.\n")
        out = self.raw_task("lane", "proj", "5", *options, code=code)
        return wt, out

    def pace_log(self):
        path = self.home / ".local/state/task/usage/pace.log"
        return path.read_text() if path.exists() else ""

    def test_usage_command_prints_both_vendors_and_the_weekly_line(self):
        out = self.raw_task("usage")
        self.assertIn("claude: unknown (no rate_limit_event recorded yet", out)
        self.assertIn("codex: unknown (no rollout file under", out)
        self.readings(claude=(50, 84), codex=(40, 84))
        out = self.raw_task("usage")
        for expected in ("claude: weekly used 50 %, 5-hour used n/a", "Europe/Copenhagen", "week elapsed 50 %",
                         "pace: on", "codex: weekly used 40 %", SPARE_HELD):
            self.assertIn(expected, out)
        lines = self.raw_task("usage", "--weekly-line").splitlines()
        self.assertRegex(lines[0], r"^claude week ending \d{4}-\d\d-\d\d: used 50 %, exhausted no, lanes held back 0$")
        self.assertRegex(lines[1], r"^codex week ending \d{4}-\d\d-\d\d: used 40 %, exhausted no, lanes held back 0$")

    def test_usage_policy_file_override_changes_the_verdict(self):
        self.readings(claude=(60, 84))
        self.assertIn("pace: on", self.raw_task("usage"))  # 60 % used, 50 % of the week gone, margin 10
        policy = self.home / "policy.toml"
        policy.write_text("ahead_margin = 5\n[reserve]\nclaude = 50\n")
        (self.home / ".config/task").mkdir(parents=True)
        (self.home / ".config/task/usage-policy-path").write_text(str(policy))
        out = self.raw_task("usage")
        self.assertIn("pace: exhausted-automated-share", out)
        self.assertIn("reserve 50 %", out)

    def test_pace_lane_with_an_unknown_reading_warns_and_changes_nothing(self):
        wt, out = self.pace_lane()  # nothing recorded: unchanged behaviour plus a warning
        self.assertIn("pace warning: claude usage unknown", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "sonnet")

    def test_pace_lane_ahead_uses_the_cheaper_model_and_holds_non_priority_repos(self):
        self.readings(claude=(70, 84))
        wt, out = self.pace_lane()
        self.assertIn("route: routine -> claude haiku", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "haiku")
        self.readings(claude=(70, 84))  # the Claude stub's own event replaced the reading
        priority = self.home / ".local/state/task/madplanner-v2/t1"
        priority.mkdir(parents=True)
        (priority / "task.json").write_text("{}")
        (wt / ".work/lane.json").unlink()
        out = self.raw_task("lane", "proj", "5", code=4)
        self.assertIn("held back by usage pacing: claude is ahead of pace; only madplanner-v2 gets new lanes", out)
        self.assertFalse((wt / ".work/lane.json").exists())
        self.assertIn("held lane proj/T-5", self.pace_log())
        self.assertIn("lanes held back 1", self.raw_task("usage", "--weekly-line"))
        self.raw_task("lane", "proj", "5", "--no-pace")
        self.assertEqual(self.wait_for_lane(wt)["model"], "sonnet")
        self.assertIn("--no-pace: lane proj/T-5 not paced", self.pace_log())

    def test_pace_lane_ahead_overrides_an_explicit_model_unless_no_pace(self):
        self.readings(claude=(70, 84))
        wt, out = self.pace_lane("--model", "opus")
        self.assertIn("route: routine -> claude haiku", out)
        self.assertIn("routine lane uses haiku, not opus", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "haiku")
        self.readings(claude=(70, 84))
        (wt / ".work/lane.json").unlink()
        out = self.raw_task("lane", "proj", "5", "--model", "opus", "--no-pace")
        self.assertIn("route: routine -> claude opus", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "opus")
        self.assertIn("--no-pace: lane proj/T-5 not paced", self.pace_log())

    def test_pace_lane_ahead_overrides_an_explicit_codex_model_unless_no_pace(self):
        policy = self.home / "policy.toml"
        policy.write_text("[spare_resets]\ncodex = 0\n")  # by default Codex holds a spare reset and is never ahead
        (self.home / ".config/task").mkdir(parents=True)
        (self.home / ".config/task/usage-policy-path").write_text(str(policy))
        self.readings(codex=(80, 84))
        wt, out = self.pace_lane("--model", "gpt-6.1-sol", builder="codex")
        self.assertIn("route: routine -> codex gpt-6-luna", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "gpt-6-luna")
        self.readings(codex=(80, 84))
        (wt / ".work/lane.json").unlink()
        out = self.raw_task("lane", "proj", "5", "--model", "gpt-6.1-sol", "--no-pace")
        self.assertIn("route: routine -> codex gpt-6.1-sol", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "gpt-6.1-sol")
        self.assertIn("--no-pace: lane proj/T-5 not paced", self.pace_log())

    def test_pace_lane_exhausted_moves_routine_to_the_other_vendor_and_holds_hard(self):
        self.readings(claude=(80, 84), codex=(20, 84))
        wt, out = self.pace_lane()
        self.assertIn("automated share used up; routine lane moves to codex", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "gpt-6-luna")
        self.assertEqual(json.loads((self.state() / "task.json").read_text())["builder"], "codex")
        self.raw_task("lane", "proj", "5", "--builder", "claude", "--tier", "hard", code=4)
        self.readings(codex=(100, 84))
        out = self.raw_task("lane", "proj", "5", "--builder", "claude", code=4)
        self.assertIn("claude automated share used up (80 % of 75 %)", out)

    def test_pace_lane_exhausted_with_spare_reset_asks_jacopo(self):
        self.readings(claude=(80, 84), codex=(100, 84))
        wt, out = self.pace_lane(builder="codex", code=4)
        self.assertIn("Jacopo: codex is at 100 %; use your spare reset", out)

    def test_pace_lane_under_used_near_reset_switches_the_boost_on_until_the_reset(self):
        self.readings(claude=(40, 30))
        wt, out = self.pace_lane("--tier", "hard")
        self.assertRegex(out, r"pace: claude under-used near its reset; boost on until ")
        self.assertEqual(self.wait_for_lane(wt)["model"], "fable")
        until = json.loads((self.home / ".local/state/task/boost.json").read_text())["claude"]
        self.assertIn(f"boost on for claude until {until}", self.pace_log())
        self.assertIn(f"boost: claude until {until}", self.raw_task("status", "proj"))

    def test_pace_review_holds_exhausted_reviewer_and_goes_deeper_when_under_used(self):
        self.stub_reviewers()
        wt, _ = self.started()
        head = self.git(wt, "rev-parse", "HEAD")
        self.raw_task("check", "proj", "5")
        self.readings(claude=(80, 84))
        out = self.raw_task("review", "proj", "5", "--run", code=4)
        self.assertIn("held back by usage pacing: claude automated share used up", out)
        self.assertFalse((wt / ".work/reviewer-call.json").exists())
        self.readings(claude=(40, 30))
        reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: ok\n#2 pass: ok\n```\n"
        self.raw_task("review", "proj", "5", "--run", env=dict(self.env, REVIEW_STUB_REPLY=reply))
        call = json.loads((wt / ".work/reviewer-call.json").read_text())
        self.assertEqual(call["args"][call["args"].index("--model") + 1], "opus")
        self.assertEqual(json.loads((self.state() / "review.json").read_text())["model"], "opus")

    def test_review_run_records_claude_stream_events_and_plain_reply(self):
        self.stub_reviewers()
        self.raw_task("start", "proj", "5", "--builder", "codex")
        wt = self.repo / ".worktrees/t5"
        head = self.git(wt, "rev-parse", "HEAD")
        self.raw_task("check", "proj", "5")
        reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: ok\n#2 pass: ok\n```"
        stub = self.home / "bin/claude"
        stub.write_text("#!/usr/bin/env python3\nimport json, os, sys, time\nsys.stdin.read()\n"
                        "print(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'unifiedWindows': {'seven_day': "
                        "{'utilization': 0.3, 'resetsAt': int(time.time()) + 86400}}}}))\n"
                        "print(json.dumps({'type': 'result', 'result': os.environ['REVIEW_STUB_REPLY']}))\n")
        self.raw_task("review", "proj", "5", "--run", env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertEqual((self.state() / "review-response.md").read_text(), reply + "\n")
        self.assertIn('"rate_limit_event"', (self.state() / "review-response.jsonl").read_text())
        self.assertEqual(json.loads((self.home / ".local/state/task/usage/claude.json").read_text())["seven_day"]["utilization"], 0.3)

    def test_claude_worker_cleans_temp_before_publishing_finished(self):
        self.stub_models()
        wt, _ = self.started()
        work = wt / ".work"
        (work / "prompt.md").write_text("task\n")
        record = work / "lane.json"
        record.write_text(json.dumps({"state": "running", "pid": os.getpid()}))
        publish = TASK_MODULE.atomic_json
        terminal = []

        def observe(path, run):
            if path == record and run["state"] == "finished":
                # Observe at the publication boundary, with no polling or sleeps.
                self.assertFalse(Path(run["temporary"]).exists())
                terminal.append(run.copy())
            publish(path, run)

        args = SimpleNamespace(repo=str(self.repo), id="5", builder="claude", tier=None,
                               model="opus", no_boost=True, _worker=True, timeout=None)
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(TASK_MODULE, "HOME", self.home), \
                mock.patch.object(TASK_MODULE, "atomic_json", side_effect=observe):
            self.assertEqual(TASK_MODULE.lane(args), 0)
        self.assertEqual(len(terminal), 1)
        self.assertEqual(json.loads(record.read_text())["state"], "finished")

    def test_lane_finalization_cleanup_order_and_failure(self):
        record = self.home / "lane.json"
        root = self.home / "claude-temp"
        root.mkdir()
        for state in ("finished", "timed_out", "cancelled", "interrupted"):
            for missing in ("neither", "inner", "outer"):
                with self.subTest(state=state, missing=missing):
                    outer = root / "lane-12345678"
                    outer.mkdir(mode=0o700, exist_ok=True)
                    temporary = outer / "lane-87654321"
                    temporary.mkdir(mode=0o700)
                    if missing == "inner":
                        temporary.rmdir()
                    elif missing == "outer":
                        shutil.rmtree(outer)
                    run = {"state": "running", "temporary": str(temporary)}
                    record.write_text(json.dumps(run))
                    publish = TASK_MODULE.atomic_json

                    def observe(path, value):
                        self.assertFalse(temporary.exists())
                        self.assertEqual(json.loads(record.read_text())["state"], "running")
                        publish(path, value)

                    with mock.patch.object(TASK_MODULE, "claude_temporary_root", return_value=root), \
                            mock.patch.object(TASK_MODULE, "atomic_json", side_effect=observe):
                        TASK_MODULE.finalize_lane_record(record, run, state)
                    self.assertEqual(json.loads(record.read_text())["state"], state)
            for error in (PermissionError("denied"), TASK_MODULE.Stop("unsafe path")):
                with self.subTest(state=state, error=type(error).__name__), \
                        mock.patch.object(TASK_MODULE, "cleanup_lane_temporary", side_effect=error), \
                        contextlib.redirect_stderr(io.StringIO()):
                    TASK_MODULE.finalize_lane_record(record, {}, state)
                    self.assertEqual(json.loads(record.read_text())["state"], state)

    def test_claude_lane_accepts_opus_and_records_selected_model(self):
        self.stub_models()
        self.raw_task("start", "proj", "5", "--builder", "claude")
        wt = self.repo / ".worktrees/t5"
        (wt / ".work/prompt.md").write_text("Approved Claude task.\n")
        self.raw_task("lane", "proj", "5", "--builder", "claude", "--model", "opus")
        record = self.wait_for_lane(wt)
        call = __import__("json").loads((wt / ".work/model-call.json").read_text())
        self.assertEqual(call["args"][:6], ["-p", "--output-format", "stream-json", "--verbose", "--model", "opus"])
        self.assertEqual(call["args"][6:], ["--effort", "high", "--permission-mode", "acceptEdits",
                                            "--allowedTools", "Bash Read Write Edit Glob Grep",
                                            "--settings", str(wt / ".work/claude-settings.json")])
        settings = json.loads((wt / ".work/claude-settings.json").read_text())
        self.assertFalse(Path(call["tmpdir"]).exists())
        self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"],
                         [call["tmpdir"], str(self.gate_dir), f"/tmp/task-gate-{os.getuid()}"])
        self.assert_claude_temporary_path(Path(call["tmpdir"]), call)
        self.assertEqual(record["model"], "opus")

    def test_claude_long_lane_temp_is_private_socket_writable_and_removed(self):
        self.stub_models()
        name = "t110-electricity-overview-uses-the-" + "long-repository-path-" * 2
        self.raw_task("start", "proj", name, "--builder", "claude")
        wt = self.repo / ".worktrees" / name
        self.assertGreater(len(str(wt / ".work/tmp")), 108)
        (wt / ".work/prompt.md").write_text("Approved Claude task.\n")
        release = self.home / "release"
        self.raw_task("lane", "proj", name, env=dict(self.env, LANE_STUB_RELEASE=str(release)))
        call = self.wait_for_lane(wt, finished=False)
        temporary = Path(call["tmpdir"])
        socket_denied = False
        try:
            self.assert_claude_temporary_path(temporary)
            self.assertEqual(temporary.stat().st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o700)
            settings = json.loads((wt / ".work/claude-settings.json").read_text())
            self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"],
                             [str(temporary), str(self.gate_dir), f"/tmp/task-gate-{os.getuid()}"])
            self.assertEqual(call["task_worktree"], str(wt))
            self.assertEqual(call["state_home"], str(wt / ".work/state"))
            try:
                with socket.socket(socket.AF_UNIX) as bridge:
                    bridge.bind(str(temporary / "srt-mux-3583008-1.sock"))
            except PermissionError:
                if os.environ.get("TASK_GATE_RUN_ID"):
                    raise
                socket_denied = True
            with tempfile.TemporaryFile(dir=temporary) as probe:
                probe.write(b"writable")
        finally:
            release.touch()
            self.wait_for_lane(wt)
        self.assertFalse(temporary.exists())
        if socket_denied:
            self.skipTest("lane sandbox forbids Unix sockets; temp writes and cleanup passed")

    def test_claude_lane_temp_removed_on_timeout_and_cancel(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / ".work/prompt.md").write_text("Approved Claude task.\n")
        env = dict(self.env, LANE_STUB_RELEASE=str(self.home / "release"))
        self.raw_task("lane", "proj", "5", "--builder", "claude", "--timeout", "0.01", env=env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = json.loads((wt / ".work/lane.json").read_text())
            if run["state"] == "timed_out":
                break
            time.sleep(0.02)
        self.assertEqual(run["state"], "timed_out")
        self.assertFalse(Path(run["temporary"]).exists())
        self.assert_group_stopped(run["pid"])
        (wt / ".work/model-call.json").unlink()
        self.raw_task("lane", "proj", "5", env=env)
        call = self.wait_for_lane(wt, finished=False)
        self.raw_task("cancel", "proj", "5")
        self.assertFalse(Path(call["tmpdir"]).exists())

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
            self.assertEqual(call["args"][4:6], ["--model", cli])
        self.assertEqual(json.loads((self.state() / "task.json").read_text())["builder"], "claude")
        self.stub_reviewers()
        head = self.git(wt, "rev-parse", "HEAD")
        reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n```\n"
        self.raw_task("check", "proj", "5")
        self.raw_task("review", "proj", "5", "--run", env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertEqual(json.loads((self.state() / "review-routing.json").read_text())["vendor"], "codex")

    def test_lane_preserves_original_request_and_sets_local_state_and_reason(self):
        import json
        self.stub_models()
        wt, _ = self.started()
        prompt = wt / ".work/prompt.md"
        prompt.write_text("Original outcome\n")
        (wt / ".work/final.md").write_text("BLOCKED: git merge main\n")
        (wt / ".work/final-1.md").write_text("older evidence\n")
        report = wt / ".work/report.md"
        report.write_text("## What changed\nStill working\n\nBLOCKED: cat > helper <<PY\n\n")
        self.raw_task("lane", "proj", "5", env=dict(self.env, SECRETSPEC_REASON=""))
        self.wait_for_lane(wt)
        self.assertEqual((wt / ".work/final-1.md").read_text(), "older evidence\n")
        archived = (wt / ".work/final-2.md").read_text()
        self.assertIn("BLOCKED: git merge main", archived)
        self.assertIn("BLOCKED: cat > helper <<PY", archived)
        self.assertEqual(report.read_text(), "## What changed\nStill working\n")
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
            self.raw_task("check", "proj", "5")
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
        self.raw_task("check", "proj", "5")
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
        self.raw_task("check", "proj", "5")
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

    def test_cumulative_criteria_merge_rejects_incomplete_final_round(self):
        wt, _ = self.started()
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        report = wt / '.work/report.md'
        complete = report.read_text()
        old = complete.replace(' met:', ' not met:').replace('implemented and verified', '(fill in)')
        for last in ('## Criteria\n', '## Criteria\n#1 met: verified\n'):
            report.write_text(old + '\n' + last)
            self.assertIn('final Criteria block', self.raw_task('merge', 'proj', '5', code=1))
        report.write_text(old + '\n' + complete)
        prompt = self.raw_task('review', 'proj', '5')
        self.assertIn('use only the last Criteria block', prompt)
        self.assertIn('missing or incomplete last block is Blocking', prompt)
        self.raw_task('merge', 'proj', '5')

    def test_start_seeds_files_directories_existing_worktree_and_refuses_escape(self):
        source = self.repo / ".work"
        source.mkdir(exist_ok=True)
        (source / "pinned").mkdir()
        (source / "pinned/code").write_text("original")
        (source / "single").write_text("file")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", "pinned", "--seed", "single")
        wt = self.repo / ".worktrees/t5"
        self.assertEqual((wt / ".work/pinned/code").read_text(), "original")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", "pinned", "--seed", "single")
        (source / "added").write_text("resumed seed")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", "added")
        self.assertEqual((wt / ".work/added").read_text(), "resumed seed")
        (source / "single").write_text("changed")
        output = self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", "single", code=1)
        self.assertIn("Seed target already exists and differs", output)
        self.assertEqual((wt / ".work/single").read_text(), "file")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--refresh")
        self.assertEqual(json.loads((self.state() / "task.json").read_text())["seeds"],
                         ["pinned", "single", "added"])
        record = json.loads((self.state() / "task.json").read_text())
        self.assertEqual(record["seeds"], ["pinned", "single", "added"])
        (source / "escape").symlink_to(self.home)
        for name in ("..", "../outside", str(self.home), "escape"):
            self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", name, code=1)

    def test_setup_73_merge_gate_needs_recorded_and_configured_seeds(self):
        source = self.repo / ".work"
        source.mkdir(exist_ok=True)
        (source / "hermes-130b8f2c").mkdir()
        (source / "hermes-130b8f2c/runtime.py").write_text("runtime source")
        (source / "configured").write_text("configured seed")
        self.raw_task("start", "proj", "5", "--builder", "codex", "--seed", "hermes-130b8f2c")
        wt = self.repo / ".worktrees/t5"
        self.commit_in(wt, ".task.toml", 'drill_seeds = ["configured"]\n')
        record = json.loads((self.state() / "task.json").read_text())
        task = TASK_MODULE.Task(self.repo, "5")
        observed = []
        def check(gate, *args, **kwargs):
            self.assertEqual((gate / ".work/hermes-130b8f2c/runtime.py").read_text(), "runtime source")
            self.assertEqual((gate / ".work/configured").read_text(), "configured seed")
            (gate / ".work/check.log").write_text("PASS seed-dependent gate")
            observed.append(gate)
            return True
        with mock.patch.object(TASK_MODULE, "run_check", side_effect=check), \
                mock.patch.object(TASK_MODULE, "HOME", self.home), \
                mock.patch.dict(os.environ, self.env):
            _, passed = TASK_MODULE.gate_merge_result(
                task, self.repo, self.git(wt, "rev-parse", "HEAD"),
                self.git(self.repo, "rev-parse", "HEAD"), record)
        self.assertTrue(passed)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())

    def test_setup_68_runner_no_verify_validation_and_dispatch(self):
        runner = self.home / "runner.md"
        runner.write_text("1. docs: builder codex, model gpt-6-luna, no-verify: gate is its verification\n")
        self.assertIn("runner file OK", self.raw_task("runner-check", "proj", str(runner)))
        runner.write_text("1. docs: builder codex, model gpt-6-luna, no-verify: \n")
        self.assertIn("nonempty reason", self.raw_task("runner-check", "proj", str(runner), code=1))
        out = self.raw_task("start", "proj", "docs", "--builder", "codex",
                            "--no-verify", "gate is its verification")
        self.assertIn("edit or apply_patch tool, never shell heredocs or cat >", out)
        self.assertIn("avoid backticks in shell commands", out)

    def test_setup_68_runner_preserves_trailing_text_and_optional_field_positions(self):
        runner = self.home / "runner.md"
        (self.repo / ".work").mkdir(exist_ok=True)
        (self.repo / ".work/pinned").write_text("seed")
        for suffix in (
                ", tier hard",
                ", tier design, seed pinned, after: previous",
                ", no-verify: manual check, tier routine, after: previous",
                ", note here",
                " some free text",
                ", seed pinned, after: previous, note here",
                ", note here, no-verify: manual check",
                ", no-verify: manual check, seed pinned, after: previous, note here",
                ", seed pinned, after: previous, note here, no-verify: manual check",
                ", seed pinned, no-verify: manual check, after: previous"):
            with self.subTest(suffix=suffix):
                runner.write_text("1. previous: builder codex, model gpt-6-luna\n"
                                  "2. docs: builder codex, model gpt-6-luna" + suffix + "\n")
                self.assertIn("runner file OK", self.raw_task("runner-check", "proj", str(runner)))
        runner.write_text("1. docs: builder codex, model gpt-6-luna, note here, no-verify: \n")
        self.assertIn("nonempty reason", self.raw_task("runner-check", "proj", str(runner), code=1))

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
            ("1. docs: builder codex, model gpt-6-luna, tier extreme\n", "invalid tier"),
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
                       GIT_CONFIG_VALUE_0="value", GIT_CONFIG_FUTURE="blocked", TASK_RUN_ID="runner",
                       TASK_GATE_RUN_ID="gate", TASK_WORKTREE="/stale/runner")
        kept = {"GIT_AUTHOR_NAME": "builder", "GIT_AUTHOR_EMAIL": "builder@example.invalid",
                "GIT_COMMITTER_NAME": "builder", "GIT_COMMITTER_EMAIL": "builder@example.invalid",
                "GIT_OPTIONAL_LOCKS": "0", "GIT_FUTURE_VARIABLE": "value",
                "CODEX_THREAD_ID": "runner", "PATH": "tools"}
        with mock.patch.dict(os.environ, dict(blocked, **kept), clear=True):
            self.assertEqual(TASK_MODULE.agent_environment(), kept)

    def test_lane_temp_cannot_allowlist_a_symlink_outside_the_worktree(self):
        wt, _ = self.started()
        (wt / ".work/tmp").symlink_to(self.home, target_is_directory=True)
        task = TASK_MODULE.Task(self.repo, "5")
        with self.assertRaisesRegex(TASK_MODULE.Stop, "TMPDIR must stay inside"):
            TASK_MODULE.lane_environment(task)

    def test_default_gate_directory_is_shared_and_narrow(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "mkdir") as mkdir:
            lock, holder = TASK_MODULE.gate_paths("slot-0")
            self.assertEqual(lock, Path("/tmp/agent-task-gates/agent-task-gate.lock"))
            self.assertEqual(holder.parent, lock.parent)
            mkdir.assert_called_once_with(exist_ok=True)

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
            # Keep the probe off the machine-wide /tmp gate directory.
            with mock.patch.dict(os.environ, TASK_GATE_DIR=str(self.gate_dir)):
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
        self.assertIn("trusted passing check", out)
        self.assertIn("reuse decision: reusing trusted lane check", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1,
                         "unchanged default should not rerun just check in a gate worktree")
        self.assertIn("gate", __import__("json").loads(run_path.read_text())["completed"])
        self.assertFalse((self.home / "sync-probe").exists(), "no dependency change should not probe or run sync")

    def test_merge_regates_when_default_moved_after_lane_check_and_is_not_in_lane(self):
        self.checked_lane_with_counter()
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        out = self.raw_task("merge", "proj", "5")
        self.assertNotIn("reusing it", out)
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
        self.assertNotIn("reusing it", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 2,
                         "stale or failed check records must rerun the merge gate")

    def test_merge_does_not_reuse_check_with_different_head(self):
        self.assert_check_record_regates(lambda check: check.update(sha="0" * 40), override=True)

    def test_merge_reuses_check_when_newer_main_was_merged_before_check(self):
        wt, _ = self.started()
        other = self.home / "other"
        self.git(self.home, "clone", "-q", str(self.origin), str(other))
        self.commit_in(other, "c.txt")
        self.git(other, "push", "-q", "origin", "main")
        self.git(wt, "fetch", "-q", "origin")
        self.git(wt, "merge", "--no-edit", "origin/main")
        self.commit_in(wt, "b.txt")
        self.prepare_merge_evidence("5")
        (self.home / "just-count").unlink(missing_ok=True)
        (self.home / "during-check.sh").write_text('echo run >> "$HOME/just-count"\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.raw_task("check", "proj", "5")
        check = __import__("json").loads((self.state() / "check.json").read_text())
        self.assertEqual(check["base_sha"], self.git(wt, "rev-parse", "origin/main"))
        out = self.raw_task("merge", "proj", "5")
        self.assertIn("trusted passing check", out)
        self.assertIn("reuse decision: reusing trusted lane check", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1)

    def test_merge_does_not_reuse_check_with_different_record_revision(self):
        self.assert_check_record_regates(lambda check: check.update(record_revision="wrong"))

    def test_merge_does_not_reuse_check_missing_gate_variant_coverage(self):
        wt, _ = self.started()
        (wt / ".task.toml").write_text('[[gate_variant]]\nname = "extra"\n')
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "configure gate variant")
        self.prepare_merge_evidence("5")
        (self.home / "just-count").unlink(missing_ok=True)
        (self.home / "during-check.sh").write_text('echo run >> "$HOME/just-count"\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.raw_task("check", "proj", "5")
        check_path = self.state() / "check.json"
        check = __import__("json").loads(check_path.read_text())
        self.assertEqual({row["name"] for row in check["variants"]}, {"extra"})
        check["variants"] = []
        check_path.write_text(__import__("json").dumps(check))
        out = self.raw_task("merge", "proj", "5")
        self.assertNotIn("reusing it", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 2)

    def test_merge_does_not_reuse_when_committed_variant_environment_differs(self):
        wt, _ = self.started()
        (wt / ".task.toml").write_text('[[gate_variant]]\nname = "extra"\n[gate_variant.env]\nCOUNT = "new"\n')
        self.git(wt, "add", ".task.toml")
        self.git(wt, "commit", "-qm", "configure gate variant environment")
        self.prepare_merge_evidence("5")
        (self.home / "just-count").unlink(missing_ok=True)
        (self.home / "during-check.sh").write_text('echo run >> "$HOME/just-count"\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.raw_task("check", "proj", "5")
        check_path = self.state() / "check.json"
        check = __import__("json").loads(check_path.read_text())
        check["variants"][0]["environment_template"]["COUNT"] = "old"
        check_path.write_text(__import__("json").dumps(check))
        out = self.raw_task("merge", "proj", "5")
        self.assertIn("gate variants or environments differ", out)
        self.assertIn("reuse decision: running full gate", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 2)

    def test_merge_does_not_reuse_failed_check_record(self):
        self.assert_check_record_regates(lambda check: check.update(result="failed"), override=True)

    def test_merge_reuses_check_without_legacy_base_sha(self):
        self.checked_lane_with_counter()
        check_path = self.state() / "check.json"
        check = __import__("json").loads(check_path.read_text())
        check.pop("base_sha")
        check_path.write_text(__import__("json").dumps(check))
        out = self.raw_task("merge", "proj", "5")
        self.assertIn("reuse decision: reusing trusted lane check", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1)

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

    def test_review_appends_instruction_to_generated_prompt(self):
        self.started()
        instruction = "If a hook or sandbox denial stops a required step, end your final reply with a line starting `BLOCKED:` and the denied command."
        self.raw_task("review", "proj", "5", "--append-prompt", instruction)
        prompt = (self.state() / "review-prompt.md").read_text()
        self.assertTrue(prompt.endswith(instruction + "\n"))
        self.assertIn("Verdict: PASS|FAIL", prompt)

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
            "Blocking finding must identify a named requested criterion or a concrete defect or regression, "
            "with file:line. Put all other "
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
        self.raw_task("check", "proj", "5")
        prepared = self.raw_task("review", "proj", "5")
        self.assertFalse((wt / ".work/reviewer-call.json").exists())
        prompt = (self.state() / "review-prompt.md").read_text()
        expected_command = next(line.strip() for line in prepared.splitlines()
                                if " < " in line and " > " in line)
        expected_args = shlex.split(expected_command.split(" < ")[0])
        if expected_args[0] == "GIT_OPTIONAL_LOCKS=0":
            expected_args.pop(0)
        if expected_args[0] == "claude":  # --run records stream-json; the printed manual command stays plain
            expected_args[2:2] = ["--output-format", "stream-json", "--verbose"]
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
        self.assertEqual(json.loads((self.state() / "review-run.json").read_text()),
                         dict(builder='claude' if builder == 'codex' else 'codex',
                              state='finished', exit_code=0))
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

    def test_review_run_records_cli_exit_and_separate_stderr(self):
        import json
        self.stub_reviewers()
        self.raw_task('start', 'proj', '5', '--builder', 'claude')
        diagnostic = "ERROR: You've hit your usage limit. Try again later.\n"
        self.raw_task("check", "proj", '5')
        out = self.raw_task('review', 'proj', '5', '--run', code=1,
                            env=dict(self.env, REVIEW_STUB_STDERR=diagnostic, REVIEW_STUB_EXIT='1'))
        self.assertIn(diagnostic, out)
        self.assertEqual((self.state() / 'review-stderr.log').read_text(), diagnostic)
        self.assertEqual(json.loads((self.state() / 'review-run.json').read_text()),
                         dict(builder='claude', state='finished', exit_code=1))
        self.assertEqual(json.loads((self.state() / 'review-routing.json').read_text()),
                         dict(vendor='claude', model='haiku', fallback_from=['codex', 'gpt-6-luna']))

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
                self.raw_task("check", "proj", name)
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
        self.raw_task("check", "proj", "direct-request")
        self.raw_task("review", "proj", "direct-request", "--run", "--request", str(request),
                      env=dict(self.env, REVIEW_STUB_REPLY=reply))
        call = json.loads((wt / ".work/reviewer-call.json").read_text())
        self.assertIn(request.read_text(), call["prompt"])
        self.assertNotIn("--add-dir", call["args"])
        self.assertEqual(json.loads((self.state("direct-request") / "review.json").read_text())["judgments"], {})

    def test_review_run_errors_do_not_record_or_replace_previous_review(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.raw_task("check", "proj", "5")
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
        self.task("check", "proj", "5")
        evidence = json.loads((self.state() / "check.json").read_text())
        self.assertTrue(evidence["trusted"])
        trusted = Path(evidence["log"]).read_bytes()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, "HOME", self.home):
            self.assertTrue(TASK_MODULE.run_check(wt, "", advisory=True))
        advisory = (wt / ".work/check.log").read_bytes()
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
        self.assertEqual(Path(evidence["log"]).read_bytes(), trusted)
        self.assertEqual((self.state() / "advisory-check.log").read_bytes(), advisory)
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
        self.assertIn("main changed a.txt that this task also changed; ask the lane to run `git merge origin/main` on its branch, then `task check` and a new review", out)
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
        wt, _ = self.started()
        self.commit_in(self.kb, "INTENT.md", "Original plan\n")
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
        archive = self.state()
        (archive / "review.json").write_text('{"judgments":{"1":"pass","2":"pass"}}')
        (archive / "review.md").write_text("Verdict: PASS\\n#1 pass: reviewed\\n")
        (self.kb / "INTENT.md").write_text("Uncommitted signed plan\n")
        realign = self.kb / "history/realign/review.md"
        realign.parent.mkdir(parents=True)
        realign.write_text("Verdict: on plan\n")
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
        self.assertEqual(self.git(self.kb, "rev-parse", "main"), merge,
                         "checkout fast-forwards with dirty INTENT and untracked realignment")
        self.assertEqual((self.kb / "INTENT.md").read_text(), "Uncommitted signed plan\n")
        self.assertEqual(realign.read_text(), "Verdict: on plan\n")
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

        incoming = self.home / "incoming-knowledge"
        self.git(self.home, "clone", "-q", str(origin), str(incoming))
        self.commit_in(incoming, "INTENT.md", "Incoming plan\n")
        self.git(incoming, "push", "-q", "origin", "main")
        out = self.raw_task("close", "proj", "5", "-m", "done", "--retro", "all routine")
        self.assertIn("local main not updated", out)
        self.assertIn("would be overwritten by merge", out)
        self.assertIn("INTENT.md", out)
        self.assertEqual(self.git(self.kb, "rev-parse", "main"), merge)
        self.assertEqual((self.kb / "INTENT.md").read_text(), "Uncommitted signed plan\n")
        self.assertEqual(realign.read_text(), "Verdict: on plan\n")

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
        config = self.home / '.config/task/gate.toml'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('slots = 1\n')
        lock_path = self.gate_dir / "agent-task-gate.lock"
        holder_path = self.gate_dir / "agent-task-gate.holder"
        self.addCleanup(lambda: holder_path.unlink(missing_ok=True))
        (self.repo / ".task.toml").write_text('check_timeout = 3\n[env]\nTASK_CHECK_SLEEP = "2.2"\n')
        self.git(self.repo, "add", ".task.toml")
        self.git(self.repo, "commit", "-qm", "enable exclusive gate")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.started()
        other = self.repo / ".worktrees/other"
        self.git(self.repo, "worktree", "add", "-q", "--no-track", "-b", "other", str(other), "main")
        (other / ".task.toml").write_text('check_timeout = 1\n[env]\nTASK_CHECK_SLEEP = "0.05"\n')
        self.git(other, "add", ".task.toml")
        self.git(other, "commit", "-qm", "short check timeout")
        (self.home / "during-check.sh").write_text(
            "#!/bin/sh\n"
            "if [ -e \"$HOME/gate-active\" ]; then echo overlap > \"$HOME/gate-overlap\"; exit 9; fi\n"
            "echo $$ > \"$HOME/gate-active\"\n"
            "sleep \"${TASK_CHECK_SLEEP:-0.05}\"\n"
            "rm -f \"$HOME/gate-active\"\n")
        (self.home / "during-check.sh").chmod(0o755)
        first = subprocess.Popen([sys.executable, str(TOOL), "check", "proj", "5"],
                                 cwd=self.home, env=self.env, stdout=subprocess.PIPE,
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
                                  cwd=self.home, env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        out1, _ = first.communicate(timeout=8)
        out2, _ = second.communicate(timeout=8)
        elapsed = __import__("time").monotonic() - started
        self.assertEqual(first.returncode, 0, out1)
        self.assertEqual(second.returncode, 0, out2)
        self.assertGreater(elapsed, 2, "the second check waited longer than its one-second check timeout")
        self.assertRegex(out2, r"Waiting for exclusive task gate held by pid \d+, proj/T-5")
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

    def test_gate_unavailable_falls_back_unless_explicitly_exclusive(self):
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
        self.git(other, 'config', 'user.name', 't')
        self.git(other, 'config', 'user.email', 't@example.invalid')
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
        self.git(wt, 'config', 'user.name', 't')
        self.git(wt, 'config', 'user.email', 't@example.invalid')
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
            '[ "$WORK_DIR" = "$PWD/output" ] || exit 10\n'
            '[ "$PRODUCT_DIR" = "$HOME/dev_workspace/proj/data" ] || exit 11\n'
            "echo 'Ran 1 test'; echo OK\n")
        (self.home / "bin/just").chmod(0o755)
        wt, _ = self.started()
        self.commit_in(wt, '.task.toml', '[env]\nPRODUCT_DIR = "{repo}/data"\nWORK_DIR = "{worktree}/output"\n')
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
        for step in check["steps"]:
            self.assertEqual((step["configured_timeout"], step["effective_timeout"]), (900, 900))

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
        step = json.loads((self.state() / "check.json").read_text())["steps"][0]
        self.assertEqual((step["configured_timeout"], step["effective_timeout"]), (1, 1))
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
        self.assertIn(f"TASK_GATE_RUN_ID={run['task_run_id']}".encode(), environment)
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
        self.assertEqual(sorted(p.name for p in (wt / ".work").iterdir()),
                         ["context", "dispatch.md", "report.md"])
        self.assertIn(f"Work only in {wt}", (wt / ".work/dispatch.md").read_text())
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
        self.assertIn("main changed a.txt that this task also changed; ask the lane to run `git merge origin/main` on its branch, then `task check` and a new review", out)
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

    def test_sighup_at_foreground_lock_creation_records_interruption(self):
        self.started()
        # Deliver the signal exactly when the lock first becomes visible,
        # before foreground_lock has written run.json or returned to main.
        code = textwrap.dedent('''\
            import os, runpy, signal, sys
            module = runpy.run_path(sys.argv[1])
            original_open = os.open
            def interrupt_on_lock(path, flags, *args, **kwargs):
                fd = original_open(path, flags, *args, **kwargs)
                if str(path).endswith('/run.lock'):
                    os.kill(os.getpid(), signal.SIGHUP)
                return fd
            os.open = interrupt_on_lock
            sys.exit(module['main'](['check', 'proj', '5']))
        ''')
        result = subprocess.run([sys.executable, '-c', code, str(TOOL)],
                                cwd=self.home, env=self.env, capture_output=True,
                                text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        run = json.loads((self.state() / 'run.json').read_text())
        self.assertEqual(run['state'], 'interrupted')
        self.assertFalse((self.state() / 'run.lock').exists())

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
