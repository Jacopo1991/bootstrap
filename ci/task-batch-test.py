#!/usr/bin/env python3
"""Exercise task-batch end to end with real Git and a serial task stand-in."""
import importlib.machinery
import importlib.util
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
from datetime import datetime, timezone
from unittest import mock
from policy_test_env import isolate_policy_environment

isolate_policy_environment()


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'home/dot_local/bin/executable_task-batch'
loader = importlib.machinery.SourceFileLoader('task_batch', str(TOOL))
spec = importlib.util.spec_from_loader(loader.name, loader)
batch_module = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
loader.exec_module(batch_module)
task_usage = sys.modules['task_usage']
STUB = r'''#!/usr/bin/env python3
import fcntl, json, os, pathlib, subprocess, sys
from datetime import datetime, timezone
home = pathlib.Path.home()
lock = (home / 'command.lock').open('w')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    (home / 'overlap').touch()
    sys.exit(99)
cmd, repo, ident, *options = sys.argv[1:]
repo = pathlib.Path(repo)
config = json.loads((home / 'scenario.json').read_text())
counts_path = home / 'counts.json'
counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
key = cmd + ':' + ident
index = counts.get(key, 0)
counts[key] = index + 1
counts_path.write_text(json.dumps(counts))
with (home / 'calls.jsonl').open('a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\n')
entries = config.get(key, config.get(cmd, [{}]))
entry = entries[min(index, len(entries) - 1)]
if cmd == 'check' and 'trusted_log' in entry:
    done = home / '.local/state/task' / repo.name / ident
    (done / 'check.log').write_text(entry['trusted_log'])
    (done / 'check.json').write_text(json.dumps(dict(trusted=True, log=str(done / 'check.log'),
         time=datetime.now(timezone.utc).isoformat())))
if entry.get('code'):
    print(entry.get('output', 'failed'))
    sys.exit(entry['code'])
if cmd == 'runner-check':
    print('runner file OK')
    sys.exit(0)
import re
match = re.fullmatch(r'(?i)t?-?(\d+)', ident)
name = 't' + str(int(match[1])) if match else ident.replace('/', '-')
wt = repo / '.worktrees' / name
work = wt / '.work'
state = home / '.local/state/task' / repo.name / name
def save(path, value):
    path.write_text(json.dumps(value))
def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()
if cmd == 'start':
    if not wt.exists():
        git(repo, 'worktree', 'add', '-b', name, str(wt))
        work.mkdir()
        state.mkdir(parents=True)
        save(state / 'task.json', dict(base_sha=git(wt, 'rev-parse', 'HEAD'), branch=name,
             builder=options[options.index('--builder') + 1], revision='revision', criteria=[],
             tier=options[options.index('--tier') + 1] if '--tier' in options else 'routine'))
    if '--seed' in options:
        import shutil
        seed = options[options.index('--seed') + 1]
        shutil.copy2(repo / '.work' / seed, work / seed)
    print('Dispatch prompt:\nContext: .work/context/\nCommit and fill .work/report.md.')
elif cmd == 'lane':
    prompt = (work / 'prompt.md').read_text()
    sys.path.insert(0, os.environ['TASK_TEST_ROUTING_DIR'])
    from task_routing import route
    vendor = options[options.index('--builder') + 1]
    tier = options[options.index('--tier') + 1] if '--tier' in options else 'routine'
    explicit = options[options.index('--model') + 1] if '--model' in options else None
    override = options[options.index('--override') + 1] if '--override' in options else ''
    recorded = json.loads((state / 'task.json').read_text()).get('tier', 'routine')
    routed_model = route(home, vendor, tier, explicit, override=override, recorded=recorded)
    with (home / 'prompts.jsonl').open('a') as out:
        out.write(json.dumps(dict(path=str(work / 'prompt.md'), prompt=prompt, args=options,
                                 routed_model=routed_model)) + '\n')
    record = json.loads((state / 'task.json').read_text())
    record['builder'] = options[options.index('--builder') + 1]
    save(state / 'task.json', record)
    save(work / 'lane.json', dict(state=entry.get('state', 'finished'),
         builder=record['builder'], exit_code=entry.get('exit_code', 0),
         started_at=datetime.now(timezone.utc).isoformat()))
    (work / 'lane.log').write_text(entry.get('log', ''))
    (work / 'final.md').write_text(entry.get('final', ''))
    (work / 'check.log').write_text(entry.get('check_log', ''))
    if entry.get('merge_main'):
        (repo / 'from-main').write_text(str(index))
        git(repo, 'add', 'from-main')
        git(repo, 'commit', '-m', 'main moves')
        git(wt, 'merge', '--no-edit', 'main')
    if entry.get('conflict_main'):
        # Commit the conflicted merge-tree result (markers included) as a two-parent merge.
        (repo / 'result').write_text('main')
        git(repo, 'add', 'result')
        git(repo, 'commit', '-m', 'main conflicts')
        ours, theirs = git(wt, 'rev-parse', 'HEAD'), git(wt, 'rev-parse', 'main')
        tree = subprocess.run(['git', '-C', str(wt), 'merge-tree', '--write-tree', ours, theirs],
                              capture_output=True, text=True)
        assert tree.returncode == 1
        git(wt, 'reset', '--hard', git(wt, 'commit-tree', tree.stdout.split()[0], '-p', ours, '-p', theirs,
                                       '-m', 'Merge main with conflict markers'))
    if (entry.get('merge_main') and not entry.get('also_commit')) or entry.get('conflict_main'):
        if entry.get('report'):
            (work / 'report.md').write_text('## What changed\nMerged main\n\n## How I verified\nTests\n')
    elif not entry.get('unfinished'):
        (wt / 'result').write_text(str(index))
        git(wt, 'add', 'result')
        git(wt, 'commit', '--allow-empty', '-m', entry.get('subject', 'Implement task'))
        (work / 'report.md').write_text('## What changed\nDone\n\n## How I verified\nTests\n')
    else:
        (wt / 'dirty').write_text('unfinished')
elif cmd == 'wait':
    if entry.get('finish_lane'):
        lane = json.loads((work / 'lane.json').read_text())
        assert lane['state'] == 'running'
        os.kill(lane['pid'], 0)
        assert (work / 'prompt.md').read_text() == 'original live prompt'
        (wt / 'result').write_text('finished existing lane')
        git(wt, 'add', 'result')
        git(wt, 'commit', '-m', 'Finish existing lane')
        (work / 'report.md').write_text('## What changed\nDone\n\n## How I verified\nTests\n')
        save(work / 'lane.json', dict(lane, state='finished', exit_code=0))
    print('interrupted')
elif cmd == 'review':
    save(state / 'review-run.json', dict(state='finished', builder=entry.get('builder', 'claude'),
         exit_code=entry.get('exit_code', 0)))
    (state / 'review-stderr.log').write_text(entry.get('stderr', ''))
    if entry.get('corrupt'):
        (state / 'review.json').write_text('{invalid')
    elif not entry.get('missing'):
        save(state / 'review.json', dict(verdict=entry.get('verdict', 'PASS'),
             commit=entry.get('commit', git(wt, 'rev-parse', 'HEAD')),
             record_revision=entry.get('revision', 'revision'), judgments={}, reviewer='claude'))
        (state / 'review.md').write_text('Verdict: FAIL\nCommit: ' + git(wt, 'rev-parse', 'HEAD') +
             '\n## Blocking\n' + entry.get('findings', 'criterion 1 missing') + '\n## Notes\noptional')
    (state / 'review-response.md').write_text(entry.get('reply', ''))
    print(entry.get('output', 'Verdict: FAIL (stdout is not authoritative)'))
elif cmd == 'merge':
    save(state / 'merge.json', dict(head=git(wt, 'rev-parse', 'HEAD')))
print(entry.get('output', 'OK'))
'''


class BatchTest(unittest.TestCase):
    def test_same_builder_reviewer_fallback_stops_before_merge_or_fixes(self):
        self.source.write_text('1. one: builder codex, tier hard\n')
        self.scenario({})
        original = batch_module.Batch.reviewer_outcome
        def fallback(worker, state):
            (state / 'review-routing.json').write_text(json.dumps(
                dict(vendor='codex', model='gpt-6-luna', fallback_from=['claude', 'haiku'])))
            return original(worker, state)
        runner = batch_module.Batch(self.repo, self.source)
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(batch_module.Batch, 'reviewer_outcome', fallback):
            self.assertEqual(runner.run(), 1)
        self.assertIn('independent review required', runner.done.read_text())
        self.assertEqual(len(self.calls('lane')), 1)
        self.assertEqual(self.calls('merge'), [])

    def test_tier_only_dispatch_and_missing_route(self):
        self.source.write_text('1. one: builder codex, tier hard\n')
        self.run_batch()
        lane = self.calls('lane')[0]
        self.assertNotIn('--model', lane)
        self.assertEqual(lane[lane.index('--tier') + 1], 'hard')
        self.source.write_text('1. one: builder codex\n')
        with self.assertRaisesRegex(batch_module.Stop, 'model or tier'):
            batch_module.parse(self.source)

    def test_lane_output_is_prefixed(self):
        result = subprocess.run([sys.executable, str(TOOL), 'proj', str(self.source)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('[one] Dispatch prompt:', result.stdout)
        self.assertIn('[one] Context: .work/context/', result.stdout)
        self.assertIn('[one] OK', result.stdout)

    def test_stop_and_interrupt_report_before_inflight_worker_finishes(self):
        for interruption in (False, True):
            with self.subTest(interruption=interruption):
                config = self.home / '.config/task'
                config.mkdir(parents=True, exist_ok=True)
                (config / 'gate.toml').write_text('slots = 2\n')
                boost = self.home / '.local/state/task/boost.json'
                boost.parent.mkdir(parents=True, exist_ok=True)
                boost.write_text(json.dumps({'codex': '2099-01-01T00:00:00+00:00'}))
                release, entered, exited = threading.Event(), threading.Event(), threading.Event()
                launched = []
                def execute(worker, task):
                    launched.append(task['id'])
                    if task['id'] == 'one':
                        self.assertTrue(entered.wait(5))
                        raise batch_module.Stop('test stop')
                    entered.set()
                    try:
                        release.wait(5)
                        return 'merged'
                    finally:
                        exited.set()
                tasks = [dict(id=i, builder='codex', after=None) for i in ('one', 'two', 'three')]
                runner = batch_module.Batch(self.repo, self.source)
                def interrupt(*args, **kwargs):
                    self.assertTrue(entered.wait(5))
                    raise KeyboardInterrupt
                patches = contextlib.ExitStack()
                with patches, mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(runner, 'require'), \
                        mock.patch.object(batch_module, 'parse', return_value=tasks), \
                        mock.patch.object(batch_module.Batch, 'task', execute):
                    if interruption:
                        patches.enter_context(mock.patch.object(batch_module, 'wait', side_effect=interrupt))
                    try:
                        self.assertEqual(runner.run(), 1)
                        self.assertIn('batch: stopped:', runner.done.read_text())
                        if interruption:
                            self.assertIn('batch: stopped: interrupted', runner.done.read_text())
                        self.assertFalse(exited.is_set())
                        self.assertEqual(set(launched), {'one', 'two'})
                    finally:
                        release.set()
                        self.assertTrue(exited.wait(5))

    def test_tier_runner_field_dispatch(self):
        self.source.write_text('1. one: builder codex, model gpt-6-luna, tier design\n')
        self.assertEqual(batch_module.parse(self.source)[0]['tier'], 'design')
        self.run_batch()
        for command in ('start', 'lane'):
            call = self.calls(command)[0]
            self.assertEqual(call[call.index('--tier') + 1], 'design')
        self.source.write_text('1. one: builder codex, model gpt-6-luna, tier unknown\n')
        with self.assertRaises(batch_module.Stop):
            batch_module.parse(self.source)

    def test_boost_capacity_dependencies_and_gate_bound(self):
        config = self.home / '.config/task'
        config.mkdir(parents=True)
        state = self.home / '.local/state/task'
        state.mkdir(parents=True)
        boost = state / 'boost.json'
        for vendor in ('codex', 'claude'):
            for boosted, slots, expected in ((False, 3, 1), (True, 3, 3), (True, 2, 2), (True, 1, 1)):
                with self.subTest(vendor=vendor, boosted=boosted, slots=slots):
                    (config / 'gate.toml').write_text(f'slots = {slots}\n')
                    boost.write_text(json.dumps({vendor: '2099-01-01T00:00:00+00:00'} if boosted else {}))
                    tasks = [dict(id=str(i), builder=vendor, after='0' if i == 3 else None) for i in range(5)]
                    lock = threading.Lock()
                    active, maximum, completed = 0, 0, set()
                    first_wave = threading.Barrier(expected)
                    def execute(worker, task):
                        nonlocal active, maximum
                        with lock:
                            if task['after']:
                                self.assertIn(task['after'], completed)
                            active += 1
                            maximum = max(maximum, active)
                        if int(task['id']) < expected:
                            first_wave.wait(timeout=5)
                        with lock:
                            active -= 1
                            completed.add(task['id'])
                        return 'merged'
                    runner = batch_module.Batch(self.repo, self.source)
                    with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(batch_module.Batch, 'task', execute):
                        runner.schedule(tasks)
                    self.assertEqual(maximum, expected)
                    self.assertEqual(len(completed), 5)
        boost.write_text(json.dumps({'codex': '2000-01-01T00:00:00+00:00'}))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(runner.capacity()[0], 1)
        boost.write_text(json.dumps({'codex': '2099-01-01T00:00:00+00:00'}))
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(runner.capacity()[1]['claude'], 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task-batch-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / 'dev_workspace/proj'
        self.repo.mkdir(parents=True)
        self.env = dict(HOME=str(self.home), PATH=os.environ['PATH'],
                        TASK_TEST_ROUTING_DIR=str(ROOT / 'home/dot_local/bin'),
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        self.git('init', '-b', 'main')
        (self.repo / '.gitignore').write_text('.work/\n.worktrees/\n')
        self.git('add', '.gitignore')
        self.git('commit', '-m', 'Initial')
        (self.home / 'bin').mkdir()
        stub = self.home / 'bin/task'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.env['PATH'] = str(stub.parent) + os.pathsep + self.env['PATH']
        self.source = self.home / 'runner-test.md'
        self.source.write_text('1. one: builder codex, model gpt-6-luna\nRequest: implement one\n'
                               'Criteria: works\nTime box: 60 min\n```text\n'
                               '1. <id>: builder codex, model <model>\n```\n')
        self.scenario({})

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env,
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def scenario(self, value):
        (self.home / 'scenario.json').write_text(json.dumps(value))

    def run_batch(self, code=0, options=()):
        prior_prompts = len(self.prompts()) if (self.home / 'prompts.jsonl').exists() else 0
        result = subprocess.run([sys.executable, str(TOOL), 'proj', str(self.source), *options],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertFalse((self.home / 'overlap').exists())
        prompts = self.prompts() if (self.home / 'prompts.jsonl').exists() else []
        for prompt in prompts[prior_prompts:]:
            ending = (batch_module.FIX_SCOPE_INSTRUCTION if 'Fix round:' in prompt['prompt']
                      else batch_module.BLOCK_INSTRUCTION)
            self.assertTrue(prompt['prompt'].endswith(ending + '\n'))
            self.assertIn(batch_module.BLOCK_INSTRUCTION, prompt['prompt'])
        for call in self.calls('review'):
            self.assertEqual(call[-2:], ['--append-prompt', batch_module.BLOCK_INSTRUCTION])
        return (self.repo / '.work/runner-test.done').read_text()

    def calls(self, command=None):
        calls = [json.loads(line) for line in (self.home / 'calls.jsonl').read_text().splitlines()]
        return [call for call in calls if command is None or call[0] == command]

    def prompts(self):
        return [json.loads(line) for line in (self.home / 'prompts.jsonl').read_text().splitlines()]

    def test_happy_path_serial_absolute_paths_and_rules_excluded(self):
        self.assertEqual(self.run_batch(), 'one: merged\nbatch: finished\n')
        self.assertEqual([c[0] for c in self.calls()],
                         ['runner-check', 'start', 'lane', 'wait', 'check', 'review', 'merge'])
        prompt = self.prompts()[0]
        self.assertEqual(prompt['path'], str(self.repo / '.worktrees/one/.work/prompt.md'))
        self.assertIn('Commit and fill', prompt['prompt'])
        self.assertNotIn('<id>', prompt['prompt'])
        self.assertIn('60.0', prompt['args'])
        self.assertFalse(self.source.with_suffix('.done').exists())

    def test_resume_committed_work_skips_start_and_lane(self):
        for command in (['start', '--builder', 'codex'],
                        ['lane', '--builder', 'codex', '--model', 'gpt-6-luna']):
            if command[0] == 'lane':
                (self.repo / '.worktrees/one/.work/prompt.md').write_text('old prompt')
            subprocess.run(['task', command[0], str(self.repo), 'one', *command[1:]],
                           env=self.env, check=True, capture_output=True)
        before = len(self.calls())
        self.assertIn('one: merged', self.run_batch())
        self.assertEqual([c[0] for c in self.calls()[before:]],
                         ['runner-check', 'wait', 'check', 'review', 'merge'])

    def test_resume_running_lane_waits_without_relaunch_or_prompt_write(self):
        subprocess.run(['task', 'start', str(self.repo), 'one', '--builder', 'codex'],
                       env=self.env, check=True, capture_output=True)
        work = self.repo / '.worktrees/one/.work'
        (work / 'lane.json').write_text(json.dumps(dict(state='running', pid=os.getpid(),
                                                       builder='codex')))
        (work / 'prompt.md').write_text('original live prompt')
        self.scenario({'wait': [{'code': 3, 'output': 'still running'}, {'finish_lane': True}]})
        before = len(self.calls())
        self.assertIn('one: merged', self.run_batch())
        self.assertEqual([c[0] for c in self.calls()[before:]],
                         ['runner-check', 'wait', 'wait', 'check', 'review', 'merge'])
        self.assertEqual((work / 'prompt.md').read_text(), 'original live prompt')
        self.assertFalse(self.calls('lane'))

    def test_tasks_after_environment_instruction_still_run(self):
        self.source.write_text(self.source.read_text() +
                               'Do not explore the environment; start with step 1.\n'
                               '2. two: builder codex, model gpt-6-luna\nRequest: implement two\n')
        self.assertEqual(self.run_batch(), 'one: merged\ntwo: merged\nbatch: finished\n')
        self.assertEqual([call[2] for call in self.calls('start')], ['one', 'two'])

    def test_resume_unfinished_rewrites_stale_prompt(self):
        subprocess.run(['task', 'start', str(self.repo), 'one', '--builder', 'codex'],
                       env=self.env, check=True, capture_output=True)
        (self.repo / '.worktrees/one/.work/prompt.md').write_text('STALE')
        self.run_batch()
        self.assertEqual(len(self.calls('start')), 1)
        self.assertNotIn('STALE', self.prompts()[0]['prompt'])

    def test_setup_72_resume_previous_finished_lane_blocked_final_relaunches(self):
        subprocess.run(['task', 'start', str(self.repo), 'one', '--builder', 'codex'],
                       env=self.env, check=True, capture_output=True)
        work = self.repo / '.worktrees/one/.work'
        (work / 'lane.json').write_text(json.dumps(dict(
            state='finished', builder='codex', exit_code=1,
            started_at='2026-10-19T23:00:00+00:00')))
        (work / 'final.md').write_text('BLOCKED: python3 -I onboard/check.py — bwrap NETLINK_ROUTE')
        self.assertIn('one: merged', self.run_batch())
        self.assertEqual(len(self.calls('lane')), 1)

    def test_stale_final_and_log_ignored_but_fresh_terminal_output_stops(self):
        work = self.home / 'outcome'
        work.mkdir()
        (work / 'lane.json').write_text(json.dumps(dict(
            state='finished', builder='codex', exit_code=1,
            started_at='2026-10-20T01:00:00+00:00')))
        runner = batch_module.Batch(self.repo, self.source)
        start = batch_module.datetime.fromisoformat('2026-10-20T01:00:00+00:00').timestamp()
        for filename, message in (
                ('final.md', 'BLOCKED: git merge main'),
                ('final.md', "ERROR: You've hit your usage limit."),
                ('lane.log', "ERROR: You've hit your usage limit.")):
            path = work / filename
            path.write_text(message)
            for stamp in (start - 10, start):
                os.utime(path, (stamp, stamp))
                runner.lane_outcome(work)
            os.utime(path, (start + 1, start + 1))
            with self.assertRaises(batch_module.Stop):
                runner.lane_outcome(work)
            path.unlink()

    def test_setup_68_no_verify_passed_as_one_argument(self):
        self.source.write_text('1. one: builder codex, model gpt-6-luna, '
                               'no-verify: gate is its verification, no separate command\n')
        self.run_batch()
        self.assertEqual(self.calls('start')[0][-2:],
                         ['--no-verify', 'gate is its verification, no separate command'])

    def test_setup_68_parse_preserves_trailing_text_and_optional_field_positions(self):
        for suffix, reason, seed, after in (
                (', note here', None, None, None),
                (' some free text', None, None, None),
                (', seed pinned, after: previous, note here', None, 'pinned', 'previous'),
                (', note here, no-verify: manual check', 'manual check', None, None),
                (', no-verify: manual check, seed pinned, after: previous, note here',
                 'manual check', 'pinned', 'previous'),
                (', seed pinned, no-verify: manual check, after: previous',
                 'manual check', 'pinned', 'previous'),
                (', seed pinned, after: previous, note here, no-verify: manual check',
                 'manual check', 'pinned', 'previous'),
                (', no-verify: manual check, with notes', 'manual check, with notes', None, None)):
            with self.subTest(suffix=suffix):
                self.source.write_text('1. docs: builder codex, model gpt-6-luna' + suffix + '\n')
                task = batch_module.parse(self.source)[0]
                self.assertEqual((task['no_verify'], task['seed'], task['after']),
                                 (reason, seed, after))
        self.source.write_text('1. docs: builder codex, model gpt-6-luna, note here, no-verify: \n')
        with self.assertRaisesRegex(batch_module.Stop, 'nonempty reason'):
            batch_module.parse(self.source)

    def test_setup_71_fix_prompts_require_edit_tools_and_origin_main(self):
        self.scenario({'check': [{'code': 1}, {}]})
        self.run_batch()
        for item in self.prompts():
            self.assertIn('edit or apply_patch tool, never shell heredocs or cat >', item['prompt'])
            self.assertIn('avoid backticks in shell commands', item['prompt'])
            self.assertIn('git merge origin/main', item['prompt'])

    def test_validation_and_plan_refusal_stop(self):
        for command in ('runner-check', 'start'):
            with self.subTest(command=command):
                self.scenario({command: [{'code': 1, 'output': 'plan not signed'}]})
                self.assertIn('batch: stopped: task ' + command + ': plan not signed', self.run_batch(1))
        self.assertFalse(self.calls('lane'))

    def test_gate_busy_and_wait_window_retried(self):
        self.scenario({cmd: [{'code': 3, 'output': 'busy'}, {}] for cmd in ('wait', 'check', 'merge')})
        self.run_batch()
        for cmd in ('wait', 'check', 'merge'):
            self.assertEqual(len(self.calls(cmd)), 2)

    def test_interrupted_and_timed_out_commits_finish(self):
        self.source.write_text(self.source.read_text().split('```')[0] +
                               '2. two: builder codex, model gpt-6-luna\n')
        self.scenario({'lane:one': [{'state': 'interrupted'}], 'lane:two': [{'state': 'timed_out'}]})
        self.assertIn('one: merged', self.run_batch())
        self.assertEqual(len(self.calls('lane')), 2)

    def test_repeated_findings_park_and_dependencies_skip(self):
        self.source.write_text(self.source.read_text().split('```')[0] +
                               '2. two: builder codex, model gpt-6-luna, after: one\n'
                               '3. three: builder codex, model gpt-6-luna, after: two\n')
        self.scenario({'review': [{'verdict': 'FAIL'}]})
        done = self.run_batch()
        self.assertIn('one: parked: repeated failure', done)
        self.assertIn('two: not started: after one', done)
        self.assertIn('three: not started: after two', done)
        self.assertEqual(len(self.calls('lane')), 2)
        prompts = self.prompts()
        self.assertNotEqual(prompts[0]['prompt'], prompts[1]['prompt'])
        self.assertIn('criterion 1 missing', prompts[1]['prompt'])
        self.assertNotIn('optional', prompts[1]['prompt'])

    def test_external_after_without_merge_record_parks_only_dependant(self):
        self.source.write_text('1. dependent: builder codex, model gpt-6-luna, after: prepare\n'
                               '2. independent: builder codex, model gpt-6-luna\n')
        self.scenario({})
        done = self.run_batch()
        self.assertIn('dependent: not started: after prepare (no merge record)', done)
        self.assertIn('independent: merged', done)
        self.assertEqual([call[2] for call in self.calls('lane')], ['independent'])

    def test_external_after_with_merge_record_is_satisfied(self):
        self.source.write_text('1. dependent: builder codex, model gpt-6-luna, after: prepare\n')
        merge = self.home / '.local/state/task/proj/prepare/merge.json'
        merge.parent.mkdir(parents=True)
        merge.write_text(json.dumps({'head': 'already-merged'}))
        self.scenario({})
        done = self.run_batch()
        self.assertIn('dependent: merged', done)
        self.assertEqual([call[2] for call in self.calls('lane')], ['dependent'])

    def test_escalation_and_round_three_park(self):
        self.scenario({'review': [dict(verdict='FAIL', findings=f'criterion {i}') for i in range(4)]})
        self.assertIn('parked: fix rounds exhausted', self.run_batch())
        lanes = self.calls('lane')
        self.assertEqual([c[c.index('--model') + 1] for c in lanes],
                         ['gpt-6-luna', 'gpt-6-luna', 'gpt-6.1-sol', 'opus'])
        self.assertEqual(lanes[-1][lanes[-1].index('--builder') + 1], 'claude')

    def test_tier_only_fix_rounds_preserve_routing(self):
        state = self.home / '.local/state/task'
        state.mkdir(parents=True)
        for vendor in ('claude', 'codex'):
            for boosted in (False, True):
                for tier in ('routine', 'hard', 'design'):
                    with self.subTest(vendor=vendor, boosted=boosted, tier=tier):
                        ident = f'{vendor}-{boosted}-{tier}'
                        self.source.write_text(f'1. {ident}: builder {vendor}, tier {tier}\n')
                        (state / 'boost.json').write_text(json.dumps(
                            {v: '2099-01-01T00:00:00+00:00' for v in ('claude', 'codex')}
                            if boosted else {}))
                        self.scenario({'review': [dict(verdict='FAIL', findings=f'criterion {i}')
                                                  for i in range(4)]})
                        self.assertIn('parked: fix rounds exhausted', self.run_batch())
                        lanes = [c for c in self.calls('lane') if c[2] == ident]
                        self.assertEqual(len(lanes), 4)
                        self.assertTrue(all('--model' not in c for c in lanes))
                        self.assertEqual([c[c.index('--tier') + 1] for c in lanes],
                                         [tier, tier, tier, tier])
                        other = 'codex' if vendor == 'claude' else 'claude'
                        self.assertEqual([c[c.index('--builder') + 1] for c in lanes],
                                         [vendor, vendor, vendor, other])
                        models = {'claude': {'routine': 'sonnet', 'hard': 'fable' if boosted else 'opus',
                                             'design': 'opus'},
                                  'codex': {'routine': 'gpt-6-luna',
                                            'hard': 'gpt-6.1-sol',
                                            'design': 'gpt-6-astra' if boosted else 'gpt-6.1-sol'}}
                        prompts = [p for p in self.prompts() if Path(p['path']).parents[1].name == ident]
                        self.assertEqual([p['routed_model'] for p in prompts],
                                         [models[vendor][tier], models[vendor][tier],
                                          models[vendor][tier], models[other][tier]])

    def test_strong_model_skips_round_two(self):
        self.source.write_text(self.source.read_text().replace('gpt-6-luna', 'gpt-6.1-sol'))
        self.scenario({'review': [dict(verdict='FAIL', findings=f'criterion {i}') for i in range(4)]})
        self.run_batch()
        self.assertEqual(len(self.calls('lane')), 3)
        self.assertIn('opus', self.calls('lane')[-1])

    def test_top_model_on_the_wrong_tier_is_parked_without_a_lane(self):
        for number, line in enumerate(('model gpt-6-astra', 'model gpt-6-astra, tier hard',
                                       'model gpt-6-astra, tier routine', 'model fable',
                                       'model claude-fable-5-1, tier design')):
            with self.subTest(line=line):
                vendor = 'claude' if 'fable' in line else 'codex'
                self.source.write_text(f'1. one{number}: builder {vendor}, {line}\nTime box: 60 min\n')
                self.assertRegex(self.run_batch(), rf'one{number}: parked: (gpt-6-astra|fable|claude-fable-5-1) is for Tier: '
                                 r'(design|hard) tasks only .*gpt-6-luna.*gpt-6.1-sol')
                self.assertFalse((self.home / 'calls.jsonl').exists() and self.calls('lane'))
                (self.repo / '.work/runner-test.done').unlink()

    def test_top_model_on_its_own_tier_runs_and_never_returns_in_fix_rounds(self):
        for builder, model, tier, final in (('codex', 'gpt-6-astra', 'design', 'opus'),
                                            ('claude', 'fable', 'hard', 'gpt-6.1-sol')):
            with self.subTest(model=model):
                self.source.write_text(f'1. {tier}: builder {builder}, model {model}, tier {tier}\n'
                                       'Time box: 60 min\n')
                self.scenario({'review': [dict(verdict='FAIL', findings=f'criterion {i}') for i in range(4)]})
                self.run_batch()
                lanes = [c for c in self.calls('lane') if c[2] == tier]
                self.assertEqual([c[c.index('--model') + 1] for c in lanes][:1], [model])
                self.assertEqual(lanes[-1][lanes[-1].index('--model') + 1], final)
                self.assertEqual([model in c for c in lanes], [True, True, False])

    def test_runner_line_override_runs_a_top_model_outside_its_tier(self):
        request = 'Jacopo 2026-10-21: design review, quick'
        for number, (builder, model, tier) in enumerate((('codex', 'gpt-6-astra', 'routine'), ('claude', 'fable', 'design'))):
            with self.subTest(model=model):
                self.source.write_text(f'1. ov{number}: builder {builder}, model {model}, tier {tier}, '
                                       f'override: {request}\nTime box: 60 min\n')
                self.scenario({})
                self.assertNotIn('parked', self.run_batch())
                lanes = [c for c in self.calls('lane') if c[2] == f'ov{number}']
                self.assertEqual(lanes[0][lanes[0].index('--override') + 1], request)
                self.assertEqual(lanes[0][lanes[0].index('--model') + 1], model)
                (self.repo / '.work/runner-test.done').unlink(missing_ok=True)

    def test_runner_tier_does_not_raise_eligibility_over_the_recorded_tier(self):
        # The task was started as routine; a runner line naming tier design or hard does not make astra or fable eligible.
        request = 'Jacopo 2026-10-21: design review'
        for number, (builder, model, tier) in enumerate((('codex', 'gpt-6-astra', 'design'), ('claude', 'fable', 'hard'))):
            with self.subTest(model=model):
                ident = f'rec{number}'
                subprocess.run(['task', 'start', str(self.repo), ident, '--builder', builder],
                               check=True, env=self.env, capture_output=True)
                line = f'1. {ident}: builder {builder}, model {model}, tier {tier}'
                self.source.write_text(f'{line}\nTime box: 60 min\n')
                self.assertRegex(self.run_batch(), rf'{ident}: parked: {model} is for Tier: {tier} tasks only \(this one is routine\)')
                self.assertEqual([c for c in self.calls('lane') if c[2] == ident], [])
                (self.repo / '.work/runner-test.done').unlink()
                self.source.write_text(f'{line}, override: {request}\nTime box: 60 min\n')
                self.scenario({})
                self.assertNotIn('parked', self.run_batch())
                lanes = [c for c in self.calls('lane') if c[2] == ident]
                self.assertEqual(lanes[0][lanes[0].index('--override') + 1], request)
                (self.repo / '.work/runner-test.done').unlink(missing_ok=True)

    def test_runner_line_override_must_not_be_empty(self):
        self.source.write_text('1. one: builder codex, model gpt-6-astra, override: \nTime box: 60 min\n')
        self.assertIn('Jacopo YYYY-MM-DD: <request>', self.run_batch(code=1))

    def test_runner_line_override_must_name_jacopo_with_a_valid_date_and_request(self):
        for text in ('x', 'Jacopo 2026-10-21', 'Jacopo 2026-10-21:', 'Jacopo 2026-13-40: look', 'Bob 2026-10-21: look'):
            with self.subTest(text=text):
                self.source.write_text(f'1. one: builder codex, model gpt-6-astra, override: {text}\nTime box: 60 min\n')
                self.assertIn('Jacopo YYYY-MM-DD: <request>', self.run_batch(code=1))
                self.assertEqual([c for c in self.calls('lane') if c[2] == 'one'], [])

    def test_two_parked_tasks_stop_batch(self):
        self.source.write_text(self.source.read_text().split('```')[0] +
                               '2. two: builder codex, model gpt-6-luna\n'
                               '3. three: builder codex, model gpt-6-luna\n')
        self.scenario({'check': [{'code': 1, 'output': 'test failure'}]})
        self.assertIn('batch: stopped: two tasks parked in a row', self.run_batch(1))
        self.assertEqual(len(self.calls('start')), 2)
        self.assertIn('test failure', self.prompts()[1]['prompt'])

    def test_missing_verdict_retried_but_stale_pass_not_used(self):
        self.scenario({'review': [{'missing': True}, {}]})
        self.run_batch()
        self.assertEqual(len(self.calls('review')), 2)

    def test_stale_review_revision_and_commit_are_not_passes(self):
        self.scenario({'review': [{'commit': 'old'}, {'revision': 'old'}, {}]})
        self.assertIn('merged after 1 fix rounds', self.run_batch())
        self.assertEqual(len(self.calls('review')), 3)

    def test_wip_commit_needs_fix(self):
        self.scenario({'lane': [{'subject': 'WIP: incomplete'}, {}]})
        self.assertIn('merged after 1 fix rounds', self.run_batch())
        self.assertIn('finish and commit', self.prompts()[1]['prompt'])

    def test_time_cap_parks_without_another_lane(self):
        subprocess.run(['task', 'start', str(self.repo), 'one', '--builder', 'codex'],
                       env=self.env, check=True, capture_output=True)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
                batch_module.time, 'monotonic', side_effect=[0, 18000]):
            runner = batch_module.Batch(self.repo, self.source)
            result = runner.task(batch_module.parse(self.source)[0])
        self.assertIn('parked: total time cap', result)
        self.assertFalse(self.calls('lane'))

    def merge_in_batch(self, lane_entry):
        """Run one task whose merge is refused for an overlap, then fixed by the given lane round.

        The lane round takes 12000 s on a fake clock; the time cap is 10800 s."""
        self.scenario({'merge': [{'code': 1, 'output': 'main changed a.txt that this task also changed; '
                                  'ask the lane to run `git merge origin/main` on its branch'}, {}],
                       'lane': [{}, lane_entry]})
        def clock():
            lanes = json.loads((self.home / 'counts.json').read_text()).get('lane:one', 0)
            return [0, 100, 12100][min(lanes, 2)]
        with mock.patch.dict(os.environ, self.env), mock.patch.object(batch_module.time, 'monotonic', clock):
            return batch_module.Batch(self.repo, self.source).task(batch_module.parse(self.source)[0])

    def test_merge_in_round_is_exempt_from_the_time_cap_and_skips_the_advisory_check(self):
        self.assertEqual(self.merge_in_batch({'merge_main': True}), 'merged after 1 fix rounds')
        self.assertEqual(len(self.calls('review')), 2)
        self.assertIn('skip `task check` this round', self.prompts()[1]['prompt'])
        self.assertNotIn('skip `task check` this round', self.prompts()[0]['prompt'])

    def test_round_with_other_changes_counts_toward_the_time_cap(self):
        self.assertIn('parked: total time cap', self.merge_in_batch({}))
        self.assertEqual(len(self.calls('review')), 1)

    def test_merge_in_plus_another_commit_counts_toward_the_time_cap(self):
        self.assertIn('parked: total time cap', self.merge_in_batch({'merge_main': True, 'also_commit': True}))

    def test_conflicted_merge_is_not_a_merge_in_round(self):
        self.assertIn('parked: total time cap', self.merge_in_batch({'conflict_main': True}))

    def test_same_check_failure_ignores_head_and_duration(self):
        self.scenario({'check': [{'code': 1, 'output': 'head abc1234 failed in 1.2s'},
                                 {'code': 1, 'output': 'head fed4321 failed in 2.9s'}]})
        self.assertIn('parked: repeated failure', self.run_batch())
        self.assertEqual(len(self.calls('lane')), 2)
        self.assertFalse(self.calls('review'))

    def test_failed_check_fixes_before_spending_review(self):
        self.scenario({'lane': [{'check_log': 'ADVISORY bwrap NETLINK_ROUTE noise'}, {}],
                       'check': [{'code': 1, 'trusted_log': 'FAILED real fixture assertion'}, {}]})
        self.assertIn('merged after 1 fix rounds', self.run_batch())
        self.assertNotIn('ADVISORY', self.prompts()[1]['prompt'])
        commands = [call[0] for call in self.calls()]
        self.assertEqual(commands.count('check'), 2)
        self.assertEqual(commands.count('review'), 1)
        self.assertGreater(commands.index('review'), max(i for i, cmd in enumerate(commands) if cmd == 'check'))
        self.assertIn('FAILED real fixture assertion', self.prompts()[1]['prompt'])
        for prompt in self.prompts():
            self.assertIn('under `## Check`', prompt['prompt'])

    def test_usage_limit_in_lane_stops(self):
        self.scenario({'lane': [{'exit_code': 1, 'log': "ERROR: You've hit your usage limit. Try again later."}]})
        self.assertIn('batch: stopped:', self.run_batch(1))
        self.assertFalse(self.calls('check'))

    def test_final_usage_limit_stops_without_fix_round_and_can_resume(self):
        self.scenario({'lane': [{'exit_code': 2, 'final': "ERROR: You've hit your usage limit.\n\n"}]})
        self.assertIn('batch: stopped: lane CLI:', self.run_batch(1))
        self.assertEqual(len(self.calls('lane')), 1)
        self.assertFalse(self.calls('check'))
        self.scenario({})
        self.assertTrue(self.run_batch().endswith('one: merged\nbatch: finished\n'))
        self.assertEqual(len(self.calls('start')), 1)
        self.assertEqual(len(self.calls('lane')), 1)

    def test_resumed_running_lane_limit_stops_without_new_lane(self):
        subprocess.run(['task', 'start', str(self.repo), 'one', '--builder', 'codex'],
                       env=self.env, check=True, capture_output=True)
        work = self.repo / '.worktrees/one/.work'
        (work / 'lane.json').write_text(json.dumps(dict(state='running', pid=os.getpid(), builder='codex',
                                                       started_at='2000-01-01T00:00:00+00:00')))
        runner = batch_module.Batch(self.repo, self.source)

        def wait(*args, **kwargs):
            (work / 'lane.json').write_text(json.dumps(dict(state='finished', exit_code=1, builder='codex',
                                                           started_at='2000-01-01T00:00:00+00:00')))
            (work / 'final.md').write_text("ERROR: You've hit your usage limit.")

        with mock.patch.dict(os.environ, self.env), mock.patch.object(runner, 'require', side_effect=wait):
            with self.assertRaisesRegex(batch_module.Stop, 'lane CLI:'):
                runner.task(batch_module.parse(self.source)[0])
        self.assertFalse(self.calls('lane'))

    def test_review_limit_stops(self):
        self.scenario({'review': [{'exit_code': 2, 'reply': "You've hit your weekly limit · resets 5pm (UTC)"}]})
        self.assertIn('batch: stopped:', self.run_batch(1))

    def test_codex_reviewer_limit_stops(self):
        self.scenario({'review': [{'exit_code': 1, 'builder': 'codex',
                                  'stderr': "ERROR: You've hit your usage limit. Try again later."}]})
        self.assertIn('batch: stopped: reviewer CLI:', self.run_batch(1))

    def test_claude_lane_limit_stops(self):
        self.source.write_text(self.source.read_text().replace('codex', 'claude').replace('gpt-6-luna', 'sonnet'))
        self.scenario({'lane': [{'exit_code': 2, 'log': "You've hit your session limit · resets 3pm (Europe/Copenhagen)"}]})
        self.assertIn('batch: stopped: lane CLI:', self.run_batch(1))

    def test_limit_mentions_in_transcripts_review_and_commands_do_not_stop(self):
        mention = 'Discuss rate limit and usage limit handling'
        self.scenario({'lane': [{'log': mention, 'final': mention}],
                       'review': [{'reply': mention, 'output': mention}],
                       'check': [{'output': mention}], 'merge': [{'output': mention}]})
        self.assertIn('batch: finished', self.run_batch())

    def test_failed_cli_transcript_is_not_a_terminal_limit(self):
        self.scenario({'lane': [{'exit_code': 1,
                                'log': "ERROR: You've hit your usage limit.\nOther terminal failure"}],
                       'review': [{'exit_code': 1, 'reply': 'Review mentions rate limit'}]})
        self.assertIn('batch: finished', self.run_batch())

    def test_successful_cli_quoting_exact_limit_error_does_not_stop(self):
        self.scenario({'lane': [{'log': "ERROR: You've hit your usage limit."}],
                       'review': [{'reply': "You've hit your limit · resets 5pm (UTC)"}]})
        self.assertIn('batch: finished', self.run_batch())

    def test_lane_hook_feedback_does_not_stop_batch(self):
        self.scenario({'lane': [{'log': 'blocked by policy hook'}]})
        self.assertIn('batch: finished', self.run_batch())

    def test_check_permission_denied_enters_fix_round(self):
        self.scenario({'check': [{'code': 1, 'output': 'check failed; see check.log',
                                  'trusted_log': 'Permission denied: test fixture'}, {}]})
        self.assertIn('merged after 1 fix rounds', self.run_batch())
        self.assertIn('Permission denied', self.prompts()[1]['prompt'])

    def test_merge_hook_and_limit_text_enters_fix_round(self):
        self.scenario({'merge': [{'code': 1, 'output': 'blocked by policy hook; rate limit test failed'}, {}]})
        self.assertIn('merged after 1 fix rounds', self.run_batch())

    def test_exact_final_hook_denial_stops(self):
        self.scenario({'lane': [{'final': 'Command blocked by PreToolUse hook: required git command'}]})
        self.assertIn('batch: stopped: lane: Command blocked by PreToolUse hook:', self.run_batch(1))
        self.assertFalse(self.calls('check'))

    def test_lane_final_blocked_stops(self):
        self.scenario({'lane': [{'final': 'Could not finish.\nBLOCKED: git commit\n\n  \n'}]})
        self.assertIn('batch: stopped: lane: BLOCKED: git commit', self.run_batch(1))
        self.assertFalse(self.calls('check'))

    def test_bwrap_netlink_denial_in_lane_final_is_a_warning(self):
        denial = 'BLOCKED: `python3 -I onboard/check.py` — Bubblewrap’s `NETLINK_ROUTE` socket creation denied.'
        self.scenario({'lane': [{'final': 'Done.\n' + denial + '\n'}], 'review': [{'reply': denial}]})
        self.assertEqual(self.run_batch(), 'one: merged\nbatch: finished\n')

    def test_other_blocked_lines_still_stop_even_when_they_mention_bwrap(self):
        self.scenario({'lane': [{'final': 'BLOCKED: git push — bwrap failed\n'}]})
        self.assertIn('batch: stopped: lane: BLOCKED: git push', self.run_batch(1))

    def test_bwrap_warning_matches_only_the_sole_denial_line(self):
        denial = 'BLOCKED: `task check` — bwrap NETLINK_ROUTE denied'
        for line in (denial + ' and git push is refused', denial + '; also BLOCKED: git commit',
                     'BLOCKED: `task check` && `git push` — bwrap NETLINK_ROUTE denied',
                     'BLOCKED: git push — bwrap NETLINK_ROUTE denied, and the remote rejected it',
                     'BLOCKED: bwrap NETLINK_ROUTE denied, then merge conflict in task.py'):
            self.assertFalse(batch_module.bwrap_denial(line), line)
            self.assertEqual(batch_module.terminal_block(line), line)
        self.assertTrue(batch_module.bwrap_denial(denial + '.'))
        self.assertEqual(batch_module.terminal_block('Done.\n' + denial + '\n'), '')

    def test_fix_prompt_after_passed_check_states_the_pass_next_to_review_findings(self):
        self.scenario({'check': [{'output': 'PASS (log: /fixture/check.log)',
                                  'trusted_log': 'noise\nGate suites finished (failed=0, skipped=2)\nexit 0'}],
                       'review': [{'verdict': 'FAIL', 'findings': 'REVIEW finding fixture'}, {}]})
        self.assertIn('merged after 1 fix rounds', self.run_batch())
        prompt = self.prompts()[1]['prompt']
        self.assertIn('REVIEW finding fixture', prompt)
        self.assertIn('PASS (log: /fixture/check.log)', prompt)
        self.assertIn('Gate suites finished (failed=0, skipped=2)', prompt)

    def test_resumed_fix_prompt_carries_trusted_check_and_review_not_advisory_log(self):
        subprocess.run(['task', 'start', str(self.repo), 'one', '--builder', 'codex'],
                       env=self.env, check=True, capture_output=True)
        state = self.home / '.local/state/task/proj/one'
        (state / 'check.log').write_text('TRUSTED failing assertion')
        (state / 'check.json').write_text(json.dumps(dict(
            trusted=True, log=str(state / 'check.log'), time='2026-10-21T00:00:00+00:00')))
        (state / 'review.md').write_text('## Blocking\nREVIEW finding\n## Notes\nx')
        work = self.repo / '.worktrees/one/.work'
        (work / 'check.log').write_text('ADVISORY bwrap NETLINK_ROUTE denied')
        (work / 'check.json').write_text(json.dumps(dict(advisory=True, time='2026-10-21T01:00:00+00:00')))
        (self.repo / '.worktrees/one/dirty').write_text('unfinished')
        self.run_batch()
        prompt = self.prompts()[0]['prompt']
        self.assertIn('TRUSTED failing assertion', prompt)
        self.assertIn('REVIEW finding', prompt)
        self.assertNotIn('ADVISORY', prompt)

    def test_merge_only_head_is_unfinished_and_starts_no_trusted_check(self):
        self.scenario({'lane': [{'merge_main': True, 'report': True}, {}]})
        self.assertIn('one: merged', self.run_batch())
        commands = [call[0] for call in self.calls() if call[0] in {'lane', 'check'}]
        self.assertEqual(commands, ['lane', 'lane', 'check'])
        self.assertIn('Unfinished work', self.prompts()[1]['prompt'])

    def test_finished_needs_changes_beyond_the_default_branch(self):
        report = '## What changed\nDone\n\n## How I verified\nTests\n'
        runner = batch_module.Batch(self.repo, self.source)
        for branch, real in (('merge-only', False), ('real-work', True)):
            wt = self.repo / '.worktrees' / branch
            self.git('worktree', 'add', '-b', branch, str(wt))
            base = self.git('-C', str(wt), 'rev-parse', 'HEAD')
            if real:
                (wt / 'result').write_text('work')
                self.git('-C', str(wt), 'add', 'result')
            self.git('-C', str(wt), 'commit', '--allow-empty', '-m', 'Own commit')
            (wt / '.work').mkdir()
            (wt / '.work/report.md').write_text(report)
            for number in (1, 2):
                (self.repo / f'main-{branch}-{number}').write_text('main')
                self.git('add', '.')
                self.git('commit', '-m', 'main moves')
                self.git('-C', str(wt), 'merge', '--no-edit', 'main')
                self.assertEqual(runner.finished(wt, dict(base_sha=base)), real, (branch, number))
        # Main moving on after the merge changes nothing.
        (self.repo / 'later').write_text('main')
        self.git('add', '.')
        self.git('commit', '-m', 'main moves again')
        self.assertFalse(runner.finished(self.repo / '.worktrees/merge-only', dict(base_sha='x')))
        self.assertTrue(runner.finished(self.repo / '.worktrees/real-work', dict(base_sha='x')))

    def test_reviewer_final_blocked_stops(self):
        self.scenario({'review': [{'reply': 'Could not review.\nBLOCKED: git show\n\n  \n'}]})
        self.assertIn('batch: stopped: reviewer: BLOCKED: git show', self.run_batch(1))
        self.assertFalse(self.calls('merge'))
        self.assertEqual(len(self.calls('review')), 1)

    def test_reviewer_final_hook_denial_stops(self):
        self.scenario({'review': [{'reply': 'Review failed.\nExecution stopped by PreToolUse hook\n'}]})
        self.assertIn('batch: stopped: reviewer: Execution stopped by PreToolUse hook', self.run_batch(1))
        self.assertFalse(self.calls('merge'))

    def test_block_signal_mid_text_does_not_stop(self):
        reply = 'BLOCKED: git show\nBlocked by PreToolUse hook\nRecovered and finished.\n'
        self.scenario({'lane': [{'final': reply}], 'review': [{'reply': reply}]})
        self.assertIn('one: merged', self.run_batch())

    def test_trusted_command_block_marker_enters_fix_round(self):
        self.scenario({'check': [{'code': 1, 'output': 'BLOCKED: test fixture'}, {}],
                       'merge': [{'code': 1, 'output': 'Command blocked by PreToolUse hook: fixture'}, {}],
                       'review': [{'output': 'BLOCKED: wrapper output is not reviewer output'}]})
        self.assertIn('merged after 2 fix rounds', self.run_batch())

    def test_claude_final_hook_denials(self):
        work = self.home / 'hook-outcome'
        work.mkdir()
        (work / 'lane.json').write_text(json.dumps(dict(state='finished', builder='claude', exit_code=1,
                                                       started_at='2000-01-01T00:00:00+00:00')))
        runner = batch_module.Batch(self.repo, self.source)
        for prefix in ('Blocked by PreToolUse hook', 'Execution stopped by PreToolUse hook'):
            for suffix in ('', ': required git command'):
                with self.subTest(prefix=prefix, suffix=suffix):
                    (work / 'final.md').write_text(prefix + suffix)
                    with self.assertRaises(batch_module.Stop):
                        runner.lane_outcome(work)
                    (work / 'final.md').write_text('Review mentions ' + prefix + suffix)
                    (work / 'lane.log').write_text(prefix + suffix)
                    runner.lane_outcome(work)

    def test_exact_claude_limit_prefixes_on_final_line(self):
        diagnostics = ["You've hit your " + name for name in (
            'session limit', 'weekly limit', 'Opus limit', 'Sonnet limit',
            'Fable limit', 'usage credit limit', 'limit', 'fast limit', 'monthly spend limit')]
        diagnostics += ['API Error: 429']
        for diagnostic in diagnostics:
            for suffix in ('', ' · resets 3pm (Europe/Copenhagen)', ': details'):
                for code in (1, 2, -15):
                    with self.subTest(diagnostic=diagnostic, suffix=suffix, code=code):
                        record = dict(builder='claude', state='finished', exit_code=code)
                        final = diagnostic + suffix
                        self.assertEqual(batch_module.cli_limit(record, 'Earlier output\n' + final + '\n'), final)
                        self.assertFalse(batch_module.cli_limit(record, final + '\nOther failure'))
                        self.assertFalse(batch_module.cli_limit(record, 'Quoted: ' + final))
                        for nonfailure in (0, None):
                            self.assertFalse(batch_module.cli_limit(dict(record, exit_code=nonfailure), final))
                        self.assertFalse(batch_module.cli_limit(dict(record, state='timed_out'), final))

    def test_terminal_cli_diagnostics(self):
        record = dict(builder='codex', state='finished', exit_code=1)
        diagnostic = "ERROR: You've hit your usage limit. Try again later."
        self.assertEqual(batch_module.cli_limit(record, diagnostic + '\nWarning: no last agent message; wrote empty content to /tmp/final.md\n'), diagnostic)
        self.assertFalse(batch_module.cli_limit(dict(record, state='timed_out'), diagnostic))
        self.assertFalse(batch_module.cli_limit(dict(record, exit_code=0), diagnostic))
        self.assertTrue(batch_module.cli_limit(record,
                                              'ERROR: exceeded retry limit, last status: 429 Too Many Requests'))
        self.assertFalse(batch_module.cli_limit(record,
                                               'ERROR: exceeded retry limit, last status: 500 Internal Server Error'))
        self.assertTrue(batch_module.cli_limit(dict(record, builder='claude'),
                                              'API Error: 429 {"error":{"type":"rate_limit_error"}}'))

    def test_unexpected_exception_writes_final_line(self):
        self.scenario({'review': [{'corrupt': True}]})
        self.assertIn('batch: stopped: Expecting property name', self.run_batch(1))

    def test_seed_numeric_id_and_default_box(self):
        (self.repo / '.work').mkdir(exist_ok=True)
        (self.repo / '.work/pinned').write_text('seed')
        self.source.write_text('1. T-12: builder claude, model sonnet, seed pinned\nRequest\n')
        self.run_batch()
        self.assertEqual(self.calls('start')[0][-2:], ['--seed', 'pinned'])
        self.assertIn('/t12/', self.prompts()[0]['path'])
        self.assertIn('90', self.calls('lane')[0])

    def test_resume_existing_worktree_applies_named_seed(self):
        subprocess.run(['task', 'start', str(self.repo), 'one', '--builder', 'codex'],
                       env=self.env, check=True, capture_output=True)
        (self.repo / '.work').mkdir(exist_ok=True)
        (self.repo / '.work/pinned').write_text('resumed input')
        self.source.write_text('1. one: builder codex, model gpt-6-luna, seed pinned\nRequest\n')
        self.run_batch()
        self.assertEqual(self.calls('start')[-1][-2:], ['--seed', 'pinned'])
        self.assertEqual((self.repo / '.worktrees/one/.work/pinned').read_text(), 'resumed input')

    def ahead_claude_batch(self):
        """Claude 70 % used with half the week gone is ahead of pace (reserve 25, margin 10)."""
        now = datetime.now(timezone.utc)
        event = {'rate_limit_info': {'unifiedWindows': {'seven_day': {
            'utilization': 0.7, 'resetsAt': int(now.timestamp() + 84 * 3600)}}}}
        task_usage.record_claude(self.home, event, now)
        self.source.write_text('1. one: builder claude, model sonnet\nRequest\n')

    def test_batch_start_is_refused_when_a_vendor_is_ahead_of_pace_unless_no_pace(self):
        self.ahead_claude_batch()
        done = self.run_batch(1)
        self.assertIn('batch: stopped: usage pacing: claude is ahead of pace; no new batch starts on its own', done)
        self.assertEqual([c[0] for c in self.calls()], ['runner-check'])
        self.assertIn('batch not started: claude ahead of pace', (self.home / '.local/state/task/usage/pace.log').read_text())
        (self.repo / '.work/runner-test.done').unlink()
        self.assertIn('one: merged', self.run_batch(options=['--no-pace']))
        self.assertIn('--no-pace: batch proj/runner-test.md not paced', (self.home / '.local/state/task/usage/pace.log').read_text())

    def test_batch_start_with_unknown_usage_warns_and_runs(self):
        self.source.write_text('1. one: builder claude, model sonnet\nRequest\n')
        result = subprocess.run([sys.executable, str(TOOL), 'proj', str(self.source)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('pace warning: claude usage unknown', result.stdout)

    def test_held_lane_is_retried_until_the_pace_check_allows_it(self):
        self.scenario({'lane': [{'code': batch_module.HELD, 'output': 'held back by usage pacing'}, {}]})
        runner = batch_module.Batch(self.repo, self.source)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(batch_module, 'HOLD_SECONDS', 0.01):
            runner.require('start', 'one', '--builder', 'codex')
            (self.repo / '.worktrees/one/.work/prompt.md').write_text('prompt')
            runner.require('lane', 'one', '--builder', 'codex', '--model', 'gpt-6-luna')
        self.assertEqual(len(self.calls('lane')), 2)


if __name__ == '__main__':
    unittest.main()
