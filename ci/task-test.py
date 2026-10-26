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
import hashlib
import json
import os
import re
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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from policy_test_env import isolate_policy_environment
from unit_fixture import install_units

# Keep only explicit test selection and trusted-gate socket-test semantics.
isolate_policy_environment("TASK_TEST_TOOL", "TASK_GATE_RUN_ID")

audit_state = tempfile.TemporaryDirectory(prefix="agent-policy-test-")
atexit.register(audit_state.cleanup)
os.environ["AGENT_POLICY_STATE_DIR"] = audit_state.name

ROOT = Path(__file__).resolve().parents[1]
TOOL = Path(os.environ.get("TASK_TEST_TOOL", ROOT / "home/dot_local/bin/executable_task"))
# Test-only source seam: installed commands have no environment override for /etc.
HOLD_FIXTURE = Path(audit_state.name) / 'etc/agent-hold'
HOLD_FIXTURE.parent.mkdir()
def hold_fixture_source(source):
    assert source.count('Path("/etc/agent-hold")') == 1, 'hold seam must replace exactly one path per source'
    source = source.replace('Path("/etc/agent-hold")', f'Path({str(HOLD_FIXTURE)!r})')
    return source.replace("name.startswith('LC_')", "name.startswith(('LC_', 'LANE_STUB_', 'REVIEW_STUB_', 'RUNNER_', 'SMOKE_')) or name in {'SIMULATE_READ_ONLY_GIT_DIR', 'PRE_COMMIT_FAIL'}")
fixture_bin = Path(audit_state.name) / 'bin'
fixture_bin.mkdir()
for sibling in TOOL.parent.iterdir():
    if sibling.is_file() and sibling != TOOL:
        target = fixture_bin / sibling.name
        if sibling.name == 'executable_task-batch':
            target.write_bytes(sibling.read_bytes())
            target.chmod(0o755)  # chezmoi makes executable_ sources executable on install.
        else:
            target.symlink_to(sibling)
fixture_tool = fixture_bin / TOOL.name
fixture_tool.write_text(hold_fixture_source(TOOL.read_text()))
fixture_tool.chmod(0o755)
TOOL = fixture_tool
REVIEW_FIXTURES = ROOT / "ci/fixtures/reviews"
SPARE_HELD = "spare reset held; reset at 100 %"
# "Idle 10 days" with an hour of margin: the host wall clock steps back by seconds, so a
# worktree aged exactly 10 days when the test reads the clock can read 9 days in `task status`.
IDLE_TEN_DAYS = 10 * 86400 + 3600
PAST = '2000-01-01T00:00:00+00:00'  # for commits and merges that must precede a later sign-off


def restore_access(path):
    """Give the owner full access back so the tree can be removed; a path already gone is fine."""
    path = Path(path)
    try:
        path.chmod(0o700 if path.is_dir() else 0o600)
    except FileNotFoundError:
        pass


def make_read_only(case, path, mode):
    """chmod a fixture path and register the restore first, so a failing or erroring test cannot
    leave a locked directory behind in the lane's .work/tmp."""
    case.addCleanup(restore_access, path)
    Path(path).chmod(mode)
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
TASK_LOADER = importlib.machinery.SourceFileLoader("task_tool_for_review_test", str(TOOL))
TASK_SPEC = importlib.util.spec_from_loader(TASK_LOADER.name, TASK_LOADER)
TASK_MODULE = importlib.util.module_from_spec(TASK_SPEC)
sys.dont_write_bytecode = True
TASK_SPEC.loader.exec_module(TASK_MODULE)
# In-process helpers must never inspect the real ~/cortex repositories either.
TASK_MODULE.CORTEX = Path(audit_state.name) / 'cortex'

STUBS = {
    "just": """#!/bin/sh
case "$1 $2" in
  "--show sync") echo probe >> "$HOME/sync-probe"; grep -q '^sync:' justfile && { echo 'sync recipe'; exit 0; }; exit 1 ;;
  "sync ")
    if [ -x "$HOME/during-sync.sh" ]; then "$HOME/during-sync.sh" || exit $?; fi
    echo OK ;;
  "check ")
    [ -x "$HOME/during-check.sh" ] && "$HOME/during-check.sh"
    if [ -f smoke_check.py ]; then
      grep -Fq 'uv run --no-project --with colorama==0.4.6 python smoke_check.py' justfile || exit 1
      uv run --no-project --with colorama==0.4.6 python smoke_check.py || exit $?
    fi
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
tamper = os.environ.get('LANE_STUB_TAMPER')
if tamper:
    pathlib.Path(tamper).write_text(('gitdir: ' if tamper.endswith('.git') else '') + '/elsewhere' + chr(10))
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
    'args': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': sys.stdin.read(), 'pid': os.getpid(),
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
if os.environ.get('REVIEW_STUB_HOLD'):
    (pathlib.Path.home() / '.local/state/task/hold').write_text('hold before reviewer retry')
delay = os.environ.get('REVIEW_STUB_DELAY', '0')
if os.environ.get('REVIEW_STUB_CHILD'):
    import subprocess
    child = subprocess.Popen(['sleep', '30'])
    (work / 'reviewer-child.pid').write_text(str(child.pid))
time.sleep(float(os.environ.get('REVIEW_STUB_FIRST_DELAY', delay) if count == 1 else delay))
code = os.environ.get('REVIEW_STUB_EXIT', '0')
sys.exit(int(os.environ.get('REVIEW_STUB_FIRST_EXIT', code) if count == 1 else code))
"""


class TaskTool(unittest.TestCase):
    def test_units_hold_all_launch_kinds_and_merge_entry(self):
        self.stub_models()
        self.stub_reviewers()
        wt, _ = self.started()
        work = wt / '.work'
        (work / 'prompt.md').write_text('Build the task.')
        runner = self.runner_fixture()
        for marker in (HOLD_FIXTURE, self.home / '.local/state/task/hold'):
            with self.subTest(marker=marker):
                marker.write_text('unit admission hold')
                try:
                    for command, record in (
                            (('lane', 'proj', '5'), work / 'lane.json'),
                            (('review', 'proj', '5', '--run', '--no-check', 'hold fixture'), self.state() / 'review-run.json'),
                            (('check', 'proj', '5'), self.state() / 'check.json'),
                            (('runner-start', 'proj', str(runner)), runner.with_suffix('.json'))):
                        self.assertIn('skipped: hold', self.raw_task(*command, code=1))
                        data = json.loads(record.read_text())
                        self.assertEqual(data.get('state', data.get('result')), 'skipped: hold')
                        self.assertTrue(data.get('unit') or data['steps'][0]['unit'])
                    before = (self.home / 'unit-calls.jsonl').read_text()
                    output = self.raw_task('merge', 'proj', '5', code=1)
                    self.assertIn('unit admission hold', output)
                    self.assertEqual((self.home / 'unit-calls.jsonl').read_text(), before)
                    for kind, arguments in (
                            ('executable_task-batch', ['proj', str(runner)]),
                            ('executable_workspace-tidy', ['--apply'])):
                        result = subprocess.run([sys.executable, str(ROOT / 'home/dot_local/bin' / kind), *arguments],
                            cwd=wt, env=dict(self.env, TASK_GATE_RUN_ID='hold-test', TASK_GATE_PORT_BASE='22000',
                                             TASK_GATE_SLOT='0', TMPDIR=str(work)), capture_output=True, text=True)
                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn('skipped: hold', result.stdout + result.stderr)
                    self.assertFalse((work / 'model-call.json').exists())
                    self.assertFalse((work / 'reviewer-call.json').exists())
                    self.assertFalse((runner.parent / 'runner-call').exists())
                finally:
                    marker.unlink()

    def test_held_lane_and_review_units_record_skipped_hold_and_wait_returns(self):
        self.stub_models()
        self.stub_reviewers()
        wt, _ = self.started()
        work = wt / '.work'
        (work / 'prompt.md').write_text('Build the task.')
        for style in ('exec-condition', 'success'):
            with self.subTest(style=style):
                (self.home / 'skip-style').write_text(style)
                marker = self.home / '.local/state/task/hold'
                marker.write_text('unit admission hold')
                try:
                    self.assertIn('skipped: hold', self.raw_task('lane', 'proj', '5', code=1))
                    lane = json.loads((work / 'lane.json').read_text())
                    self.assertEqual(lane['state'], 'skipped: hold')
                    self.assertTrue(lane['unit'])
                    self.assertTrue(lane['ended_at'])
                    started = time.monotonic()
                    waited = self.raw_task('wait', 'proj', '5', '--max', '5', code=1)
                    self.assertLess(time.monotonic() - started, 30)
                    self.assertIn('skipped: hold', waited)
                    self.assertEqual(len(waited.strip().splitlines()), 1)
                    self.assertIn('skipped: hold', self.raw_task('review', 'proj', '5', '--run', '--no-check', 'hold fixture', code=1))
                    review = json.loads((self.state() / 'review-run.json').read_text())
                    self.assertEqual(review['state'], 'skipped: hold')
                    self.assertTrue(review['unit'])
                    self.assertRegex(review['ended_at'], r'^\d{4}-\d\d-\d\dT')
                    self.assertEqual(review['ended_at'][-1], lane['ended_at'][-1])
                    self.assertEqual(len(review['ended_at']), len(lane['ended_at']))
                    marker.unlink()
                    self.raw_task('risk', 'proj', '5', 'high', '--reason', 'safety')
                    self.prepare_merge_evidence('5')
                    brief = self.home / 'redteam-brief.md'
                    brief.write_text('Attack the safety assumptions.\n')
                    marker.write_text('unit admission hold')
                    self.assertIn('skipped: hold', self.raw_task('redteam', 'proj', '5', '--brief', str(brief), code=1))
                    red = json.loads((self.state() / 'redteam-run.json').read_text())
                    self.assertEqual((red['state'], red['result']), ('skipped: hold', 'skipped: hold'))
                    self.assertTrue(red['unit'])
                    self.assertEqual(len(red['ended_at']), len(lane['ended_at']))
                finally:
                    marker.unlink(missing_ok=True)

    def test_finished_and_failed_units_are_not_recorded_as_skipped(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('Build the task.')
        (self.home / 'skip-style').write_text('success')
        self.raw_task('lane', 'proj', '5')
        self.raw_task('wait', 'proj', '5', '--max', '5')
        self.assertNotEqual(json.loads((wt / '.work/lane.json').read_text())['state'], 'skipped: hold')

    def test_units_unheld_run_and_pulling_hold_does_not_stop_running_unit(self):
        wt, _ = self.started()
        with mock.patch.dict(os.environ, self.env, clear=True):
            for kind in ('lane', 'review', 'gate', 'runner', 'browser', 'tidy'):
                target = self.home / kind
                process = TASK_MODULE.launch_unit(kind, self.repo, 'proof', ['touch', str(target)],
                                                  cwd=self.repo, env=TASK_MODULE.agent_environment())
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertTrue(target.exists())
            process = TASK_MODULE.launch_unit('lane', self.repo, 'drain', ['sleep', '30'],
                                              cwd=self.repo, env=TASK_MODULE.agent_environment())
            try:
                self.raw_task('hold', 'pull', 'drain the running unit')
                self.assertIsNone(process.poll())
                self.assertFalse((self.home / 'stopped').exists())
                (wt / '.work/lane.json').write_text(json.dumps(dict(state='running', pid=process.pid,
                    unit=process.unit, process_start=TASK_MODULE.task_usage.process_start(process.pid))))
                self.raw_task('cancel', 'proj', '5')
                self.assertEqual((self.home / 'stopped').read_text(), process.unit)
            finally:
                if process.poll() is None: process.stop()
                else: process.wait()
                self.raw_task('hold', 'release')

    def test_unit_missing_systemd_fails_closed_without_process_fallback(self):
        with mock.patch.object(TASK_MODULE.subprocess, 'Popen', side_effect=FileNotFoundError('systemd-run')) as launch:
            with self.assertRaisesRegex(TASK_MODULE.Stop, 'systemd-run --user unavailable; launch failed closed'):
                TASK_MODULE.launch_unit('lane', self.repo, 'missing', ['touch', str(self.home / 'forbidden')],
                                        cwd=self.repo, env={})
            self.assertEqual(launch.call_count, 1)
        self.assertFalse((self.home / 'forbidden').exists())

    def test_merge_has_exactly_one_entry_hold_check(self):
        import ast
        tree = ast.parse(TOOL.read_text())
        merge = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'merge')
        checks = [node for node in ast.walk(merge) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == 'hold']
        self.assertEqual(len(checks), 1)
        self.assertIs(merge.body[0].value, checks[0])

    def test_readme_describes_unit_hold_and_merge_entry(self):
        section = (ROOT / 'README.md').read_text().split('### Hold switch\n', 1)[1].split('```sh', 1)[0]
        for text in ('systemd-run --user', 'ExecCondition=', 'skipped: hold',
                     'drain', 'checks the hold once on entry', 'inside its existing gate unit'):
            self.assertIn(text, section)
        for obsolete in ('before every deletion', 'during the last gate step prevents publication',
                         'before Playwright', 'Each builder, reviewer, gate step, runner and publication action'):
            self.assertNotIn(obsolete, section)

    def test_unit_launch_proof_codex_and_claude_sandbox_commands(self):
        self.stub_models()
        wt, _ = self.started()
        work = wt / '.work'
        prompt = work / 'proof-prompt'
        prompt.write_text('sandbox launch proof')
        for vendor, command in (
                ('codex', ['codex', 'exec', '-c', 'default_permissions="lane"', '--model', 'gpt-6.1-sol',
                           '--output-last-message', str(work / 'proof-final'), '-']),
                ('claude', ['claude', '-p', '--permission-mode', 'acceptEdits',
                            '--allowedTools', 'Bash Read Write Edit Glob Grep WebSearch WebFetch'])):
            with self.subTest(vendor=vendor), mock.patch.dict(os.environ, self.env, clear=True):
                environment = TASK_MODULE.lane_environment(TASK_MODULE.Task(self.repo, '5'), vendor)
                release = work / 'proof-release'
                release.unlink(missing_ok=True)
                environment['LANE_STUB_RELEASE'] = str(release)
                for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
                    os.environ.pop(key, None)  # The supplied launch environment owns the bus.
                with prompt.open() as source, (work / 'proof.log').open('w') as output:
                    process = TASK_MODULE.launch_unit('lane', self.repo, 't5', command,
                        cwd=wt, env=environment, stdin=source, stdout=output, stderr=output)
                    self.assertGreater(process.pid, 0)
                    self.assertTrue(TASK_MODULE.task_usage.process_start(process.pid))
                    release.touch()
                    self.assertEqual(process.wait(timeout=10), 0)
                call = json.loads((work / 'model-call.json').read_text())
                self.assertEqual(call['args'], command[1:])
                self.assertEqual(call['cwd'], str(wt))
                self.assertEqual(call['prompt'], 'sandbox launch proof')
                self.assertEqual(call['tmpdir'], environment['TMPDIR'])
                log = (work / 'proof.log').read_text()
                self.assertIn(f'{vendor}-log', log)
                if vendor == 'claude':
                    self.assertIn('claude-final: sandbox launch proof', log)
                else:
                    self.assertEqual((work / 'proof-final').read_text(), 'codex-final: sandbox launch proof')
                recorded = json.loads((self.home / 'unit-calls.jsonl').read_text().splitlines()[-1])
                self.assertEqual(recorded['env'], environment)
                self.assertIn('--pipe', recorded['args'])
                self.assertNotIn('--pty', recorded['args'])
                self.assertEqual(recorded['args'][recorded['args'].index('--working-directory') + 1], str(wt))
                for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
                    self.assertEqual(environment[key], self.env[key])
                    self.assertNotIn(f'--setenv={key}={self.env[key]}', recorded['args'])
                self.assertEqual(recorded['environment_file_mode'], 0o600)
                self.assertFalse(Path(recorded['environment_file']).exists())
                self.assertIn('--user', recorded['args'])
                self.assertIn(f'ExecCondition={self.home}/.local/bin/task hold --check', recorded['args'])
                self.assertRegex(process.unit, r'^task-lane-proj-t5-[a-f0-9]+$')
                self.assertGreater(process.pid, 0)
                TASK_MODULE.cleanup_lane_temporary(environment['TMPDIR'] if vendor == 'claude' else None)
        for missing in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
            environment = dict(self.env)
            environment.pop(missing)
            target = work / 'forbidden-without-bus'
            with (work / 'missing-bus.log').open('w') as output, \
                    self.assertRaisesRegex(TASK_MODULE.Stop, 'launch log'):
                TASK_MODULE.launch_unit('lane', self.repo, 'no-bus', ['touch', str(target)],
                    cwd=wt, env=environment, stdout=output, stderr=output)
            self.assertFalse(target.exists())
            self.assertIn('No user systemd bus', (work / 'missing-bus.log').read_text())

    def test_unit_environment_values_stay_out_of_argv_and_file_is_removed(self):
        environment = dict(self.env, HTTPS_PROXY='http://fixture-user:fixture-password@proxy.invalid:8080',
                           GATE_VALUE='quote " backslash \\ and\nsecond line')
        with mock.patch.dict(os.environ, self.env, clear=True):
            process = TASK_MODULE.launch_unit('gate', self.repo, 'env', ['true'], cwd=self.repo, env=environment)
            self.assertEqual(process.wait(timeout=10), 0)
        recorded = json.loads((self.home / 'unit-calls.jsonl').read_text().splitlines()[-1])
        self.assertEqual(recorded['env'], environment)
        for key in ('HTTPS_PROXY', 'GATE_VALUE'):
            self.assertNotIn(environment[key], json.dumps(recorded['args']))
        self.assertEqual(recorded['environment_file_mode'], 0o600)
        self.assertFalse(Path(recorded['environment_file']).exists())
        files = []
        def failed_launch(argv, **kwargs):
            file = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('EnvironmentFile=')))
            files.append(file)
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            raise OSError('fixture launch failure')
        with mock.patch.object(TASK_MODULE.subprocess, 'Popen', side_effect=failed_launch):
            with self.assertRaises(TASK_MODULE.Stop):
                TASK_MODULE.launch_unit('gate', self.repo, 'error', ['true'], cwd=self.repo, env=environment)
        self.assertFalse(files[0].exists())

    def test_review_launch_failure_records_terminal_state_and_releases_pending_run(self):
        wt, _ = self.started()
        state = self.state()
        pending = self.home / '.local/state/task/pending-runs/fixture-reservation.json'
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text('{}')
        prompt, reply = state / 'prompt.md', state / 'reply.md'
        prompt.write_text('review')
        with mock.patch.dict(os.environ, dict(self.env, TASK_PLAN='fixture-plan'), clear=True), \
                mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(TASK_MODULE, 'budget_run', return_value={'id': 'fixture-reservation'}), \
                mock.patch.object(TASK_MODULE, 'plan_budget'), \
                mock.patch.object(TASK_MODULE, 'launch_unit', side_effect=TASK_MODULE.Stop('launch refused')):
            with self.assertRaisesRegex(TASK_MODULE.Stop, 'launch refused'):
                TASK_MODULE.run_reviewer('claude -p --model haiku', wt, prompt, reply)
        self.assertEqual(json.loads((state / 'review-run.json').read_text())['state'], 'launch_failed')
        self.assertFalse(pending.exists())

    def test_unit_stop_accepts_only_gone_units_after_nonzero_stop(self):
        for state in ('inactive', 'failed', '', 'active', 'activating'):
            with self.subTest(state=state):
                process = mock.Mock()
                process.poll.return_value = None
                with mock.patch.object(TASK_MODULE, 'runner_service', return_value={'MainPID': '123'}):
                    unit = TASK_MODULE.UnitProcess(process, 'fixture-unit', self.env)
                stop = subprocess.CompletedProcess([], 1)
                shown = subprocess.CompletedProcess([], 0 if state else 1, stdout=f'ActiveState={state}\n',
                                                     stderr='' if state else 'Unit fixture-unit not found')
                with mock.patch.object(TASK_MODULE.subprocess, 'run', side_effect=[stop, shown]) as calls:
                    if state in ('active', 'activating'):
                        with self.assertRaisesRegex(TASK_MODULE.Stop, 'Cannot stop'):
                            unit.stop()
                        process.wait.assert_not_called()
                    else:
                        unit.stop()
                        process.wait.assert_called_once()
                    self.assertIn('--property=ActiveState', calls.call_args_list[1].args[0])


    def test_hold_read_only_commands_and_release_preserves_jacopo(self):
        self.started()
        runner = self.runner_fixture()
        HOLD_FIXTURE.write_text('Jacopo: keep it held')
        try:
            self.raw_task('hold', 'pull', 'agent reason')
            for command in [('status', 'proj'), ('usage',), ('boost',),
                            ('lock', 'proj'), ('runner-check', 'proj', str(runner)), ('plan', 'proj')]:
                self.raw_task(*command)
            displayed = self.raw_task('hold')
            self.assertIn('Jacopo: keep it held', displayed)
            self.assertIn('agent reason', displayed)
            self.assertIn('sudo agent-hold release', displayed)
            self.raw_task('hold', 'release')
            self.assertFalse((self.home / '.local/state/task/hold').exists())
            self.assertEqual(HOLD_FIXTURE.read_text(), 'Jacopo: keep it held')
            self.assertIn('Jacopo: keep it held', self.raw_task('hold'))
        finally:
            HOLD_FIXTURE.unlink()
        self.assertIn('no hold', self.raw_task('hold'))
        self.raw_task('start', 'proj', 'docs', '--builder', 'codex')

    def test_hold_round_trip_holder_required_and_content_is_only_display(self):
        self.assertIn('no hold', self.raw_task('hold'))
        unnamed = dict(self.env)
        unnamed.pop('TASK_HOLDER')
        self.assertIn('TASK_HOLDER', self.raw_task('hold', 'pull', 'reason', env=unnamed, code=1))
        self.raw_task('hold', 'pull', code=1)
        marker = self.home / '.local/state/task/hold'
        for extra, name in [((), 'test-seat'), (('--holder', 'named-seat'), 'named-seat')]:
            self.raw_task('hold', 'pull', 'pause here', *extra)
            data = json.loads(marker.read_text())
            self.assertEqual((data['mode'], data['who'], data['why']), ('drain', name, 'pause here'))
            datetime.fromisoformat(data['when'])
            self.assertIn('TASK_HOLDER', self.raw_task('hold', 'release', env=unnamed, code=1))
            self.assertTrue(marker.exists())
            self.raw_task('hold', 'release', '--holder', 'named-release', env=unnamed)
            self.assertFalse(marker.exists())
        self.raw_task('hold', 'pull', 'named', '--holder', 'seat', env=unnamed)
        self.raw_task('hold', 'release')
        for content in ('', '{broken', '{"mode":"released"}'):
            marker.write_text(content)
            self.assertIn(str(marker), self.raw_task('hold', '--check', code=1))
            self.assertIn('skipped: hold', self.raw_task('realign', 'proj', code=1))
            run = json.loads((self.home / '.local/state/task/proj/realign/review-run.json').read_text())
            self.assertEqual(run['state'], 'skipped: hold')
            self.assertTrue(run['unit'])
        self.raw_task('hold', 'release')
        self.assertIn('no hold', self.raw_task('hold'))

    def test_hold_inspection_errors_fail_closed(self):
        marker = self.home / '.local/state/task/hold'
        for path in (marker, HOLD_FIXTURE):
            with self.subTest(path=path):
                path.write_text('unreadable reason')
                make_read_only(self, path, 0o000)
                self.assertIn('cannot read', self.raw_task('hold', '--check', code=1))
                self.assertIn('skipped: hold', self.raw_task('realign', 'proj', code=1))
                restore_access(path)
                path.unlink()
                path.mkdir()
                self.assertIn('not a regular file', self.raw_task('hold', '--check', code=1))
                self.assertIn('skipped: hold', self.raw_task('realign', 'proj', code=1))
                path.rmdir()
                path.symlink_to(self.home / 'absent')
                self.assertIn('not a regular file', self.raw_task('hold', '--check', code=1))
                self.assertIn('skipped: hold', self.raw_task('realign', 'proj', code=1))
                path.unlink()
                os.mkfifo(path)
                self.assertIn('not a regular file', self.raw_task('hold', '--check', code=1))
                self.assertIn('skipped: hold', self.raw_task('realign', 'proj', code=1))
                path.unlink()
                path.write_bytes(b'\xff')
                self.assertIn('cannot read', self.raw_task('hold', '--check', code=1))
                self.assertIn('skipped: hold', self.raw_task('realign', 'proj', code=1))
                path.unlink()
        with mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(Path, 'lstat', side_effect=PermissionError('inspection denied')):
            self.assertIn('cannot inspect', TASK_MODULE.hold_status())
        marker.touch()
        with mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(os, 'open', side_effect=FileNotFoundError('changed during inspection')):
            self.assertIn('cannot read', TASK_MODULE.hold_status())
        marker.unlink()

    def test_hold_root_command_and_install_wiring(self):
        source = (ROOT / 'system/agent-hold').read_text()
        self.assertLessEqual(len(source.splitlines()), 25)
        def root_command(*args, uid=0):
            with mock.patch.object(sys, 'argv', ['agent-hold', *args]), \
                    mock.patch.object(os, 'geteuid', return_value=uid), \
                    mock.patch.object(os, 'fchown') as ownership, \
                    contextlib.redirect_stdout(io.StringIO()) as out, \
                    contextlib.redirect_stderr(out):
                try:
                    exec(compile(hold_fixture_source(source), 'agent-hold-fixture', 'exec'), {'__name__': '__main__'})
                    code = 0
                except SystemExit as stopped:
                    code = stopped.code
                return code, out.getvalue(), ownership.call_args_list
        self.assertEqual(root_command()[0], 0)
        self.assertIn('no hold', root_command()[1])
        self.assertEqual(root_command('pull', 'human pause', uid=1001)[0], 1)
        code, output, owners = root_command('pull', 'human pause')
        try:
            self.assertEqual(code, 0, output)
            self.assertIn('jacopo', output)
            self.assertIn('human pause', output)
            self.assertEqual(owners[0].args[1:], (0, 0))
            self.assertEqual(stat.S_IMODE(HOLD_FIXTURE.stat().st_mode), 0o644)
            datetime.fromisoformat(HOLD_FIXTURE.read_text().split()[2].rstrip(':'))
            self.assertIn('human pause', self.raw_task('hold'))
            self.assertEqual(root_command('release', uid=1001)[0], 1)
            self.assertTrue(HOLD_FIXTURE.exists())
            self.assertEqual(root_command('release')[0], 0)
            self.assertFalse(HOLD_FIXTURE.exists())
        finally:
            HOLD_FIXTURE.unlink(missing_ok=True)
        installer = (ROOT / 'system/agent-hold.sh').read_text()
        self.assertIn('require_root', installer)
        self.assertIn('install -o root -g root -m 0755', installer)
        self.assertIn('/usr/local/bin/agent-hold', installer)
        self.assertIn('sudo "$bash_cmd" "$ROOT/system/agent-hold.sh"', (ROOT / 'install.sh').read_text())

    def test_hold_unit_conditions(self):
        units = ROOT / 'home/dot_config/systemd/user'
        for name in ('workspace-tidy.service',):
            unit = (units / name).read_text()
            self.assertNotIn('ConditionPathExists', unit)
            self.assertIn('ExecCondition=%h/.local/bin/task hold --check', unit.split('[Service]')[1].splitlines())
        self.assertNotIn('ExecCondition', (units / 'task-watch.service').read_text())

    def test_hold_check_no_holder_and_fail_closed_for_systemd(self):
        unnamed = dict(self.env)
        unnamed.pop('TASK_HOLDER')
        self.raw_task('hold', '--check', env=unnamed)
        for marker in (HOLD_FIXTURE, self.home / '.local/state/task/hold'):
            with self.subTest(marker=marker):
                marker.write_text('hold at admission')
                self.assertIn(str(marker), self.raw_task('hold', '--check', env=unnamed, code=1))
                make_read_only(self, marker, 0o000)
                self.assertIn('cannot read', self.raw_task('hold', '--check', env=unnamed, code=1))
                restore_access(marker)
                marker.unlink()
                marker.symlink_to(self.home / 'missing-marker-target')
                self.assertIn('not a regular file', self.raw_task('hold', '--check', env=unnamed, code=1))
                marker.unlink()
        parent = self.home / '.local/state/task'
        make_read_only(self, parent, 0o000)
        self.assertIn('cannot inspect', self.raw_task('hold', '--check', env=unnamed, code=1))

    def test_hold_seam_rejects_missing_or_duplicate_substitution(self):
        for source in ('Path("/changed/path")', 'Path("/etc/agent-hold")\n' * 2):
            with self.assertRaisesRegex(AssertionError, 'exactly one'):
                hold_fixture_source(source)













    def test_hold_round5_cancel_prunes_after_failed_worktree_removal(self):
        self.started()
        run_path = self.state() / 'run.json'
        run_path.write_text(json.dumps(dict(state='running', pid=99999999, pgid=99999999)))
        gate = self.repo / '.worktrees/.task-gate-t5-99999999'
        gate.mkdir()
        self.raw_task('hold', 'pull', 'cancel must remain available')
        transport = TASK_MODULE.subprocess.run
        pruned = []
        def fail_removal(command, **kwargs):
            if command[-4:-1] == ['worktree', 'remove', '--force']:
                return subprocess.CompletedProcess(command, 1, '', 'fixture removal failure')
            if command[-2:] == ['worktree', 'prune']:
                pruned.append(command)
            return transport(command, **kwargs)
        # The first sweep sees a live gate; the post-cancel sweep cleans it up.
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(TASK_MODULE, 'pid_alive', side_effect=[True, False, False, False]), \
                mock.patch.object(TASK_MODULE, 'task_lock_status', return_value='running'), \
                mock.patch.object(TASK_MODULE.os, 'killpg', side_effect=ProcessLookupError), \
                mock.patch.object(TASK_MODULE.subprocess, 'run', side_effect=fail_removal):
            self.assertEqual(TASK_MODULE.main(['cancel', str(self.repo), '5']), 0)
        self.assertEqual(json.loads(run_path.read_text())['state'], 'interrupted')
        self.assertFalse(gate.exists())
        self.assertEqual(len(pruned), 1)





    def test_hold_between_gate_steps_prevents_second_launch(self):
        task_file = next((self.kb / 'backlog/tasks').iterdir())
        task_file.write_text(task_file.read_text().replace('Verify: `true`',
            f'Verify: `touch {self.home}/second-gate-launch`'))
        wt, _ = self.started()
        script = self.home / 'during-check.sh'
        script.write_text(f'#!/bin/sh\n"{TOOL}" hold pull "between gate steps" --holder gate-test\n')
        script.chmod(0o755)
        output = self.raw_task('check', 'proj', '5', code=1)
        self.assertIn('skipped: hold', output)
        log = (self.state() / 'check.log').read_text()
        self.assertIn('Ran 3 tests', log)
        self.assertFalse((self.home / 'second-gate-launch').exists())



    def test_project_lock_contention_commands_and_read_only_status(self):
        self.started()
        self.raw_task('lock', 'proj', '--holder', 'seat-a')
        commands = [
            ('start', 'proj', 'new-task', '--builder', 'codex'),
            ('lane', 'proj', '5'), ('runner-start', 'proj', 'runner-demo.md'),
            ('review', 'proj', '5', '--run'), ('merge', 'proj', '5'),
            ('risk', 'proj', '5', 'low'), ('redteam', 'proj', '5', '--brief', 'brief.md'),
            ('close', 'proj', '5', '-m', 'done', '--retro', 'retro'),
            ('check', 'proj', '5'), ('realign', 'proj'),
        ]
        env = dict(self.env, TASK_HOLDER='seat-b')
        for command in commands:
            result = subprocess.run([sys.executable, str(TOOL), *command], env=env, cwd=self.home,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, command)
            self.assertIn('held by seat-a', result.stdout + result.stderr, command)
        self.assertIn('seat-a', self.raw_task('lock', 'proj'))
        self.assertIn('proj:', self.raw_task('status', 'proj'))
        self.sandbox_state()
        self.assertIn('advisory lane check', self.raw_task('check', 'proj', '5', env=env))

    def test_project_lock_crash_ttl_refresh_and_source_change(self):
        api = TASK_MODULE.task_lock
        with mock.patch.dict(os.environ, TASK_HOLDER='seat-env'):
            self.assertEqual(api.acquire(self.home, self.repo)['holder'], 'seat-env')
        api.release(self.home, self.repo, holder='seat-env')
        first = api.acquire(self.home, self.repo, 'seat-a', ttl=120)
        original = first['taken_at']
        api.acquire(self.home, self.repo, 'seat-a', ttl=120)
        self.assertGreaterEqual(api.read(self.home, self.repo)['expires_epoch'], first['expires_epoch'])
        data = api.read(self.home, self.repo)
        data.update(expires_epoch=time.time() - 1, taken_at=original)
        api.write(self.home, self.repo, data)
        self.assertTrue(api.free(api.read(self.home, self.repo)))
        data.update(expires_epoch=time.time() + 600, released=False)
        api.write(self.home, self.repo, data)
        (self.repo / 'a.txt').write_text('changed\n')
        self.git(self.repo, 'add', 'a.txt')
        self.git(self.repo, 'commit', '-qm', 'source changed')
        api.release(self.home, self.repo, holder='seat-a')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            api.acquire(self.home, self.repo, 'seat-b', revision=self.git(self.repo, 'rev-parse', 'HEAD'))
        self.assertIn('Sources changed', out.getvalue())

    def lock_process(self, holder='batch-seat'):
        script = self.home / 'lock-owner.py'
        script.write_text(textwrap.dedent(f'''\
            import pathlib, sys, time
            sys.path.insert(0, {str(TOOL.parent)!r})
            import task_lock, task_usage
            task_lock.atomic_write = task_usage._write
            task_lock.acquire(pathlib.Path.home(), {str(self.repo)!r}, sys.argv[1], revision=task_lock.head({str(self.repo)!r}))
            print('locked', flush=True)
            time.sleep(30)
        '''))
        child = subprocess.Popen([sys.executable, str(script), holder], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_lock_process, child)
        self.assertEqual(child.stdout.readline().strip(), 'locked')
        return child

    def stop_lock_process(self, child):
        if child.poll() is None: child.kill()
        child.communicate()

    def test_project_lock_child_refresh_keeps_runner_process(self):
        api = TASK_MODULE.task_lock
        runner = self.lock_process()
        try:
            first = api.read(self.home, self.repo)
            refreshed = api.acquire(self.home, self.repo, 'batch-seat')
            self.assertTrue(refreshed['process'])
            self.assertEqual(refreshed['token'], first['token'])
            runner.terminate()
            runner.wait()
            self.assertTrue(api.free(api.read(self.home, self.repo)))
            self.raw_task('start', 'proj', 'docs', '--builder', 'codex', env=dict(self.env, TASK_HOLDER='batch-seat'))
            recovered = api.read(self.home, self.repo)
            self.assertTrue(recovered['process'])
            self.assertNotEqual(recovered['token'], first['token'])
        finally:
            if runner.poll() is None:
                runner.terminate()
                runner.wait()

    def test_project_lock_corrupt_file_is_held_until_forced_release(self):
        target = TASK_MODULE.task_lock.path(self.home, self.repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{broken')
        self.assertIn('held by unknown', self.raw_task('lock', 'proj'))
        with self.assertRaises(TASK_MODULE.task_lock.LockBusy):
            TASK_MODULE.task_lock.acquire(self.home, self.repo, 'seat-a')
        self.assertIn('held by unknown', self.raw_task('lock', 'proj', '--release', code=1))
        self.raw_task('lock', 'proj', '--release', '--force')
        self.assertFalse(target.exists())

    def test_project_lock_release_requires_recorded_holder(self):
        api = TASK_MODULE.task_lock
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                self.raw_task('lock', 'proj', '--holder', 'seat-a')
                before = api.path(self.home, self.repo).read_text()
                args = ('--holder', 'seat-b') if explicit else ()
                output = self.raw_task('lock', 'proj', '--release', *args,
                                       env=dict(self.env, TASK_HOLDER='seat-a' if explicit else 'seat-b'), code=1)
                self.assertIn('held by seat-a', output)
                self.assertIn('--release --force', output)
                self.assertEqual(api.path(self.home, self.repo).read_text(), before)
                self.assertIn('seat-a', self.raw_task('lock', 'proj'))
                args = ('--holder', 'seat-a') if explicit else ()
                self.raw_task('lock', 'proj', '--release', *args,
                              env=dict(self.env, TASK_HOLDER='seat-b' if explicit else 'seat-a'))
                self.assertIn('free', self.raw_task('lock', 'proj'))

    def test_project_lock_release_without_identity_and_forced_recovery(self):
        api = TASK_MODULE.task_lock
        unnamed = dict(self.env)
        unnamed.pop('TASK_HOLDER')
        for held in (False, True):
            with self.subTest(held=held):
                if held: self.raw_task('lock', 'proj', '--holder', 'seat-a')
                before = api.read(self.home, self.repo)
                for identity in (None, '', '   '):
                    env = unnamed if identity is None else dict(unnamed, TASK_HOLDER=identity)
                    self.assertIn('TASK_HOLDER', self.raw_task('lock', 'proj', '--release', env=env, code=1))
                    self.assertEqual(api.read(self.home, self.repo), before)
                self.raw_task('lock', 'proj', '--release', '--force', env=unnamed)
                self.assertIn('free', self.raw_task('lock', 'proj'))

    def test_project_lock_release_free_lock_is_noop(self):
        api = TASK_MODULE.task_lock
        self.raw_task('lock', 'proj', '--release', '--holder', 'seat-b')
        self.assertIsNone(api.read(self.home, self.repo))
        data = api.acquire(self.home, self.repo, 'seat-a')
        for fields in ({'released': True}, {'expires_epoch': 0}, {'process': True}):
            with self.subTest(fields=fields):
                api.write(self.home, self.repo, dict(data, **fields))
                self.raw_task('lock', 'proj', '--release', '--holder', 'seat-b')
                self.assertTrue(api.free(api.read(self.home, self.repo)))

    def test_project_lock_release_refuses_running_work_even_with_force(self):
        self.lock_process('seat-a')
        api = TASK_MODULE.task_lock
        before = api.path(self.home, self.repo).read_text()
        for holder in ('seat-a', 'seat-b'):
            for args in ((), ('--force',)):
                with self.subTest(holder=holder, args=args):
                    output = self.raw_task('lock', 'proj', '--release', '--holder', holder, *args, code=1)
                    self.assertIn('Work is still running', output)
                    self.assertEqual(api.path(self.home, self.repo).read_text(), before)

    def test_project_lock_identity_invalid_arguments_and_simultaneous_take(self):
        unnamed = dict(self.env)
        unnamed.pop('TASK_HOLDER')
        for command in [('start', 'proj', 'docs', '--builder', 'codex'), ('lock', 'proj', '--ttl', '1')]:
            self.assertIn('TASK_HOLDER', self.raw_task(*command, env=unnamed, code=1))
        self.raw_task('runner-start', 'proj', 'invalid.md', code=1)
        self.assertIsNone(TASK_MODULE.task_lock.read(self.home, self.repo))
        barrier = self.home / 'acquire.py'
        barrier.write_text(textwrap.dedent(f'''\
            import pathlib, runpy, sys, time
            home = pathlib.Path.home()
            (home / (sys.argv[1] + '.ready')).touch()
            while not (home / 'go').exists(): time.sleep(.005)
            sys.argv = [{str(TOOL)!r}, 'lock', 'proj', '--holder', sys.argv[1]]
            runpy.run_path({str(TOOL)!r}, run_name='__main__')
        '''))
        children = [subprocess.Popen([sys.executable, str(barrier), name],
                    env=self.env, cwd=self.home, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for name in ('seat-a', 'seat-b')]
        for name in ('seat-a', 'seat-b'): self.wait_runner_file(self.home / (name + '.ready'))
        (self.home / 'go').touch()
        outputs = [child.communicate() for child in children]
        self.assertEqual(sorted(child.returncode for child in children), [0, 1], outputs)

    def test_project_lock_recovery_commands_and_pending_sources(self):
        api = TASK_MODULE.task_lock
        for cause in ('dead', 'ttl'):
            with self.subTest(cause=cause):
                api.release(self.home, self.repo, force=True)
                if cause == 'dead':
                    process = self.lock_process('test-seat')
                    data = api.read(self.home, self.repo)
                    process.kill(); process.wait()
                else:
                    data = api.acquire(self.home, self.repo, 'test-seat')
                    data['expires_epoch'] = 0
                api.write(self.home, self.repo, data)
                self.raw_task('start', 'proj', 'docs', '--builder', 'codex')
                recovered = api.read(self.home, self.repo)
                self.assertTrue(recovered['process'])
                self.assertNotEqual(recovered['token'], data['token'])
        api.release(self.home, self.repo)
        old = api.read(self.home, self.repo)['head']
        self.raw_task('lock', 'proj', '--holder', 'test-seat')
        wt = self.repo / '.worktrees/docs'
        self.commit_in(wt, 'changed.txt')
        # Explicit takeover does not consume the baseline before work compares it.
        self.assertEqual(api.read(self.home, self.repo)['head'], old)
        with mock.patch.dict(os.environ, TASK_HOLDER='test-seat'), contextlib.redirect_stdout(io.StringIO()) as out:
            api.work(self.home, self.repo, self.git(wt, 'rev-parse', 'HEAD'))
        self.assertIn('Sources changed', out.getvalue())

    def test_project_lock_malformed_fields_and_inspection_errors_refuse(self):
        api = TASK_MODULE.task_lock
        data = api.acquire(self.home, self.repo, 'test-seat')
        for key, value in [('released', 'false'), ('process', 'true'), ('pending', 1),
                           ('expires_epoch', '1'), ('expires_epoch', float('nan')),
                           ('taken_at', 1), ('holder', None), ('token', None)]:
            with self.subTest(key=key, value=value):
                api.write(self.home, self.repo, dict(data, **{key: value}))
                self.assertIn('--release --force', self.raw_task('start', 'proj', 'docs', '--builder', 'codex', code=1))
        api.write(self.home, self.repo, data)
        api.work(self.home, self.repo)
        for operation in ('active',):
            with mock.patch.object(api, operation, side_effect=PermissionError('inspection denied')):
                with self.assertRaisesRegex(api.LockBusy, '--release --force'):
                    api.acquire(self.home, self.repo, 'test-seat')
        with mock.patch.object(Path, 'open', side_effect=PermissionError('guard denied')):
            with self.assertRaisesRegex(api.LockBusy, '--release --force'):
                api.acquire(self.home, self.repo, 'test-seat')

    def test_project_lock_source_revision_refuses(self):
        api = TASK_MODULE.task_lock
        data = api.acquire(self.home, self.repo, 'test-seat')
        with mock.patch.object(api, 'head', side_effect=api.LockBusy('Cannot establish execution source revision')):
            with self.assertRaisesRegex(api.LockBusy, 'source revision'):
                api.work(self.home, self.repo)
        self.assertEqual(api.read(self.home, self.repo), data)

    def test_project_lock_merge_prevents_takeover_through_publication(self):
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        # Even an administrative release cannot open a publication race.
        script = self.home / 'during-check.sh'
        script.write_text(f'"{sys.executable}" "{TOOL}" lock proj --release\n'
                          f'"{sys.executable}" "{TOOL}" lock proj --holder intruder\n')
        script.chmod(0o755)
        base = self.git(self.repo, 'rev-parse', 'origin/main')
        output = self.raw_task('merge', 'proj', '5', '--keep')
        self.assertIn('Merged t5', output)
        self.assertNotEqual(self.git(self.repo, 'rev-parse', 'origin/main'), base)
        self.assertIn('still running', (self.home / '.local/state/task/proj/t5/gate-check.log').read_text())

    def test_project_lock_local_merge_checks_ownership_before_recording(self):
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        self.git(self.repo, 'remote', 'remove', 'origin')
        before = self.git(self.repo, 'rev-parse', 'main')
        # Simulate replacement of the ownership record while the local merge gates.
        replacement = self.home / 'replace-lock.py'
        replacement.write_text(textwrap.dedent('''\
            import json, pathlib
            target = pathlib.Path.home() / '.local/state/task/proj/lock.json'
            data = json.loads(target.read_text())
            data.update(holder='intruder', token='replacement-token')
            target.write_text(json.dumps(data))
        '''))
        during = self.home / 'during-check.sh'
        during.write_text(f'"{sys.executable}" "{replacement}"\n')
        during.chmod(0o755)
        self.assertIn('held by intruder', self.raw_task('merge', 'proj', '5', '--keep', code=1))
        self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), before)
        self.assertFalse((self.home / '.local/state/task/proj/t5/merge.json').exists())

    def test_project_lock_lane_outlives_launcher_and_worker(self):
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('Build the b file.')
        self.stub_models()
        release = self.home / 'release-builder'
        self.raw_task('lock', 'proj', '--holder', 'test-seat', '--ttl', '.001')
        self.raw_task('lane', 'proj', '5', env=dict(self.env, LANE_STUB_RELEASE=str(release)))
        self.wait_runner_file(wt / '.work/model-call.json')
        data = TASK_MODULE.task_lock.read(self.home, self.repo)
        data['expires_epoch'] = 0
        TASK_MODULE.task_lock.write(self.home, self.repo, data)
        self.assertTrue(data['process'])
        self.assertIn('held by test-seat', self.raw_task('lock', 'proj', '--holder', 'other', code=1))
        worker = json.loads((wt / '.work/lane.json').read_text())['pid']
        os.kill(worker, signal.SIGKILL)
        try:
            # The builder inherited ownership too, so killing its wrapper cannot admit overlap.
            self.assertIn('held by test-seat', self.raw_task('lock', 'proj', '--holder', 'other', code=1))
        finally:
            release.touch()
            self.wait_project_lock_free()
        self.raw_task('start', 'proj', 'docs', '--builder', 'codex', env=dict(self.env, TASK_HOLDER='other'))

    def wait_project_lock_free(self, timeout=10):
        api = TASK_MODULE.task_lock
        deadline = time.monotonic() + timeout
        while not api.free(api.read(self.home, self.repo)) and time.monotonic() < deadline: time.sleep(.02)
        self.assertTrue(api.free(api.read(self.home, self.repo)), 'process lock must be released before cleanup')
    def run_rows(self):
        path = self.home / '.local/state/task/runs.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def budget_fixture(self, ident='chain', points=3, allowed=None, expires=None):
        path = self.home / 'cortex/cortex-kb-company/plans' / f'{ident}.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        expiry = expires or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        path.write_text(f'id = "{ident}"\nexpires = "{expiry}"\n[points]\nclaude = {points}\ncodex = {points}\n[allowed]\n' +
                        '\n'.join(f'{repo} = {json.dumps(tasks)}' for repo, tasks in (allowed or {'proj': ['*']}).items()) + '\n')
        return path

    @contextlib.contextmanager
    def budget_environment(self):
        with mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(TASK_MODULE, 'CORTEX', self.home / 'cortex'), \
                mock.patch.dict(os.environ, dict(self.env, TASK_PLAN='chain'), clear=True), \
                mock.patch.object(TASK_MODULE.task_usage, 'readings', return_value={}):
            yield TASK_MODULE

    def test_plan_budget_duplicate_crash_and_exact_short_batch_chain(self):
        self.budget_fixture(points=3)
        with self.budget_environment() as api:
            fields = dict(repo='proj', task='docs', kind='lane', vendor='codex', commit='head-1')
            first = api.budget_run(**fields)  # crash before launch: only a pending row survives
            self.assertEqual(self.run_rows(), [])
            self.assertEqual(api.budget_run(**fields)['id'], first['id'])
            pending = self.home / '.local/state/task/pending-runs'
            self.assertEqual(len(list(pending.glob('*.json'))), 1)
            api.task_usage.run_log(self.home, first, 'finished')
            retry = api.budget_run(**fields)
            self.assertNotEqual(retry['id'], first['id'])
            api.task_usage.run_log(self.home, retry, 'finished')
            for number in (2, 3):
                run = api.budget_run(**dict(fields, commit=f'head-{number}'))
                api.task_usage.run_log(self.home, run, 'finished')
            self.assertEqual(len(self.run_rows()), 4)
            with self.assertRaisesRegex(api.Stop, 'codex points exhausted'):
                api.budget_run(**dict(fields, commit='head-4'))
            retry = api.budget_run(**fields)
            api.task_usage.run_log(self.home, retry, 'finished')
            self.assertEqual(len({r['id'] for r in self.run_rows()}), 5, 'every invocation keeps its own outcome')
            before = {p: p.read_bytes() for p in (self.home / '.local/state/task').rglob('*') if p.is_file()}
            self.assertIn('codex used 3 left 0', api.plan_budget('chain'))
            self.raw_task('plan-budget', 'chain')
            self.raw_task('usage', '--weekly-line')
            self.raw_task('status', 'proj')
            self.assertEqual(before, {p: p.read_bytes() for p in (self.home / '.local/state/task').rglob('*') if p.is_file()})
            self.assertEqual(len(self.run_rows()), 5, 'reporting never adds draws or refills')

    def test_plan_budget_pending_vendor_kind_scope_and_expiry(self):
        plan = self.budget_fixture(points=1, allowed={'proj': ['T-5']})
        with self.budget_environment() as api:
            fields = dict(repo='proj', task='T-5', kind='lane', vendor='codex')
            pending = api.budget_run(**fields)
            self.assertEqual(api.budget_run(**fields)['id'], pending['id'])
            with self.assertRaisesRegex(api.Stop, 'points exhausted'):
                api.budget_run(**dict(fields, kind='review'))
            other = api.budget_run(**dict(fields, kind='review', vendor='claude'))
            self.assertNotEqual(other['id'], pending['id'])
            for outside in ({'task': 'T-6'}, {'repo': 'elsewhere'}):
                with self.assertRaisesRegex(api.Stop, 'outside allowed'):
                    api.budget_run(**dict(fields, **outside))
            plan.write_text(plan.read_text().replace('expires = "', 'expires = "2000-01-01T00:00:00Z" # '))
            with self.assertRaisesRegex(api.Stop, 'expired'):
                api.budget_run(**fields)

    def test_plan_budget_breaker_classes_are_consecutive_latched_and_clearable(self):
        self.budget_fixture(points=20)
        with self.budget_environment() as api:
            def end(kind, outcome, reason):
                row = api.budget_run(repo='proj', task='docs', kind=kind, vendor='codex', commit=str(len(self.run_rows())))
                api.task_usage.run_log(self.home, row, outcome, reason=reason)
            end('lane', 'failed', 'same text')
            end('lane', 'interrupted', 'same text')
            self.assertIn('disabled []', api.plan_budget('chain'))
            end('review', 'failed', 'unrelated kind')
            end('lane', 'interrupted', 'different text')
            with self.assertRaisesRegex(api.Stop, 'lane disabled'):
                end('lane', 'finished', '')
            self.assertIn("disabled ['lane']", self.raw_task('plan-budget', 'chain'))
            before = len(self.run_rows())
            self.raw_task('plan-budget', 'chain', '--clear', 'lane')
            self.assertEqual(len(self.run_rows()), before + 1)
            self.assertEqual(self.run_rows()[-1]['outcome'], 'cleared')
            self.assertIn('disabled []', self.raw_task('plan-budget', 'chain'))
            end('lane', 'failed', 'one')
            end('lane', 'finished', 'success resets consecutive failures')
            end('lane', 'failed', 'two')
            self.assertIn('disabled []', api.plan_budget('chain'))
            end('lane', 'failed', 'three')
            self.assertIn("disabled ['lane']", api.plan_budget('chain'))

    def test_plan_budget_read_only_missing_malformed_and_status(self):
        state = self.home / '.local/state/task'
        before = {p: p.read_bytes() for p in state.rglob('*') if p.is_file()}
        self.budget_fixture(points=0)
        self.assertIn('codex used 0 left 0', self.raw_task('plan-budget', 'chain'))
        self.assertEqual(before, {p: p.read_bytes() for p in state.rglob('*') if p.is_file()})
        self.assertIn('Missing or malformed plan', self.raw_task('plan-budget', 'absent', code=1))
        path = self.budget_fixture('broken')
        valid = path.read_text()
        for bad in ('id = [', valid.replace('id = "broken"', 'id = "wrong"'),
                    valid.replace('codex = 3', 'codex = -1'), valid.replace('codex = 3', 'codex = true'),
                    valid.replace('[allowed]', 'allowed = "bad"\n[invalid]'),
                    valid.replace('+00:00', '+01:00')):
            with self.subTest(bad=bad):
                path.write_text(bad)
                self.assertIn('Missing or malformed plan', self.raw_task('plan-budget', 'broken', code=1))
                self.assertIn('plan broken: malformed', self.raw_task('status', 'proj'))
        self.budget_fixture('expired', expires='2000-01-01T00:00:00Z')
        self.assertNotIn('plan expired:', self.raw_task('status', 'proj'))
        self.assertIn('plan chain:', self.raw_task('status', 'proj'))

    def test_plan_budget_cli_start_lane_and_review_share_the_log(self):
        self.budget_fixture(points=2)
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--plan', 'chain')
        wt = self.repo / '.worktrees/t5'
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        self.raw_task('lane', 'proj', '5', '--plan', 'chain')
        self.wait_for_lane(wt)
        self.assertEqual([(r['kind'], r['vendor']) for r in self.run_rows()], [('start', 'codex'), ('lane', 'codex')])
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--plan', 'chain')
        self.raw_task('lane', 'proj', '5', '--plan', 'chain')
        self.wait_for_lane(wt)
        self.assertEqual(len(self.run_rows()), 4, 'completed start and same-commit lane keep separate free attempts')
        self.commit_in(wt, 'b.txt')
        self.assertIn('points exhausted', self.raw_task('lane', 'proj', '5', '--plan', 'chain', code=1))
        self.raw_task('check', 'proj', '5')
        self.stub_reviewers()
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n'
        self.raw_task('review', 'proj', '5', '--run', '--plan', 'chain', env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertEqual((self.run_rows()[-1]['kind'], self.run_rows()[-1]['vendor']), ('review', 'claude'))
        self.assertIn('codex used 2 left 0', self.raw_task('plan-budget', 'chain'))
        self.assertIn('claude used 1 left 1', self.raw_task('plan-budget', 'chain'))

    def test_plan_budget_refused_start_draws_nothing_and_never_trips_breaker(self):
        self.budget_fixture(points=1)
        task_file = self.kb / 'backlog/tasks/t-5 - Add-the-b-file.md'
        original = task_file.read_text()
        justfile = self.repo / 'justfile'
        intent = self.kb / 'INTENT.md'
        cases = [
            (justfile, None, 'No justfile'),
            (task_file, original.replace('- [ ]', 'criterion'), 'no acceptance criteria'),
            (task_file, original.replace('Verify:', 'Command:'), 'no `Verify:'),
            (task_file, original.replace('`true`', '`missing-plan-test-command`'), 'not installed'),
            (intent, intent.read_text() + '\nChanged plan\n', 'changed since sign-off'),
        ]
        for path, content, message in cases:
            with self.subTest(message=message):
                saved = path.read_text()
                if content is None:
                    path.unlink()
                else:
                    path.write_text(content)
                try:
                    for _ in range(2):
                        output = self.raw_task('start', 'proj', '5', '--builder', 'codex', '--plan', 'chain', code=1)
                        self.assertIn(message, output)
                        self.assertEqual(self.run_rows(), [])
                        self.assertEqual(list((self.home / '.local/state/task/pending-runs').glob('*.json')), [])
                        self.assertFalse((self.repo / '.worktrees/t5').exists())
                finally:
                    path.write_text(saved)
        self.assertIn('disabled []', self.raw_task('plan-budget', 'chain'))
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--plan', 'chain')
        self.assertEqual([(r['kind'], r['outcome']) for r in self.run_rows()], [('start', 'finished')])

    def test_plan_budget_failed_start_reuses_task_key(self):
        self.budget_fixture(points=1)
        probe = self.home / 'failed-start.py'
        probe.write_text(textwrap.dedent(f'''\
            import runpy
            from unittest import mock
            api = runpy.run_path({str(TOOL)!r})
            with mock.patch.dict(api['start'].__globals__, prepare_environment=mock.Mock(side_effect=RuntimeError('after creation'))):
                api['main'](['start', 'proj', '5', '--builder', 'codex', '--plan', 'chain'])
        '''))
        result = subprocess.run([sys.executable, str(probe)], env=self.env, cwd=self.home,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('after creation', result.stderr)
        rows = self.run_rows()
        self.assertEqual([r['outcome'] for r in rows], ['failed'])
        record = json.loads((self.state() / 'task.json').read_text())
        self.assertEqual(record['plan'], 'chain')
        self.assertEqual(record['revision'], TASK_MODULE.record_revision(record))
        self.assertIn('Conflicting plan', self.raw_task('start', 'proj', '5', '--builder', 'codex', '--plan', 'other', code=1))
        self.raw_task('start', 'proj', '5', '--builder', 'codex')
        self.assertEqual([r['outcome'] for r in self.run_rows()], ['failed', 'finished'])
        self.assertIn('codex used 1 left 0', self.raw_task('plan-budget', 'chain'))
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['plan'], 'chain')
        self.stub_models()
        wt = self.repo / '.worktrees/t5'
        (wt / '.work/prompt.md').write_text('task')
        self.assertIn('points exhausted', self.raw_task('lane', 'proj', '5', code=1))
        self.assertFalse((wt / '.work/model-call.json').exists())

    def test_plan_budget_refused_start_allows_corrected_plan_before_binding(self):
        self.budget_fixture('valid', points=10)
        self.budget_fixture('outside', allowed={'proj': ['other']})
        self.budget_fixture('expired', expires='2000-01-01T00:00:00Z')
        self.budget_fixture('exhausted', points=0)
        self.budget_fixture('malformed').write_text('id = [')
        for ident, message in (('missing', 'Missing or malformed'), ('malformed', 'Missing or malformed'),
                               ('outside', 'outside allowed'), ('expired', 'expired'),
                               ('exhausted', 'points exhausted')):
            with self.subTest(plan=ident):
                name = 'refused-' + ident
                rows = self.run_rows()
                self.assertIn(message, self.raw_task('start', 'proj', name, '--builder', 'codex',
                                                    '--plan', ident, code=1))
                self.assertFalse((self.state(name) / 'task.json').exists(), 'no refused binding')
                self.assertFalse((self.repo / '.worktrees' / name).exists())
                self.assertEqual(self.run_rows(), rows)
                self.assertEqual(list((self.home / '.local/state/task/pending-runs').glob('*.json')), [])
                self.raw_task('start', 'proj', name, '--builder', 'codex', '--plan', 'valid')
                record = json.loads((self.state(name) / 'task.json').read_text())
                self.assertEqual(record['plan'], 'valid')
                self.assertEqual(record['revision'], TASK_MODULE.record_revision(record))
                self.assertEqual(self.run_rows()[-1]['plan'], 'valid')

    def test_plan_budget_failed_worktree_creation_keeps_binding_for_retry(self):
        self.budget_fixture(points=1)
        probe = self.home / 'failed-worktree.py'
        probe.write_text(textwrap.dedent(f'''\
            import runpy
            from unittest import mock
            api = runpy.run_path({str(TOOL)!r})
            original = api['git']
            def git(repo, *args):
                if args[:2] == ('worktree', 'add'): raise RuntimeError('worktree creation failed')
                return original(repo, *args)
            with mock.patch.dict(api['start'].__globals__, git=git):
                api['main'](['start', 'proj', '5', '--builder', 'codex', '--plan', 'chain'])
        '''))
        result = subprocess.run([sys.executable, str(probe)], env=self.env, cwd=self.home,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('worktree creation failed', result.stderr)
        self.assertFalse((self.repo / '.worktrees/t5').exists())
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['plan'], 'chain')
        self.assertEqual([r['outcome'] for r in self.run_rows()], ['failed'])
        self.assertIn('Conflicting plan', self.raw_task('start', 'proj', '5', '--builder', 'codex', code=1,
                                                     env=dict(self.env, TASK_PLAN='other')))
        self.raw_task('start', 'proj', '5', '--builder', 'codex')
        self.assertEqual([(r['plan'], r['outcome']) for r in self.run_rows()],
                         [('chain', 'failed'), ('chain', 'finished')])
        self.assertIn('codex used 1 left 0', self.raw_task('plan-budget', 'chain'))

    def test_plan_budget_no_plan_start_refuses_missing_branch_without_refresh(self):
        self.started()
        record_path = self.state() / 'task.json'
        original = record_path.read_text()
        damaged = json.loads(original)
        damaged.pop('branch')
        record_path.write_text(json.dumps(damaged))
        before = record_path.read_bytes()
        output = self.raw_task('start', 'proj', '5', '--builder', 'codex', code=1)
        self.assertIn('damaged; ask the PM to refresh', output)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.run_rows(), [])
        # Also pin the late record check, after initial plan binding validation.
        record_path.write_text(original)
        probe = self.home / 'damage-start-record.py'
        probe.write_text(textwrap.dedent(f'''\
            import json, runpy
            from pathlib import Path
            from unittest import mock
            api = runpy.run_path({str(TOOL)!r})
            prepare = api['prepare_environment']
            def damage(*args):
                prepare(*args)
                path = Path({str(record_path)!r})
                record = json.loads(path.read_text())
                record.pop('branch')
                path.write_text(json.dumps(record))
            with mock.patch.dict(api['start'].__globals__, prepare_environment=damage):
                raise SystemExit(api['main'](['start', 'proj', '5', '--builder', 'codex']))
        '''))
        result = subprocess.run([sys.executable, str(probe)], env=self.env, cwd=self.home,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('damaged; ask the PM to refresh', result.stderr)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.run_rows(), [])

    def test_plan_budget_review_unstarted_task_stops_before_opening_lock(self):
        self.assertFalse(self.state().exists())
        output = self.raw_task('review', 'proj', '5', '--run', code=1)
        self.assertIn('No pinned task record', output)
        self.assertNotIn('Traceback', output)
        self.assertFalse((self.state() / 'review.lock').exists())
        self.assertEqual(self.run_rows(), [])

    def test_plan_budget_refresh_repairs_damaged_record_and_preserves_binding(self):
        self.started()
        record_path = self.state() / 'task.json'
        self.budget_fixture(points=1)
        for planned in (False, True):
            with self.subTest(planned=planned):
                if planned:
                    self.raw_task('start', 'proj', '5', '--builder', 'codex', '--plan', 'chain', '--refresh')
                record = json.loads(record_path.read_text())
                record['revision'] = 'damaged'
                record_path.write_text(json.dumps(record))
                self.assertIn('damaged', self.raw_task('start', 'proj', '5', '--builder', 'codex', code=1))
                if planned:
                    before = record_path.read_bytes()
                    self.assertIn('Conflicting plan', self.raw_task('start', 'proj', '5', '--builder', 'codex',
                                                                 '--refresh', '--plan', 'other', code=1))
                    self.assertEqual(record_path.read_bytes(), before)
                self.raw_task('start', 'proj', '5', '--builder', 'codex', '--refresh')
                repaired = json.loads(record_path.read_text())
                self.assertEqual(repaired['revision'], TASK_MODULE.record_revision(repaired))
                self.assertEqual(repaired.get('plan'), 'chain' if planned else None)
                self.assertEqual(len(self.run_rows()), 2 if planned else 0)
        self.assertTrue(all(r['plan'] == 'chain' for r in self.run_rows()))
        self.assertIn('codex used 1 left 0', self.raw_task('plan-budget', 'chain'))

    def test_plan_budget_lane_and_review_keys_follow_commits(self):
        wt, _ = self.started()
        self.budget_fixture(points=2)
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        heads = []
        for number in range(2):
            if number:
                self.commit_in(wt, 'b.txt')
            heads.append(self.git(wt, 'rev-parse', 'HEAD'))
            for repeat in range(2):
                extra = ('--extra-round', 'fourth launch for commit-key fixture') if number == repeat == 1 else ()
                self.raw_task('lane', 'proj', '5', '--plan', 'chain', *extra)
                self.wait_for_lane(wt)
            self.assertEqual(len(self.run_rows()), 2 * (number + 1))
        self.assertEqual([r['commit'] for r in self.run_rows()], [h for h in heads for _ in range(2)])
        self.stub_reviewers()
        review_heads = []
        for number in range(2):
            if number:
                self.commit_in(wt, 'c.txt')
            head = self.git(wt, 'rev-parse', 'HEAD')
            review_heads.append(head)
            self.raw_task('check', 'proj', '5')
            reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n'
            for _ in range(2):
                self.raw_task('review', 'proj', '5', '--run', '--plan', 'chain',
                              env=dict(self.env, REVIEW_STUB_REPLY=reply))
            rows = [r for r in self.run_rows() if r['kind'] == 'review']
            self.assertEqual(len(rows), 2 * (number + 1))
        self.assertEqual([r['commit'] for r in rows], [h for h in review_heads for _ in range(2)])
        self.assertIn('codex used 2 left 0', self.raw_task('plan-budget', 'chain'))
        self.assertIn('claude used 2 left 0', self.raw_task('plan-budget', 'chain'))

    def test_plan_budget_launches_respect_risk_round_limits_and_log_effort(self):
        wt, _ = self.started()
        self.budget_fixture(points=1)
        self.stub_models()
        work = wt / '.work'
        (work / 'prompt.md').write_text('round-limit fixture\n')
        for builder in ('codex', 'claude'):
            for risk, maximum in (('low', 2), ('normal', 3), ('high', 3)):
                with self.subTest(builder=builder, risk=risk):
                    self.raw_task('risk', 'proj', '5', risk, '--reason', 'planned round fixture')
                    (self.state() / 'rounds.json').unlink(missing_ok=True)
                    (work / 'lane.json').unlink(missing_ok=True)
                    for number in range(1, maximum + 1):
                        self.raw_task('lane', 'proj', '5', '--builder', builder, '--plan', 'chain', '--effort', 'medium', '--no-pace')
                        run = self.wait_for_lane(wt)['usage_run']
                        self.assertEqual((run['plan'], run['effort'], run['round']), ('chain', 'medium', number))
                    (work / 'model-call.json').unlink()
                    before = {p.name: p.read_bytes() for p in work.glob('final*.md')}
                    rows = self.run_rows()
                    pending = list((self.home / '.local/state/task/pending-runs').glob('*.json'))
                    self.assertEqual(pending, [])
                    refused = self.raw_task('lane', 'proj', '5', '--builder', builder, '--plan', 'chain', '--no-pace', code=1)
                    self.assertIn(f'risk {risk}: maximum {maximum}', refused)
                    self.assertEqual(self.run_rows(), rows)
                    self.assertEqual(list((self.home / '.local/state/task/pending-runs').glob('*.json')), [])
                    self.assertFalse((work / 'model-call.json').exists())
                    self.assertEqual({p.name: p.read_bytes() for p in work.glob('final*.md')}, before)
                    self.raw_task('lane', 'proj', '5', '--builder', builder, '--plan', 'chain', '--no-pace', '--extra-round', 'bounded planned fix')
                    self.assertEqual(self.wait_for_lane(wt)['usage_run']['round'], maximum + 1)
                    self.assertIn(f'{builder} used 1 left 0', self.raw_task('plan-budget', 'chain'))
        rows = self.run_rows()
        self.assertEqual(len(rows), 22)
        self.assertEqual(len({(r['vendor'], r['commit']) for r in rows}), 2, 'rounds do not change draw keys')

    def test_plan_budget_reviewer_fallback_shares_reviewed_commit_draw(self):
        wt, _ = self.started()
        self.budget_fixture(points=1)
        self.stub_reviewers()
        self.raw_task('check', 'proj', '5')
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n'
        self.raw_task('review', 'proj', '5', '--run', '--plan', 'chain',
                      env=dict(self.env, REVIEW_STUB_FIRST_REPLY="You've hit your weekly limit",
                               REVIEW_STUB_STDERR="ERROR: You've hit your usage limit.",
                               REVIEW_STUB_FIRST_EXIT='2', REVIEW_STUB_REPLY=reply, REVIEW_STUB_EXIT='0'))
        self.assertEqual((wt / '.work/reviewer-count').read_text(), '2')
        self.assertEqual(json.loads((self.state() / 'review.json').read_text())['reviewer'], 'codex')
        rows = self.run_rows()
        self.assertEqual([(r['kind'], r['commit'], r['vendor'], r['outcome']) for r in rows],
                         [('review', head, 'claude', 'failed'), ('review', head, 'codex', 'finished')])
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])
        self.assertEqual(rows[1]['replaces'], rows[0]['id'])
        self.assertIn('claude used 0 left 1', self.raw_task('plan-budget', 'chain'))
        self.assertIn('codex used 1 left 0', self.raw_task('plan-budget', 'chain'))

    def test_plan_budget_same_commit_failures_are_separate_attempts_and_trip_breaker(self):
        wt, _ = self.started()
        self.budget_fixture(points=1)
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        for _ in range(2):
            self.raw_task('lane', 'proj', '5', '--plan', 'chain',
                          env=dict(self.env, LANE_STUB_EXIT='1'))
            self.assertEqual(self.wait_for_lane(wt)['exit_code'], 1)
        rows = self.run_rows()
        self.assertEqual([r['outcome'] for r in rows], ['failed', 'failed'])
        self.assertEqual(len({r['id'] for r in rows}), 2)
        self.assertEqual(len({r['commit'] for r in rows}), 1)
        self.assertIn('codex used 1 left 0', self.raw_task('plan-budget', 'chain'))
        self.assertIn('lane disabled', self.raw_task('lane', 'proj', '5', '--plan', 'chain', code=1))
        self.assertEqual(self.run_rows(), rows)
        self.assertEqual(list((self.home / '.local/state/task/pending-runs').glob('*.json')), [])

    def test_plan_budget_cancelled_attempts_keep_outcomes_and_trip_breaker(self):
        wt, _ = self.started()
        self.budget_fixture(points=1)
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        release = self.home / 'release-cancelled-model'
        self.addCleanup(release.touch)
        for _ in range(2):
            (wt / '.work/model-call.json').unlink(missing_ok=True)
            self.raw_task('lane', 'proj', '5', '--plan', 'chain', env=dict(self.env, LANE_STUB_RELEASE=str(release)))
            self.wait_for_lane(wt, finished=False)
            run = json.loads((wt / '.work/lane.json').read_text())['usage_run']
            reservation = self.home / '.local/state/task/pending-runs' / (run['id'] + '.json')
            self.assertTrue(json.loads(reservation.read_text())['invoked'])
            self.raw_task('cancel', 'proj', '5')
            self.wait_project_lock_free()
        rows = self.run_rows()
        self.assertEqual([r['outcome'] for r in rows], ['interrupted', 'interrupted'])
        self.assertEqual(len({r['id'] for r in rows}), 2)
        self.assertIn('codex used 1 left 0', self.raw_task('plan-budget', 'chain'))
        self.assertIn('lane disabled', self.raw_task('lane', 'proj', '5', '--plan', 'chain', code=1))
        self.assertEqual(self.run_rows(), rows)

    def test_plan_budget_vendor_change_requires_and_charges_executing_vendor(self):
        wt, _ = self.started()
        plan = self.budget_fixture(points=1)
        plan.write_text(plan.read_text().replace('codex = 1', 'codex = 0'))
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        self.raw_task('lane', 'proj', '5', '--builder', 'claude', '--plan', 'chain')
        self.wait_for_lane(wt)
        self.assertIn('codex points exhausted', self.raw_task('lane', 'proj', '5', '--builder', 'codex', '--plan', 'chain', code=1))
        self.assertEqual(len(self.run_rows()), 1)
        self.budget_fixture(points=1)
        for _ in range(2):
            self.raw_task('lane', 'proj', '5', '--builder', 'codex', '--plan', 'chain')
            self.wait_for_lane(wt)
        self.assertEqual([r['vendor'] for r in self.run_rows()], ['claude', 'codex', 'codex'])
        self.assertIn('claude used 1 left 0; codex used 1 left 0', self.raw_task('plan-budget', 'chain'))

    def test_plan_budget_fallback_refused_for_zero_points_keeps_primary_charge(self):
        wt, _ = self.started()
        plan = self.budget_fixture(points=1)
        plan.write_text(plan.read_text().replace('codex = 1', 'codex = 0'))
        self.stub_reviewers()
        self.raw_task('check', 'proj', '5')
        self.assertIn('codex points exhausted', self.raw_task('review', 'proj', '5', '--run', '--plan', 'chain', code=1,
                      env=dict(self.env, REVIEW_STUB_FIRST_REPLY="You've hit your weekly limit", REVIEW_STUB_FIRST_EXIT='2')))
        self.assertEqual((wt / '.work/reviewer-count').read_text(), '1')
        self.assertEqual([(r['vendor'], r['outcome']) for r in self.run_rows()], [('claude', 'failed')])
        self.assertIn('claude used 1 left 0; codex used 0 left 0', self.raw_task('plan-budget', 'chain'))

    def test_plan_budget_start_binding_inherits_and_rejects_cli_and_environment_conflicts(self):
        self.budget_fixture(points=10)
        output = self.raw_task('start', 'proj', '5', '--builder', 'codex', '--plan', 'chain')
        wt = self.repo / '.worktrees/t5'
        self.assertIn('task lane proj T-5`', output)
        record = json.loads((self.state() / 'task.json').read_text())
        self.assertEqual(record['plan'], 'chain')
        self.assertEqual(record['revision'], TASK_MODULE.record_revision(record))
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        self.raw_task('lane', 'proj', '5')  # another shell, with no TASK_PLAN
        self.wait_for_lane(wt)
        self.raw_task('check', 'proj', '5')
        self.stub_reviewers()
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n'
        self.raw_task('review', 'proj', '5', '--run', env=dict(self.env, REVIEW_STUB_REPLY=reply))
        runner = self.runner_fixture()
        runner.write_text('1. 5: builder codex, model gpt-6-luna\n')
        self.raw_task('runner-start', 'proj', str(runner))
        self.wait_project_lock_free()
        self.assertEqual(json.loads((self.home / 'unit-calls.jsonl').read_text().splitlines()[-1])['env']['TASK_PLAN'], 'chain')
        self.assertEqual([r['plan'] for r in self.run_rows()], ['chain'] * 3)
        rows = self.run_rows()
        commands = [('start', 'proj', '5', '--builder', 'codex'), ('lane', 'proj', '5'),
                    ('review', 'proj', '5', '--run'), ('runner-start', 'proj', str(runner))]
        for command in commands:
            for extra, env in ((('--plan', 'other'), self.env), ((), dict(self.env, TASK_PLAN='other')),
                               (('--plan', 'chain'), dict(self.env, TASK_PLAN='other'))):
                with self.subTest(command=command, extra=extra):
                    self.assertIn('Conflicting plan', self.raw_task(*command, *extra, env=env, code=1))
                    self.assertEqual(self.run_rows(), rows)
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--refresh')
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['plan'], 'chain')

    def test_plan_budget_fallback_refused_at_spawn_preserves_primary_charge(self):
        wt, _ = self.started()
        self.stub_reviewers()
        self.raw_task('check', 'proj', '5')
        probe = self.home / 'refused-fallback.py'
        for rule in ('expired', 'outside allowed', 'review disabled'):
            with self.subTest(rule=rule):
                plan = self.budget_fixture(points=10)
                log = self.home / '.local/state/task/runs.jsonl'
                log.unlink(missing_ok=True)
                for pending in log.parent.glob('pending-runs/*.json'): pending.unlink()
                for name in ('reviewer-count', 'reviewer-call.json'):
                    (wt / '.work' / name).unlink(missing_ok=True)
                mutation = {
                    'expired': 'plan.write_text(plan.read_text().replace(\'expires = "\', \'expires = "2000-01-01T00:00:00Z" # \'))',
                    'outside allowed': 'plan.write_text(plan.read_text().replace(\'["*"]\', \'["other"]\'))',
                    'review disabled': "\n".join([
                        "for number in range(2):",
                        "    row = api['task_usage'].run_log(api['HOME'], plan='chain', repo='proj', task='seed-' + str(number), kind='review', vendor='codex', commit=str(number))",
                        "    api['task_usage'].run_log(api['HOME'], row, 'failed')",
                    ]),
                }[rule]
                probe.write_text(textwrap.dedent(f'''\
                    import runpy, sys
                    from pathlib import Path
                    from unittest import mock
                    api = runpy.run_path({str(TOOL)!r})
                    original = api['atomic_json']
                    plan = Path({str(plan)!r})
                    changed = False
                    def delayed(path, data):
                        global changed
                        original(path, data)
                        if path.name == 'review-run.json' and data.get('usage_run', {{}}).get('vendor') == 'codex' and not changed:
                            changed = True
                ''') + textwrap.indent(mutation, '        ') + '\n' +
                    "with mock.patch.dict(api['review'].__globals__, atomic_json=delayed):\n" +
                    "    sys.exit(api['main'](['review', 'proj', '5', '--run', '--plan', 'chain']))\n")
                result = subprocess.run([sys.executable, str(probe)], cwd=self.home,
                                        env=dict(self.env, REVIEW_STUB_FIRST_REPLY="You've hit your weekly limit",
                                                 REVIEW_STUB_FIRST_EXIT='2'), capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(rule, result.stderr)
                self.assertEqual((wt / '.work/reviewer-count').read_text(), '1', 'fallback never ran')
                rows = self.run_rows()
                self.assertEqual((rows[0]['vendor'], rows[0]['outcome']), ('claude', 'failed'))
                reservations = list(log.parent.glob('pending-runs/*.json'))
                self.assertEqual(len(reservations), 1)
                fallback = json.loads(reservations[0].read_text())
                self.assertEqual(fallback['replaces'], rows[0]['id'])
                self.assertFalse(fallback.get('invoked', False))
                self.assertIn('claude used 1 left 9', self.raw_task('plan-budget', 'chain'))
                codex_used = 3 if rule == 'review disabled' else 1  # two concurrent seed keys
                self.assertIn(f'codex used {codex_used} left {10 - codex_used}', self.raw_task('plan-budget', 'chain'))
                if rule != 'review disabled':
                    self.budget_fixture(points=10)  # restore expiry/scope in this fixture
                self.raw_task('plan-budget', 'chain', '--clear', 'review')
                (wt / '.work/reviewer-count').unlink()
                head = self.git(wt, 'rev-parse', 'HEAD')
                reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n'
                self.raw_task('review', 'proj', '5', '--run', '--plan', 'chain',
                              env=dict(self.env, REVIEW_STUB_FIRST_REPLY="You've hit your weekly limit",
                                       REVIEW_STUB_FIRST_EXIT='2', REVIEW_STUB_REPLY=reply))
                attempts = [r for r in self.run_rows() if r.get('task') == 'T-5']
                self.assertEqual([(r['vendor'], r['outcome']) for r in attempts],
                                 [('claude', 'failed'), ('claude', 'failed'), ('codex', 'finished')])
                self.assertEqual(attempts[-1]['id'], fallback['id'], 'reuse the refused reservation')
                self.assertNotEqual(attempts[0]['id'], attempts[1]['id'])
                self.assertEqual(set(attempts[-1]['superseded']), {r['id'] for r in attempts[:-1]})
                self.assertIn('claude used 0 left 10', self.raw_task('plan-budget', 'chain'))
                self.assertIn(f'codex used {codex_used} left {10 - codex_used}', self.raw_task('plan-budget', 'chain'))
                self.assertEqual(list(log.parent.glob('pending-runs/*.json')), [])

    def test_plan_budget_lane_rechecks_expiry_scope_and_breaker_at_model_spawn(self):
        wt, _ = self.started()
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        worker = self.home / 'delayed-worker.py'
        launcher = self.home / 'delayed-launch.py'
        launcher.write_text(textwrap.dedent(f'''\
            import runpy, subprocess, sys
            from unittest import mock
            api = runpy.run_path({str(TOOL)!r})
            original = subprocess.Popen
            def spawn(argv, *args, **kwargs):
                if '--_worker' in argv:
                    entry = argv.index('--') + 1
                    argv = [*argv[:entry], sys.executable, {str(worker)!r}, *argv[entry + 2:]]
                return original(argv, *args, **kwargs)
            with mock.patch.object(subprocess, 'Popen', side_effect=spawn):
                sys.exit(api['main'](['lane', 'proj', '5', '--plan', 'chain', '--no-pace', '--no-boost']))
        '''))
        for rule in ('expired', 'outside allowed', 'lane disabled'):
            with self.subTest(rule=rule):
                plan = self.budget_fixture(points=10)
                log = self.home / '.local/state/task/runs.jsonl'
                log.unlink(missing_ok=True)
                for pending in log.parent.glob('pending-runs/*.json'): pending.unlink()
                mutation = {
                    'expired': 'plan.write_text(plan.read_text().replace(\'expires = "\', \'expires = "2000-01-01T00:00:00Z" # \'))',
                    'outside allowed': 'plan.write_text(plan.read_text().replace(\'["*"]\', \'["other"]\'))',
                    'lane disabled': "\n".join([
                        "for number in range(2):",
                        "    run = api['budget_run'](repo='proj', task='seed-' + str(number), kind='lane', vendor='codex')",
                        "    api['task_usage'].run_log(api['HOME'], run, 'failed')",
                    ]),
                }[rule]
                worker.write_text(textwrap.dedent(f'''\
                    import runpy, sys
                    from pathlib import Path
                    from unittest import mock
                    api = runpy.run_path({str(TOOL)!r})
                    original = api['lane_environment']
                    plan = Path({str(plan)!r})
                    def delayed(*args):
                        environment = original(*args)
                ''') + textwrap.indent(mutation, '    ') + '\n    return environment\n' +
                    "with mock.patch.dict(api['lane'].__globals__, lane_environment=delayed):\n    sys.exit(api['main'](sys.argv[1:]))\n")
                result = subprocess.run([sys.executable, str(launcher)], env=self.env, cwd=self.home, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                run = self.wait_for_lane(wt)
                self.wait_project_lock_free()
                self.assertIn(rule, (wt / '.work/lane.log').read_text())
                self.assertFalse((wt / '.work/model-call.json').exists())
                self.assertFalse(run['usage_run'].get('invoked', False))
                self.assertEqual(len(self.run_rows()), 2 if rule == 'lane disabled' else 0)
                pending = list(log.parent.glob('pending-runs/*.json'))
                self.assertEqual(len(pending), 1, 'refusal preserves the existing reservation without a new attempt')
                self.assertFalse(json.loads(pending[0].read_text()).get('invoked', False))
                self.assertEqual(list((self.home / '.local/state/task/usage/running').glob('*/*.json')), [])

    def test_plan_budget_review_rechecks_reservation_before_spawn(self):
        wt, _ = self.started()
        plan = self.budget_fixture(points=1)
        self.stub_reviewers()
        prompt = self.state() / 'review-prompt.md'
        prompt.write_text('review')
        reply = self.state() / 'review-response.md'
        with self.budget_environment() as api:
            original = api.atomic_json
            def delayed(path, data):
                original(path, data)
                if path.name == 'review-run.json':
                    plan.write_text(plan.read_text().replace('expires = "', 'expires = "2000-01-01T00:00:00Z" # '))
            with mock.patch.object(api, 'atomic_json', side_effect=delayed):
                with self.assertRaisesRegex(api.Stop, 'expired'):
                    api.run_reviewer('claude -p --model haiku', wt, prompt, reply)
        self.assertEqual(list((self.home / '.local/state/task/usage/running').glob('*/*.json')), [])
        self.assertFalse((wt / '.work/reviewer-call.json').exists())
        self.assertEqual(self.run_rows(), [])
        pending = list((self.home / '.local/state/task/pending-runs').glob('*.json'))
        self.assertEqual(len(pending), 1)
        self.assertFalse(json.loads(pending[0].read_text()).get('invoked', False))

    def test_plan_budget_concurrent_reviews_cannot_share_files_or_reservation(self):
        wt, _ = self.started()
        self.budget_fixture(points=1)
        self.stub_reviewers()
        self.raw_task('check', 'proj', '5')
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n'
        first = subprocess.Popen([sys.executable, str(TOOL), 'review', 'proj', '5', '--run', '--plan', 'chain'],
                                 env=dict(self.env, REVIEW_STUB_REPLY=reply, REVIEW_STUB_DELAY='1'),
                                 cwd=self.home, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_runner_file(wt / '.work/reviewer-call.json')
            before = {p.name: p.read_bytes() for p in self.state().glob('review*') if p.is_file()}
            self.assertIn('Review already running', self.raw_task('review', 'proj', '5', '--run', '--plan', 'chain', code=1))
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.state().glob('review*') if p.is_file()})
            output, errors = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, output + errors)
        finally:
            if first.poll() is None: first.kill(); first.communicate()
        self.assertEqual((wt / '.work/reviewer-count').read_text(), '1')
        self.assertEqual(len(self.run_rows()), 1)

    def test_plan_budget_review_child_keeps_execution_lock_after_parent_is_killed(self):
        wt, _ = self.started()
        self.budget_fixture(points=10)
        self.stub_reviewers()
        self.raw_task('check', 'proj', '5')
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n'
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                for name in ('reviewer-count', 'reviewer-call.json'):
                    (wt / '.work' / name).unlink(missing_ok=True)
                env = dict(self.env, REVIEW_STUB_REPLY=reply, REVIEW_STUB_DELAY='30')
                if fallback:
                    env.update(REVIEW_STUB_FIRST_REPLY="You've hit your weekly limit",
                               REVIEW_STUB_FIRST_EXIT='2', REVIEW_STUB_FIRST_DELAY='0')
                parent = subprocess.Popen([sys.executable, str(TOOL), 'review', 'proj', '5', '--run', '--plan', 'chain'],
                                          env=env, cwd=self.home, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                child = None
                try:
                    counter = wt / '.work/reviewer-count'
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if counter.exists() and counter.read_text() == ('2' if fallback else '1'): break
                        if parent.poll() is not None:
                            output, errors = parent.communicate()
                            self.fail(output + errors)
                        time.sleep(.02)
                    else:
                        self.fail('reviewer did not start')
                    child = json.loads((wt / '.work/reviewer-call.json').read_text())['pid']
                    parent.kill()
                    parent.communicate(timeout=10)
                    os.kill(child, 0)
                    before = {p.name: p.read_bytes() for p in self.state().glob('review*') if p.is_file()}
                    self.assertIn('Review already running', self.raw_task('review', 'proj', '5', '--run', '--plan', 'chain', code=1))
                    self.assertEqual(before, {p.name: p.read_bytes() for p in self.state().glob('review*') if p.is_file()})
                    self.assertEqual(counter.read_text(), '2' if fallback else '1')
                finally:
                    if parent.poll() is None: parent.kill(); parent.communicate()
                    if child is None and (wt / '.work/reviewer-call.json').exists():
                        child = json.loads((wt / '.work/reviewer-call.json').read_text())['pid']
                    if child is not None:
                        try: os.kill(child, signal.SIGKILL)
                        except ProcessLookupError: pass
                    self.wait_project_lock_free()
                with (self.state() / 'review.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(list((self.home / '.local/state/task/usage/running').glob('*/*.json')), [])
                self.raw_task('plan-budget', 'chain', '--clear', 'review')

    def test_plan_budget_file_points_and_lifetime_bounds(self):
        now = datetime.now(timezone.utc)
        class Clock(datetime):
            @staticmethod
            def now(tz): return now
        path = self.budget_fixture(points=200, expires=(now + timedelta(days=31)).isoformat())
        valid = path.read_text()
        state = self.home / '.local/state/task'
        before = {p: p.read_bytes() for p in state.rglob('*') if p.is_file()}
        with self.budget_environment() as api, mock.patch.object(api, 'datetime', Clock):
            self.assertIn('codex used 0 left 200', api.plan_budget('chain'))
            path.write_text(valid + '#' * (16384 - len(valid.encode())))
            self.assertIn('left 200', api.plan_budget('chain'))
            for invalid, message in (
                (path.read_text() + '#', '16 KiB'),
                (valid.replace('codex = 200', 'codex = 201'), 'between 0 and 200'),
                (valid.replace((now + timedelta(days=31)).isoformat(), (now + timedelta(days=31, seconds=1)).isoformat()), '31 days'),
                (f'created = "{(now - timedelta(seconds=1)).isoformat()}"\n' + valid, '31 days'),
                ('created = "bad"\n' + valid, '31 days'),
                (f'created = "{(now + timedelta(seconds=1)).isoformat()}"\n' + valid, 'future'),
                ('created = "9999-01-01T00:00:00Z"\n' + valid.replace((now + timedelta(days=31)).isoformat(),
                                                                               '9999-01-20T00:00:00Z'), 'future'),
            ):
                with self.subTest(message=message):
                    path.write_text(invalid)
                    with self.assertRaisesRegex(api.Stop, message): api.plan_budget('chain')
                    with self.assertRaisesRegex(api.Stop, message):
                        api.budget_run(repo='proj', task='docs', kind='lane', vendor='codex')
            path.write_text(f'created = "{now.isoformat()}"\n' + valid)
            self.assertIn('left 200', api.plan_budget('chain'))
        self.assertEqual(before, {p: p.read_bytes() for p in state.rglob('*') if p.is_file()})

    def test_plan_budget_manual_review_prompt_and_record_never_carry_plan(self):
        wt, _ = self.started()
        self.budget_fixture(points=0)
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = self.home / 'manual-review.md'
        reply.write_text(f'Verdict: PASS\nCommit: {head}\n#1 pass: exists\n#2 pass: checked\n')
        self.raw_task('review', 'proj', '5', '--record', str(reply), '--reviewer', 'claude', 'haiku', '--plan', 'chain')
        self.raw_task('review', 'proj', '5', '--plan', 'chain')
        pending = list((self.home / '.local/state/task/pending-runs').glob('*.json'))
        self.assertEqual(len(pending), 1)
        self.assertIsNone(json.loads(pending[0].read_text())['plan'])
        # Even old stored routing metadata cannot smuggle a plan into a manual record.
        route_path = self.state() / 'review-routing.json'
        route = json.loads(route_path.read_text())
        route['usage_run']['plan'] = 'chain'
        route_path.write_text(json.dumps(route))
        self.raw_task('review', 'proj', '5', '--record', str(reply), '--plan', 'chain')
        self.assertTrue(all(r['plan'] is None for r in self.run_rows()))
        self.assertIn('claude used 0 left 0', self.raw_task('plan-budget', 'chain'))
        self.assertEqual(list(pending[0].parent.glob('*.json')), [])

    def test_plan_budget_no_plan_start_and_lane_match_fixed_traces(self):
        # Fixed main traces keep this regression check effective after merge.
        wt, _ = self.started()
        self.stub_models()
        probe = self.home / 'no-plan-probe.py'
        probe.write_text(textwrap.dedent(f'''\
            import json, os, runpy, subprocess, sys
            from pathlib import Path
            from types import SimpleNamespace
            from unittest import mock
            sys.path.insert(0, {str(TOOL.parent)!r})
            api = runpy.run_path(sys.argv[1])
            namespace = api['main'].__globals__
            events = []
            def traced(name, original):
                def call(*args, **kwargs):
                    events.append(name)
                    if name == 'guards' and events.count(name) == 2 and sys.argv[2] == 'guard':
                        raise api['Stop']('resolved model refused')
                    return original(*args, **kwargs)
                return call
            original_popen = subprocess.Popen
            def spawn(argv, *args, **kwargs):
                if '--_worker' in argv:
                    events.append('spawn')
                    if sys.argv[2] == 'crash': raise RuntimeError('spawn refused')
                    return SimpleNamespace(pid=os.getpid(), unit='task-lane-proj-t5-trace', skipped=False)
                return original_popen(argv, *args, **kwargs)
            replacements = {{name: traced(name, namespace[name]) for name in
                            ('guards', 'tier_guard', 'archive_lane_final', 'check_plan',
                             'seed_worktree', 'prepare_environment', 'refresh_context')}}
            replacements.update(plan_budget=mock.Mock(side_effect=AssertionError('no plan')),
                                budget_run=mock.Mock(side_effect=AssertionError('no plan')))
            try:
                with mock.patch.dict(namespace, replacements), \\
                        mock.patch.dict(namespace, launch_unit=lambda kind, repo, ident, argv, **kwargs: spawn(argv)), \\
                        mock.patch.object(api['task_usage'], 'record_override', traced('override', api['task_usage'].record_override)), \\
                        mock.patch.object(api['task_usage'], 'run_log', traced('run_log', api['task_usage'].run_log)):
                    raise SystemExit(api['main'](sys.argv[3:]))
            finally:
                Path({str(self.home / 'events.json')!r}).write_text(json.dumps(events))
        '''))
        traces = {}
        for source, tool in (('build', TOOL),):
            for mode in ('start', 'success', 'crash', 'guard'):
                if mode == 'start':
                    command = ['start', 'proj', f'no-plan-{source}', '--builder', 'codex']
                else:
                    (wt / '.work/prompt.md').write_text('task')
                    (wt / '.work/final.md').write_text('Previous final\n')
                    (wt / '.work/report.md').write_text('Previous report\nBLOCKED: previous stop\n')
                    (wt / '.work/lane.json').unlink(missing_ok=True)
                    command = ['lane', 'proj', '5', '--tier', 'hard', '--model', 'gpt-6.1-sol',
                               '--override', 'Jacopo 2026-10-23: parity probe', '--no-pace', '--no-boost']
                result = subprocess.run([sys.executable, str(probe), str(tool), mode, *command],
                                        env=self.env, cwd=self.home, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1 if mode in {'crash', 'guard'} else 0, result.stdout + result.stderr)
                traces[source, mode] = json.loads((self.home / 'events.json').read_text())
                pending = list((self.home / '.local/state/task/pending-runs').glob('*.json'))
                self.assertEqual(len(pending), int(mode == 'success'))
                for row in pending:
                    self.assertIsNone(json.loads(row.read_text())['plan'])
                    row.unlink()
                if mode != 'start':
                    # Each probe must start from the same pinned tier.
                    record = json.loads((self.state() / 'task.json').read_text())
                    record['tier'] = 'routine'
                    record['revision'] = TASK_MODULE.record_revision(record)
                    (self.state() / 'task.json').write_text(json.dumps(record))
        expected = {
            'start': ['check_plan', 'seed_worktree', 'prepare_environment', 'refresh_context'],
            'success': ['refresh_context', 'guards', 'tier_guard', 'override', 'guards', 'archive_lane_final', 'spawn', 'run_log'],
            'crash': ['refresh_context', 'guards', 'tier_guard', 'override', 'guards', 'archive_lane_final', 'spawn'],
            'guard': ['refresh_context', 'guards', 'tier_guard', 'override', 'guards'],
        }
        for mode, expected_trace in expected.items():
            self.assertEqual(traces['build', mode], expected_trace, mode)
        success = traces['build', 'success']
        self.assertLess(success.index('override'), success.index('archive_lane_final'))
        self.assertLess(success.index('archive_lane_final'), success.index('spawn'))
        self.assertLess(success.index('spawn'), success.index('run_log'))
        self.assertNotIn('run_log', traces['build', 'start'])

    def test_plan_budget_refused_lane_preserves_override_and_final_records(self):
        wt, _ = self.started()
        self.stub_models()
        work = wt / '.work'
        (work / 'prompt.md').write_text('task')
        (work / 'final.md').write_text('Previous final\n')
        (work / 'report.md').write_text('Previous report\nBLOCKED: previous stop\n')
        before = {p.name: p.read_bytes() for p in work.glob('*.md')}
        for reason in ('points exhausted', 'expired', 'outside allowed', 'lane disabled'):
            with self.subTest(reason=reason):
                self.budget_fixture(points=0 if reason == 'points exhausted' else 5,
                                    allowed={'proj': ['other']} if reason == 'outside allowed' else None,
                                    expires='2000-01-01T00:00:00Z' if reason == 'expired' else None)
                if reason == 'lane disabled':
                    with self.budget_environment() as api:
                        for number in range(2):
                            run = api.budget_run(repo='proj', task='T-5', kind='lane', vendor='codex', commit=str(number))
                            api.task_usage.run_log(self.home, run, 'failed')
                rows = self.run_rows()
                output = self.raw_task('lane', 'proj', '5', '--plan', 'chain', '--tier', 'design',
                                       '--model', 'gpt-6-astra', '--override', 'Jacopo 2026-10-22: design pass', code=1)
                self.assertIn(reason, output)
                self.assertEqual(self.overrides(), [])
                self.assertEqual({p.name: p.read_bytes() for p in work.glob('final*.md')},
                                 {'final.md': before['final.md']})
                self.assertEqual((work / 'report.md').read_bytes(), before['report.md'])
                self.assertEqual(self.run_rows(), rows)
                self.assertEqual(list((self.home / '.local/state/task/pending-runs').glob('*.json')), [])
                self.assertFalse((work / 'lane.json').exists())

    def test_plan_budget_commit_key_ignores_round_environment(self):
        self.budget_fixture(points=1)
        with self.budget_environment() as api, mock.patch.dict(os.environ, TASK_PLAN_ROUND='1'):
            fields = dict(repo='proj', task='docs', kind='lane', vendor='codex', commit='first')
            run = api.budget_run(**fields)
            api.task_usage.run_log(self.home, run, 'finished')
            retry = api.budget_run(**fields)
            self.assertNotEqual(retry['id'], run['id'])
            api.task_usage.run_log(self.home, retry, 'finished')
            with self.assertRaisesRegex(api.Stop, 'points exhausted'):
                api.budget_run(**dict(fields, commit='second'))
            self.assertEqual(len(self.run_rows()), 2)

    def test_plan_budget_runner_launch_and_inheritance(self):
        wt, _ = self.started()
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        self.budget_fixture(points=1, allowed={'proj': ['docs', 'T-5']})
        runner = self.runner_fixture()
        self.raw_task('runner-start', 'proj', str(runner), '--plan', 'chain')
        self.wait_project_lock_free()
        environment = json.loads((self.home / 'unit-calls.jsonl').read_text().splitlines()[-1])['env']
        self.assertEqual(environment['TASK_PLAN'], 'chain')
        self.assertEqual(self.run_rows(), [])
        self.assertEqual(list((self.home / '.local/state/task/pending-runs').glob('*.json')), [])
        self.assertIn('codex used 0 left 1', self.raw_task('plan-budget', 'chain'))
        # The deterministic runner passes the plan to its actual model lane.
        (self.home / 'bin/task-batch').write_text(f'#!{sys.executable}\n' +
            'import subprocess, sys\n' +
            f'subprocess.run([sys.executable, {str(TOOL)!r}, "lane", "proj", "5", "--no-pace", "--no-boost"], check=True)\n')
        self.raw_task('runner-start', 'proj', str(runner), '--plan', 'chain')
        self.wait_project_lock_free()
        self.wait_for_lane(wt)
        rows = self.run_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['kind'], rows[0]['vendor'], rows[0]['outcome'], rows[0]['plan']),
                         ('lane', 'codex', 'finished', 'chain'))
        self.assertIn('points exhausted', self.raw_task('runner-start', 'proj', str(runner), '--plan', 'chain', code=1))
        runner.write_text('1. unapproved: builder codex, model gpt-6-luna\n')
        self.assertIn('outside allowed', self.raw_task('runner-start', 'proj', str(runner), '--plan', 'chain', code=1))

    def test_plan_budget_runner_checks_expiry_and_disabled_kind_without_drawing(self):
        runner = self.runner_fixture()
        self.budget_fixture(expires='2000-01-01T00:00:00Z')
        self.assertIn('expired', self.raw_task('runner-start', 'proj', str(runner), '--plan', 'chain', code=1))
        self.budget_fixture(points=5)
        with self.budget_environment() as api:
            for number in range(2):
                run = api.budget_run(repo='proj', task=f'old-batch-{number}', kind='runner-start', vendor='codex')
                api.task_usage.run_log(self.home, run, 'failed')
        rows = self.run_rows()
        self.assertIn('runner-start disabled', self.raw_task('runner-start', 'proj', str(runner), '--plan', 'chain', code=1))
        self.assertEqual(self.run_rows(), rows)
        self.assertEqual(list((self.home / '.local/state/task/pending-runs').glob('*.json')), [])
        self.assertFalse((self.home / 'service-args').exists())

    def test_plan_budget_atomic_pending_reservations_across_repositories(self):
        self.budget_fixture(points=1, allowed={'proj': ['*'], 'other': ['*']})
        with self.budget_environment() as api:
            results, errors = [], []
            def draw(repo):
                try:
                    results.append(api.budget_run(repo=repo, task='docs', kind='lane', vendor='codex'))
                except api.Stop as error:
                    errors.append(str(error))
            threads = [threading.Thread(target=draw, args=(repo,)) for repo in ('proj', 'other')]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIn('points exhausted', errors[0])
            self.assertIn('codex used 1 left 0', api.plan_budget('chain'))

    def test_plan_budget_real_lane_crash_before_spawn_reuses_reservation(self):
        wt, _ = self.started()
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        self.budget_fixture(points=1)
        crash = self.home / 'crash-before-spawn.py'
        crash.write_text(textwrap.dedent(f'''\
            import runpy, subprocess
            from unittest import mock
            api = runpy.run_path({str(TOOL)!r})
            original_popen = subprocess.Popen
            def crash_worker(argv, *positional, **keywords):
                if '--_worker' in argv: raise RuntimeError('crash before launch')
                return original_popen(argv, *positional, **keywords)
            with mock.patch.object(subprocess, 'Popen', side_effect=crash_worker):
                api['main'](['lane', {str(self.repo)!r}, '5', '--plan', 'chain', '--no-pace', '--no-boost'])
        '''))
        # Let process exit release the launcher's lock, as a real crash would.
        result = subprocess.run([sys.executable, str(crash)], env=self.env, cwd=self.home,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('RuntimeError: crash before launch', result.stderr)
        self.assertIn('free', self.raw_task('lock', 'proj'))
        self.assertEqual(self.run_rows(), [])
        pending = list((self.home / '.local/state/task/pending-runs').glob('*.json'))
        self.assertEqual(len(pending), 1)
        reserved = json.loads(pending[0].read_text())
        self.raw_task('lane', 'proj', '5', '--plan', 'chain')
        self.wait_for_lane(wt)
        self.assertEqual([r['id'] for r in self.run_rows()], [reserved['id']])
        self.assertEqual(list(pending[0].parent.glob('*.json')), [])

    def test_run_log_lane_review_merge_and_retry(self):
        self.budget_fixture('plan-42', points=10)
        wt, _ = self.started()
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        self.raw_task('lane', 'proj', '5', env=dict(self.env, TASK_PLAN='plan-42'))
        lane = self.wait_for_lane(wt)
        rows = self.run_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['kind'], row['task'], row['repo'], row['round']), ('lane', 'T-5', 'proj', 1))
        self.assertEqual((row['vendor'], row['model'], row['effort'], row['tier']), ('codex', lane['model'], 'high', 'routine'))
        self.assertEqual((row['plan'], row['outcome'], row['before'], row['ccusage_tokens']), ('plan-42', 'finished', None, None))
        self.assertIsNotNone(row['start'])
        self.assertIsNotNone(row['end'])
        self.assertEqual(row['usage_delta'], {'weekly': None, 'five_hour': None})
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.task_usage.run_log(self.home, lane['usage_run'], 'finished')
        self.assertEqual(len(self.run_rows()), 1)
        self.commit_in(wt, 'b.txt')
        self.raw_task('check', 'proj', '5')
        self.stub_reviewers()
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: file exists\n#2 pass: check passed\n'
        self.raw_task('review', 'proj', '5', '--run', env=dict(self.env, REVIEW_STUB_REPLY=reply, TASK_PLAN='plan-42'))
        rows = self.run_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]['kind'], rows[1]['vendor'], rows[1]['task'], rows[1]['outcome']), ('review', 'claude', 'T-5', 'finished'))
        self.task('merge', 'proj', '5', '--keep')
        rows = self.run_rows()
        merged = [r for r in rows if r['outcome'] == 'merged']
        self.assertEqual({r['run_id'] for r in merged}, {r['id'] for r in rows if r['outcome'] == 'finished'})
        self.assertEqual(len({r['id'] for r in rows}), len(rows))
        self.assertTrue(all(r['type'] == 'outcome' for r in merged))
        with mock.patch.dict(os.environ, self.env, clear=True):
            TASK_MODULE.task_usage.run_outcome(self.home, 'proj', 'T-5', 'merged')
        self.assertEqual(len(self.run_rows()), 4)
        self.assertIn("'merged': 1", self.raw_task('status', 'proj'))

    def test_run_log_crash_is_pending_and_status_is_read_only(self):
        wt, _ = self.started()
        self.stub_models()
        (wt / '.work/prompt.md').write_text('task')
        self.raw_task('lane', 'proj', '5', env=dict(self.env, LANE_STUB_RELEASE=str(self.home / 'release')))
        self.wait_for_lane(wt, finished=False)
        lane = json.loads((wt / '.work/lane.json').read_text())
        self.assertIsNone(lane['usage_run']['plan'])
        os.killpg(lane['pid'], signal.SIGKILL)
        self.assertEqual(self.run_rows(), [])
        pending = self.home / '.local/state/task/pending-runs' / (lane['usage_run']['id'] + '.json')
        before = pending.read_bytes()
        self.assertIn("'unfinished': 1", self.raw_task('status', 'proj'))
        self.assertEqual(pending.read_bytes(), before)
        self.assertEqual(self.run_rows(), [])

    def test_run_log_pm_start_end_and_manual_review_retry(self):
        env = dict(self.env, TASK_VENDOR='codex', TASK_MODEL='gpt-6.1-sol', TASK_EFFORT='high', TASK_PLAN='pm-plan')
        self.raw_task('note', 'proj', '-', 'pm-run', 'start', env=env)
        self.raw_task('note', 'proj', '-', 'pm-run', 'start', env=env)
        self.assertIn("'unfinished': 1", self.raw_task('status', 'proj'))
        self.raw_task('note', 'proj', '-', 'pm-run', 'end', env=env)
        self.raw_task('note', 'proj', '-', 'pm-run', 'end', env=env)
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual((self.run_rows()[0]['kind'], self.run_rows()[0]['plan']), ('pm', 'pm-plan'))
        wt, _ = self.started()
        self.raw_task('review', 'proj', '5')
        reply = self.home / 'manual-review.md'
        reply.write_text(f"Verdict: PASS\nCommit: {self.git(wt, 'rev-parse', 'HEAD')}\n#1 pass: yes\n#2 pass: yes\n")
        for _ in range(2):
            self.raw_task('review', 'proj', '5', '--record', str(reply))
        self.assertEqual(len(self.run_rows()), 2)

    def test_run_log_usage_resets_failures_outcomes_and_concurrent_retry(self):
        usage = TASK_MODULE.task_usage
        now = datetime.now(timezone.utc)
        reset = now + __import__('datetime').timedelta(days=3)
        reading = dict(vendor='codex', reason='', weekly=10, five_hour=20, resets_at=reset, five_resets=reset)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(usage, 'readings', return_value={'codex': reading}):
            run = usage.run_log(self.home, repo='proj', task='T-5', kind='lane', vendor='codex', model='gpt-6.1-sol')
            reading.update(weekly=13, five_hour=24)
            errors = []
            def finish():
                try:
                    usage.run_log(self.home, run, 'failed', reason='exit', exit_code=2)
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=finish) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(self.run_rows()), 1)
            self.assertEqual(self.run_rows()[0]['usage_delta'], {'weekly': 3, 'five_hour': 4})
            usage.run_outcome(self.home, 'proj', 'T-5', 'parked: repeated failure')
            usage.run_outcome(self.home, 'proj', 'T-5', 'stopped: time cap')
            self.assertEqual(self.run_rows()[-1]['reason'], 'stopped: time cap')
            self.assertIn('weekly usage delta 3', usage.run_summary(self.home)[1])
            self.assertIn('weekly usage delta 3', usage.weekly_lines(self.home)[1])
            run = usage.run_log(self.home, repo='proj', task='T-5', kind='lane', vendor='codex')
            reading.update(weekly=1, five_hour=1, resets_at=reset + __import__('datetime').timedelta(days=7), five_resets=reset + __import__('datetime').timedelta(hours=5))
            usage.run_log(self.home, run, 'interrupted', reason='cancelled')
            self.assertEqual(self.run_rows()[-1]['usage_delta'], {'weekly': None, 'five_hour': None})

    def test_run_log_runner_park_stop_and_pm_failure(self):
        loader = importlib.machinery.SourceFileLoader('run_log_batch_test', str(TOOL.with_name('executable_task-batch')))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        self.started()
        usage = TASK_MODULE.task_usage
        source = self.home / 'batch.md'
        source.write_text('test batch')
        runner = module.Batch(self.repo, source)
        task = dict(id='5', builder='codex', after=None)
        with mock.patch.dict(os.environ, self.env, clear=True):
            run = usage.run_log(self.home, repo='proj', task='T-5', kind='lane', vendor='codex')
            usage.run_log(self.home, run, 'failed')
            with mock.patch.object(module.Batch, 'task', return_value='parked: repeated failure'), \
                    mock.patch.object(module.Batch, 'capacity', return_value=(1, dict(codex=1, claude=1))):
                runner.schedule([task])
            self.assertEqual(self.run_rows()[-1]['outcome'], 'parked')
            self.assertEqual(self.run_rows()[-1]['run_id'], run['id'])
            with mock.patch.object(module.Batch, 'task', side_effect=module.Stop('time cap')):
                with self.assertRaisesRegex(module.Stop, 'time cap'):
                    runner.run_task(task)
            self.assertEqual(self.run_rows()[-1]['outcome'], 'stopped')
        env = dict(self.env, TASK_VENDOR='claude', TASK_MODEL='sonnet', TASK_EFFORT='high')
        self.raw_task('note', 'proj', '-', 'pm-run', 'start', env=env)
        self.raw_task('note', 'proj', '-', 'pm-run', 'end', 'failed', 'time cap', env=env)
        self.assertEqual((self.run_rows()[-1]['outcome'], self.run_rows()[-1]['reason']), ('failed', 'end failed time cap'))

    def test_run_log_daily_weekly_cutoffs_and_unfinished_stop(self):
        usage = TASK_MODULE.task_usage
        now = datetime.now(timezone.utc)
        rows = [dict(id=str(days), vendor='codex', start=(now - __import__('datetime').timedelta(days=days)).isoformat(),
                     end=(now - __import__('datetime').timedelta(days=days)).isoformat(), outcome='finished',
                     usage_delta={'weekly': 2}) for days in (0, 2, 8)]
        log = self.home / '.local/state/task/runs.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('\n'.join(map(json.dumps, rows)) + '\n')
        self.assertIn('runs 1, weekly usage delta 2', usage.run_summary(self.home, now=now)[1])
        self.assertIn('runs 2, weekly usage delta 4', usage.weekly_lines(self.home, now)[1])
        with mock.patch.dict(os.environ, self.env, clear=True):
            run = usage.run_log(self.home, repo='proj', task='crashed', kind='lane', vendor='codex')
            usage.run_outcome(self.home, 'proj', 'crashed', 'stopped: time cap')
        summary = self.raw_task('status', 'proj')
        self.assertIn("'unfinished': 1", summary)
        self.assertIn("'stopped': 1", summary)
        self.assertFalse(any(r['id'] == run['id'] and r['outcome'] == 'finished' for r in self.run_rows()))

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
        self.assertIn('cannot change this task', self.lane_refused('--tier', 'hard'))
        out = self.task('lane', 'proj', '5', '--tier', 'hard', '--override', 'Jacopo 2026-10-21: tier')
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
        self.assertIn("## Check", (wt / '.work/report.md').read_text())
        direct = self.raw_task('start', 'proj', 'direct', '--builder', 'codex')
        self.assertIn('Acceptance criteria:\n(PM: none given)\nVerify:\n(PM: none given)', direct)
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
        self.assertIn('lane-contract.md', prompt)
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
                self.assertRegex(output, r'z-good:\n  project lock: [^\n]+\n  env keys: PRODUCT_DIR')
                self.assertNotIn('do-not-display', output)
                if config == b'\xff':
                    self.assertIn(f'Cannot read {self.repo / ".task.toml"}', output)
        config = self.repo / '.task.toml'
        config.unlink()
        config.mkdir()  # A real OSError from the newly added configuration read.
        output = self.raw_task('status')
        self.assertIn(f'env keys: unavailable (Cannot read {config}.)', output)
        self.assertRegex(output, r'z-good:\n  project lock: [^\n]+\n  env keys: PRODUCT_DIR')

    def test_configured_environment_rejects_reserved_names(self):
        names = TASK_MODULE.GIT_OVERRIDE_ENV | {'GIT_OVERRIDE_ENV', 'GIT_CONFIG', 'GIT_CONFIG_COUNT',
                                              'TASK_RUN_ID', 'TASK_RUN_ANY', 'TMPDIR', 'TASK_GATE_SLOT',
                                              'TASK_GATE_ANY'}
        for name in names:
            with self.subTest(name=name):
                (self.repo / '.task.toml').write_text(f'[env]\n{name} = "never-print-this"\n')
                with self.assertRaisesRegex(TASK_MODULE.Stop, 'reserved'):
                    TASK_MODULE.configured_environment(self.repo)

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
        for name in ('check.json', 'gate-check.json'):
            with mock.patch.dict(os.environ, self.env), mock.patch.object(TASK_MODULE, 'HOME', self.home):
                self.assertFalse(TASK_MODULE.run_check(wt, '', state=self.state(), evidence_name=name))
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
        self.commit_in(self.kb, name, original, when=PAST)
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
        # A status word of another length: git then sees the edit by size. With "replaced" (same
        # length as "accepted") only timestamps differ, and after a backward step of the host
        # clock `git add` can find the file unchanged and stage nothing.
        (self.kb / name).write_text(original.replace('accepted', 'superseded'))
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
        self.commit_in(self.kb, status_name, original, when=PAST)
        self.commit_in(self.kb, binary_name, original, when=PAST)
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
        self.commit_in(self.kb, name, old, when=PAST)
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
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
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
        self.risk_policy_fixture('[review.normal]\nmax_rounds = 20\n')
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
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
                # Astra and fable follow the recorded tier, so the task is recorded at each tier first.
                self.task('start', 'proj', '5', '--builder', builder, '--tier', tier, '--override', 'Jacopo 2026-10-21: tier')
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
            self.task('lane', 'proj', '5', '--builder', builder, '--tier', 'hard', '--no-boost', '--override', 'Jacopo 2026-10-21: tier')
            no_boost_hard = 'gpt-6.1-sol' if builder == 'codex' else explicit
            self.assertEqual(self.wait_for_lane(wt)['model'], no_boost_hard)

    def test_boost_pauses_per_vendor_and_resumes_without_rewriting_state(self):
        from task_routing import active, route, lane_limits, UNBOOSTED
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
        self.task('boost', '--claude-until', '2099-01-01T00:00:00+00:00',
                  '--codex-until', '2099-01-01T00:00:00+00:00')
        path = self.home / '.local/state/task/boost.json'
        original = path.read_bytes()
        for vendor in ('claude', 'codex'):
            other = 'codex' if vendor == 'claude' else 'claude'
            for five, age, allowed in ((49.9, 0, True), (50, 0, False), (62, 0, False),
                                       (10, 46, False), (10, 0, True)):
                with self.subTest(vendor=vendor, five=five, age=age):
                    self.readings(**{vendor: (40, 84)}, five=five, age=timedelta(minutes=age))
                    if age:
                        TASK_MODULE.task_usage.claim(self.home, vendor, 'lane', 2 ** 22 + 7)
                    self.assertEqual(vendor in active(self.home), allowed)
                    self.assertEqual(lane_limits(self.home, 3)[vendor], 3 if allowed else 1)
                    self.assertIn(other, active(self.home))
                    if not allowed:
                        for tier in ('routine', 'hard', 'design'):
                            expected = ('sonnet' if vendor == 'claude' else 'gpt-6-luna') if tier == 'routine' else UNBOOSTED[vendor]
                            self.assertEqual(route(self.home, vendor, tier), expected)
                        self.assertIn(f'boost paused: {vendor}', self.task('status', 'proj'))
            if vendor == 'claude':
                state = self.home / '.local/state/task/usage/claude.json'
                data = json.loads(state.read_text())
                data.pop('five_hour')
                state.write_text(json.dumps(data))
            else:
                # A window seen recently but missing from the newest event still fails closed.
                rollout = next((self.home / '.codex/sessions').rglob('rollout-*.jsonl'))
                prior = rollout.read_text()
                self.readings(codex=(40, 84))
                rollout.write_text(prior + rollout.read_text())
            self.assertNotIn(vendor, active(self.home))
            self.assertIn(f'boost paused: {vendor} no current 5-hour reading', self.task('boost'))
            self.readings(**{vendor: (40, 84)}, five=10)
        self.assertEqual(path.read_bytes(), original)

    def test_deeper_review_requires_boost_and_hard_tier_or_explicit_final(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'exercise high review routing')
        self.task('boost', '--claude-until', '2099-01-01T00:00:00+00:00',
                  '--codex-until', '2099-01-01T00:00:00+00:00')
        self.assertIn('--final requires --run', self.task('review', 'proj', '5', '--final', code=1))
        for builder, vendor, normal, deeper in (('codex', 'claude', 'haiku', 'opus'),
                                                ('claude', 'codex', 'gpt-6-luna', 'gpt-6.1-sol')):
            for tier, final, five, age, expected in (
                    ('routine', False, 10, 0, normal), ('design', False, 10, 0, normal),
                    ('hard', False, 10, 0, deeper), ('routine', True, 10, 0, deeper),
                    ('hard', False, 50, 0, normal), ('routine', True, 62, 0, normal),
                    ('hard', True, 10, 46, normal),
                    *([('hard', False, None, 0, deeper), ('routine', True, None, 0, deeper)] if vendor == 'codex' else [])):
                with self.subTest(builder=builder, tier=tier, final=final, five=five, age=age):
                    self.task('start', 'proj', '5', '--refresh', '--builder', builder, '--tier', tier,
                              '--override', 'Jacopo 2026-10-23: test tiers')
                    self.raw_task('check', 'proj', '5')
                    self.readings(**{vendor: (40, 84)}, five=five, age=timedelta(minutes=age))
                    if age:
                        TASK_MODULE.task_usage.claim(self.home, vendor, 'lane', 2 ** 22 + 7)
                    head = self.git(wt, 'rev-parse', 'HEAD')
                    reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n'
                    self.raw_task('review', 'proj', '5', '--run', '--no-pace', *(['--final'] if final else []),
                                  env=dict(self.env, REVIEW_STUB_REPLY=reply))
                    metadata = json.loads((self.state() / 'review.json').read_text())
                    self.assertEqual((metadata['reviewer'], metadata['model']), (vendor, expected))

    def test_codex_boost_without_a_five_hour_window_routes_and_starts_automatically(self):
        from task_routing import active, route, lane_limits
        self.readings(codex=(40, 30))
        self.task('boost', '--codex-until', '2099-01-01T00:00:00+00:00')
        self.assertIn('codex', active(self.home))
        self.assertEqual(route(self.home, 'codex', 'design'), 'gpt-6-astra')
        self.assertEqual(lane_limits(self.home, 3)['codex'], 3)
        self.assertNotIn('boost paused: codex', self.task('usage'))
        self.task('boost', 'off')
        wt, out = self.pace_lane(builder='codex', recorded=('--tier', 'design'))
        self.assertIn('pace: codex under-used near its reset; boost on until', out)
        self.assertEqual(self.wait_for_lane(wt)['model'], 'gpt-6-astra')

    def test_command_reads_usage_once_for_boost_routing_and_refreshes_next_command(self):
        usage = TASK_MODULE.task_usage
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
        self.task('boost', '--claude-until', '2099-01-01T00:00:00+00:00',
                  '--codex-until', '2099-01-01T00:00:00+00:00')
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(usage, 'read_claude', wraps=usage.read_claude) as claude, \
                mock.patch.object(usage, 'read_codex', wraps=usage.read_codex) as codex:
            for five in (10, 62):
                self.readings(claude=(40, 84), codex=(40, 84), five=five)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(TASK_MODULE.main(['status', str(self.repo)]), 0)
                self.assertEqual('boost paused: claude' in output.getvalue(), five == 62)
                self.assertEqual('boost paused: codex' in output.getvalue(), five == 62)
            self.assertEqual((claude.call_count, codex.call_count), (2, 2))

    def test_above_threshold_does_not_start_automatic_boost_and_usage_explains(self):
        self.readings(claude=(40, 30), five=62)
        wt, out = self.pace_lane('--tier', 'hard', recorded=('--tier', 'hard'))
        self.assertIn('boost paused: claude 5-hour 62 % ≥ 50 %', out)
        self.assertEqual(self.wait_for_lane(wt)['model'], 'opus')
        self.assertFalse((self.home / '.local/state/task/boost.json').exists())
        self.readings(claude=(40, 30), five=62)
        self.assertIn('boost paused: claude 5-hour 62 % ≥ 50 %', self.task('usage'))

    def lane_refused(self, *options, env=None):
        out = self.raw_task('lane', 'proj', '5', *options, code=1, env=env)
        self.assertFalse((self.repo / '.worktrees/t5/.work/lane.json').exists())
        return out

    def test_astra_only_for_the_design_tier(self):
        self.stub_models()
        task_file = next((self.kb / 'backlog/tasks').glob('t-5 *'))
        task_file.write_text(task_file.read_text() + '\nTier: design\n')
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        for tier in ('routine', 'hard'):
            out = self.lane_refused('--tier', tier, '--model', 'gpt-6-astra')
            self.assertIn(f'gpt-6-astra is for Tier: design tasks only (this one is {tier})', out)
            self.assertIn('gpt-6-luna', out)
            self.assertIn('gpt-6.1-sol', out)
        self.raw_task('lane', 'proj', '5', '--model', 'gpt-6-astra')
        self.assertEqual(self.wait_for_lane(wt)['model'], 'gpt-6-astra')

    def test_fable_only_for_the_hard_tier(self):
        self.stub_models()
        task_file = next((self.kb / 'backlog/tasks').glob('t-5 *'))
        task_file.write_text(task_file.read_text() + '\nTier: hard\n')
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        for tier in ('routine', 'design'):
            for model in ('fable', 'claude-fable-5-1'):
                out = self.lane_refused('--builder', 'claude', '--tier', tier, '--model', model)
                self.assertIn(f'{model} is for Tier: hard tasks only (this one is {tier})', out)
        self.raw_task('lane', 'proj', '5', '--builder', 'claude', '--tier', 'hard', '--model', 'fable')
        self.assertEqual(self.wait_for_lane(wt)['model'], 'fable')

    def test_no_pace_and_pace_do_not_override_the_top_model_rule(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        self.readings(codex=(70, 84))
        for options, env in (((), None), (('--no-pace',), None), ((), dict(self.env, TASK_NO_PACE='1'))):
            out = self.lane_refused('--model', 'gpt-6-astra', *options, env=env)
            self.assertIn('gpt-6-astra is for Tier: design tasks only', out)
            self.assertNotIn('route:', out)

    def test_a_tier_flag_does_not_raise_eligibility_over_the_recorded_tier(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        self.task('boost', '--codex-until', '2099-01-01T00:00:00+00:00')
        cases = (('codex', 'design', 'gpt-6-astra'), ('claude', 'hard', 'fable'))
        for builder, tier, model in cases:
            out = self.lane_refused('--builder', builder, '--tier', tier, '--model', model)
            self.assertIn(f'{model} is for Tier: {tier} tasks only (this one is routine)', out)
            self.assertIn('--override', out)
        # Tier routing that would pick astra is refused: the --tier cannot change the recorded tier.
        self.assertIn("--tier design cannot change this task's recorded tier (routine)", self.lane_refused('--tier', 'design'))
        self.assertEqual(self.overrides(), [])
        request = 'Jacopo 2026-10-21: design look'
        for builder, tier, model in cases:
            (wt / '.work/final.md').unlink(missing_ok=True)
            out = self.raw_task('lane', 'proj', '5', '--builder', builder, '--tier', tier, '--model', model, '--override', request)
            self.assertEqual(self.wait_for_lane(wt)['model'], model)
            self.assertIn(f'override recorded: {model} on a ', out)
        # The ledger names the tier the task was recorded at; the first override moved it to design.
        self.assertEqual([(e['model'], e['tier'], e['override']) for e in self.overrides()],
                         [('tier change', 'routine to design', request), ('gpt-6-astra', 'routine', request),
                          ('tier change', 'design to hard', request), ('fable', 'design', request)])

    def test_a_tier_flag_never_changes_the_recorded_tier_without_an_override(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        # The review's sequence: a tier change with a cheap model, then astra on the now-design task.
        out = self.lane_refused('--tier', 'design', '--model', 'gpt-6-luna')
        self.assertIn("--tier design cannot change this task's recorded tier (routine)", out)
        self.assertIn('--override', out)
        out = self.lane_refused('--tier', 'design', '--model', 'gpt-6-luna', '--override', 'Jacopo 2026-10-21')
        self.assertIn('override must read', out)
        self.assertIn('gpt-6-astra is for Tier: design tasks only (this one is routine)', self.lane_refused('--model', 'gpt-6-astra'))
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['tier'], 'routine')
        # `task start` on the existing record is the same rule.
        out = self.raw_task('start', 'proj', '5', '--builder', 'codex', '--tier', 'design', code=1)
        self.assertIn("cannot change this task's recorded tier (routine)", out)
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['tier'], 'routine')
        self.assertEqual(self.overrides(), [])
        request = 'Jacopo 2026-10-21: design pass'
        out = self.raw_task('lane', 'proj', '5', '--tier', 'design', '--model', 'gpt-6-luna', '--override', request)
        self.wait_for_lane(wt)
        self.assertIn('Updating task tier to design.', out)
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['tier'], 'design')
        self.assertEqual([(e['model'], e['tier'], e['override']) for e in self.overrides()],
                         [('tier change', 'routine to design', request)])

    def overrides(self):
        path = self.home / '.local/state/task/usage/overrides.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_override_allows_astra_on_a_routine_task_and_is_recorded(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        request = 'Jacopo 2026-10-21: use Astra on this one'
        out = self.raw_task('lane', 'proj', '5', '--model', 'gpt-6-astra', '--override', request)
        self.assertEqual(self.wait_for_lane(wt)['model'], 'gpt-6-astra')
        self.assertIn('override recorded', out)
        # The detached worker re-runs the lane with the override but does not record it a second time.
        (entry,) = self.overrides()
        self.assertEqual((entry['repo'], entry['task'], entry['model'], entry['tier'], entry['override']),
                         ('proj', 'T-5', 'gpt-6-astra', 'routine', request))
        self.assertRegex(entry['time'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d')
        # An override on a model that is within its tier is not an override: nothing recorded.
        (wt / '.work/final.md').unlink(missing_ok=True)
        self.raw_task('lane', 'proj', '5', '--model', 'gpt-6-luna', '--override', request)
        self.wait_for_lane(wt)
        self.assertEqual(len(self.overrides()), 1)

    def test_without_an_override_the_tier_rule_holds_and_nothing_is_recorded(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        for options in ((), ('--override', ''), ('--override', '  ')):
            out = self.lane_refused('--model', 'gpt-6-astra', *options)
            self.assertIn('gpt-6-astra is for Tier: design tasks only', out)
            self.assertIn('--override', out)
        self.assertEqual(self.overrides(), [])

    def test_override_must_name_jacopo_with_a_valid_date_and_a_request(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        bad = ('x', 'Jacopo', 'Jacopo 2026-10-21', 'Jacopo 2026-10-21:', 'Jacopo 2026-10-21:   ', 'Jacopo 2026-13-40: look',
               'Jacopo 2026-02-30: look', 'Bob 2026-10-21: look', 'Jacopo 21-10-2026: look', 'Jacopo: look 2026-10-21')
        for text in bad:
            for model in ('gpt-6-astra', 'gpt-6-luna'):
                with self.subTest(text=text, model=model):
                    out = self.lane_refused('--model', model, '--override', text)
                    self.assertIn('Jacopo YYYY-MM-DD: <request>', out)
        self.assertEqual(self.overrides(), [])
        # Name case does not matter.
        self.raw_task('lane', 'proj', '5', '--model', 'gpt-6-astra', '--override', 'jacopo 2026-10-21: look')
        self.wait_for_lane(wt)
        self.assertEqual([entry['override'] for entry in self.overrides()], ['jacopo 2026-10-21: look'])

    def test_runner_check_requires_the_dated_override_format(self):
        runner = self.home / 'runner.md'
        for text in ('x', 'Jacopo 2026-10-21', 'Jacopo 2026-13-01: look'):
            runner.write_text(f'1. a: builder codex, model gpt-6-astra, override: {text}\n')
            self.assertIn('Jacopo YYYY-MM-DD: <request>', self.raw_task('runner-check', 'proj', str(runner), code=1))
        runner.write_text('1. a: builder codex, model gpt-6-astra, override: Jacopo 2026-10-21: look, with a comma\n')
        self.assertIn('runner file OK', self.raw_task('runner-check', 'proj', str(runner)))

    def test_reviews_refuse_astra_and_fable_even_with_an_override(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        self.raw_task('lane', 'proj', '5', '--model', 'gpt-6-astra', '--override', 'Jacopo 2026-10-21: design look')
        self.wait_for_lane(wt)
        self.stub_reviewers()
        self.raw_task('check', 'proj', '5')
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = self.home / 'review.md'
        reply.write_text(f'Verdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n')
        for vendor, model in (('codex', 'gpt-6-astra'), ('claude', 'fable'), ('claude', 'claude-fable-5-1')):
            out = self.raw_task('review', 'proj', '5', '--record', str(reply), '--reviewer', vendor, model, code=1)
            self.assertIn(f'reviews never use {model}; no override applies', out)
        # `task review` takes no override at all, and the lane override leaves the routed reviewer cheap.
        self.assertIn('unrecognized arguments: --override', self.raw_task(
            'review', 'proj', '5', '--run', '--override', 'Jacopo 2026-10-21', code=2))
        self.raw_task('review', 'proj', '5', '--run', env=dict(self.env, REVIEW_STUB_REPLY=reply.read_text()))
        from task_routing import TOP_ONLY
        route = json.loads((self.state() / 'review-routing.json').read_text())
        self.assertNotIn(route['model'], TOP_ONLY)

    def test_status_and_weekly_line_show_overrides(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('task\n')
        self.assertIn('model overrides, last 7 days: 0', self.raw_task('status', 'proj'))
        self.assertEqual(self.raw_task('usage', '--weekly-line').splitlines()[-1], 'model overrides, last 7 days: 0')
        self.raw_task('lane', 'proj', '5', '--model', 'gpt-6-astra', '--override', 'Jacopo 2026-10-21: design look')
        self.wait_for_lane(wt)
        for out in (self.raw_task('status', 'proj'), self.raw_task('usage', '--weekly-line')):
            self.assertIn('model overrides, last 7 days: 1 (proj/T-5 gpt-6-astra on routine at ', out)
            self.assertIn('Jacopo 2026-10-21: design look', out)

    def test_reviews_never_pick_astra_or_fable(self):
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
        from task_routing import TOP_ONLY
        self.stub_reviewers()
        wt, _ = self.started()
        self.task('boost', '--codex-until', '2099-01-01T00:00:00+00:00', '--claude-until', '2099-01-01T00:00:00+00:00')
        head = self.git(wt, 'rev-parse', 'HEAD')
        reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n'
        for builder in ('codex', 'claude'):
            for tier in ('routine', 'hard', 'design'):
                with self.subTest(builder=builder, tier=tier):
                    self.task('start', 'proj', '5', '--refresh', '--builder', builder, '--tier', tier,
                              '--override', 'Jacopo 2026-10-21: tier')
                    self.raw_task('check', 'proj', '5')
                    self.raw_task('review', 'proj', '5', '--run', env=dict(self.env, REVIEW_STUB_REPLY=reply))
                    route = json.loads((self.state() / 'review-routing.json').read_text())
                    self.assertNotIn(route['model'], TOP_ONLY)
                    call = json.loads((wt / '.work/reviewer-call.json').read_text())
                    self.assertFalse(set(TOP_ONLY) & set(call['args']))
        # The deeper review a pace spend picks is the other cap on review models.
        self.assertFalse(set(TOP_ONLY) & set(TASK_MODULE.task_usage.DEEPER.values()))

    def test_tier_file_flag_state_and_status(self):
        task_file = next((self.kb / 'backlog/tasks').glob('t-5 *'))
        task_file.write_text(task_file.read_text() + '\nTier: hard\n')
        self.task('start', 'proj', '5', '--builder', 'codex')
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['tier'], 'hard')
        self.assertIn('tier=hard', self.task('status', 'proj'))
        self.task('start', 'proj', '5', '--refresh', '--builder', 'codex', '--tier', 'design', '--override', 'Jacopo 2026-10-21: tier')
        self.assertEqual(json.loads((self.state() / 'task.json').read_text())['tier'], 'design')

    def test_tier_defaults_and_override_policy(self):
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
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
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
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

    def assert_knowledge_paths(self, name=None):
        default = self.kb
        if name is not None:
            self.kb = default.with_name(name)
            default.rename(self.kb)
            # A decoy proves callers use the key even when the default exists.
            default.mkdir()
            (default / 'INTENT.md').write_text('Wrong knowledge repository\n')
            self.commit_in(self.repo, '.task.toml', f'knowledge = "{name}"\n')
        plan = self.kb / 'INTENT.md'
        self.assertIn('plan: ok', self.raw_task('plan', 'proj'))
        plan.write_text('---\ncurrent: m-1\nrealign_every: 1\n---\nNamed goal\n')
        self.assertIn('plan not signed', self.task('start', 'proj', '5', code=1))
        self.assertIn('plan: ok', self.raw_task('plan', 'proj', '--sign'))
        self.assertIn('signed_hash:', plan.read_text())
        wt, _ = self.started()
        record = json.loads((self.state() / 'task.json').read_text())
        self.assertEqual(record['title'], 'Add the b file')
        self.assertEqual(record['milestone'], 'm-1')
        self.assertEqual(len(list((wt / '.work/context/backlog/tasks').glob('t-5 *'))), 1)
        self.assertIn('Named goal', (wt / '.work/context/INTENT.md').read_text())
        plan.write_text(plan.read_text() + 'Changed\n')
        self.assertIn('changed since sign-off', self.task('start', 'proj', '5', '--refresh', code=1))
        self.raw_task('note', 'proj', '5', 'improve', 'Named improvement')
        self.assertIn('Named improvement', (self.kb / 'improvements.md').read_text())
        status = self.raw_task('status', 'proj')
        self.assertIn('changed since sign-off', status)
        self.assertIn('open improvements: 1', status)
        runner = self.home / 'runner.md'
        runner.write_text('1. 5: builder codex, model gpt-6-luna\n2. direct: builder codex, model gpt-6-luna\n')
        self.assertIn('stop at direct', self.raw_task('runner-check', 'proj', str(runner)))
        stub = self.home / 'bin/codex'
        stub.write_text('#!/usr/bin/env python3\nimport pathlib,sys\n'
                        'pathlib.Path("prompt-copy").write_text(sys.stdin.read())\n'
                        'print("partial reply")\nsys.exit(1)\n')
        stub.chmod(0o755)
        output = self.raw_task('realign', 'proj', code=1)
        reply = Path(output.strip().split('Reply: ', 1)[1])
        state_name = name if name is not None else 'proj'
        self.assertEqual(reply.parent, self.home / '.local/state/task' / state_name / 'realign')
        self.assertEqual(reply.read_text(), 'partial reply\n')
        self.assertIn('Named goal', (self.kb / 'prompt-copy').read_text())
        self.assertFalse((self.kb / 'history/realign').exists())
        stub.write_text(stub.read_text().replace('sys.exit(1)', 'sys.exit(0)'))
        self.raw_task('realign', 'proj')
        self.assertEqual(len(list((self.kb / 'history/realign').glob('realign-*.md'))), 1)
        if name is not None:
            self.assertEqual(list(default.iterdir()), [default / 'INTENT.md'])

    def test_knowledge_default_paths(self):
        self.assert_knowledge_paths()

    def test_knowledge_named_paths(self):
        self.assert_knowledge_paths('cortex-core')

    def test_knowledge_prefixed_key_keeps_state_name(self):
        self.assert_knowledge_paths('cortex-kb-custom')

    def test_knowledge_rejects_paths_and_symlinks(self):
        config = self.repo / '.task.toml'
        for value in ('../outside', '..', '.', '/tmp/outside', str(self.kb),
                      'nested/repo', '', 1, [], True):
            with self.subTest(value=value):
                config.write_text('knowledge = ' + json.dumps(value) + '\n')
                for command in (('plan', 'proj'), ('plan', 'proj', '--sign'),
                                ('start', 'proj', '5', '--builder', 'codex'), ('realign', 'proj')):
                    self.assertIn('knowledge must be a single repository name under ~/cortex',
                                  self.raw_task(*command, code=1))
        (self.home / 'outside').mkdir()
        for target in (self.kb, self.home / 'outside', self.home / 'missing'):
            with self.subTest(target=target):
                link = self.kb.with_name('linked')
                link.symlink_to(target, target_is_directory=True)
                config.write_text('knowledge = "linked"\n')
                self.assertIn('symlinked target', self.raw_task('plan', 'proj', code=1))
                link.unlink()
        config.unlink()
        self.kb.rename(self.home / 'original-kb')
        self.kb.symlink_to(self.home / 'original-kb', target_is_directory=True)
        self.assertIn('symlinked target', self.raw_task('plan', 'proj', code=1))

    def test_knowledge_missing_repo_keeps_plan_exemption(self):
        for name in (None, 'missing'):
            with self.subTest(name=name):
                if name is None:
                    self.kb.rename(self.home / 'unused-kb')
                else:
                    (self.repo / '.task.toml').write_text(f'knowledge = "{name}"\n')
                with mock.patch.object(TASK_MODULE, 'CORTEX', self.home / 'cortex'):
                    task = TASK_MODULE.Task(self.repo, '5')
                    TASK_MODULE.check_plan(task)
                self.assertIn('No Backlog.md task', self.task('start', 'proj', '5', code=1))
                self.task('start', 'proj', 'direct-' + (name or 'default'))
                self.assertNotIn('plan: stop:', self.raw_task('status', 'proj'))

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
        # A merge before the new sign-off, at a fixed past time (see PAST).
        (state / 'merge.json').write_text(json.dumps({'time': PAST}))
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
        self.assertIn('default_permissions="review"', (self.kb / 'args-copy').read_text())
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
    def runner_fixture(self):
        installed_task = self.home / '.local/bin/task'
        installed_task.parent.mkdir(parents=True, exist_ok=True)
        if not installed_task.exists(): installed_task.symlink_to(TOOL)
        work = self.repo / '.work'
        work.mkdir(exist_ok=True)
        runner = work / 'runner-b1.md'
        runner.write_text('1. docs: builder codex, model gpt-6-luna\n')
        install_units(self.home)
        scripts = {
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
        self.assertFalse((self.home / 'unit-calls.jsonl').exists())
        runner.write_text('1. docs: builder codex, model gpt-6-luna\n')
        hold = runner.parent / 'hold'
        hold.touch()
        try:
            out = self.raw_task('runner-start', 'proj', str(runner))
            data = json.loads(runner.with_suffix('.json').read_text())
            unit = data['unit']
            self.assertRegex(unit, r'^task-runner-proj-b1-[a-f0-9]+$')
            self.assertIn(unit, out)
            args = json.loads((self.home / 'unit-calls.jsonl').read_text().splitlines()[-1])['args']
            for option in ('--user', '--wait', '--pipe', '--service-type=exec'):
                self.assertIn(option, args)
            self.assertEqual(args[args.index('--working-directory') + 1], str(self.repo))
            self.assertEqual(args.count(f'ExecCondition={self.home}/.local/bin/task hold --check'), 1)
            call = json.loads(self.wait_runner_file(runner.parent / 'runner-call'))
            self.assertEqual(call['args'], [str(self.repo), str(runner), '--_unit'])
            self.assertEqual(call['prompt'], '')
            self.assertIn('already active', self.raw_task('runner-start', 'proj', str(runner), code=1))
            self.assertIn(unit + ': active', self.raw_task('status', 'proj'))
            self.raw_task('cancel', 'proj', 'runner-b1')
            self.assertEqual((self.home / 'stopped').read_text(), unit)
            self.assertIn(unit + ': inactive', self.raw_task('status', 'proj'))
        finally:
            hold.unlink()
            self.wait_project_lock_free()
        for final in ('batch: finished', 'batch: stopped: deliberate'):
            for own_final in ('', final):
                with self.subTest(stale=final, own_final=own_final):
                    done = runner.with_suffix('.done')
                    stale = done.read_text() + final + '\n'
                    done.write_text(stale)
                    self.raw_task('runner-start', 'proj', str(runner), env=dict(self.env, RUNNER_FINAL=own_final))
                    self.wait_project_lock_free()
                    current = done.read_text()[len(stale):]
                    self.assertTrue(done.read_text().startswith(stale))
                    self.assertEqual(current, own_final + '\n' if own_final else
                        'batch: stopped: runner exited (code 7) without a final line; see ' + str(runner.with_suffix('.log')) + '\n')
                    self.assertIn('last=' + current.strip(), self.raw_task('status', 'proj'))

    def test_runner_wrapper_uses_one_unit_and_cancel_prevents_next_lane(self):
        runner = self.runner_fixture()
        batch = self.home / 'bin/task-batch'
        batch.write_text(textwrap.dedent('''\
            #!/usr/bin/env python3
            import os, pathlib, runpy, sys, time
            work = pathlib.Path.cwd() / '.work'
            if '--_unit' not in sys.argv:
                helper = runpy.run_path(str(pathlib.Path.home() / '.local/bin/task'))
                process = helper['launch_unit']('runner', pathlib.Path.cwd(), 'nested',
                    [sys.executable, __file__, *sys.argv[1:], '--_unit'],
                    cwd=pathlib.Path.cwd(), env=dict(os.environ))
                sys.exit(process.wait())
            (work / 'batch-ready').touch()
            while not (work / 'next-lane').exists(): time.sleep(.02)
            (work / 'forbidden-lane').touch()
        '''))
        batch.chmod(0o755)
        self.raw_task('runner-start', 'proj', str(runner))
        try:
            self.wait_runner_file(runner.parent / 'batch-ready')
            unit = json.loads(runner.with_suffix('.json').read_text())['unit']
            calls = [json.loads(line) for line in (self.home / 'unit-calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls), 1, calls)
            self.assertEqual(calls[0]['args'][calls[0]['args'].index('--unit') + 1], unit)
            self.raw_task('cancel', 'proj', 'runner-b1')
            self.assertEqual((self.home / 'stopped').read_text(), unit)
            (runner.parent / 'next-lane').touch()
            self.wait_project_lock_free()
            self.assertFalse((runner.parent / 'forbidden-lane').exists())
            self.assertFalse(runner.with_suffix('.unit.json').exists())
        finally:
            self.raw_task('cancel', 'proj', 'runner-b1')

    def test_runner_passes_lock_holder_to_children(self):
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('Build the b file.')
        self.stub_models()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        runner = self.runner_fixture()
        batch = self.home / 'bin/task-batch'
        batch.write_text(textwrap.dedent(f'''\
            #!/usr/bin/env python3
            import json, os, pathlib, subprocess, sys, time
            work = pathlib.Path.cwd() / '.work'
            while not (work / 'children-go').exists(): time.sleep(.02)
            rows = []
            for args in (('lane', 'proj', '5'), ('review', 'proj', '5', '--run'), ('merge', 'proj', '5', '--keep')):
                result = subprocess.run([sys.executable, {str(TOOL)!r}, *args], env=os.environ, capture_output=True, text=True)
                rows.append(dict(args=args, code=result.returncode, output=result.stdout + result.stderr))
            (work / 'runner-children.json').write_text(json.dumps(rows))
            while not (work / 'children-stop').exists(): time.sleep(.02)
        '''))
        batch.chmod(0o755)
        env = dict(self.env, TASK_HOLDER='batch-seat')
        self.raw_task('runner-start', 'proj', str(runner), env=env)
        api = TASK_MODULE.task_lock
        data = api.read(self.home, self.repo)
        data['expires_epoch'] = time.time() - 10
        api.write(self.home, self.repo, data)
        self.assertIn('held by batch-seat', self.raw_task('lock', 'proj', '--holder', 'other', code=1))
        # A valid reviewer and its current-head check are required, not just absence of a lock error.
        reviewer = self.home / 'bin/claude'
        head = self.git(wt, 'rev-parse', 'HEAD')
        reviewer.write_text('#!/bin/sh\nprintf ' + repr('Verdict: PASS\nCommit: ' + head + '\n#1 pass: exists\n#2 pass: checked\nBlocking: none\nNotes: none\n'))
        reviewer.chmod(0o755)
        (self.repo / '.work/children-go').touch()
        try:
            rows = json.loads(self.wait_runner_file(self.repo / '.work/runner-children.json'))
            self.assertEqual([row['code'] for row in rows], [0, 0, 0], rows)
            self.assertEqual(api.read(self.home, self.repo)['token'], data['token'])
            self.assertIn('held by batch-seat', self.raw_task('lock', 'proj', '--holder', 'other', code=1))
            self.assertTrue(self.git(self.repo, 'rev-parse', 'origin/main'))
        finally:
            (self.repo / '.work/children-stop').touch()
            deadline = time.monotonic() + 5
            self.wait_project_lock_free()

    def test_project_lock_systemd_runner_process_ignores_ttl(self):
        runner = self.runner_fixture()
        hold = runner.parent / 'hold'
        hold.touch()
        try:
            self.raw_task('runner-start', 'proj', str(runner))
            api = TASK_MODULE.task_lock
            data = api.read(self.home, self.repo)
            self.assertTrue(data['process'])
            data['expires_epoch'] = 0
            api.write(self.home, self.repo, data)
            self.raw_task('start', 'proj', 'docs', '--builder', 'codex')
            self.assertEqual(api.read(self.home, self.repo)['token'], data['token'])
            self.assertIn('held by test-seat', self.raw_task('lock', 'proj', '--holder', 'other', code=1))
        finally:
            hold.unlink()
            self.wait_project_lock_free()
        self.raw_task('start', 'proj', 'docs', '--builder', 'codex', env=dict(self.env, TASK_HOLDER='other'))

    def test_runner_real_batch_handoff_survives_wrapper_death(self):
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        report = (wt / '.work/report.md').read_text()
        (wt / '.work/report.md').unlink()  # The real batch must run a lane first.
        runner = self.runner_fixture()
        runner.write_text('1. T-5: builder codex, model gpt-6-luna\nTime box: 0.5 min\n')
        for name, target in [('task', TOOL), ('task-batch', TOOL.with_name('executable_task-batch'))]:
            binary = self.home / 'bin' / name
            binary.unlink(missing_ok=True)
            binary.symlink_to(target)
        self.stub_models()
        codex = self.home / 'bin/codex'
        codex.write_text(MODEL_STUB.replace("prompt = sys.stdin.read()",
                         "prompt = sys.stdin.read()\n(work / 'report.md').write_text(" + repr(report) + ")"))
        head = self.git(wt, 'rev-parse', 'HEAD')
        reviewer = self.home / 'bin/claude'
        reviewer.write_text('#!/bin/sh\nprintf ' + repr('Verdict: PASS\nCommit: ' + head +
                            '\n#1 pass: exists\n#2 pass: checked\nBlocking: none\nNotes: none\n'))
        release = self.home / 'release-builder'
        env = dict(self.env, TASK_HOLDER='real-batch-seat', LANE_STUB_RELEASE=str(release))
        self.raw_task('runner-start', 'proj', str(runner), '--no-pace', env=env)
        try:
            self.wait_runner_file(wt / '.work/model-call.json')
            api = TASK_MODULE.task_lock
            data = api.read(self.home, self.repo)
            data['expires_epoch'] = 0
            api.write(self.home, self.repo, data)
            wrapper = json.loads(runner.with_suffix('.json').read_text())['pid']
            os.kill(wrapper, signal.SIGKILL)
            self.assertIn('held by real-batch-seat', self.raw_task('lock', 'proj', '--holder', 'other', code=1))
        finally:
            release.touch()
            self.wait_project_lock_free(timeout=40)
        done = runner.with_suffix('.done').read_text()
        self.assertIn('T-5: merged', done)
        self.assertIn('batch: finished', done)
        self.assertTrue((self.state() / 'merge.json').is_file())
        self.assertEqual(json.loads((self.state() / 'review.json').read_text())['verdict'], 'PASS')
        self.assertEqual(self.git(self.repo, 'merge-base', '--is-ancestor', head, 'origin/main'), '')

    def test_runner_llm_flag_removed(self):
        runner = self.runner_fixture()
        out = self.raw_task('runner-start', 'proj', str(runner), '--llm', code=2)
        self.assertIn('unrecognized arguments: --llm', out)
        self.assertFalse((self.home / 'service-args').exists())

    def test_runner_unit_survives_callers_group_and_cancel(self):
        runner = self.runner_fixture()
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
            unit = json.loads(runner.with_suffix('.json').read_text())['unit']
            self.assertIn(unit + ': active', self.raw_task('status', 'proj'))
            self.raw_task('cancel', 'proj', 'runner-b1')
            self.assertIn('batch: stopped:', self.wait_runner_file(runner.with_suffix('.done')))
        finally:
            hold.unlink(missing_ok=True)
            if caller.poll() is None:
                os.killpg(caller.pid, signal.SIGKILL)
                caller.wait()

    def test_runner_unit_exit_guard_and_logs(self):
        runner = self.runner_fixture()
        self.assertIn('task-runner-proj-b1-', self.raw_task('runner-start', 'proj', str(runner)))
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

    def test_reliability_cancel_unit_stop_failure_preserves_running_record(self):
        wt, _ = self.started()
        lane = wt / '.work/lane.json'
        for state in ('active', 'inactive', 'failed'):
            lane.write_text(json.dumps(dict(state='running', pid=os.getpid(), unit='task-lane-proj-t5-test')))
            with self.subTest(state=state), \
                    mock.patch.object(TASK_MODULE, 'resolve_repo', return_value=self.repo), \
                    mock.patch.object(TASK_MODULE, 'prune_stale_gate_worktrees'), \
                    mock.patch.object(TASK_MODULE, 'Task', return_value=SimpleNamespace(path=wt)), \
                    mock.patch.object(TASK_MODULE.subprocess, 'run', side_effect=[
                        subprocess.CompletedProcess([], 1),
                        subprocess.CompletedProcess([], 0, stdout=f'ActiveState={state}\n', stderr='')]):
                if state == 'active':
                    with self.assertRaisesRegex(TASK_MODULE.Stop, 'Cannot stop .* kept the running record'):
                        TASK_MODULE.cancel(SimpleNamespace(repo='proj', id='5'))
                else:
                    TASK_MODULE.cancel(SimpleNamespace(repo='proj', id='5'))
                self.assertEqual(json.loads(lane.read_text())['state'], 'running' if state == 'active' else 'cancelled')

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
            'risk normal: recorded PASS review for lane head with every criterion pass by the other vendor',
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
        self.assertFalse((self.home / f'.local/state/task/usage/running/codex/{run["pid"]}.json').exists())
        self.assertIsNone(run['exit_code'])
        self.assertIn('lane timed_out', self.raw_task('status', 'proj').split('Needs attention:')[1])
        self.assertTrue((wt / '.work/descendant.pid').exists())
        self.assert_group_stopped(run['pid'])
        self.assert_group_stopped(run['child_pgid'])
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
        self.assertIn("Cancelled lane unit", out)
        self.assertEqual(json.loads((wt / ".work/lane.json").read_text())["state"], "cancelled")
        self.assertFalse((self.home / f'.local/state/task/usage/running/codex/{cancelled["pid"]}.json').exists())
        self.assert_group_stopped(cancelled['pid'])
        self.assert_group_stopped(cancelled['child_pgid'])
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

    def test_status_shows_bwrap_denial_once_as_known_warning(self):
        wt, _ = self.started()
        work = wt / '.work'
        (work / 'lane.json').write_text(json.dumps(dict(builder='codex', state='finished', exit_code=0)))
        for final, shown in (('BLOCKED: `task check` — bwrap NETLINK_ROUTE denied', 1),
                             ('BLOCKED: git commit', 0)):
            (work / 'final.md').write_text('Done.\n' + final + '\n')
            out = self.raw_task('status', 'proj')
            self.assertEqual(out.count('Known warning (setup #11)'), shown)

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
        self.enterContext(mock.patch.object(TASK_MODULE, 'HOME', self.home))
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
        self.assertIn("Mandatory read: .work/context/lane-contract.md", out)
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
            self.addCleanup(sock.close)
            sock.bind(str(wt / ".work/service.sock"))
        except PermissionError:
            self.skipTest('lane sandbox forbids Unix socket creation; PM trusted check must run unsandboxed')
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
                output = io.StringIO()
                stdout = io.StringIO()
                with contextlib.redirect_stderr(output), contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as stop:
                    with TASK_MODULE.exclusive_gate_lock(TASK_MODULE.Task(self.repo, "5")):
                        self.fail("must never enter unlocked")
                self.assertEqual(stop.exception.code, 3)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn(f"gate busy: held by pid {os.getpid()} (holder details unavailable)", output.getvalue())
                self.assertNotIn('proj/T-9', output.getvalue())

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
            (self.gate_dir / 'agent-task-gate.holder').write_text('{"pid": 99999999}')
            process.kill()
            process.wait(timeout=5)
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
        # In-process admission checks use the same temporary home as CLI fixtures.
        self.enterContext(mock.patch.object(TASK_MODULE, 'HOME', self.home))
        def close_project_locks():
            for key in list(TASK_MODULE.task_lock.OWNERS):
                if Path(key).is_relative_to(self.home):
                    TASK_MODULE.task_lock.OWNERS.pop(key)[0].close()
        self.addCleanup(close_project_locks)
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        install_units(self.home)
        (bin_dir / 'task').symlink_to(TOOL)
        installed = self.home / '.local/bin/task'
        installed.parent.mkdir(parents=True)
        installed.symlink_to(TOOL)
        for name, body in STUBS.items():
            (bin_dir / name).write_text(textwrap.dedent(body))
            (bin_dir / name).chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{bin_dir}:{os.environ['PATH']}", **IDENTITY, TASK_HOLDER='test-seat')
        self.env.update(XDG_RUNTIME_DIR=str(self.home / 'runtime'),
                        DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(self.home / 'runtime/bus'))
        self.enterContext(mock.patch.dict(os.environ, TASK_HOLDER='test-seat'))
        self.contract_text = "# Fake lane contract\n"
        self.install_contract(".agents")
        for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_THREAD_ID"):
            self.env.pop(marker, None)
        self.gate_dir = self.home / "shared-gate"
        self.gate_dir.mkdir()
        self.env["TASK_GATE_DIR"] = str(self.gate_dir)
        self.lane_root = self.home / "task-lane"
        self.env["TASK_LANE_TEMP_ROOT"] = str(self.lane_root)
        self.origin =self.home / "origin.git"
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

    def install_contract(self, root, text=None):
        path = self.home / root / "skills/slim-workflow/lane-contract.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.contract_text if text is None else text)

    def test_start_copies_contract_by_source_order_and_lane_refreshes_it(self):
        copy = self.repo / ".worktrees/t5/.work/context/lane-contract.md"
        self.install_contract(".claude", "claude copy\n")
        self.started()
        self.assertEqual(copy.read_text(), self.contract_text, ".agents wins over .claude")
        (self.home / ".agents/skills/slim-workflow/lane-contract.md").unlink()
        self.install_contract(".claude", "claude copy\n")
        with mock.patch.object(TASK_MODULE, "HOME", self.home):
            TASK_MODULE.refresh_context(TASK_MODULE.Task(self.repo, "T-5"))
        self.assertEqual(copy.read_text(), "claude copy\n", "refresh falls back to .claude and replaces the copy")
        self.assertFalse(copy.stat().st_mode & 0o222)

    def test_start_stops_without_an_installed_contract(self):
        for root in (".agents", ".claude"):
            (self.home / root / "skills/slim-workflow/lane-contract.md").unlink(missing_ok=True)
        out = self.task("start", "proj", "T-5", "--builder", "codex", code=1)
        self.assertIn("No lane contract", out)
        self.assertFalse((self.repo / ".worktrees/t5").exists(), "stops before creating the worktree")

    def started(self):
        out = self.task("start", "proj", "T-5", "--builder", "codex")
        wt = self.repo / ".worktrees/t5"
        self.assertTrue(wt.is_dir(), out)
        return wt, out

    def risk_policy_fixture(self, text):
        path = self.home / 'cortex/cortex-kb-company/policy/usage-pacing.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_risk_defaults_legacy_records_changes_and_high_reason(self):
        self.assertIn('--risk-reason', self.raw_task('start', 'proj', '5', '--builder', 'codex', '--risk', 'high', code=1))
        wt, _ = self.started()
        record_path = self.state() / 'task.json'
        record = json.loads(record_path.read_text())
        self.assertEqual(record['risk'], 'normal')
        record.pop('risk'); record.pop('risk_reason')
        record['revision'] = TASK_MODULE.record_revision(record)
        record_path.write_text(json.dumps(record))
        self.assertIn('tier=routine risk=normal', self.raw_task('status', 'proj'))
        self.assertIn('one-line reason', self.raw_task('risk', 'proj', '5', 'high', code=1))
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'rails change')
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--refresh')
        record = json.loads(record_path.read_text())
        self.assertEqual((record['risk'], record['risk_reason']), ('high', 'rails change'))
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--risk', 'low')
        self.assertIn('risk=low', self.raw_task('status', 'proj'))
        ledger = [json.loads(line) for line in (self.home / '.local/state/task/usage/overrides.jsonl').read_text().splitlines()]
        self.assertEqual([(r['model'], r['tier']) for r in ledger], [(None, 'routine')] * 2)
        self.assertEqual([(r['kind'], r['old_risk'], r['risk'], r['override']) for r in ledger],
                         [('risk_change', 'normal', 'high', 'rails change'),
                          ('risk_change', 'high', 'low', 'PM risk choice')])
        self.assertIn('risk normal to high on routine', self.raw_task('status', 'proj'))
        self.assertIn('nonempty line', self.raw_task('risk', 'proj', '5', 'high', '--reason', 'two\nlines', code=1))

    def test_risk_low_runs_trusted_check_and_merges_without_review_or_report(self):
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--risk', 'low')
        wt = self.repo / '.worktrees/t5'
        self.commit_in(wt, 'b.txt')
        for flags in (('--run',), (), ('--record', 'missing.md')):
            out = self.raw_task('review', 'proj', '5', *flags, code=1)
            self.assertIn('risk low: trusted check only', out)
        self.assertIn('risk low', self.raw_task('redteam', 'proj', '5', '--brief', 'missing.md', code=1))
        (wt / '.work/report.md').unlink()
        self.assertIn('Risk: low', self.raw_task('check', 'proj', '5'))
        self.assertFalse((self.state() / 'review.json').exists())
        self.raw_task('merge', 'proj', '5', '--keep')
        self.assertEqual(self.git(self.repo, 'show', 'origin/main:b.txt'), 'x')

    def test_risk_normal_keeps_cheap_review_in_boost_and_final_for_both_builders(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.readings(claude=(40, 84), codex=(40, 84), five=10)
        self.task('boost', '--claude-until', '2099-01-01T00:00:00+00:00', '--codex-until', '2099-01-01T00:00:00+00:00')
        for builder, vendor, model in (('codex', 'claude', 'haiku'), ('claude', 'codex', 'gpt-6-luna')):
            self.raw_task('start', 'proj', '5', '--refresh', '--builder', builder, '--tier', 'hard', '--override', 'Jacopo 2026-10-23: routing fixture')
            self.raw_task('check', 'proj', '5')
            head = self.git(wt, 'rev-parse', 'HEAD')
            reply = f'Verdict: PASS\nCommit: {head}\n#1 pass: checked\n#2 pass: checked\n'
            self.raw_task('review', 'proj', '5', '--run', '--final', '--no-pace', env=dict(self.env, REVIEW_STUB_REPLY=reply))
            review = json.loads((self.state() / 'review.json').read_text())
            self.assertEqual((review['reviewer'], review['model']), (vendor, model))
        self.assertIn('risk normal', self.raw_task('redteam', 'proj', '5', '--brief', 'missing', code=1))

    def test_risk_normal_manual_deeper_review_refused_even_with_stored_route(self):
        wt, _ = self.started()
        self.raw_task('review', 'proj', '5')
        for model in ('opus', 'claude-opus-5-5', 'fable', 'claude-fable-5-1', 'gpt-6.1-sol'):
            self.assertIn('risk normal', self.raw_task('review', 'proj', '5', '--record', 'missing', '--reviewer', 'claude', model, code=1))

    def test_risk_policy_selects_reviewers_and_unparseable_policy_fails_closed(self):
        self.started()
        self.risk_policy_fixture('[review.normal]\ncodex = "sonnet"\n')
        self.assertIn('--model sonnet', self.raw_task('review', 'proj', '5'))
        self.risk_policy_fixture('[review.normal]\ncodex = "opus"\n')
        self.assertIn('risk normal', self.raw_task('review', 'proj', '5', code=1))
        self.risk_policy_fixture('[review.normal]\nunknown = 1\n')  # warned; [review.normal] keeps its defaults
        self.assertIn('invalid review.normal; ignored, other keys kept', self.raw_task('check', 'proj', '5'))
        self.risk_policy_fixture('[review.normal\n')
        self.assertIn('usage-pacing.toml', self.raw_task('check', 'proj', '5', code=1))

    def test_risk_status_invalid_review_reports_problem_and_keeps_the_other_keys(self):
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        for text, problem, risk in (('[review.normal]\nunknown = 1\n', 'invalid review.normal;', 'normal'),
                                    ('[review]\nhigh_paths = { proj = ["b.txt"] }\n[review.low]\nmax_rounds = 0\n',
                                     'invalid review.low.max_rounds;', 'high')):  # the valid high_paths stay in force
            with self.subTest(policy=text):
                self.risk_policy_fixture(text)
                out = self.raw_task('status', 'proj')
                self.assertIn(problem + ' ignored, other keys kept', out)
                self.assertNotIn('built-in defaults used', out)
                self.assertIn(f'risk={risk}', out)
                self.assertIn('branch=t5', out)
                self.assertNotIn('Traceback', out)
                self.assertIn(problem, self.raw_task('review', 'proj', '5'))
        self.risk_policy_fixture('[review\n')  # an unparseable file still fails closed for risk
        self.assertIn('usage-pacing.toml', self.raw_task('review', 'proj', '5', code=1))

    def test_risk_forced_high_global_and_company_repo_paths_refuse_lowering(self):
        wt, _ = self.started()
        target = wt / 'rails/guard.py'
        target.parent.mkdir()
        self.commit_in(wt, 'rails/guard.py')
        self.risk_policy_fixture('[review]\nhigh_paths = { proj = ["rails/**"] }\n')
        self.assertIn('risk=high', self.raw_task('status', 'proj'))
        self.assertIn('Risk: high (forced by rails/guard.py)', self.raw_task('check', 'proj', '5'))
        for value in ('low', 'normal'):
            self.assertIn('rails/guard.py', self.raw_task('risk', 'proj', '5', value, code=1))
            self.assertIn('rails/guard.py', self.raw_task('start', 'proj', '5', '--builder', 'codex', '--risk', value, code=1))
        self.assertIn('risk high: merge requires red-team', self.raw_task('merge', 'proj', '5', '--override', 'cannot skip red-team', code=1))
        self.risk_policy_fixture('')
        (wt / 'policy').mkdir()
        self.commit_in(wt, 'policy/access.toml')
        self.assertIn('forced by policy/access.toml', self.raw_task('check', 'proj', '5'))

    def test_risk_bootstrap_defaults_cover_rails_but_not_task_itself(self):
        wt, _ = self.started()
        fake_task = SimpleNamespace(repo=Path('bootstrap'))
        with mock.patch.object(TASK_MODULE, 'HOME', self.home), mock.patch.object(TASK_MODULE, 'default_branch', return_value='main'), \
                mock.patch.object(TASK_MODULE, 'has_origin', return_value=False):
            for path in ('home/dot_local/bin/executable_task', 'system/agent-policy/pre_tool_use.py',
                         'home/dot_local/share/bootstrap/agent-policy/remove_pre_tool_use.py',
                         'home/dot_local/share/bootstrap/agent-policy/remove_context_reminder.py',
                         'system/claude-managed-guardrails.json', 'home/dot_codex/rules/default.rules',
                         'system/codex-requirements.toml', 'home/dot_claude/settings.json',
                         'home/dot_local/bin/executable_task-hold', 'HOLD.founder', 'profiles/sandbox.json',
                         'home/dot_gitconfig.tmpl', '.gitleaks.toml'):
                target = wt / path
                target.parent.mkdir(parents=True, exist_ok=True)
                self.commit_in(wt, path)
                risk, _, forced = TASK_MODULE.risk_context(fake_task, wt, {})
                self.assertEqual(risk, 'normal' if path.endswith('executable_task') else 'high')
                if risk == 'high': self.assertTrue(forced)
                self.git(self.repo, 'update-ref', 'refs/heads/main', self.git(wt, 'rev-parse', 'HEAD'))

    def test_risk_rules_paths_are_high_but_skill_support_files_are_normal(self):
        wt, _ = self.started()
        fake = SimpleNamespace(repo=Path('proj'))
        high = ('skills/company/SKILL.md', 'skills/company/references/nested/rules.md',
                'AGENTS.md', 'CLAUDE.md', 'home/dot_codex/AGENTS.md', 'home/dot_claude/CLAUDE.md',
                'INTENT.md', 'decisions/d-1.md', 'backlog/decisions/d-1.md', 'policy/usage.toml')
        normal = ('skills/company/evals/case.json', 'skills/company/scripts/run.py',
                  'skills/company/assets/logo.svg')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                mock.patch.object(TASK_MODULE, 'default_branch', return_value='main'), \
                mock.patch.object(TASK_MODULE, 'has_origin', return_value=False):
            for name in (*high, *normal):
                with self.subTest(path=name):
                    (wt / name).parent.mkdir(parents=True, exist_ok=True)
                    self.commit_in(wt, name)
                    risk, _, forced = TASK_MODULE.risk_context(fake, wt, {})
                    self.assertEqual((risk, forced), ('high', name) if name in high else ('normal', ''))
                    self.git(self.repo, 'update-ref', 'refs/heads/main', self.git(wt, 'rev-parse', 'HEAD'))

    def test_risk_redteam_both_vendors_wrapper_and_default_models(self):
        wt, _ = self.started()
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'wrapper fixture')
        wrapper = self.home / 'cortex/cortex-core/skills/company/references/red-team-brief.md'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('# Ambition bar fixture\nJudge against today\'s best practice.\n')
        brief = self.home / 'brief.md'
        brief.write_text('Attack the task.\n')
        for vendor in ('claude', 'codex'):
            binary = self.home / 'bin' / vendor
            binary.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                              'pathlib.Path(".work/redteam-call.json").write_text(json.dumps(dict(args=sys.argv[1:], brief=sys.stdin.read())))\n'
                              'if "-o" in sys.argv: pathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text("Reviewed risks\\n")\n'
                              'else: print("Reviewed risks")\n')
            binary.chmod(0o755)
        for builder, vendor, model in (('codex', 'claude', 'sonnet'), ('claude', 'codex', 'gpt-6.1-sol')):
            self.raw_task('start', 'proj', '5', '--builder', builder, '--refresh')
            self.raw_task('redteam', 'proj', '5', '--brief', str(brief))
            meta = json.loads((self.state() / 'redteam.json').read_text())
            call = json.loads((wt / '.work/redteam-call.json').read_text())
            self.assertEqual((meta['vendor'], meta['model'], meta['ambition_wrapper']), (vendor, model, True))
            self.assertEqual(call['brief'], wrapper.read_text() + '\n\n' + brief.read_text())
            self.assertIn(model, call['args'])
            if vendor == 'codex':
                self.assertEqual(call['args'][:5], ['exec', '-c', 'default_permissions="review"',
                                                  '-c', 'web_search="live"'])
                self.assertNotIn('--add-dir', call['args'])
            else:
                self.assertIn('dontAsk', call['args'])
        wrapper.unlink()
        self.raw_task('redteam', 'proj', '5', '--brief', str(brief))
        self.assertFalse(json.loads((self.state() / 'redteam.json').read_text())['ambition_wrapper'])

    def test_risk_close_low_without_review_after_worktree_removal(self):
        self.raw_task('start', 'proj', '5', '--builder', 'codex', '--risk', 'low')
        wt = self.repo / '.worktrees/t5'
        self.commit_in(wt, 'b.txt')
        self.raw_task('merge', 'proj', '5')
        self.assertFalse(wt.exists())
        self.raw_task('close', 'proj', '5', '-m', 'done', '--retro', 'routine')
        text = next((self.kb / 'backlog/tasks').glob('t-5*')).read_text()
        self.assertIn('status: Done', text)
        self.assertIn('[x] #1', text)
        self.assertIn('[x] #2', text)
        self.assertNotIn('## Review evidence', text)

    def test_risk_close_high_forced_paths_stale_missing_empty_redteam_and_normal_review(self):
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.assertIn('recorded review', self.raw_task('close', 'proj', '5', '-m', 'done', '--retro', 'routine', code=1))
        self.prepare_merge_evidence('5')
        review_path = self.state() / 'review.json'
        review = json.loads(review_path.read_text())
        review['model'] = 'opus'
        review_path.write_text(json.dumps(review))
        self.assertIn('cheap review', self.raw_task('close', 'proj', '5', '-m', 'done', '--retro', 'routine', code=1))
        (wt / 'skills/company').mkdir(parents=True)
        self.commit_in(wt, 'skills/company/SKILL.md')
        self.prepare_merge_evidence('5')
        head = self.git(wt, 'rev-parse', 'HEAD')
        self.git(self.repo, 'worktree', 'remove', '--force', str(wt))
        output = self.state() / 'redteam-output.md'
        for commit, content in ((None, None), ('old-head', 'Risks'), (head, '')):
            if commit:
                output.write_text(content)
                (self.state() / 'redteam.json').write_text(json.dumps(dict(commit=commit, output=str(output))))
            self.assertIn('risk high: close requires red-team', self.raw_task('close', 'proj', '5', '-m', 'done', '--retro', 'routine', code=1))
        output.write_text('Risks reviewed')
        self.raw_task('close', 'proj', '5', '-m', 'done', '--retro', 'routine')

    def test_risk_high_redteam_command_head_staleness_and_waiver(self):
        wt, _ = self.started()
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'safety')
        self.prepare_merge_evidence('5')
        brief = self.home / 'redteam-brief.md'
        brief.write_text('Attack the safety assumptions.\n')
        binary = self.home / 'bin/claude'
        binary.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(".work/redteam-call.json").write_text(json.dumps(dict(args=sys.argv[1:], '
                          'brief=sys.stdin.read(), locks=os.environ.get("GIT_OPTIONAL_LOCKS"))))\n'
                          'print("Risks reviewed.")\n')
        binary.chmod(0o755)
        self.risk_policy_fixture('[review.high]\nred_team_model_claude = "haiku"\n')
        self.raw_task('redteam', 'proj', '5', '--brief', str(brief))
        head = self.git(wt, 'rev-parse', 'HEAD')
        meta = json.loads((self.state() / 'redteam.json').read_text())
        call = json.loads((wt / '.work/redteam-call.json').read_text())
        self.assertEqual(call, dict(args=['-p', '--permission-mode', 'dontAsk', '--model', 'haiku', '--effort', 'high',
                         '--allowedTools', 'Read', 'Grep', 'Glob', 'WebSearch', 'WebFetch',
                         'Bash(git --no-optional-locks:*)'],
                         brief=brief.read_text(), locks='0'))
        self.assertEqual(meta['commit'], head)
        self.assertEqual((meta['vendor'], meta['model'], meta['ambition_wrapper']), ('claude', 'haiku', False))
        self.assertIn(f"redteam={meta['output']} matches", self.raw_task('status', 'proj'))
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        self.assertIn('stale', self.raw_task('status', 'proj'))
        self.assertIn('risk high: merge requires red-team', self.raw_task('merge', 'proj', '5', code=1))
        self.raw_task('merge', 'proj', '5', '--keep', '--no-redteam', 'PM accepts bounded change')
        ledger = (self.home / '.local/state/task/usage/overrides.jsonl').read_text()
        self.assertIn('PM accepts bounded change', ledger)

    def test_risk_high_redteam_current_output_allows_merge_and_failed_run_preserves_evidence(self):
        wt, _ = self.started()
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'guard')
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        output = self.state() / 'redteam.md'
        output.write_text('Reviewed risks\n')
        (self.state() / 'redteam.json').write_text(json.dumps(dict(commit=self.git(wt, 'rev-parse', 'HEAD'), output=str(output))))
        brief = self.home / 'brief.md'
        brief.write_text('Check the guard.\n')
        binary = self.home / 'bin/claude'
        binary.write_text('#!/bin/sh\nexit 1\n')
        binary.chmod(0o755)
        self.assertIn('red-team failed', self.raw_task('redteam', 'proj', '5', '--brief', str(brief), code=1))
        self.assertEqual(json.loads((self.state() / 'redteam.json').read_text())['output'], str(output))
        self.assertEqual(output.read_text(), 'Reviewed risks\n')
        binary.write_text('#!/bin/sh\necho "Reviewed risks"\n')
        self.raw_task('redteam', 'proj', '5', '--brief', str(brief))
        self.raw_task('merge', 'proj', '5', '--keep')

    def test_risk_redteam_invalid_brief_and_unavailable_claude_preserve_old_evidence(self):
        wt, _ = self.started()
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'safety')
        head = self.git(wt, 'rev-parse', 'HEAD')
        output = self.state() / f'redteam-{head}.md'
        output.write_text('Existing reviewed risks\n')
        meta = self.state() / 'redteam.json'
        meta.write_text(json.dumps(dict(commit=head, output=str(output))))
        before = meta.read_bytes(), output.read_bytes()
        brief = self.home / 'brief.md'
        unreadable = self.home / 'unreadable.md'
        unreadable.write_text('valid content')
        make_read_only(self, unreadable, 0)
        binary = self.home / 'bin/claude'
        binary.write_text('#!/bin/sh\ntouch "$HOME/redteam-launched"\nexit 1\n')
        binary.chmod(0o755)
        for value in (None, self.home, unreadable, b'', b' \n', b'\xff'):
            with self.subTest(brief=value):
                if isinstance(value, bytes):
                    brief.write_bytes(value)
                target = value if isinstance(value, Path) else brief
                out = self.raw_task('redteam', 'proj', '5', '--brief', str(target), code=1)
                self.assertIn('task redteam: risk high:', out)
                self.assertNotIn('Traceback', out)
                self.assertFalse((self.home / 'redteam-launched').exists())
                self.assertEqual((meta.read_bytes(), output.read_bytes()), before)
        brief.write_text('Attack this boundary.\n')
        # An explicit PATH with only Git makes the missing-Claude fixture independent
        # of any real vendor CLI installed on the machine.
        git_only = self.home / 'git-only'
        git_only.mkdir()
        (git_only / 'git').symlink_to(shutil.which('git'))
        out = self.raw_task('redteam', 'proj', '5', '--brief', str(brief), code=1,
                            env=dict(self.env, PATH=str(git_only)))
        self.assertIn('claude is not available on PATH', out)
        self.assertNotIn('Traceback', out)
        self.assertEqual((meta.read_bytes(), output.read_bytes()), before)
        binary.write_text('#!/missing-fixture-interpreter\n')
        out = self.raw_task('redteam', 'proj', '5', '--brief', str(brief), code=1)
        self.assertIn('red-team failed', out)
        self.assertNotIn('Traceback', out)
        self.assertEqual((meta.read_bytes(), output.read_bytes()), before)

    def test_risk_no_redteam_waiver_is_not_ledgered_when_remaining_gate_fails(self):
        wt, _ = self.started()
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'safety')
        self.commit_in(wt, 'b.txt')
        ledger = self.home / '.local/state/task/usage/overrides.jsonl'
        before = ledger.read_bytes()
        out = self.raw_task('merge', 'proj', '5', '--no-redteam', 'bounded waiver', code=1)
        self.assertIn('Merge refused', out)
        self.assertEqual(ledger.read_bytes(), before)
        self.prepare_merge_evidence('5')
        self.commit_in(self.repo, 'FAIL')
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        out = self.raw_task('merge', 'proj', '5', '--no-redteam', 'bounded waiver', code=1)
        self.assertIn('Gate check failed', out)
        self.assertEqual(ledger.read_bytes(), before)
        self.git(self.repo, 'rm', 'FAIL')
        self.git(self.repo, 'commit', '-qm', 'repair fixture gate')
        self.git(self.repo, 'push', '-q', 'origin', 'main')
        self.raw_task('merge', 'proj', '5', '--keep', '--no-redteam', 'bounded waiver')
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        waived = [row for row in rows if row.get('kind') == 'no_redteam']
        self.assertEqual(len(waived), 1)
        self.assertEqual((waived[0]['override'], waived[0]['commit']), ('bounded waiver', self.git(wt, 'rev-parse', 'HEAD')))

    def test_risk_extra_round_under_limit_is_refused_without_launch_or_grant(self):
        wt, _ = self.started()
        self.stub_models()
        (wt / '.work/prompt.md').write_text('build fixture\n')
        for used, extra in ((0, 0), (3, 2)):
            with self.subTest(used=used, extra=extra):
                rounds = self.state() / 'rounds.json'
                rounds.write_text(json.dumps(dict(used=used, extra=extra)))
                before = rounds.read_bytes()
                out = self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', 'premature grant', code=1)
                self.assertIn('only when the round limit is reached', out)
                self.assertEqual(rounds.read_bytes(), before)
                self.assertFalse((wt / '.work/model-call.json').exists())
                self.assertNotIn('premature grant', self.raw_task('status', 'proj'))

    def test_risk_round_limits_per_tier_and_extra_allowance_persists_across_refresh(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / '.work/prompt.md').write_text('build fixture\n')
        for risk, maximum in (('low', 2), ('normal', 3), ('high', 3)):
            with self.subTest(risk=risk):
                self.raw_task('risk', 'proj', '5', risk, '--reason', 'round fixture')
                (self.state() / 'rounds.json').unlink(missing_ok=True)
                (wt / '.work/lane.json').unlink(missing_ok=True)
                for _ in range(maximum):
                    self.raw_task('lane', 'proj', '5', '--no-pace')
                    self.wait_for_lane(wt)
                refused = self.raw_task('lane', 'proj', '5', '--no-pace', code=1)
                self.assertIn(f'risk {risk}: maximum {maximum}', refused)
                self.assertIn('nonempty reason', self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', ' ', code=1))
                self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', 'small contained fix')
                self.wait_for_lane(wt)
                self.raw_task('start', 'proj', '5', '--builder', 'codex', '--refresh')
                self.assertIn(f'maximum {maximum + 1}', self.raw_task('lane', 'proj', '5', '--no-pace', code=1))
                before = (self.state() / 'rounds.json').read_bytes()
                refused = self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', 'second grant', code=1)
                self.assertIn('extra rounds exhausted (1/1 granted)', refused)
                self.assertEqual((self.state() / 'rounds.json').read_bytes(), before)
        self.assertEqual((self.home / '.local/state/task/usage/overrides.jsonl').read_text().count('small contained fix'), 3)

    def test_risk_policy_round_limits_and_lane_effort_are_forwarded_and_logged(self):
        self.stub_models()
        wt, _ = self.started()
        self.risk_policy_fixture('[review.normal]\nmax_rounds = 6\nextra_rounds = 0\n')
        (wt / '.work/prompt.md').write_text('effort fixture\n')
        for builder in ('codex', 'claude'):
            for effort in ('low', 'medium', 'high'):
                self.raw_task('lane', 'proj', '5', '--builder', builder, '--effort', effort, '--no-pace')
                run = self.wait_for_lane(wt)
                call = json.loads((wt / '.work/model-call.json').read_text())['args']
                self.assertIn(f'model_reasoning_effort={effort}' if builder == 'codex' else effort, call)
                self.assertEqual(run['usage_run']['effort'], effort)
        self.assertIn('risk normal: maximum 6', self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', 'no allowance', code=1))

    def test_risk_extra_round_policy_two_grants_capped_across_tier_changes(self):
        self.stub_models()
        wt, _ = self.started()
        self.risk_policy_fixture('[review.normal]\nmax_rounds = 1\nextra_rounds = 2\n')
        (wt / '.work/prompt.md').write_text('round fixture\n')
        self.raw_task('lane', 'proj', '5', '--no-pace')
        self.wait_for_lane(wt)
        for count in (1, 2):
            self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', 'one more')
            self.wait_for_lane(wt)
            self.assertEqual(json.loads((self.state() / 'rounds.json').read_text())['extra'], count)
        self.assertIn('(2/2 granted)', self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', 'excess', code=1))
        self.raw_task('risk', 'proj', '5', 'low')
        self.raw_task('lane', 'proj', '5', '--no-pace')
        self.wait_for_lane(wt)
        self.assertIn('(2/1 granted)', self.raw_task('lane', 'proj', '5', '--no-pace', '--extra-round', 'excess', code=1))

    def test_round_cause_setup_rounds_are_recorded_and_not_counted(self):
        self.stub_models()
        says = self.home / 'lane-says'
        stub = MODEL_STUB.replace("sys.exit(int(", f"sys.stderr.write(pathlib.Path({str(says)!r}).read_text())\nsys.exit(int(")
        (self.home / 'bin/codex').write_text(stub)
        wt, _ = self.started()
        self.risk_policy_fixture('[review.normal]\nmax_rounds = 1\nextra_rounds = 0\n')
        (wt / '.work/prompt.md').write_text('round cause fixture\nNever work around a read-only file system.\n')
        rounds = self.state() / 'rounds.json'
        cases = (('Error: git push denied by policy hook (task-guard)\n', 'policy-denial', False),
                 ('error: Failed to initialize cache at `/home/agent/.cache/uv`\n'
                  '  Caused by: Read-only file system (os error 30)\n', 'missing-tool-or-cache', False),
                 ('bash: line 1: just: command not found\n', 'missing-tool-or-cache', False),
                 ('FAILED (failures=1)\nAssertionError: 2 != 3\n', 'code-or-test', True))
        for number, (text, cause, counted) in enumerate(cases, 1):
            with self.subTest(cause=cause, text=text):
                says.write_text(text)
                self.raw_task('lane', 'proj', '5', '--no-pace')
                self.wait_for_lane(wt)
                entry = json.loads(rounds.read_text())['rounds'][-1]
                self.assertEqual((entry['round'], entry['cause'], entry['counted']), (number, cause, counted))
        self.assertEqual(json.loads(rounds.read_text())['used'], 4)
        self.assertIn('risk normal: maximum 1', self.raw_task('lane', 'proj', '5', '--no-pace', code=1))
        ledger = self.home / '.local/state/task/usage/overrides.jsonl'
        for bad in (('9', 'blind round'), ('4', ' '), ('1', 'already free')):
            self.assertIn('setup-round' if bad[0] != '1' else 'already does not count',
                          self.raw_task('lane', 'proj', '5', '--setup-round', *bad, code=1))
        out = self.raw_task('lane', 'proj', '5', '--setup-round', '4', 'blind round: the hook hid the cache error')
        self.assertIn('round 4 marked setup-caused', out)
        entry = json.loads(rounds.read_text())['rounds'][3]
        self.assertEqual((entry['counted'], entry['marked']), (False, 'blind round: the hook hid the cache error'))
        row = json.loads(ledger.read_text().splitlines()[-1])
        self.assertEqual((row['model'], row['tier'], row['override']),
                         ('setup round', 'round 4', 'blind round: the hook hid the cache error'))
        says.write_text('done\n')
        self.raw_task('lane', 'proj', '5', '--no-pace')
        self.assertEqual(self.wait_for_lane(wt)['usage_run']['round'], 5)

    def test_round_cause_setup_round_refunds_its_extra_round_grant(self):
        self.stub_models()
        says = self.home / 'lane-says'
        stub = MODEL_STUB.replace("sys.exit(int(", f"sys.stderr.write(pathlib.Path({str(says)!r}).read_text())\nsys.exit(int(")
        (self.home / 'bin/codex').write_text(stub)
        wt, _ = self.started()
        self.risk_policy_fixture('[review.normal]\nmax_rounds = 1\nextra_rounds = 2\n')
        (wt / '.work/prompt.md').write_text('refund fixture\n')
        rounds = self.state() / 'rounds.json'
        ledger = self.home / '.local/state/task/usage/overrides.jsonl'
        for flags, text, extra in (((), 'done\n', 0),
                                   (('--extra-round', 'retry after hook'), 'Error: git push denied by policy hook\n', 0),
                                   (('--extra-round', 'fix the test'), 'FAILED (failures=1)\n', 1)):
            with self.subTest(flags=flags, text=text):
                says.write_text(text)
                self.raw_task('lane', 'proj', '5', '--no-pace', *flags)
                self.wait_for_lane(wt)
                self.assertEqual(json.loads(rounds.read_text())['extra'], extra)
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual([(row['model'], row['override']) for row in rows if 'extra rounds' in row['model']],
                         [('extra rounds', 'retry after hook'), ('extra rounds refund', 'policy-denial'),
                          ('extra rounds', 'fix the test')])
        self.raw_task('lane', 'proj', '5', '--setup-round', '3', 'the test failed on a read-only cache')
        self.assertEqual(json.loads(rounds.read_text())['extra'], 0)
        self.assertEqual([json.loads(line)['model'] for line in ledger.read_text().splitlines()[-2:]],
                         ['extra rounds refund', 'setup round'])

    def state(self, name="t5"):
        return self.home / ".local/state/task/proj" / name

    def sandbox_state(self):
        """Simulate the sandbox's private-state mount, independently of vendor markers."""
        state = self.home / ".local/state/task"
        state.mkdir(parents=True, exist_ok=True)
        make_read_only(self, state, 0o555)
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

    def wait_for_lane(self, wt, finished=True, state="finished"):
        import json
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if finished:
                record = json.loads((wt / ".work/lane.json").read_text())
                if record["state"] == state:
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

    def commit_in(self, wt, name, text="x\n", when=None):
        """Commit a file. Pass `when` for a commit that must precede a later sign-off: the host
        wall clock steps back by seconds, so a commit made "now" can end up dated after it."""
        (wt / name).write_text(text)
        self.git(wt, "add", name)
        stamp = {} if when is None else {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", "-C", str(wt), "commit", "-qm", f"add {name}"],
                       env=dict(self.env, **stamp), check=True, capture_output=True)

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

    def archive_fixture(self, root):
        root.mkdir(parents=True, exist_ok=True)
        payloads = {'house.sqlite': b'database', 'house.sqlite-wal': b'wal',
                    'other.sqlite3': b'db', 'other.db': b'db', 'other.duckdb': b'db',
                    'house.sqlite-shm': b'shm', 'house.sqlite-journal': b'journal',
                    'innocent.txt': b'SQLite format 3\0payload', 'module.pyc': b'cache',
                    'report.md': b'report', 'check.json': b'{}', 'review.txt': b'review'}
        for name, content in payloads.items():
            (root / name).write_bytes(content)
        for name, size in [('check-report.json', '51M'), ('keep.log', '49M')]:
            subprocess.run(['truncate', '-s', size, str(root / name)], check=True)
        (root / '.pycache').mkdir()
        (root / '.pycache/source.py').write_text('cache')
        outside = self.home / 'outside.sqlite'
        outside.write_bytes(b'SQLite format 3\0outside')
        (root / 'linked.db').symlink_to(outside)
        (root / 'linked-dir').symlink_to(self.home, target_is_directory=True)
        return payloads, outside

    def assert_archive_skips(self, skipped, payloads, prefix=''):
        import hashlib
        for name in ('house.sqlite', 'house.sqlite-wal', 'other.sqlite3', 'other.db',
                     'other.duckdb', 'house.sqlite-shm', 'house.sqlite-journal', 'innocent.txt'):
            entry = skipped[prefix + name]
            self.assertEqual(entry['size'], len(payloads[name]))
            self.assertEqual(entry['sha256'], hashlib.sha256(payloads[name]).hexdigest())
            self.assertEqual(entry['reason'], 'database')
        big = skipped[prefix + 'check-report.json']
        self.assertEqual(big['size'], 51 * 1024 ** 2)
        digest = hashlib.sha256()
        for _ in range(51):
            digest.update(bytes(1024 ** 2))
        self.assertEqual(big['sha256'], digest.hexdigest())
        self.assertEqual(big['reason'], 'file over 50 MB')
        for name in ('.pycache', 'module.pyc'):
            self.assertEqual(skipped[prefix + name]['reason'], 'cache/runtime')
            self.assertNotIn('sha256', skipped[prefix + name])

    def test_archive_exclusions_shared_with_ignored_files(self):
        import runpy
        wt, _ = self.started()
        payloads, outside = self.archive_fixture(wt / '.work/fixture')
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
        manifest = next(self.work_archives().glob('*/manifest.json'))
        skipped = {row['path']: row for row in json.loads(manifest.read_text())['skipped']}
        self.assert_archive_skips(skipped, payloads, 'fixture/')
        saved = manifest.parent / '.work/fixture'
        for name in ('keep.log', 'report.md', 'check.json', 'review.txt'):
            self.assertTrue((saved / name).is_file())
        self.assertTrue((saved / 'linked.db').is_symlink())
        self.assertTrue((saved / 'linked-dir').is_symlink())
        self.assertTrue(outside.exists())
        self.assertTrue((wt / '.work/fixture/house.sqlite').exists())
        # Exercise tidy's real ignored-file entry point with the same payloads.
        tool = ROOT / 'home/dot_local/bin/executable_workspace-tidy'
        with mock.patch.dict(os.environ, HOME=str(self.home)):
            tidy = runpy.run_path(str(tool), run_name='archive_test')
            files = [item.relative_to(wt / '.work/fixture')
                     for item in (wt / '.work/fixture').iterdir() if item.name != '.pycache']
            files.append(Path('.pycache/source.py'))
            destination = tidy['archive_ignored'](self.repo, wt / '.work/fixture', files, [])
        record = json.loads((destination / 'manifest.json').read_text())
        self.assert_archive_skips({row['path']: row for row in record['skipped']}, payloads)
        self.assertTrue((destination / 'ignored/keep.log').exists())
        self.assertFalse((destination / 'ignored/house.sqlite').exists())

    def test_archive_ignored_manifest_collisions(self):
        import runpy
        source = self.home / 'ignored-source'
        source.mkdir()
        outside = self.home / 'outside.txt'
        outside.write_text('untouched')
        (source / 'manifest.json').write_text('original ignored manifest')
        (source / 'manifest.json.tmp').symlink_to(outside)
        with mock.patch.dict(os.environ, HOME=str(self.home)):
            tidy = runpy.run_path(str(ROOT / 'home/dot_local/bin/executable_workspace-tidy'),
                                  run_name='archive_test')
            destination = tidy['archive_ignored'](self.repo, source,
                [Path('manifest.json'), Path('manifest.json.tmp')], [])
        self.assertIsNotNone(destination)
        self.assertEqual(outside.read_text(), 'untouched')
        self.assertEqual((destination / 'ignored/manifest.json').read_text(),
                         'original ignored manifest')
        self.assertTrue((destination / 'ignored/manifest.json.tmp').is_symlink())
        self.assertEqual(json.loads((destination / 'manifest.json').read_text()), {'skipped': []})

    def test_archive_prune_keeps_unreadable_and_malformed_snapshots(self):
        root = self.home / '.local/state/task/archive'
        snapshot = root / 'proj/t5/good'
        snapshot.mkdir(parents=True)
        report_file = snapshot / 'report.md'
        report_file.write_text('keep')
        make_read_only(self, report_file, 0)
        for name, record in [('array', []), ('scalar', 1), ('bad-pruned', {'pruned': {}})]:
            bad = snapshot.with_name(name)
            bad.mkdir()
            (bad / 'manifest.json').write_text(json.dumps(record))
            (bad / 'copy.db').write_text('keep in invalid snapshot')
        report = []
        TASK_MODULE.prune_evidence_archive(root, True, report)
        self.assertTrue(report_file.exists())
        self.assertTrue(any('kept' in line and 'report.md' in line for line in report))
        for name in ('array', 'scalar', 'bad-pruned'):
            self.assertTrue((snapshot.with_name(name) / 'copy.db').exists())
            self.assertTrue(any('cannot prune' in line and name in line for line in report))

    def test_archive_unreadable_ordinary_capture_keeps_worktree(self):
        import runpy
        wt, _ = self.started()
        file = wt / '.work/report.md'
        file.write_text('keep')
        make_read_only(self, file, 0)
        report = []
        with mock.patch.dict(os.environ, HOME=str(self.home)):
            tidy = runpy.run_path(str(ROOT / 'home/dot_local/bin/executable_workspace-tidy'),
                                  run_name='archive_test')
            self.assertFalse(tidy['archive_work'](self.repo, wt, report))
            self.assertIsNone(tidy['archive_ignored'](self.repo, wt,
                                                    [Path('.work/report.md')], report))
        self.assertTrue(wt.exists())
        self.assertTrue(file.exists())
        self.assertTrue(any('kept' in line and 'cannot archive' in line for line in report))

    def test_archive_dry_runs_do_not_hash_and_tidy_skips_pending(self):
        root = self.home / '.local/state/task/archive'
        snapshot = self.work_archives() / 'snapshot'
        snapshot.mkdir(parents=True)
        (snapshot / 'manifest.json').write_text('{}')
        (snapshot / 'copy.db').write_bytes(b'db')
        subprocess.run(['truncate', '-s', '51M', str(snapshot / 'check.log')], check=True)
        pending = self.work_archives() / '.pending-active'
        pending.mkdir()
        (pending / 'copy.db').write_bytes(b'db')
        report = []
        with mock.patch.object(TASK_MODULE.hashlib, 'sha256', side_effect=AssertionError('dry-run hash')), \
                mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.prune_evidence_archive(root, False, report)
        self.assertTrue(any('database' in line and 'copy.db' in line for line in report))
        TASK_MODULE.prune_evidence_archive(root, True, [])
        self.assertEqual((pending / 'copy.db').read_bytes(), b'db')
        self.assertFalse((snapshot / 'copy.db').exists())

    def test_archive_prune_failed_unlink_is_not_recorded(self):
        root = self.home / '.local/state/task/archive'
        snapshot = self.work_archives() / 'snapshot'
        snapshot.mkdir(parents=True)
        manifest = snapshot / 'manifest.json'
        manifest.write_text('{}')
        database = snapshot / 'copy.db'
        database.write_bytes(b'db')
        unlink = Path.unlink
        def fail_database(path, *args, **kwargs):
            if path == database:
                raise PermissionError('fixture unlink denied')
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', fail_database):
            report = []
            TASK_MODULE.prune_evidence_archive(root, True, report)
        self.assertEqual(json.loads(manifest.read_text()), {})
        self.assertTrue(database.exists())
        self.assertTrue(any('fixture unlink denied' in line for line in report))

    def test_atomic_json_failure_removes_temporary_file(self):
        directory = self.home / 'atomic-json'
        directory.mkdir()
        target = directory / 'manifest.json'
        target.write_text('{}')
        for operation in ('serialization', 'replace'):
            with self.subTest(operation=operation):
                patch = (mock.patch.object(TASK_MODULE.json, 'dumps', side_effect=ValueError('serialize'))
                         if operation == 'serialization' else
                         mock.patch.object(TASK_MODULE.os, 'replace', side_effect=OSError('replace')))
                with patch, self.assertRaises((ValueError, OSError)):
                    TASK_MODULE.atomic_json(target, {'new': True})
                self.assertEqual(list(directory.iterdir()), [target])
                self.assertEqual(target.read_text(), '{}')

    def test_archive_prune_dry_run_apply_and_symlinks(self):
        root = self.home / '.local/state/task/archive'
        snapshot = root / 'proj/t5/snapshot'
        payloads, outside = self.archive_fixture(snapshot / '.work')
        (snapshot / '.work/empty-after').mkdir()
        (snapshot / '.work/empty-after/copy.db').write_bytes(b'db')
        manifest = snapshot / 'manifest.json'
        manifest.write_text(json.dumps({'skipped': [{'path': 'old'}]}))
        (snapshot / 'manifest.json.tmp').symlink_to(outside)
        outside_content = outside.read_bytes()
        # Old ignored-file archives have no manifest yet.
        old = root / 'proj/t5/old-ignored'
        old.mkdir()
        (old / 'copy.db').write_bytes(b'db')
        (root / 'linked').symlink_to(self.home, target_is_directory=True)
        before = manifest.read_bytes()
        report = []
        tidy = ROOT / 'home/dot_local/bin/executable_workspace-tidy'
        dry_run = subprocess.run([sys.executable, str(tidy)], env=self.env,
                                 capture_output=True, text=True, check=True)
        report = dry_run.stdout.splitlines()
        self.assertEqual(manifest.read_bytes(), before)
        self.assertTrue((snapshot / '.work/house.sqlite').exists())
        self.assertTrue((old / 'copy.db').exists())
        self.assertFalse((old / 'manifest.json').exists())
        listed = {line.split('would remove ', 1)[1].rsplit(' (', 1)[0]
                  for line in report if 'would remove ' in line and ' bytes)' in line}
        subprocess.run([sys.executable, str(tidy), '--apply'], env=self.env,
                       capture_output=True, text=True, check=True)
        record = json.loads(manifest.read_text())
        pruned = {row['path']: row for row in record['pruned']}
        self.assert_archive_skips(pruned, payloads, '.work/')
        self.assertEqual(record['skipped'], [{'path': 'old'}])
        self.assertEqual(listed, {str(snapshot / row['path']) for row in record['pruned']} |
                         {str(old / 'copy.db')})
        for row in record['pruned']:
            self.assertFalse((snapshot / row['path']).exists())
            self.assertIsNotNone(__import__('datetime').datetime.fromisoformat(row['date']))
        for name in ('keep.log', 'report.md', 'check.json', 'review.txt'):
            self.assertTrue((snapshot / '.work' / name).exists())
        self.assertFalse((snapshot / '.work/empty-after').exists())
        total = sum(row['size'] for row in record['pruned']) + 2
        self.assertTrue(any(f'{total} bytes total' in line for line in report))
        self.assertTrue((snapshot / '.work/linked.db').is_symlink())
        self.assertTrue(outside.exists())
        self.assertEqual(outside.read_bytes(), outside_content)
        TASK_MODULE.prune_evidence_archive(root, True, [])
        self.assertEqual(json.loads(manifest.read_text()), record)

    def test_archive_unreadable_file_records_null_hash(self):
        wt, _ = self.started()
        for name in ('check.db',):
            file = wt / '.work' / name
            file.write_bytes(b'db')
            make_read_only(self, file, 0)
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
        manifest = next(self.work_archives().glob('*/manifest.json'))
        for name in ('check.db',):
            row = next(row for row in json.loads(manifest.read_text())['skipped'] if row['path'] == name)
            self.assertIsNone(row['sha256'])
            self.assertIn('error', row)
            self.assertFalse((manifest.parent / '.work' / name).exists())

    def test_archive_runtime_failures_and_deep_tree(self):
        wt, _ = self.started()
        work = wt / '.work'
        for name in TASK_MODULE.WORK_CACHES:
            (work / name).mkdir()
            (work / name / 'state').write_text('runtime')
        (work / 'outside').symlink_to(self.home)
        (work / 'unreadable').write_text('private')
        make_read_only(self, work / 'unreadable', 0)
        (work / 'locked').mkdir()
        make_read_only(self, work / 'locked', 0)
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
        make_read_only(self, gate / 'locked', 0o500)
        failed = subprocess.CompletedProcess([], 1)
        warning = io.StringIO()
        with mock.patch.object(TASK_MODULE, 'run_git', return_value=failed), \
                contextlib.redirect_stderr(warning):
            TASK_MODULE.remove_gate_worktree(TASK_MODULE.Task(self.repo, '5'), gate)
        self.assertIn('Gate cleanup warning:', warning.getvalue())
        self.assertTrue((gate / 'locked/file').exists())

    def test_archive_named_virtualenv_and_runtime(self):
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

    def test_oversized_directory_keeps_broad_evidence_in_archive(self):
        wt, _ = self.started()
        names = ['report.json', 'report.txt', 'run.json', 'exception.json',
                 'task.json', 'gate-check.json', 'arbitrary.json']
        names += ['notes' + suffix for suffix in
                  ('.md', '.jsonl', '.txt', '.html', '.toml', '.yaml', '.yml', '.csv', '.log')]
        names += [word + '.dat' for word in
                  ('report', 'prompt', 'request', 'check', 'review', 'merge',
                   'manifest', 'run', 'task', 'exception', 'gate')]
        for container in (wt,):
            directory = container / '.work/big'
            directory.mkdir(parents=True)
            for name in names:
                (directory / name).write_text('evidence')
            for name, megabytes in (('payload.bin', 150), ('state.sqlite', 100)):
                with (directory / name).open('wb') as stream:
                    stream.truncate(megabytes * 1024 ** 2)
            (directory / 'empty').mkdir()
        with mock.patch.object(TASK_MODULE, 'HOME', self.home):
            TASK_MODULE.preserve_work(self.repo, 't5', wt)
        archived = next(path.parent.parent for path in self.work_archives().glob('*/.work/big'))
        for container in (archived,):
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
        self.assertIn("Mandatory read: .work/context/lane-contract.md", out)
        self.assertIn("Acceptance criteria:\n- [ ] #1 b exists\n- [ ] #2 checks pass\n", out)
        self.assertIn("Verify:\nVerify: `true`\n", out)
        self.assertEqual((wt / ".work/dispatch.md").read_text(), out.split("Dispatch prompt:\n\n", 1)[1])
        for rule in ("Commit on the branch", "advisory feedback", "three lines", "task merge --detach",
                     "apply_patch", "heredocs"):
            self.assertNotIn(rule, out)
        self.assertEqual((wt / ".work/context/lane-contract.md").read_text(), self.contract_text)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "", "worktrees stay out of status")
        again = self.task("start", "proj", "5", "--builder", "codex")
        self.assertNotIn("Created", again, "starting twice reuses the worktree")
        self.task("start", "proj", "T-9", "--builder", "codex", code=1)  # no such task

    def test_status_lists_idle_unmerged_worktrees_and_never_removes_them(self):
        old = int(time.time()) - IDLE_TEN_DAYS
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
        old = int(time.time()) - IDLE_TEN_DAYS
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
        old = int(time.time()) - IDLE_TEN_DAYS
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
        old = int(time.time()) - IDLE_TEN_DAYS
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
        self.assertIn(f"Mandatory read: {self.repo / '.worktrees/t5/.work/context/lane-contract.md'}.", prompt)
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
        make_read_only(self, state, 0o555)
        out = self.raw_task("check", "proj", "5")
        self.assertIn("advisory lane check (not evidence for merge)", out)
        self.assertIn("PASS", out)
        evidence = __import__("json").loads((wt / ".work/check.json").read_text())
        self.assertEqual(evidence["result"], "passed")
        self.assertTrue(evidence["advisory"])
        self.assertFalse(evidence["trusted"])
        self.assertTrue((wt / ".work/check.log").is_file())
        self.assertFalse((state / "check.json").exists())

    def test_read_only_fixtures_leave_the_lane_tmp_removable(self):
        lane_tmp = self.home / 'lane/.work/tmp'

        def lock_tree(case, *, restore):
            fixture = lane_tmp / case.id().rsplit('.', 1)[-1]
            (fixture / 'inner').mkdir(parents=True)
            (fixture / 'inner/file').write_text('held')
            (fixture / 'private').write_text('private')
            for path, mode in ((fixture / 'private', 0), (fixture / 'inner', 0), (fixture, 0o555)):
                if restore:
                    make_read_only(case, path, mode)
                else:
                    path.chmod(mode)

        class Restored(unittest.TestCase):
            def test_failing(self):
                lock_tree(self, restore=True)
                self.fail('leaves every path locked')

            def test_erroring(self):
                lock_tree(self, restore=True)
                raise RuntimeError('leaves every path locked')

        class Unrestored(unittest.TestCase):
            def test_failing(self):
                lock_tree(self, restore=False)
                self.fail('leaves every path locked')

        def run(case):
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
            return unittest.TextTestRunner(stream=io.StringIO()).run(suite)

        result = run(Restored)
        self.assertEqual((len(result.failures), len(result.errors)), (1, 1))
        shutil.rmtree(lane_tmp)  # no chmod first: the fixtures must have restored access
        self.assertFalse(lane_tmp.exists())
        if os.geteuid():  # control: without the restore the same tree blocks removal
            lane_tmp.mkdir(parents=True)
            run(Unrestored)
            with self.assertRaises(PermissionError):
                shutil.rmtree(lane_tmp)
            locked = lane_tmp / 'test_failing'
            for path in (locked, locked / 'inner', locked / 'private'):
                restore_access(path)
            shutil.rmtree(lane_tmp)

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
        shared = self.home / f"task-gate-{os.getuid()}"
        shared.mkdir()
        lane_dir = self.lane_root / "proj-t5"
        lane_json = wt / ".work/lane.json"
        # Per case: the lane record, the inherited TMPDIR, the expected parent, whether the record gains the path.
        cases = {
            "recorded directory": ({"state": "running", "temporary": str(outside)}, wt / ".work/tmp", outside, False),
            "no record, shared gate TMPDIR": (None, shared, lane_dir, False),
            "record inside the checkout, TMPDIR inside the checkout": (
                {"state": "running", "temporary": str(wt / ".work/tmp")}, wt / ".work/tmp", lane_dir, True),
            "record names a vanished directory": (
                {"state": "running", "temporary": str(self.home / "gone")}, shared, lane_dir, True),
        }
        for matrix in (False, True):
            if matrix:
                (wt / ".task.toml").write_text('[[gate_variant]]\nname = "one"\n[[gate_variant]]\nname = "two"\n')
                self.git(wt, "add", ".task.toml")
                self.git(wt, "commit", "-qm", "gate variants")
            for label, (lane_record, inherited, expected, recorded) in cases.items():
                with self.subTest(matrix=matrix, case=label):
                    for name in ("gate", "verify"):
                        (self.home / f"{name}-tmp").unlink(missing_ok=True)
                    shutil.rmtree(lane_dir, ignore_errors=True)
                    lane_json.unlink(missing_ok=True)
                    if lane_record:
                        lane_json.write_text(json.dumps(lane_record))
                    out = self.raw_task("check", "proj", "5", env=dict(self.env, CLAUDECODE="1", TMPDIR=str(inherited)))
                    self.assertIn("PASS", out)
                    for name in ("gate", "verify"):
                        lines = (self.home / f"{name}-tmp").read_text().splitlines()
                        self.assertEqual(len(lines), 2 if matrix else 1)
                        for line in lines:
                            seen = Path(line)
                            self.assertEqual(seen.parent, expected)
                            self.assertNotEqual(seen.parent, inherited, "the inherited TMPDIR is never the parent")
                            self.assertFalse(seen.exists(), "the advisory check TMPDIR is removed afterwards")
                    if recorded:
                        self.assertEqual(json.loads(lane_json.read_text())["temporary"], str(lane_dir))
                        self.assertEqual(stat.S_IMODE(lane_dir.stat().st_mode), 0o700)
                    self.assertEqual(lane_dir.exists(), expected == lane_dir)
        with mock.patch.dict(os.environ, TASK_LANE_TEMP_ROOT=str(self.lane_root)):
            lane_json.unlink(missing_ok=True)
            self.assertEqual(TASK_MODULE.advisory_temporary_parent(wt), str(lane_dir))
            self.assertFalse(lane_json.exists(), "no lane record is invented for a check outside a lane")

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
                    ("risk", "proj", "5", "low"), ("redteam", "proj", "5", "--brief", "missing.md"),
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
            self.assertEqual(recorded, self.lane_root / "proj-t5")
            self.assertTrue(recorded.is_dir())
            with mock.patch.dict(os.environ, TASK_LANE_TEMP_ROOT=str(self.lane_root)):
                self.assertEqual(TASK_MODULE.advisory_temporary_parent(wt), str(recorded))
            self.assertEqual(call["prompt"], 'Read the pinned task context in .work/context/ before working.\n\n' + prompt)
            git_dir, common = (self.git(wt, "rev-parse", "--path-format=absolute", flag) for flag in ("--git-dir", "--git-common-dir"))
            self.assertNotEqual(git_dir, common)
            grants = [git_dir, f"{common}/objects", f"{common}/refs", f"{common}/logs"]
            self.assertEqual(call["args"], ["exec", "-c", 'default_permissions="lane"',
                                           "-c", 'web_search="live"',
                                            *(a for g in grants for a in ("--add-dir", g)), "--model", "gpt-6.1-sol",
                                            "-c", "model_reasoning_effort=high", "--output-last-message", str(wt / ".work/final.md"), "-"])
            added = [call["args"][i + 1] for i, a in enumerate(call["args"]) if a == "--add-dir"]
            for forbidden in (common, f"{common}/config", f"{common}/hooks", f"{common}/info", f"{common}/worktrees"):
                self.assertNotIn(forbidden, added)
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
        self.assertFalse((self.home / f'.local/state/task/usage/running/codex/{record["pid"]}.json').exists())
        self.assertFalse(recorded.exists(), "the Codex lane's recorded temporary directory is removed")
        self.assertEqual((wt / ".work/final.md").read_text(), "codex-final: Read the pinned task context in .work/context/ before working.\n\n" + prompt)
        self.assertIn("codex-log", (wt / ".work/lane.log").read_text())
        self.assertIn("lane: finished (exit 0)", self.raw_task("status", "proj"))
        self.assertFalse((self.home / "runner-record.json").exists())

    def test_lane_caches_are_private_to_codex_and_claude_environment_is_unchanged(self):
        wt, _ = self.started()
        task = TASK_MODULE.Task(self.repo, "5")
        codex = TASK_MODULE.lane_environment(task, "codex")
        private = Path(codex["TMPDIR"])
        self.assertEqual(private, wt / ".work/tmp")
        expected = {
            "UV_CACHE_DIR": private / "uv-cache",
            "PIP_CACHE_DIR": private / "pip-cache",
            "PRE_COMMIT_HOME": private / "pre-commit",
            "npm_config_cache": private / "npm-cache",
        }
        for name, path in expected.items():
            self.assertEqual(Path(codex[name]), path)
            self.assertTrue(path.is_dir())
            self.assertTrue(path.resolve().is_relative_to(wt.resolve()))
        grants = TASK_MODULE.lane_git_grants(task)
        self.assertFalse(any("/.cache/" in entry for entry in grants))
        with mock.patch.object(TASK_MODULE, "new_lane_temporary", return_value=wt / ".work/tmp/lane-12345678"):
            claude = TASK_MODULE.lane_environment(task, "claude")
        for name in expected:
            self.assertNotIn(name, claude)

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
        lane.write_text(json.dumps(dict(state="running", pid=123456, unit="task-lane-proj-t5-test", temporary=str(inner))))
        shutil.rmtree(outer)
        transport = TASK_MODULE.subprocess.run
        def stop_unit(command, **kwargs):
            if command[:3] == ['systemctl', '--user', 'stop']:
                return subprocess.CompletedProcess(command, 0)
            return transport(command, **kwargs)
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(TASK_MODULE, "HOME", self.home), \
                mock.patch.object(TASK_MODULE, "claude_temporary_root", return_value=root), \
                mock.patch.object(TASK_MODULE, "pid_alive", return_value=True), \
                mock.patch.object(TASK_MODULE.subprocess, "run", side_effect=stop_unit):
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

    def test_batch_tail_uses_trusted_check_log_only(self):
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
            self.assertEqual(batch.check_output(self.state()),
                             Path(evidence["log"]).read_text())
            self.assertNotEqual(batch.check_output(self.state()), advisory)
            (wt / "FAIL").unlink()
            self.assertTrue(TASK_MODULE.run_check(wt, "", advisory=True))
            # A newer advisory run never replaces the trusted result in a fix prompt (PM 2026-10-21).
            self.assertEqual(batch.check_output(self.state()), Path(evidence["log"]).read_text())

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
        self.assertFalse((self.home / f'.local/state/task/usage/running/claude/{record["pid"]}.json').exists())
        self.assertEqual(call["args"], ["-p", "--output-format", "stream-json", "--verbose",
                                       "--model", "sonnet", "--effort", "high",
                                       "--permission-mode", "acceptEdits", "--allowedTools",
                                       "Bash Read Write Edit Glob Grep WebSearch WebFetch",
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

    def readings(self, claude=None, codex=None, five=None, age=timedelta()):
        """Fixture usage readings in the temporary HOME: (weekly used %, hours until the weekly reset)."""
        now = datetime.now(timezone.utc)
        if claude:
            event = {"rate_limit_info": {"unifiedWindows": {"seven_day": {
                "utilization": claude[0] / 100, "resetsAt": int(now.timestamp() + claude[1] * 3600)}}}}
            if five is not None:
                event["rate_limit_info"]["unifiedWindows"]["five_hour"] = {
                    "utilization": five / 100, "resetsAt": int(now.timestamp() + 5 * 3600)}
            TASK_MODULE.task_usage.record_claude(self.home, event, now - age)
        if codex:
            limits = {"primary": {"used_percent": codex[0], "window_minutes": 10080,
                                  "resets_at": int(now.timestamp() + codex[1] * 3600)}}
            if five is not None:
                limits["secondary"] = {"used_percent": five, "window_minutes": 300,
                                       "resets_at": int(now.timestamp() + 5 * 3600)}
            path = self.home / ".codex/sessions/2026/10/20/rollout-fixture.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"timestamp": (now - age).isoformat(), "payload": {"rate_limits": limits}}) + "\n")

    def pace_lane(self, *options, code=0, builder="claude", recorded=()):
        self.stub_models()
        self.raw_task("start", "proj", "5", "--builder", builder, *recorded)
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
        self.assertEqual(lines[0], "claude last 7d: runs 0, weekly usage delta None, outcomes {}")
        self.assertEqual(lines[1], "codex last 7d: runs 0, weekly usage delta None, outcomes {}")

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
        self.assertIn("claude last 7d: runs 1", self.raw_task("usage", "--weekly-line"))
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
        self.raw_task("lane", "proj", "5", "--builder", "claude", "--tier", "hard", "--override", "Jacopo 2026-10-21: tier", code=4)
        self.readings(codex=(100, 84))
        out = self.raw_task("lane", "proj", "5", "--builder", "claude", code=4)
        self.assertIn("claude automated share used up (80 % of 75 %)", out)

    def test_pace_lane_exhausted_with_spare_reset_asks_jacopo(self):
        self.readings(claude=(80, 84), codex=(100, 84))
        wt, out = self.pace_lane(builder="codex", code=4)
        self.assertIn("Jacopo: codex is at 100 %; use your spare reset", out)

    def test_pace_lane_under_used_near_reset_switches_the_boost_on_until_the_reset(self):
        self.readings(claude=(40, 30), five=10)
        # Fable follows the recorded tier, so the task is started as hard.
        wt, out = self.pace_lane("--tier", "hard", recorded=("--tier", "hard"))
        self.assertRegex(out, r"pace: claude under-used near its reset; boost on until ")
        self.assertEqual(self.wait_for_lane(wt)["model"], "fable")
        until = json.loads((self.home / ".local/state/task/boost.json").read_text())["claude"]
        self.assertIn(f"boost on for claude until {until}", self.pace_log())
        self.assertIn(f"boost: claude until {until}", self.raw_task("status", "proj"))

    def ramp_policy(self, extra=""):
        policy = self.home / "policy.toml"
        policy.write_text("codex_ramp = { per_day = 20, ceiling = 80 }\n" + extra)
        (self.home / ".config/task").mkdir(parents=True, exist_ok=True)
        (self.home / ".config/task/usage-policy-path").write_text(str(policy))

    def ramp_ledger(self):
        path = self.home / ".local/state/task/usage/overrides.jsonl"
        return [row for row in map(json.loads, path.read_text().splitlines()) if row.get("kind") == "ramp_exempt"] if path.exists() else []

    def test_ramp_lane_past_the_codex_ramp_is_refused_and_an_exempt_lane_is_ledgered(self):
        self.ramp_policy()
        self.readings(codex=(30, 167))  # day 1 of the window: allowance 20 %
        wt, out = self.pace_lane(builder="codex", code=4)
        self.assertIn("held back by usage pacing: codex past its daily ramp: weekly used 30 % > allowance 20 % (day 1", out)
        self.assertFalse((wt / ".work/lane.json").exists())
        self.assertEqual(self.ramp_ledger(), [])
        out = self.raw_task("lane", "proj", "5", "--ramp-exempt", "bogus", code=2)
        self.raw_task("lane", "proj", "5", "--ramp-exempt", "hold")
        self.assertEqual(self.wait_for_lane(wt)["model"], "gpt-6-luna")
        rows = self.ramp_ledger()
        self.assertEqual([(r["exemption"], r["vendor"], r["past_ramp"], r["override"]) for r in rows],
                         [("hold", "codex", True, "ramp-exempt hold")])
        self.assertIn("ramp_exempt on routine", self.raw_task("status", "proj"))

    def test_ramp_leaves_claude_lanes_unchanged(self):
        self.ramp_policy("ahead_margin = 90\n")  # 50 % on day 1 is past a 20 %/day ramp; keep it on pace
        self.readings(claude=(50, 167), codex=(30, 167))
        wt, out = self.pace_lane()
        self.assertNotIn("ramp", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "sonnet")

    def test_ramp_review_and_redteam_on_codex_are_refused_unless_exempt(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.raw_task("start", "proj", "5", "--builder", "claude", "--refresh")  # Codex reviews and red-teams
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'exercise the codex ramp')
        head = self.git(wt, "rev-parse", "HEAD")
        self.raw_task("check", "proj", "5")
        self.ramp_policy()
        self.readings(codex=(45, 6 * 24 - 3))  # day 2: allowance 40 %
        out = self.raw_task("review", "proj", "5", "--run", code=4)
        self.assertIn("codex past its daily ramp: weekly used 45 % > allowance 40 % (day 2", out)
        self.assertFalse((wt / ".work/reviewer-call.json").exists())
        reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: ok\n#2 pass: ok\n```\n"
        out = self.raw_task("review", "proj", "5", "--run", "--ramp-exempt", "security", env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertIn("override recorded: ramp-exempt security on codex past its daily ramp", out)
        brief = self.home / "brief.md"
        brief.write_text("Attack the task.\n")
        out = self.raw_task("redteam", "proj", "5", "--brief", str(brief), code=4)
        self.assertIn("task redteam: held back by usage pacing: codex past its daily ramp", out)
        binary = self.home / "bin/codex"
        binary.write_text('#!/usr/bin/env python3\nimport pathlib, sys\nsys.stdin.read()\n'
                          'pathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text("Reviewed risks\\n")\n')
        self.raw_task("redteam", "proj", "5", "--brief", str(brief), "--ramp-exempt", "money")
        self.assertEqual(json.loads((self.state() / "redteam.json").read_text())["vendor"], "codex")
        self.assertEqual([(r["exemption"], r["past_ramp"]) for r in self.ramp_ledger()], [("security", True), ("money", True)])

    def test_usage_prints_the_codex_ramp_and_the_fallback_block(self):
        self.readings(claude=(50, 84), codex=(10, 167), five=10)
        self.assertNotIn("fallback:", self.raw_task("usage"))
        self.ramp_policy('blocked = { claude = "hook defect T-70" }\nbogus = 1\n')
        self.readings(claude=(50, 84), codex=(30, 167), five=10)
        out = self.raw_task("usage")
        self.assertIn("  ramp: day 1, allowance 20 % (20 %/day, ceiling 80 %); past ramp", out)
        self.assertIn("unknown key bogus; ignored, other keys kept", out)
        self.assertIn("fallback:\n  claude blocked by a foundation defect (hook defect T-70): builds -> wait; reviews of Codex work -> wait\n"
                      "  codex past ramp (day 1, 20 %): builds -> wait; reviews of Claude work -> wait", out)

    def reset_feed(self, **event):
        """Watcher state holding one feed event (decision-24); times are relative to now."""
        now = datetime.now(timezone.utc)
        event = dict(announced_at=(now - timedelta(hours=8)).isoformat(), event_status="confirmed", timing_kind="announced",
                     **{key: value if value is None else (now + value).isoformat() for key, value in event.items()})
        path = self.home / ".local/state/task/codex-resets.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": now.isoformat(), "ok": True, "source": "fixture", "events": {"e": event}}))

    def spend_ledger(self):
        path = self.home / ".local/state/task/usage/overrides.jsonl"
        return [row for row in map(json.loads, path.read_text().splitlines()) if row.get("kind") == "spend_down"] if path.exists() else []

    def test_spend_down_codex_lane_past_ramp_and_ceiling_with_any_model_starts_and_is_ledgered(self):
        self.ramp_policy()
        self.readings(codex=(95, 167))  # day 1: allowance 20 %, ceiling 80 %
        self.reset_feed(target_date=timedelta(hours=20), completed_at=None)
        self.assertIn("  spend-down until ", self.raw_task("usage"))
        wt, out = self.pace_lane("--model", "gpt-6-astra", builder="codex")  # a design-only model on a routine task
        self.assertNotIn("held back", out)
        self.assertIn("override recorded: decision-24 spend-down on codex until", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "gpt-6-astra")
        rows = self.spend_ledger()
        self.assertEqual([(r["override"], r["vendor"], r["model"], r["past_ramp"], r["model_guard"]) for r in rows],
                         [("decision-24 spend-down", "codex", "gpt-6-astra", True, True)])
        self.assertIn("spend_down on routine", self.raw_task("status", "proj"))

    def test_spend_down_ends_when_the_reset_landed_or_its_target_passed(self):
        self.ramp_policy()
        self.readings(codex=(30, 167))  # read after the reset: day 1, past the 20 % allowance
        for event in (dict(target_date=timedelta(hours=20), completed_at=timedelta(hours=-2)),
                      dict(target_date=timedelta(hours=-2), completed_at=None)):
            with self.subTest(event=event):
                self.reset_feed(**event)
                out = self.raw_task("usage")
                self.assertNotIn("spend-down until", out)
                self.assertIn("  ramp: day 1, allowance 20 % (20 %/day, ceiling 80 %); past ramp", out)
        wt, out = self.pace_lane(builder="codex", code=4)
        self.assertIn("held back by usage pacing: codex past its daily ramp", out)
        self.assertEqual(self.spend_ledger(), [])

    def test_spend_down_feed_down_fails_closed_with_one_warning_and_claude_is_unaffected(self):
        self.ramp_policy("ahead_margin = 90\n")
        self.readings(claude=(50, 167), codex=(30, 167))
        self.reset_feed(target_date=timedelta(hours=20), completed_at=None)
        out = self.raw_task("usage")
        self.assertFalse(any(line.startswith("claude") and "spend-down" in line for line in out.splitlines()))
        wt, out = self.pace_lane("--model", "fable", code=1)  # Claude's guard still refuses a hard-only model
        self.assertIn("fable is for Tier: hard tasks only", out)
        out = self.raw_task("lane", "proj", "5")
        self.assertNotIn("spend-down", out)
        self.assertEqual(self.wait_for_lane(wt)["model"], "sonnet")
        self.assertEqual(self.spend_ledger(), [])
        state = self.home / ".local/state/task/codex-resets.json"
        state.write_text(json.dumps(dict(json.loads(state.read_text()), ok=False)))
        out = self.raw_task("usage")
        self.assertEqual(out.count("codex reset feed"), 1)
        self.assertIn("pace warning: codex reset feed down or unparseable; no spend-down, the codex ramp applies", out)
        self.assertIn("past ramp", out)

    def test_pace_review_holds_exhausted_reviewer_and_final_goes_deeper_when_under_used(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.raw_task('risk', 'proj', '5', 'high', '--reason', 'exercise high review pacing')
        head = self.git(wt, "rev-parse", "HEAD")
        self.raw_task("check", "proj", "5")
        self.readings(claude=(80, 84))
        out = self.raw_task("review", "proj", "5", "--run", code=4)
        self.assertIn("held back by usage pacing: claude automated share used up", out)
        self.assertFalse((wt / ".work/reviewer-call.json").exists())
        self.readings(claude=(40, 30), five=10)
        reply = f"```review\nVerdict: PASS\nCommit: {head}\n#1 pass: ok\n#2 pass: ok\n```\n"
        self.raw_task("review", "proj", "5", "--run", env=dict(self.env, REVIEW_STUB_REPLY=reply))
        self.assertEqual(json.loads((self.state() / "review.json").read_text())["model"], "haiku")
        self.raw_task("review", "proj", "5", "--run", "--final", env=dict(self.env, REVIEW_STUB_REPLY=reply))
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
                               model="opus", no_boost=True, _worker=True, timeout=None, override="", effort="high")
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(TASK_MODULE, "HOME", self.home), \
                mock.patch.object(TASK_MODULE, "atomic_json", side_effect=observe):
            self.assertEqual(TASK_MODULE.lane(args), 0)
        self.assertEqual(len(terminal), 1)
        self.assertEqual(json.loads(record.read_text())["state"], "finished")

    def test_worker_signals_interrupt_release_claim_and_stop_builder_group(self):
        self.stub_models()
        wt, _ = self.started()
        self.risk_policy_fixture('[review.normal]\nmax_rounds = 4\n')
        work = wt / '.work'
        (work / 'prompt.md').write_text('task')
        env = dict(self.env, LANE_STUB_RELEASE=str(self.home / 'never-release'))
        for vendor in ('codex', 'claude'):
            for sig in (signal.SIGTERM, signal.SIGHUP):
                with self.subTest(vendor=vendor, signal=sig):
                    for name in ('model-call.json', 'descendant.pid'):
                        (work / name).unlink(missing_ok=True)
                    self.raw_task('lane', 'proj', '5', '--builder', vendor, env=env)
                    self.wait_for_lane(wt, finished=False)
                    deadline = time.monotonic() + 5
                    while not (work / 'descendant.pid').exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((work / 'descendant.pid').exists())
                    run = json.loads((work / 'lane.json').read_text())
                    child_group = run['child_pgid']
                    claim = self.home / f'.local/state/task/usage/running/{vendor}/{run["pid"]}.json'
                    self.assertTrue(claim.exists())
                    # Signal only the worker; cleanup must stop its separately running builder.
                    os.kill(run['pid'], sig)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        run = json.loads((work / 'lane.json').read_text())
                        if run['state'] != 'running':
                            break
                        time.sleep(0.02)
                    self.assertEqual((run['state'], run['exit_code']), ('interrupted', None))
                    self.assertFalse(claim.exists())
                    self.assert_group_stopped(child_group)
                    self.assert_group_stopped(run['pid'])
                    logs = [json.loads(line) for line in (self.home / '.local/state/task/runs.jsonl').read_text().splitlines()]
                    self.assertEqual(logs[-1]['outcome'], 'interrupted')

    def test_reviewer_signals_stop_live_group_before_releasing_claim(self):
        self.stub_reviewers()
        wt, _ = self.started()
        self.commit_in(wt, 'b.txt')
        self.prepare_merge_evidence('5')
        work = wt / '.work'
        env = dict(self.env, REVIEW_STUB_DELAY='30', REVIEW_STUB_CHILD='1')
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=sig):
                (work / 'reviewer-child.pid').unlink(missing_ok=True)
                with (self.home / 'review-process.log').open('w') as output:
                    process = subprocess.Popen([sys.executable, str(TOOL), 'review', 'proj', '5', '--run'],
                                               cwd=self.home, env=env, stdout=output, stderr=output)
                    try:
                        deadline = time.monotonic() + 5
                        while not (work / 'reviewer-child.pid').exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue((work / 'reviewer-child.pid').exists(),
                                        (self.home / 'review-process.log').read_text())
                        child = json.loads((work / 'reviewer-call.json').read_text())['pid']
                        unit_pid = json.loads((self.state() / 'review-run.json').read_text())['pid']
                        claim = self.home / f'.local/state/task/usage/running/claude/{unit_pid}.json'
                        self.assertTrue(claim.exists())
                        process.send_signal(sig)
                        process.wait(timeout=5)
                        self.assertNotEqual(process.returncode, 0)
                        self.assertFalse(claim.exists())
                        self.assert_group_stopped(child)
                        self.assertEqual(json.loads((self.state() / 'review-run.json').read_text())['state'], 'interrupted')
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()

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
                    TASK_MODULE.task_usage.claim(self.home, 'claude', 'lane', os.getpid())
                    claim = self.home / f'.local/state/task/usage/running/claude/{os.getpid()}.json'
                    run = {"state": "running", "temporary": str(temporary), 'builder': 'claude', 'pid': os.getpid()}
                    record.write_text(json.dumps(run))
                    publish = TASK_MODULE.atomic_json

                    def observe(path, value):
                        self.assertFalse(temporary.exists())
                        self.assertFalse(claim.exists())
                        self.assertEqual(json.loads(record.read_text())["state"], "running")
                        publish(path, value)

                    with mock.patch.object(TASK_MODULE, "claude_temporary_root", return_value=root), \
                            mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                            mock.patch.object(TASK_MODULE, "atomic_json", side_effect=observe):
                        TASK_MODULE.finalize_lane_record(record, run, state)
                    self.assertEqual(json.loads(record.read_text())["state"], state)

            for error in (PermissionError("denied"), TASK_MODULE.Stop("unsafe path")):
                with self.subTest(state=state, error=type(error).__name__), \
                        mock.patch.object(TASK_MODULE, "cleanup_lane_temporary", side_effect=error), \
                        contextlib.redirect_stderr(io.StringIO()):
                    TASK_MODULE.finalize_lane_record(record, {}, state)
                    self.assertEqual(json.loads(record.read_text())["state"], state)

    def test_reviewer_releases_claim_on_success_failure_interruption_and_refusal(self):
        wt, _ = self.started()
        prompt, reply = self.state() / 'prompt.md', self.state() / 'reply.md'
        prompt.write_text('review')
        claim = self.home / f'.local/state/task/usage/running/claude/{os.getpid()}.json'
        for result in (0, 3, KeyboardInterrupt(), subprocess.TimeoutExpired('claude', 1), OSError('refused')):
            with self.subTest(result=result):
                process = mock.Mock(pid=os.getpid())
                def wait(**kwargs):
                    self.assertTrue(claim.exists())
                    if isinstance(result, BaseException):
                        raise result
                    return result
                process.wait.side_effect = wait
                # A timeout's second wait reaps the killed child.
                if isinstance(result, subprocess.TimeoutExpired):
                    def timed_wait(**kwargs):
                        if kwargs:
                            return wait(**kwargs)
                        return -9
                    process.wait.side_effect = timed_wait
                spawn = {'side_effect': result} if isinstance(result, OSError) else {'return_value': process}
                expected = contextlib.nullcontext() if isinstance(result, int) else self.assertRaises(
                    KeyboardInterrupt if isinstance(result, KeyboardInterrupt) else TASK_MODULE.Stop)
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(TASK_MODULE, 'HOME', self.home), \
                        mock.patch.object(TASK_MODULE.subprocess, 'Popen', **spawn), \
                        mock.patch.object(TASK_MODULE, 'main_repo', return_value=self.repo), \
                        mock.patch.object(TASK_MODULE.os, 'killpg'), expected:
                    code = TASK_MODULE.run_reviewer('claude -p --model haiku', wt, prompt, reply, unit='task-review-proj-t5-test')
                    if isinstance(result, int): self.assertEqual(code, result)
                self.assertFalse(claim.exists())

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
                                            "--allowedTools", "Bash Read Write Edit Glob Grep WebSearch WebFetch",
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
        self.risk_policy_fixture('[review.normal]\nmax_rounds = 10\n')
        import json
        self.stub_models()
        wt, _ = self.started()
        (wt / ".work/prompt.md").write_text("task\n")
        # Fable follows the recorded tier, so the task is recorded as hard first.
        self.raw_task("start", "proj", "5", "--builder", "codex", "--tier", "hard", "--override", "Jacopo 2026-10-21: tier")
        for model, cli in TASK_MODULE.MODELS["claude"].items():
            tier = ("--tier", "hard") if cli == "fable" else ()
            out = self.raw_task("lane", "proj", "5", "--builder", "claude", "--model", model, *tier)
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
        self.assertIn("Mandatory read: .work/context/lane-contract.md", out)
        self.assertNotIn("heredocs", out)

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

    def test_agent_environment_explicit_allowlist_removes_runner_ownership(self):
        blocked = {name: "blocked" for name in TASK_MODULE.GIT_OVERRIDE_ENV}
        blocked.update(GIT_CONFIG="blocked", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="override",
                       GIT_CONFIG_VALUE_0="value", GIT_CONFIG_FUTURE="blocked", TASK_RUN_ID="runner",
                       TASK_GATE_RUN_ID="gate", TASK_WORKTREE="/stale/runner")
        kept = {"GIT_AUTHOR_NAME": "builder", "GIT_AUTHOR_EMAIL": "builder@example.invalid",
                "GIT_COMMITTER_NAME": "builder", "GIT_COMMITTER_EMAIL": "builder@example.invalid",
                "GIT_OPTIONAL_LOCKS": "0", "PATH": "tools"}
        blocked.update(GIT_FUTURE_VARIABLE='value', CODEX_THREAD_ID='runner')
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
        make_read_only(self, exclude_path, 0o444)
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

    def test_merge_flags_cooldown_config_files_as_dependency_changes(self):
        wt, _ = self.started()
        self.commit_in(wt, ".npmrc", "min-release-age=0\n")
        self.commit_in(wt, "uv.toml", 'exclude-newer = "2099-01-01T00:00:00Z"\n')
        self.prepare_merge_evidence("5")
        out = self.task("merge", "proj", "5")
        self.assertIn("WARNING: dependency files changed (", out)
        self.assertIn(".npmrc", out)
        self.assertIn("uv.toml", out)

    def test_merge_flags_tool_uv_table_in_pyproject(self):
        wt, _ = self.started()
        self.commit_in(wt, "pyproject.toml", '[tool.uv]\nexclude-newer = "2099-01-01T00:00:00Z"\n')
        self.prepare_merge_evidence("5")
        out = self.task("merge", "proj", "5")
        self.assertIn("WARNING: dependency files changed (pyproject.toml)", out)

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
        for name in ("bash", "git", "python3", "systemd-run", "systemctl"):
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
        self.assertIn("Mandatory read:", prompt)

    def test_review_prints_claude_headless_command_and_persisted_prompt(self):
        wt, _ = self.started()
        out = self.raw_task("review", "proj", "5")
        prompt = self.state() / "review-prompt.md"
        reply = self.state() / "review-response.md"
        self.assertIn(f"claude -p --add-dir {self.kb} --permission-mode dontAsk --model haiku --effort high "
                      "--allowedTools Read Grep Glob WebSearch WebFetch 'Bash(git --no-optional-locks:*)' <", out)
        self.assertIn(f"{prompt} > {reply}", out)
        self.assertIn(f"task review proj 5 --record {reply}", out)
        self.assertIn("Worktree:", prompt.read_text())
        routing = json.loads((self.state() / "review-routing.json").read_text())
        self.assertEqual({k: routing[k] for k in ('vendor', 'model')}, {"vendor": "claude", "model": "haiku"})
        self.assertEqual(routing['usage_run']['kind'], 'review')
        self.assertTrue(prompt.read_text().endswith(
            f"Mandatory read: {wt / '.work/context/lane-contract.md'}. Reply in its review block with the "
            "full 40-character Commit sha, then one line per criterion: "
            "`#N pass|fail|unclear: <one line of evidence>`.\n"))
        self.assertNotIn("Split findings", prompt.read_text())
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
        if builder == "claude":
            self.assertIn('web_search="live"', call["args"])
            self.assertEqual(call["args"][call["args"].index('web_search="live"') - 1], "-c")
        else:
            self.assertIn("WebSearch", call["args"])
            self.assertIn("WebFetch", call["args"])
        self.assertEqual(call["cwd"], str(wt))
        self.assertEqual(call["prompt"], prompt)
        if builder == "claude":
            self.assertEqual(call["git_optional_locks"], "0")
        self.assertEqual((self.state() / "review-response.md").read_text(), reply)
        run = json.loads((self.state() / "review-run.json").read_text())
        self.assertRegex(run['unit'], r'^task-review-proj-t5-[a-f0-9]+$')
        self.assertGreater(run['pid'], 0)
        self.assertTrue(run['process_start'])
        self.assertEqual({key: run[key] for key in ('builder', 'state', 'exit_code')},
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
        run = json.loads((self.state() / 'review-run.json').read_text())
        self.assertRegex(run['unit'], r'^task-review-proj-t5-[a-f0-9]+$')
        self.assertEqual({key: run[key] for key in ('builder', 'state', 'exit_code')},
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

    def git_paths(self, wt):
        common = Path(self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        return common, common / "worktrees" / wt.name

    def test_start_records_git_paths_from_the_main_checkout_and_old_tasks_migrate(self):
        wt, _ = self.started()
        common, git_dir = self.git_paths(wt)
        record = self.state() / "git-paths.json"
        self.assertEqual(json.loads(record.read_text()), {"common": str(common), "git_dir": str(git_dir), "dot_git": str(wt / ".git")})
        record.unlink()
        with mock.patch.object(TASK_MODULE, "HOME", self.home):
            self.assertEqual(TASK_MODULE.tamper_reason(TASK_MODULE.Task(self.repo, "5")), "")
        self.assertEqual(json.loads(record.read_text())["git_dir"], str(git_dir))

    def test_codex_lane_grants_stop_without_a_worktree_git_dir_or_with_worktree_config_on(self):
        with mock.patch.object(TASK_MODULE, "HOME", self.home):
            with self.assertRaisesRegex(TASK_MODULE.Stop, "does not exist"):
                TASK_MODULE.lane_git_grants(TASK_MODULE.Task(self.repo, "5"))
        self.stub_models()
        wt, _ = self.started()
        (wt / ".work/prompt.md").write_text("Build.\n")
        self.git(self.repo, "config", "extensions.worktreeConfig", "true")
        self.assertIn("extensions.worktreeConfig is on", self.raw_task("lane", "proj", "5", code=1))
        self.assertFalse((wt / ".work/model-call.json").exists())

    def test_worktree_config_file_matters_only_with_the_extension_on(self):
        wt, _ = self.started()
        _, git_dir = self.git_paths(wt)
        task = TASK_MODULE.Task(self.repo, "5")
        with mock.patch.object(TASK_MODULE, "HOME", self.home):
            (git_dir / "config.worktree").write_text("")
            self.assertEqual(TASK_MODULE.tamper_reason(task), "")
            (git_dir / "config.worktree").write_text("[core]\n\thooksPath = /x\n")
            self.assertEqual(TASK_MODULE.tamper_reason(task), "", "ignored while the extension is unset")
            self.git(self.repo, "config", "extensions.worktreeConfig", "true")
            self.assertIn("config.worktree must be empty", TASK_MODULE.tamper_reason(task))
            (git_dir / "config.worktree").write_text("")
            self.assertEqual(TASK_MODULE.tamper_reason(task), "")

    def test_lane_worker_end_marks_rewritten_git_metadata_tampered_and_entries_refuse_first(self):
        self.stub_models()
        wt, _ = self.started()
        _, git_dir = self.git_paths(wt)
        (wt / ".work/prompt.md").write_text("Build.\n")
        for target in (git_dir / "commondir", git_dir / "gitdir", wt / ".git"):
            with self.subTest(target=target.name):
                original = target.read_text()
                self.env["LANE_STUB_TAMPER"] = str(target)
                self.raw_task("lane", "proj", "5", "--no-pace")
                self.wait_for_lane(wt, state="tampered")
                self.env.pop("LANE_STUB_TAMPER")
                for command in (("check", "proj", "5"), ("check", "proj", "5", "--detach"), ("merge", "proj", "5"),
                                ("review", "proj", "5", "--run")):
                    out = self.raw_task(*command, code=1)
                    self.assertIn("Git metadata tampered: ", out)
                    self.assertNotIn("fatal", out, "refused before any Git ran in the worktree")
                out = self.raw_task("check", cwd=wt, code=1)
                self.assertIn("Git metadata tampered", out)
                self.assertNotIn("fatal", out)
                target.write_text(original)
        self.assertEqual(self.git(wt, "status", "--porcelain"), "")

    def test_a_normal_lane_passes_the_metadata_check_and_a_removed_worktree_is_skipped(self):
        self.stub_models()
        wt, _ = self.started()
        (wt / ".work/prompt.md").write_text("Build.\n")
        self.raw_task("lane", "proj", "5", "--no-pace")
        self.assertEqual(self.wait_for_lane(wt)["state"], "finished")
        with mock.patch.object(TASK_MODULE, "HOME", self.home):
            self.assertEqual(TASK_MODULE.tamper_reason(TASK_MODULE.Task(self.repo, "5")), "")
            self.assertEqual(TASK_MODULE.tamper_reason(TASK_MODULE.Task(self.repo, "9")), "")

    def test_batch_runner_stops_on_a_tampered_lane_before_any_git(self):
        loader = importlib.machinery.SourceFileLoader("batch_tamper_test", str(ROOT / "home/dot_local/bin/executable_task-batch"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        batch = importlib.util.module_from_spec(spec)
        loader.exec_module(batch)
        work = self.home / "work"
        work.mkdir()
        (work / "lane.json").write_text(json.dumps({"state": "tampered", "started_at": TASK_MODULE.utc_now()}))
        with self.assertRaisesRegex(batch.Stop, "tampered"):
            batch.Batch.lane_outcome(None, work)
        source = (ROOT / "home/dot_local/bin/executable_task-batch").read_text()
        self.assertIn('.get("state") in {"running", "tampered"}', source, "a resumed tampered lane also stops before finished()")

    def test_realign_and_review_run_commands_get_no_git_grants(self):
        source = (ROOT / "home/dot_local/bin/executable_task").read_text()
        self.assertIn("GIT_OPTIONAL_LOCKS=0 codex exec -c 'default_permissions=\\\"review\\\"' -c 'web_search=\\\"live\\\"' -m gpt-6-luna", source)
        self.assertEqual(source.count("lane_git_grants("), 2)  # definition and the one lane call

    def test_review_prints_read_only_codex_command_for_claude_builder(self):
        self.raw_task("start", "proj", "5", "--builder", "claude")
        out = self.raw_task("review", "proj", "5")
        prompt = self.state() / "review-prompt.md"
        reply = self.state() / "review-response.md"
        self.assertIn(f"GIT_OPTIONAL_LOCKS=0 codex exec -c 'default_permissions=\"review\"' -c 'web_search=\"live\"' --add-dir {self.kb} "
                      "-m gpt-6-luna -c model_reasoning_effort=high - <", out)
        self.assertIn(f"{prompt} > {reply}", out)
        self.assertIn(f"task review proj 5 --record {reply}", out)
        for command in ("git --no-optional-locks diff", "git --no-optional-locks log",
                        "git --no-optional-locks show"):
            self.assertIn(command, prompt.read_text())

    def test_direct_review_commands_do_not_add_kb_directory_without_backlog_file(self):
        self.raw_task("start", "proj", "direct-request", "--builder", "claude")
        out = self.raw_task("review", "proj", "direct-request")
        self.assertIn("codex exec -c 'default_permissions=\"review\"'", out)
        self.assertNotIn("--sandbox", out)
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

    def base_branch(self, name, file="t33.txt", text="scorer\n"):
        self.git(self.repo, "switch", "-q", "-c", name)
        self.commit_in(self.repo, file, text)
        self.git(self.repo, "switch", "-q", "main")

    def test_start_base_merges_declared_branch_and_lane_hook_allows_only_it(self):
        self.git(self.repo, "remote", "remove", "origin")
        self.base_branch("t33-scorer")
        self.base_branch("other", "other.txt")
        out = self.task("start", "proj", "T-5", "--base", "t33-scorer")
        wt = self.repo / ".worktrees/t5"
        self.assertIn("Merged base t33-scorer into t5-add-the-b-file.", out)
        self.assertEqual((wt / "t33.txt").read_text(), "scorer\n")
        self.assertFalse((wt / "other.txt").exists())
        record = json.loads((self.state() / "task.json").read_text())
        self.assertEqual(record["bases"], ["t33-scorer"])
        self.assertEqual(record["revision"], TASK_MODULE.record_revision(record))
        # Resuming keeps the declaration; a new commit on the base is merged again.
        self.git(self.repo, "switch", "-q", "t33-scorer")
        self.commit_in(self.repo, "t33.txt", "scorer v2\n")
        self.git(self.repo, "switch", "-q", "main")
        self.assertIn("Merged base t33-scorer", self.task("start", "proj", "T-5", "--base", "t33-scorer"))
        self.assertEqual((wt / "t33.txt").read_text(), "scorer v2\n")
        self.assertNotIn("Merged base", self.task("start", "proj", "T-5"))
        self.assertEqual(json.loads((self.state() / "task.json").read_text())["bases"], ["t33-scorer"])
        # The lane hook reads that private record: the declared base merges, others are refused.
        spec = importlib.util.spec_from_file_location("agent_policy_t70", ROOT / "system/agent-policy/pre_tool_use.py")
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        def judge(tool, data):
            event = {"tool_name": tool, "tool_input": data, "cwd": str(wt)}
            with mock.patch.object(policy.Path, "home", return_value=self.home), \
                    mock.patch.dict(os.environ, {"TASK_WORKTREE": str(wt.resolve())}):
                return policy.evaluate(event, (self.repo.parent,))
        self.assertIsNone(judge("Bash", {"command": "git merge --no-edit t33-scorer"}))
        self.assertEqual(judge("Bash", {"command": "git merge --no-edit other"}),
                         "In a repository without a remote, only git merge [--no-edit] main is allowed.")
        for command in ("git merge --no-edit refs/heads/t33-scorer", "git merge --no-edit t33-scorer~1",
                        "git merge --squash t33-scorer"):
            self.assertIsNotNone(judge("Bash", {"command": command}), command)
        # ...and the lane cannot add a base to that record itself.
        record_path = str(self.state() / "task.json")
        self.assertIsNotNone(judge("Write", {"file_path": record_path, "content": "{}"}))
        self.assertIsNotNone(judge("Edit", {"file_path": record_path, "old_string": "t33-scorer",
                                            "new_string": "other"}))
        self.assertIsNotNone(judge("Bash", {"command": f"sed -i s/t33-scorer/other/ {record_path}"}))
        if os.geteuid():  # root ignores the read-only mount's permission bits
            before = (self.state() / "task.json").read_text()
            self.sandbox_state()  # a lane sees private task state read-only, so task start refuses
            self.assertIn("Private task state is not writable",
                          self.task("start", "proj", "T-5", "--base", "other", cwd=wt, code=1))
            self.assertEqual((self.state() / "task.json").read_text(), before)
        self.assertIsNotNone(judge("Bash", {"command": "git merge --no-edit other"}))

    def test_start_base_refuses_bad_names_and_reports_conflicts(self):
        self.git(self.repo, "remote", "remove", "origin")
        self.base_branch("t33-scorer", "a.txt", "base side\n")
        for name in ("-x", "--strategy=ours", "t33-scorer:main", "../t33-scorer", "/tmp/t33-scorer",
                     "refs/heads/t33-scorer", "a..b", "t33-scorer~1", "main", "HEAD", "x.lock", "missing"):
            out = self.task("start", "proj", "T-5", f"--base={name}", code=1)
            self.assertIn("--base", out, name)
            self.assertFalse((self.repo / ".worktrees/t5").exists(), name)
        self.commit_in(self.repo, "a.txt", "main side\n")
        out = self.task("start", "proj", "T-5", "--base", "t33-scorer", code=1)
        wt = self.repo / ".worktrees/t5"
        self.assertIn("Merging base t33-scorer into t5-add-the-b-file failed; it was aborted", out)
        self.assertEqual(self.git(wt, "status", "--porcelain"), "")
        self.assertEqual((wt / "a.txt").read_text(), "main side\n")

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

    def checked_lane_then_main_edits(self, name, lane_text, main_text):
        """Check a lane that edits `name`, then move main with its own edit of the same file."""
        (self.repo / name).write_text("one\nmiddle\nthree\n")
        self.git(self.repo, "add", name)
        self.git(self.repo, "commit", "-qm", f"add {name}")
        self.git(self.repo, "push", "-q", "origin", "main")
        wt, _ = self.started()
        (wt / name).write_text(lane_text)
        self.git(wt, "commit", "-qam", f"lane edits {name}")
        self.prepare_merge_evidence("5")
        (self.home / "during-check.sh").write_text('echo run >> "$HOME/just-count"\n')
        (self.home / "during-check.sh").chmod(0o755)
        self.raw_task("check", "proj", "5")
        (self.repo / name).write_text(main_text)
        self.git(self.repo, "commit", "-qam", f"main edits {name}")
        self.git(self.repo, "push", "-q", "origin", "main")
        return wt

    def test_markdown_only_overlap_that_merges_cleanly_runs_the_gate_on_the_merged_result(self):
        self.checked_lane_then_main_edits("README.md", "lane\nmiddle\nthree\n", "one\nmiddle\nmain\n")
        out = self.raw_task("merge", "proj", "5")
        self.assertIn("reuse decision: running full gate because main changed only Markdown files", out)
        self.assertIn("Merged", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 2,
                         "the lane check is not reused; the gate runs on the merged result")
        self.assertEqual(self.git(self.repo, "show", "origin/main:README.md"), "lane\nmiddle\nmain")

    def test_non_markdown_overlap_that_merges_cleanly_still_blocks(self):
        wt = self.checked_lane_then_main_edits("code.py", "lane\nmiddle\nthree\n", "one\nmiddle\nmain\n")
        before = self.git(self.origin, "rev-parse", "main")
        out = self.raw_task("merge", "proj", "5", code=1)
        self.assertIn("main changed code.py that this task also changed; ask the lane to run `git merge origin/main`", out)
        self.assertEqual(len((self.home / "just-count").read_text().splitlines()), 1, "no gate ran")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), before)

    def test_markdown_overlap_with_a_conflict_still_blocks(self):
        self.checked_lane_then_main_edits("README.md", "lane\nmiddle\nthree\n", "main\nmiddle\nthree\n")
        before = self.git(self.origin, "rev-parse", "main")
        out = self.raw_task("merge", "proj", "5", code=1)
        self.assertIn("main changed README.md that this task also changed; ask the lane to run `git merge origin/main`", out)
        self.assertNotIn("Merged", out)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), before)

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

    def test_fenced_review_with_blocking_and_notes_inside_the_fence_parses(self):
        text = (REVIEW_FIXTURES / "fence-with-sections.md").read_text()
        self.assertEqual(TASK_MODULE.parse_review_reply(text, []),
                         ("PASS", "097ee880fcd0a472d3aabdc03de70db78d6dd8c3", None, {}))
        sha = "b" * 40
        fail = (f"```review\nVerdict: FAIL\nCommit: {sha}\n#1 fail: broken\n\n"
                "**Blocking**\n- #1: a.py:3 breaks\n\nNotes\n- none\n```\n")
        self.assertEqual(TASK_MODULE.parse_review_reply(fail, [1]),
                         ("FAIL", sha, None, {1: ("fail", "broken")}))
        with self.assertRaises(TASK_MODULE.ReviewParseError):
            TASK_MODULE.parse_review_reply(fail.replace("**Blocking**", "Findings"), [1])

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


class WatcherTests(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader('watch_fixture', str(TOOL.with_name('executable_task-watch')))
        self.watch = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.watch)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.report = self.write('reports/seat.md', 'report')
        self.write('.config/task/watch-reports.json', json.dumps({'seat-report': {'path': str(self.report), 'seconds': 7200}}))
        self.readings = mock.patch.object(self.watch.usage, 'readings', return_value={})
        self.readings.start()
        self.addCleanup(self.readings.stop)
        self.enterContext(mock.patch.dict(os.environ, TASK_RESET_FEEDS=''))  # no network in tests

    def write(self, name, text):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def scan(self):
        return self.watch.findings(self.home, self.now)

    def unit_properties(self, **overrides):
        props = dict(LoadState='loaded', ActiveState='active', SubState='waiting', TriggeredBy='', Unit='', Triggers='',
                     UnitFileState='enabled', NextElapseUSecRealtime='Thu 2026-10-22 14:01:00 CEST')
        props.update(overrides)
        return '\n'.join(f'{key}={value}' for key, value in props.items())

    def test_coverage_per_class_and_interim_sources(self):
        with mock.patch.object(sys, 'argv', ['task-watch', '--coverage']), contextlib.redirect_stdout(io.StringIO()) as out:
            self.watch.main()
        for kind in ('P1', 'P2', 'P3', 'P4', 'P5', 'seat-report', 'collector'):
            self.assertIn(kind, out.getvalue())
        for kind in ('P1', 'P3'):
            self.assertIn(kind + ' interim: company skill check', out.getvalue())
        p4 = next(line for line in out.getvalue().splitlines() if line.startswith('P4 '))
        for reason in ('failed', 'inactive', 'disabled', 'no next run', 'not found'):
            self.assertIn(reason, p4)
        self.assertEqual(self.scan(), {})

    def test_p2_existing_readers_reserve_weekly_and_five_hour(self):
        self.readings.stop()
        self.write('cortex/cortex-kb-company/policy/usage-pacing.toml', '[spare_resets]\ncodex = 0\n')
        future = self.now.timestamp() + 5 * 86400
        path = self.write('.local/state/task/usage/claude.json', json.dumps({'seven_day': {'at': self.now.isoformat(), 'utilization': .76, 'resetsAt': future}}))
        self.assertIn('P2:claude', self.scan())
        path.write_text(json.dumps({'seven_day': {'at': self.now.isoformat(), 'utilization': .10, 'resetsAt': future}, 'five_hour': {'at': self.now.isoformat(), 'utilization': 1., 'resetsAt': self.now.timestamp() + 3600}}))
        self.assertIn('P2:claude', self.scan())
        path.write_text('{}')
        self.assertNotIn('P2:claude', self.scan())

    def test_invalid_review_key_is_ignored_other_keys_kept_and_watching_continues(self):
        self.readings.stop()
        self.write('cortex/cortex-kb-company/policy/usage-pacing.toml',
                   '[reserve]\nclaude = 0\n[review.normal]\nunknown = 1\n')
        future = self.now.timestamp() + 5 * 86400
        self.write('.local/state/task/usage/claude.json', json.dumps({'seven_day': {
            'at': self.now.isoformat(), 'utilization': .80, 'resetsAt': future}}))
        self.write('.config/task/live-units', 'broken.timer\n')
        with mock.patch.object(self.watch, 'command', return_value=self.unit_properties(LoadState='not-found')), \
                contextlib.redirect_stderr(io.StringIO()) as warnings:
            found = self.scan()
        self.assertIn('invalid review.normal', warnings.getvalue())
        self.assertIn('ignored, other keys kept', warnings.getvalue())
        self.assertNotIn('built-in defaults used', warnings.getvalue())
        self.assertNotIn('P2:claude', found)  # the valid reserve.claude = 0 is kept (100 % threshold), so 80 % does not trip it.
        self.assertIn('P4:broken.timer', found)

    def test_p4_only_registered_units_and_errors_do_not_clear(self):
        self.write('.config/task/live-units', '  # comment\n\n product.service \nproduct.timer\n')
        with mock.patch.object(self.watch, 'command', side_effect=[self.unit_properties(ActiveState='failed'), self.unit_properties()]) as command:
            found = self.scan()
        self.assertEqual(list(found), ['P4:product.service'])
        self.assertEqual(command.call_count, 2)
        for call, unit in zip(command.call_args_list, ('product.service', 'product.timer')):
            self.assertEqual(call.args, ('systemctl', '--user', 'show', '--property=LoadState,ActiveState,SubState,TriggeredBy,Unit,Triggers,UnitFileState,NextElapseUSecRealtime', '--', unit))
        with mock.patch.object(self.watch, 'command', side_effect=subprocess.TimeoutExpired('systemctl', 20)):
            with self.assertRaises(subprocess.TimeoutExpired): self.scan()

    def test_p4_service_timer_and_missing_unit_health(self):
        cases = [
            ('simple.service', {}, None),
            ('simple.service', {'ActiveState': 'inactive'}, 'inactive'),
            ('simple.service', {'ActiveState': 'failed'}, 'failed'),
            ('oneshot.service', {'ActiveState': 'inactive', 'TriggeredBy': 'product.timer'}, None),
            ('oneshot.service', {'ActiveState': 'failed', 'TriggeredBy': 'product.timer'}, 'failed'),
            ('triggered.service', {'ActiveState': 'inactive', 'TriggeredBy': 'product.socket'}, None),
            ('product.timer', {}, None),
            ('product.timer', {'ActiveState': 'failed'}, 'failed'),
            ('product.timer', {'ActiveState': 'inactive'}, 'inactive'),
            ('product.timer', {'UnitFileState': 'disabled'}, 'disabled'),
            ('product.timer', {'UnitFileState': 'enabled-runtime'}, 'disabled'),
            ('product.timer', {'NextElapseUSecRealtime': ''}, 'no next run'),
            ('product.timer', {'NextElapseUSecRealtime': 'n/a'}, 'no next run'),
            ('missing.service', {'LoadState': 'not-found', 'ActiveState': 'inactive'}, 'not found'),
            ('missing.timer', {'LoadState': 'not-found', 'ActiveState': 'inactive'}, 'not found'),
        ]
        for unit, props, reason in cases:
            with self.subTest(unit=unit, props=props):
                self.write('.config/task/live-units', unit + '\n')
                with mock.patch.object(self.watch, 'command', return_value=self.unit_properties(**props)) as command:
                    found = self.scan()
                command.assert_called_once()
                if reason:
                    self.assertEqual(list(found), ['P4:' + unit])
                    self.assertEqual(found['P4:' + unit]['detail'], reason)
                else:
                    self.assertEqual(found, {})

    def test_p4_fake_systemctl_timer_running_unit_and_waiting_without_next_elapse(self):
        self.write('.config/task/live-units', 'product.timer\nproduct.service\n')
        properties = self.write('unit-properties.json', '{}')
        systemctl = self.write('bin/systemctl', '#!/usr/bin/python3\n'
                              'import json, os, pathlib, sys\n'
                              'home = pathlib.Path(os.environ["HOME"])\n'
                              'with (home / "unit-calls").open("a") as log: log.write(sys.argv[-1] + "\\n")\n'
                              'print(json.loads((home / "unit-properties.json").read_text())[sys.argv[-1]])\n')
        systemctl.chmod(0o755)
        env = dict(HOME=str(self.home), PATH=f'{systemctl.parent}:/usr/bin:/bin',
                   PYTHONDONTWRITEBYTECODE='1', TASK_RESET_FEEDS='')
        cases = [
            ({'SubState': 'running'}, 'activating', None),
            ({'SubState': 'running'}, 'inactive', None),
            ({}, 'activating', None),
            ({}, 'active', None),
            ({'NextElapseUSecRealtime': 'n/a'}, 'active', None),
            ({'Unit': '', 'Triggers': 'product.service'}, 'activating', None),
            ({}, 'inactive', 'no next run'),
            ({}, 'failed', 'no next run'),
            ({'SubState': 'running', 'UnitFileState': 'disabled'}, 'active', 'disabled'),
            ({'SubState': 'running', 'ActiveState': 'failed'}, 'active', 'failed'),
            ({'SubState': 'running', 'ActiveState': 'inactive'}, 'active', 'inactive'),
        ]
        for overrides, active, reason in cases:
            with self.subTest(timer=overrides, service=active):
                timer = dict(Unit='product.service', NextElapseUSecRealtime='')
                timer.update(overrides)
                properties.write_text(json.dumps({
                    'product.timer': self.unit_properties(**timer),
                    'product.service': self.unit_properties(ActiveState=active, TriggeredBy='product.timer'),
                }))
                (self.home / 'unit-calls').unlink(missing_ok=True)
                with mock.patch.dict(os.environ, env, clear=True):
                    found = self.scan()
                self.assertEqual(found.get('P4:product.timer', {}).get('detail'), reason)
                self.assertEqual(found.get('P4:product.service', {}).get('detail'),
                                 'failed' if active == 'failed' else None)
                self.assertEqual((self.home / 'unit-calls').read_text().splitlines(),
                                 ['product.timer', 'product.service'])

    def test_p4_fake_systemctl_reason_updates_and_recovery_close(self):
        self.write('.config/task/live-units', 'product.timer\n')
        properties = self.write('unit-properties', self.unit_properties(UnitFileState='disabled'))
        systemctl = self.write('bin/systemctl', '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/unit-calls"\ncat "$HOME/unit-properties"\n')
        systemctl.chmod(0o755)
        ping = self.write('.local/bin/job-ping', '#!/bin/sh\nexit 0\n')
        ping.chmod(0o755)
        env = dict(HOME=str(self.home), PATH=f'{systemctl.parent}:/usr/bin:/bin',
                   PYTHONDONTWRITEBYTECODE='1', TASK_HOLDER='watcher-test', TASK_RESET_FEEDS='')
        state = self.home / '.local/state/task'
        first_seen = None
        for props, reason in [({'UnitFileState': 'disabled'}, 'disabled'),
                              ({'UnitFileState': 'disabled'}, 'disabled'),
                              ({'NextElapseUSecRealtime': ''}, 'no next run'), ({}, None), ({}, None)]:
            properties.write_text(self.unit_properties(**props))
            result = subprocess.run([sys.executable, TOOL.with_name('executable_task-watch')],
                                    cwd=self.home, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            snapshot = json.loads((state / 'watch-state.json').read_text())
            if reason:
                self.assertEqual(list(snapshot), ['P4:product.timer'])
                row = snapshot['P4:product.timer']
                self.assertEqual(row['detail'], reason)
                first_seen = first_seen or row['first_seen']
                self.assertEqual(row['first_seen'], first_seen)
            else:
                self.assertEqual(snapshot, {})
        rows = [json.loads(line) for line in (state / 'watch.jsonl').read_text().splitlines()]
        self.assertEqual([row['status'] for row in rows], ['open', 'closed'])
        self.assertEqual([row['detail'] for row in rows], ['disabled', 'no next run'])
        self.assertTrue(all(row['key'] == 'product.timer' and row['class'] == 'P4' for row in rows))
        calls = (self.home / 'unit-calls').read_text().splitlines()
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call.endswith('-- product.timer') for call in calls))

    def test_p5_company_and_product_asks_yaml_forms(self):
        for repo, labels in [('company', '[ask]'), ('product-harness', '\n  - "ask"')]:
            self.write(f'cortex/cortex-kb-{repo}/backlog/tasks/ask.md', f'---\nstatus: To Do\nlabels: {labels}\ndue_date: \'2000-01-01\'\n---\n')
        self.assertEqual(len(self.scan()), 2)
        for name, status, due, labels in [('done', 'Done', '2000-01-01', '[ask]'), ('future', 'To Do', '2999-01-01', '[ask]'), ('other', 'To Do', '2000-01-01', '[not-ask]'), ('today', 'To Do', str(self.now.astimezone(self.watch.usage.CPH).date()), '[ask]')]:
            self.write(f'cortex/cortex-kb-company/backlog/tasks/{name}.md', f'---\nstatus: {status}\nlabels: {labels}\ndue_date: {due}\n---\nlabels: [ask]\n')
        self.assertEqual(len(self.scan()), 2)

    def test_p5_reads_only_company_and_active_product_repositories(self):
        active = ('company', 'product-harness', 'household-signal-harness', 'madplanner-v2',
                  'consultancy-website', 'home-energy-lab')
        excluded = ('typo3-dkm', 'customer-harness', 'photo-organizer', 'unknown-product')
        for repo in active + excluded:
            self.write(f'cortex/cortex-kb-{repo}/backlog/tasks/ask.md',
                       '---\nstatus: To Do\nlabels: [ask]\ndue_date: 2000-01-01\n---\n')
        original = Path.read_text
        def read_allowed(path, *args, **kwargs):
            self.assertFalse(any('cortex-kb-' + repo in path.parts for repo in excluded), path)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read_allowed):
            found = self.scan()
        self.assertEqual({Path(row['source']).parts[-4] for row in found.values()},
                         {'cortex-kb-' + repo for repo in active})

    def test_installed_layout_real_scan_and_ping(self):
        installed = self.home / '.local/bin'
        installed.mkdir(parents=True)
        shutil.copy2(TOOL, installed / 'task')
        shutil.copy2(TOOL.with_name('executable_task-watch'), installed / 'task-watch')
        for helper in TOOL.parent.glob('task_*.py'):
            shutil.copy2(helper, installed / helper.name)
        self.assertFalse((installed / 'executable_task').exists())
        ping = self.write('.local/bin/job-ping', '#!/bin/sh\nprintf "%s %s\\n" "$1" "$2" >> "$HOME/pings"\n')
        ping.chmod(0o755)
        ask = self.write('cortex/cortex-kb-company/backlog/tasks/ask.md',
                         '---\nstatus: To Do\nlabels: [ask]\ndue_date: 2000-01-01\n---\n')
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'TASK_RESET_FEEDS': ''}
        result = subprocess.run([sys.executable, installed / 'task-watch'], cwd=self.home,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in (self.home / '.local/state/task/watch.jsonl').read_text().splitlines()]
        self.assertEqual([(row['class'], row['source'], row['status']) for row in rows],
                         [('P5', str(ask), 'open')])
        self.assertEqual((self.home / 'pings').read_text(), 'task-watch 0\n')

    def test_stale_missing_reports_collector_and_git_commit_age(self):
        self.now = datetime(2026, 10, 22, 12, tzinfo=self.watch.usage.CPH)
        os.utime(self.report, (0, 0))
        self.assertIn('seat-report:seat-report', self.scan())
        self.report.unlink()
        self.assertIn('seat-report:seat-report', self.scan())
        self.write('.config/task/watch-reports.json', json.dumps({'collector': {'path': 'missing-heartbeat', 'seconds': 3600}}))
        self.assertIn('collector:collector', self.scan())
        (self.home / '.config/task/watch-reports.json').unlink()
        self.write('cortex/cortex-kb-company/STATUS.md', 'report')
        with mock.patch.object(self.watch, 'command', return_value=str(int(self.now.timestamp() - 7900))) as command:
            self.assertIn('seat-report:seat-report', self.scan())
        self.assertIn('--format=%ct', command.call_args.args)

    def report_at(self, stamp, checked):
        os.utime(self.report, (stamp.timestamp(), stamp.timestamp()))
        self.now = checked
        return 'seat-report:seat-report' in self.scan()

    def test_report_overnight_age_excludes_night_and_handles_dst(self):
        for year, month, day in [(2026, 10, 22), (2026, 3, 28), (2026, 10, 24)]:
            stamp = datetime(year, month, day, 23, tzinfo=self.watch.usage.CPH)
            next_day = stamp.date() + timedelta(days=1)
            for hour, minute in [(1, 30), (6, 59), (9, 10), (9, 11)]:
                checked = datetime.combine(next_day, datetime.min.time(), self.watch.usage.CPH).replace(hour=hour, minute=minute)
                with self.subTest(stamp=stamp, checked=checked):
                    self.assertEqual(self.report_at(stamp, checked), hour == 9 and minute == 11)

    def test_report_daytime_gap_and_short_grace(self):
        stamp = datetime(2026, 10, 22, 10, tzinfo=self.watch.usage.CPH)
        for minute, stale in [(0, False), (10, False), (11, True)]:
            with self.subTest(minute=minute):
                self.assertEqual(self.report_at(stamp, stamp.replace(hour=12, minute=minute)), stale)

    def test_report_late_evening_age_resumes_in_morning(self):
        stamp = datetime(2026, 10, 22, 22, 30, tzinfo=self.watch.usage.CPH)
        self.assertFalse(self.report_at(stamp, stamp.replace(day=23, hour=7, minute=40)))
        self.assertTrue(self.report_at(stamp, stamp.replace(day=23, hour=8, minute=41)))

    def test_ledger_open_update_close_reopen_and_source_failure(self):
        state = self.home / '.local/state/task'
        os.utime(self.report, (0, 0))
        with mock.patch.object(Path, 'home', return_value=self.home), mock.patch.object(sys, 'argv', ['task-watch']):
            self.watch.main()
            first = json.loads((state / 'watch-state.json').read_text())
            self.watch.main()
            second = json.loads((state / 'watch-state.json').read_text())
            key = 'seat-report:seat-report'
            self.assertEqual(first[key]['first_seen'], second[key]['first_seen'])
            self.assertGreaterEqual(second[key]['last_seen'], first[key]['last_seen'])
            self.assertEqual(len((state / 'watch.jsonl').read_text().splitlines()), 1)
            with mock.patch.object(self.watch, 'findings', side_effect=ValueError('fixture')):
                with self.assertRaises(ValueError): self.watch.main()
            self.assertEqual(json.loads((state / 'watch-state.json').read_text()), second)
            os.utime(self.report, None)
            self.watch.main()
            self.watch.main()
            os.utime(self.report, (0, 0))
            self.watch.main()
        rows = [json.loads(line) for line in (state / 'watch.jsonl').read_text().splitlines()]
        self.assertEqual([row['status'] for row in rows], ['open', 'closed', 'open'])
        self.assertTrue(all(row['class'] == 'seat-report' and 'source' in row and 'last_seen' in row for row in rows))

    def test_codex_reset_feed_polled_hourly_with_fallback_and_never_fails_the_watch(self):
        fixtures = ROOT / 'ci/fixtures/usage'
        calls, sources = [], {'http://primary.test/api': (fixtures / 'codex-resets.json').read_bytes(),
                              'http://rss.test/feed.xml': (fixtures / 'codex-reset.xml').read_bytes()}
        def fetch(url):
            calls.append(url)
            if isinstance(sources[url], Exception): raise sources[url]
            return sources[url]
        state = self.home / '.local/state/task/codex-resets.json'
        os.environ['TASK_RESET_FEEDS'] = 'http://primary.test/api http://rss.test/feed.xml'
        with mock.patch.object(Path, 'home', return_value=self.home), mock.patch.object(sys, 'argv', ['task-watch']), \
                mock.patch.object(self.watch.usage, '_fetch', fetch):
            self.watch.main()
            self.watch.main()  # within the hour: no second poll
            self.assertEqual(calls, ['http://primary.test/api'])
            self.assertEqual(len(json.loads(state.read_text())['events']), 4)
            state.write_text(json.dumps(dict(json.loads(state.read_text()), checked_at='2000-01-01T00:00:00+00:00')))
            sources['http://primary.test/api'] = OSError('down')
            self.watch.main()
            self.assertEqual(json.loads(state.read_text())['source'], 'http://rss.test/feed.xml')
            self.assertEqual(len(json.loads(state.read_text())['events']), 6)
            state.write_text(json.dumps(dict(json.loads(state.read_text()), checked_at='2000-01-01T00:00:00+00:00')))
            sources['http://rss.test/feed.xml'] = OSError('down')
            self.watch.main()  # both down: the watch still runs, the state says so
            self.assertFalse(json.loads(state.read_text())['ok'])
        self.assertEqual(len(calls), 5)

    def test_cli_ping_success_failure_and_write_boundary(self):
        ping = self.write('.local/bin/job-ping', '#!/bin/sh\nprintf "%s %s\\n" "$1" "$2" >> "$HOME/pings"\n')
        ping.chmod(0o755)
        before = {str(p.relative_to(self.home)) for p in self.home.rglob('*') if p.is_file()}
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'TASK_RESET_FEEDS': ''}
        tool = TOOL.with_name('executable_task-watch')
        for expected in (0, 1):
            if expected: self.write('.config/task/watch-reports.json', '{broken')
            result = subprocess.run([sys.executable, tool], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual((self.home / 'pings').read_text(), 'task-watch 0\ntask-watch 1\n')
        added = {str(p.relative_to(self.home)) for p in self.home.rglob('*') if p.is_file()} - before
        self.assertTrue(all(p == 'pings' or p.startswith('.local/state/task/watch') for p in added), added)
        subprocess.run([sys.executable, tool, '--coverage'], env=env, check=True, capture_output=True)
        self.assertEqual((self.home / 'pings').read_text().count('\n'), 2)

    def test_timer_shipped_disabled_and_line_cap(self):
        units = ROOT / 'home/dot_config/systemd/user'
        self.assertIn('OnCalendar=*:0/15', (units / 'task-watch.timer').read_text())
        self.assertFalse((units / 'timers.target.wants/symlink_task-watch.timer').exists())
        self.assertLessEqual(len(TOOL.with_name('executable_task-watch').read_text().splitlines()), 85)


SMOKE_HELPER = ROOT / "home/dot_local/bin/executable_task-smoke"
# One stand-in binary per builder name: the reviewer prompt carries "Verdict:", the lane prompt does not.
SMOKE_AGENT = """#!/usr/bin/env python3
import os, pathlib, subprocess, sys
prompt = sys.stdin.read()


def git(*args):
    return subprocess.run(['git', '-c', 'user.name=lane', '-c', 'user.email=lane@example.invalid', *args],
                          capture_output=True, text=True)


if 'Verdict:' in prompt:
    head = git('rev-parse', 'HEAD').stdout.strip()
    verdict = os.environ.get('SMOKE_REVIEW_VERDICT', 'PASS')
    judged = 'pass' if verdict == 'PASS' else 'fail'
    print(f"```review\\nVerdict: {verdict}\\nCommit: {head}\\n#1 {judged}: smoke.txt contains ok\\n```\\nBlocking: none\\nNotes: none")
    sys.exit(0)
commands = [line[len('Run: '):] for line in prompt.splitlines() if line.startswith('Run: ')]
if commands:  # the real-repository lane (T-62): run each command and stop
    for command in commands:
        subprocess.run(command, shell=True)
    if '--output-last-message' in sys.argv:
        pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('done')
    sys.exit(0)
if 'uv run --no-project --with colorama==0.4.6 python smoke_check.py' in prompt:
    checked = subprocess.run(['uv', 'run', '--no-project', '--with', 'colorama==0.4.6',
                              'python', 'smoke_check.py'], capture_output=True, text=True)
    if checked.returncode:
        print(checked.stderr or checked.stdout, file=sys.stderr)
        sys.exit(1)
pathlib.Path('smoke.txt').write_text('ok\\n')
if os.environ.get('SMOKE_STUB_RO') == '1':
    # T-53's defect: the agent's commit is refused, and the agent stops under its stop rule.
    print("fatal: Unable to create '/home/agent/dev_workspace/proj/.git/worktrees/t1/index.lock': Read-only file system", file=sys.stderr)
    sys.exit(0)
for step in (['add', 'smoke.txt'], ['commit', '-qm', 'smoke: write smoke.txt'], ['merge', '-q', '--no-edit', 'main']):
    done = git(*step)
    if done.returncode:
        print(done.stderr, file=sys.stderr)
        sys.exit(1)
pathlib.Path('.work/report.md').write_text("# Task report\\n\\n## What changed\\n\\nsmoke.txt.\\n\\n## How I verified\\n\\nSteps in order.\\n\\n"
                                           "## Criteria\\n\\n#1 met: smoke.txt contains ok\\n\\n## Not done\\n\\n(none)\\n\\n"
                                           "## Noticed, not done\\n\\n(none)\\n")
with pathlib.Path('.work/report.md').open('a') as report:
    report.write(os.environ.get('SMOKE_WEB_LOOKUP', 'Web lookup: astral-sh/uv release v-test https://github.com/astral-sh/uv/releases') + '\\n')
if '--output-last-message' in sys.argv:
    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('done')
"""


class SmokeTests(unittest.TestCase):
    """task smoke on a temporary HOME with stand-in agents: no real Codex, Claude, systemd or GitHub."""

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='smoke-')))
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        install_units(self.home)
        installed = self.home / '.local/bin/task'
        installed.parent.mkdir(parents=True)
        installed.symlink_to(TOOL)
        stubs = {'just': STUBS['just'], 'gh': STUBS['gh'], 'pre-commit': STUBS['pre-commit'],
                 'backlog': STUBS['backlog'], 'codex': SMOKE_AGENT, 'claude': SMOKE_AGENT}
        for name, body in stubs.items():
            (bin_dir / name).write_text(textwrap.dedent(body))
            (bin_dir / name).chmod(0o755)
        uv = bin_dir / 'uv'
        uv.write_text("#!/bin/sh\n[ \"$SMOKE_UV_FAIL\" = 1 ] && { echo 'error: uv cache is read-only' >&2; exit 1; }\n"
                      "[ -n \"$UV_CACHE_DIR\" ] && { [ -e \"$UV_CACHE_DIR/.read-only\" ] || ! touch \"$UV_CACHE_DIR/.probe\"; } 2>/dev/null && { echo \"error: Failed to initialize cache at "
                      "$UV_CACHE_DIR: Read-only file system (os error 30)\" >&2; exit 2; }\n"
                      "[ \"$1 $2\" = 'run --no-project' ] || exit 2\n"
                      "echo \"$PWD\" >> \"$HOME/uv-runs\"\nexit 0\n")
        uv.chmod(0o755)
        shutil.copy(SMOKE_HELPER, bin_dir / 'task-smoke')
        (bin_dir / 'task-smoke').chmod(0o755)  # chezmoi sets this on install
        self.env = dict(HOME=str(self.home), PATH=f"{bin_dir}:{os.environ['PATH']}", **IDENTITY,
                        XDG_RUNTIME_DIR=str(self.home / 'runtime'),
                        DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(self.home / 'runtime/bus'),
                        TASK_SMOKE_TOOL=str(TOOL), TASK_HOLDER='test-seat',
                        TASK_GATE_DIR=str(self.home / 'shared-gate'), TASK_LANE_TEMP_ROOT=str(self.home / 'task-lane'))
        for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_THREAD_ID", "SMOKE_STUB_RO"):
            self.env.pop(marker, None)
        (self.home / 'shared-gate').mkdir()
        contract = self.home / '.agents/skills/slim-workflow/lane-contract.md'
        contract.parent.mkdir(parents=True)
        contract.write_text("# Fake lane contract\n")
        for real in (self.home / 'dev_workspace/company', self.home / 'dev_workspace/bootstrap'):
            self.real_repo(real)

    def real_repo(self, path, justfile="check:\n    echo ok\n", files=()):
        path.mkdir(parents=True)
        (path / 'justfile').write_text(justfile)
        for name, text in files:
            (path / name).write_text(text)
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'real fixture')):
            subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-C', str(path), *args],
                           check=True, capture_output=True)

    def smoke(self, *args, code=0, env=None):
        result = subprocess.run([sys.executable, str(self.home / 'bin/task-smoke'), *args], env=env or self.env,
                                cwd=self.home, capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def run_id(self, out):
        return re.search(r"(?m)^run: (\S+)$", out).group(1)

    def scratch(self, run):
        return {'repo': self.home / 'dev_workspace' / f'task-smoke-{run}',
                'kb': self.home / 'cortex' / f'cortex-kb-task-smoke-{run}',
                'state': self.home / '.local/state/task' / f'task-smoke-{run}',
                'real': self.home / 'dev_workspace' / f'task-smoke-{run}-real'}

    def smoke_module(self):
        loader = importlib.machinery.SourceFileLoader('task_smoke_web_test', str(SMOKE_HELPER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        return helper

    def test_both_builder_briefs_require_web_lookup_and_file_edit_tool(self):
        helper = self.smoke_module()
        for builder, (_, ident) in helper.BUILDERS.items():
            with self.subTest(builder=builder):
                prompt = helper.PROMPT.format(ident=ident, builder=builder)
                self.assertIn('with your file-edit tool', prompt)
                self.assertIn('never an inline interpreter (`python -c`, heredocs)', prompt)
                self.assertIn('with your web tool', prompt)
                self.assertIn('https://github.com/astral-sh/uv/releases', prompt)
                self.assertIn('Web lookup: <fact and source URL>', prompt)

    def test_lookup_report_line_must_be_present_and_nonempty_without_pinning_value(self):
        helper = self.smoke_module()
        wt = self.home / 'report-test'
        (wt / '.work').mkdir(parents=True)
        report = wt / '.work/report.md'
        for line in ('', 'Web lookup:', 'Web lookup:   ', 'Web lookup:\t',
                     'Web lookup:\n## Not done\nnone'):
            with self.subTest(line=line):
                report.write_text('#1 met: smoke.txt contains ok\n' + line + '\n')
                self.assertIn('Web lookup:', helper.report_error(wt, ''))
        for value in ('any changing public fact', 'v999 https://github.com/astral-sh/uv/releases'):
            report.write_text('#1 met: smoke.txt contains ok\nWeb lookup: ' + value + '\n')
            self.assertIsNone(helper.report_error(wt, ''))

    def test_held_lane_reports_skip_hold_not_timeout(self):
        marker = self.home / '.local/state/task/hold'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('unit admission hold')
        for style in ('exec-condition', 'success'):
            with self.subTest(style=style):
                (self.home / 'skip-style').write_text(style)
                out = self.smoke('--builder', 'claude', code=1)
                self.assertIn('SKIP hold', out)
                self.assertNotIn('still running', out)
                self.assertNotRegex(out, r'(?m)^  FAIL')
                self.assertRegex(out, r'smoke \S+: SKIP hold')
                self.run_id(out)

    def test_missing_lookup_fails_at_report_step_for_each_builder(self):
        for builder in ('codex', 'claude'):
            with self.subTest(builder=builder):
                out = self.smoke('--builder', builder, code=1, env=dict(self.env, SMOKE_WEB_LOOKUP=''))
                self.assertIn('FAIL report:', out)
                self.assertIn('Web lookup:', out)
                self.assertNotRegex(out, r'(?m)^  (PASS|FAIL) (check|review|merge)$')

    def test_stub_run_passes_every_step_for_both_builders(self):
        out = self.smoke()
        run = self.run_id(out)
        steps = ('start', 'setup', 'lane', 'commit', 'merge main', 'report', 'check', 'review', 'merge')
        for step in (*steps, 'real start', 'real lane', 'real company', 'real bootstrap'):
            self.assertEqual(len(re.findall(rf"(?m)^  PASS {step}$", out)), 2, (step, out))
        self.assertNotIn('real cortex-core', out, 'cortex-core is checked only when it exists with a justfile')
        self.assertNotIn('FAIL', out)
        record = json.loads((self.home / '.local/state/task/smoke' / f'{run}.json').read_text())
        self.assertEqual(record['result'], 'PASS')
        uv_runs = (self.home / 'uv-runs').read_text().splitlines()
        repo = self.scratch(run)['repo']
        self.assertEqual(len(uv_runs), 4)
        self.assertTrue(all(Path(path).is_relative_to(repo) for path in uv_runs), uv_runs)
        self.assertEqual({name: b['result'] for name, b in record['builders'].items()}, {'codex': 'PASS', 'claude': 'PASS'})
        for path in self.scratch(run).values():
            self.assertFalse(path.exists(), f"a passing run removes {path}")

    def test_real_check_failing_only_in_the_lane_fails_smoke_on_pre_t60_profile(self):
        """T-62: a lane whose uv cache is the shared read-only one (the profile before T-60) fails the smoke."""
        helper = self.smoke_module()
        helper.WORKSPACE, helper.CORTEX = self.home / 'dev_workspace', self.home / 'cortex'
        uv_line = 'uv run --no-project --with colorama==0.4.6 python smoke_check.py'
        cortex = self.home / 'cortex/cortex-core'
        self.real_repo(cortex, f"check:\n    {uv_line}\n", [('smoke_check.py', 'print(1)\n')])
        (self.home / 'dev_workspace/company/FAIL').write_text('broken on main\n')  # fails outside too
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-C', str(self.home / 'dev_workspace/company'), 'add', 'FAIL'], check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-C', str(self.home / 'dev_workspace/company'), 'commit', '-qm', 'break main'], check=True)
        helper.REAL_REPOS = tuple((name, self.home / path.relative_to(helper.HOME), required)
                                  for name, path, required in helper.REAL_REPOS)
        repos = helper.real_repos()
        self.assertEqual([name for name, _ in repos], ['company', 'bootstrap', 'cortex-core'])
        found = {'repo': self.home / 'dev_workspace/task-smoke-x', 'real': self.home / 'dev_workspace/task-smoke-x-real'}
        shared = self.home / 'shared-uv-cache'
        shared.mkdir()
        (shared / '.read-only').write_text('')  # root ignores the mode below; the stub honours either
        shared.chmod(0o500)
        self.addCleanup(shared.chmod, 0o700)
        self.enterContext(mock.patch.dict(os.environ, PATH=self.env['PATH'], HOME=str(self.home)))
        outside = helper.outside_checks('x', found, repos)
        self.assertEqual({name: result['exit'] for name, result in outside.items()},
                         {'company': 1, 'bootstrap': 0, 'cortex-core': 0})
        calls = []

        def fake_task(*args):  # the lane runs mocked: each Run: line under the pre-T-60 lane profile
            calls.append(args)
            wt = found['repo'] / '.worktrees/t3'
            if args[0] == 'start':
                (wt / '.work').mkdir(parents=True)
            if args[0] == 'lane':
                lane_env = dict(os.environ, UV_CACHE_DIR=str(shared))
                for line in (wt / '.work/prompt.md').read_text().splitlines():
                    if line.startswith('Run: '):
                        subprocess.run(line[5:], shell=True, cwd=wt, env=lane_env, capture_output=True)
            return subprocess.CompletedProcess(args, 0, '', '')

        helper.task = fake_task
        lines = []
        steps = helper.run_real_checks('x', found, 'codex', lines.append, repos, outside)
        self.assertEqual([args[0] for args in calls], ['start', 'lane', 'wait'])
        results = {step['step']: step['result'] for step in steps}
        self.assertEqual(results, {'real start': 'PASS', 'real lane': 'PASS', 'real company': 'WARN',
                                   'real bootstrap': 'PASS', 'real cortex-core': 'FAIL'})
        self.assertIn('  FAIL real cortex-core: setup failure: just check fails in the codex lane but passes outside: '
                      f'error: Failed to initialize cache at {shared}: Read-only file system (os error 30)', lines)
        clone = found['repo'] / '.worktrees/t3/.work/real/cortex-core'
        self.assertEqual(subprocess.run(['git', '-C', str(clone), 'branch', '--show-current'], capture_output=True,
                                        text=True).stdout.strip(), 'task-smoke-x', 'checks run on a throwaway branch')
        self.assertEqual(subprocess.run(['git', '-C', str(cortex), 'branch', '--list', 'task-smoke-x'], capture_output=True,
                                        text=True).stdout.strip(), '', 'the real repository is untouched')

    def test_missing_required_real_repository_fails_smoke(self):
        shutil.rmtree(self.home / 'dev_workspace/company')
        out = self.smoke('--builder', 'codex', code=1)
        self.assertIn('FAIL real repositories:', out)
        self.assertIn('company is not a repository', out)

    def test_review_fail_verdict_fails_at_review_step(self):
        out = self.smoke('--builder', 'codex', code=1, env=dict(self.env, SMOKE_REVIEW_VERDICT='FAIL'))
        self.assertRegex(out, r"(?m)^  FAIL review: review verdict is FAIL$")
        self.assertNotRegex(out, r"(?m)^  (PASS|FAIL) merge$")

    def test_read_only_git_metadata_fails_at_commit_step(self):
        out = self.smoke('--builder', 'codex', code=1, env=dict(self.env, SMOKE_STUB_RO='1'))
        run = self.run_id(out)
        self.assertIn("FAIL commit: fatal: Unable to create '/home/agent/dev_workspace/proj/.git/worktrees/t1/index.lock': "
                      "Read-only file system", out)
        self.assertRegex(out, r"(?m)^  PASS lane$")
        self.assertNotRegex(out, r"(?m)^  (PASS|FAIL) (merge main|report|check|review|merge)")
        for path in self.scratch(run).values():
            self.assertTrue(path.exists(), f"a FAIL keeps {path}")
        self.assertIn(f"task smoke --clean {run}", out)

    def test_uv_cache_write_failure_fails_smoke_at_lane_step(self):
        out = self.smoke('--builder', 'codex', code=1, env=dict(self.env, SMOKE_UV_FAIL='1'))
        self.assertRegex(out, r"(?m)^  FAIL lane: error: uv cache is read-only$")
        self.assertNotRegex(out, r"(?m)^  (PASS|FAIL) commit$")

    def test_existing_scratch_paths_are_refused_and_left_alone(self):
        loader = importlib.machinery.SourceFileLoader('task_smoke_for_test', str(SMOKE_HELPER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        helper.WORKSPACE, helper.CORTEX, helper.STATE = (
            self.home / 'dev_workspace', self.home / 'cortex', self.home / '.local/state/task')
        run = '20990101T000000Z-aaaaaa'
        for key in ('repo', 'kb', 'state'):
            path = self.scratch(run)[key]
            path.mkdir(parents=True)
            (path / 'keep.txt').write_text('mine\n')
            with self.assertRaisesRegex(helper.Stop, 'already exists; refusing to reuse it'):
                helper.check_free(helper.paths(run))
            self.assertEqual((path / 'keep.txt').read_text(), 'mine\n', f'{key} is left alone')
            shutil.rmtree(path)
        helper.check_free(helper.paths(run))  # nothing exists: no refusal

    def test_clean_removes_exactly_what_the_run_created(self):
        out = self.smoke('--builder', 'codex', code=1, env=dict(self.env, SMOKE_STUB_RO='1'))
        run = self.run_id(out)
        other = '20990101T000000Z-bbbbbb'
        sibling = {'repo': self.home / 'dev_workspace' / f'task-smoke-{other}',
                   'kb': self.home / 'cortex' / f'cortex-kb-task-smoke-{other}',
                   'state': self.home / '.local/state/task' / f'task-smoke-{other}'}
        for path in sibling.values():
            path.mkdir(parents=True)
        proj = self.home / '.local/state/task/proj'
        proj.mkdir(parents=True)
        self.smoke('--clean', 'not-a-run-id', code=1)
        self.assertTrue(self.scratch(run)['repo'].exists(), 'a malformed run id removes nothing')
        out = self.smoke('--clean', run)
        for path in self.scratch(run).values():
            self.assertFalse(path.exists(), f"clean removes {path}")
        for path in sibling.values():
            self.assertTrue(path.is_dir(), f"clean leaves {path}")
        self.assertTrue(proj.is_dir(), 'clean leaves other task state')
        self.assertTrue((self.home / '.local/state/task/smoke' / f'{run}.json').is_file(),
                        'the run record stays, since install records cite it')
        self.assertIn('nothing to remove', self.smoke('--clean', run))

    def test_task_smoke_dispatches_to_the_helper(self):
        result = subprocess.run([sys.executable, str(TOOL), 'smoke', '--clean', 'not-a-run-id'], env=self.env,
                                cwd=self.home, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('run id', result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
