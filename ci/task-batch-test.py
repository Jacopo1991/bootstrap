#!/usr/bin/env python3
"""Exercise task-batch end to end with real Git and a serial task stand-in."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'home/dot_local/bin/executable_task-batch'
loader = importlib.machinery.SourceFileLoader('task_batch', str(TOOL))
spec = importlib.util.spec_from_loader(loader.name, loader)
batch_module = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
loader.exec_module(batch_module)
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
             builder=options[options.index('--builder') + 1], revision='revision', criteria=[]))
    if '--seed' in options:
        import shutil
        seed = options[options.index('--seed') + 1]
        shutil.copy2(repo / '.work' / seed, work / seed)
    print('Dispatch prompt:\nContext: .work/context/\nCommit and fill .work/report.md.')
elif cmd == 'lane':
    prompt = (work / 'prompt.md').read_text()
    with (home / 'prompts.jsonl').open('a') as out:
        out.write(json.dumps(dict(path=str(work / 'prompt.md'), prompt=prompt, args=options)) + '\n')
    record = json.loads((state / 'task.json').read_text())
    record['builder'] = options[options.index('--builder') + 1]
    save(state / 'task.json', record)
    save(work / 'lane.json', dict(state=entry.get('state', 'finished'),
         builder=record['builder'], exit_code=entry.get('exit_code', 0),
         started_at=datetime.now(timezone.utc).isoformat()))
    (work / 'lane.log').write_text(entry.get('log', ''))
    (work / 'final.md').write_text(entry.get('final', ''))
    (work / 'check.log').write_text(entry.get('check_log', ''))
    if not entry.get('unfinished'):
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
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task-batch-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / 'dev_workspace/proj'
        self.repo.mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.home),
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

    def run_batch(self, code=0):
        prior_prompts = len(self.prompts()) if (self.home / 'prompts.jsonl').exists() else 0
        result = subprocess.run([sys.executable, str(TOOL), 'proj', str(self.source)],
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

    def test_escalation_and_round_three_park(self):
        self.scenario({'review': [dict(verdict='FAIL', findings=f'criterion {i}') for i in range(4)]})
        self.assertIn('parked: fix rounds exhausted', self.run_batch())
        lanes = self.calls('lane')
        self.assertEqual([c[c.index('--model') + 1] for c in lanes],
                         ['gpt-6-luna', 'gpt-6-luna', 'gpt-6.1-sol', 'opus'])
        self.assertEqual(lanes[-1][lanes[-1].index('--builder') + 1], 'claude')

    def test_strong_model_skips_round_two(self):
        self.source.write_text(self.source.read_text().replace('gpt-6-luna', 'gpt-6-astra'))
        self.scenario({'review': [dict(verdict='FAIL', findings=f'criterion {i}') for i in range(4)]})
        self.run_batch()
        self.assertEqual(len(self.calls('lane')), 3)
        self.assertIn('opus', self.calls('lane')[-1])

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

    def test_same_check_failure_ignores_head_and_duration(self):
        self.scenario({'check': [{'code': 1, 'output': 'head abc1234 failed in 1.2s'},
                                 {'code': 1, 'output': 'head fed4321 failed in 2.9s'}]})
        self.assertIn('parked: repeated failure', self.run_batch())
        self.assertEqual(len(self.calls('lane')), 2)
        self.assertFalse(self.calls('review'))

    def test_failed_check_fixes_before_spending_review(self):
        self.scenario({'lane': [{'check_log': 'FAILED real fixture assertion'}, {}],
                       'check': [{'code': 1}, {}]})
        self.assertIn('merged after 1 fix rounds', self.run_batch())
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
        self.scenario({'lane': [{'check_log': 'Permission denied: test fixture'}, {}],
                       'check': [{'code': 1, 'output': 'check failed; see check.log'}, {}]})
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


if __name__ == '__main__':
    unittest.main()
