#!/usr/bin/env python3
"""Every launch and publication needs admission at the action, or a reviewed exception."""
import ast
from collections import Counter
from pathlib import Path
import re
import shlex
import subprocess
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILES = ('executable_task', 'executable_task-batch', 'executable_gate-browser',
         'executable_workspace-tidy')
STARTS = {'Popen', 'run', 'call', 'check_call', 'check_output'}
PUBLICATIONS = {'push', 'commit-tree', 'merge', 'update-ref'}

# One occurrence per entry; duplicates cannot borrow the same exception.
# Runtime-classified transports are tested under a hold as well as at their callers.
ALLOWLIST = {
    ('executable_task', 'run_git', 'subprocess.run'): 'Runtime queries and cancellation (merge --abort, worktree prune, named disposable gates); every other Git command admits at transport.',
    ('executable_task', 'git', 'run_git:<dynamic>'): ('Forwards unchanged to runtime-classified Git transport.', 'run_git(cwd, *args)'),
    ('executable_task', 'ok', 'run_git:<dynamic>'): ('Forwards unchanged to runtime-classified Git transport.', 'run_git(cwd, *args)'),
    ('executable_task', 'runner_service', 'subprocess.run'): 'Read-only systemctl show query.',
    ('executable_task', 'start_locked', 'subprocess.run'): 'Read-only command availability query.',
    ('executable_task', 'sync_dependencies_after_merge', 'subprocess.run'): 'Read-only just --show sync query.',
    ('executable_task', 'gh', 'subprocess.run'): 'Runtime PR list/view allowlist; all other GitHub commands admit at transport.',
    ('executable_task', 'open_pr', 'gh:pr list <dynamic>'): ('Read-only PR list with a timeout, via runtime-classified transport.', "gh(repo, 'pr', 'list', '--head', branch, '--state', 'open', '--json', 'number', '--jq', '.[0].number', timeout=60)"),
    ('executable_task', 'cancel', 'subprocess.run'): 'Operator cancellation must remain available during hold.',
    ('executable_task:RUNNER_WRAPPER', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_task-batch', 'git', 'run_git:<dynamic>'): ('Forwards unchanged to runtime-classified Git transport.', 'run_git(repo, *args)'),
    ('executable_task-batch', 'run', 'run_git:<dynamic>'): ('Merge-history closure forwards unchanged to runtime-classified Git transport.', 'run_git(path, *args)'),
    ('executable_task-batch', 'run_git', 'subprocess.run'): 'Runtime query allowlist; all other commands admit at transport.',
    ('executable_task-batch', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_gate-browser', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_workspace-tidy', 'git', 'subprocess.run'): 'Runtime query allowlist; all other commands, including deletions, admit at transport.',
    ('executable_workspace-tidy', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_task', 'merge', 'gh:pr view'): 'Read-only publication read-back.',
    ('executable_task', 'close_pr', 'gh:pr list'): 'Read-only PR query.',
    ('executable_task', 'close_with_pr', 'gh:pr view'): 'Read-only publication read-back.',
    ('executable_task', 'gate_merge_result', 'run_git:merge --abort'): 'Cancel a conflicted disposable gate merge.',
    ('executable_task', 'remove_gate_worktree', 'run_git:worktree prune'): (
        'Cancellation maintenance: prune stale worktree metadata after failed gate removal; moves no branch.',
        "run_git(task.repo, 'worktree', 'prune')"),
    ('executable_task', 'apply_close_update', 'ok:diff <dynamic>'): (
        'Read-only cached diff; expanded paths follow -- and cannot select another command.',
        "ok(kb, 'diff', '--cached', '--quiet', '--', *paths)"),
    ('executable_workspace-tidy', 'all_contained', 'git:rev-list <dynamic>'): (
        'Read-only reachability query; expanded commit and ref sets do not publish or delete refs.',
        "git(repo, 'rev-list', '--max-count=1', *sorted(commits), '--not', *refs)"),
    ('executable_workspace-tidy', 'removal_blocker', 'git:<dynamic>'): (
        'Literal status query with fixed global options; no dynamic subcommand.',
        "git(path, '--no-optional-locks', '-c', 'status.showUntrackedFiles=all', 'status', '--porcelain', '-z', '--untracked-files=all', '--ignore-submodules=none', '--ignored=matching')"),
}

# Only these functions may execute Git/GitHub. Even an admitted raw call elsewhere fails.
TRANSPORTS = {('executable_task', 'run_git'): 'git', ('executable_task', 'gh'): 'gh',
              ('executable_task-batch', 'run_git'): 'git',
              ('executable_workspace-tidy', 'git'): 'git'}


def name(node):
    return ast.unparse(node)


def action(node):
    """Classify calls without relying on source line numbers or command variable names."""
    callee = name(node.func)
    if (callee.startswith('subprocess.') and callee.split('.')[-1] in STARTS
            or callee.startswith('os.') and callee.split('.')[-1].startswith(('exec', 'spawn'))):
        return callee
    if callee in {'git', 'run_git', 'ok', 'gh'}:
        args = node.args[1:]
        width = 2 if callee == 'gh' else 1
        header = args[:width]
        literal = len(header) == width and all(isinstance(arg, ast.Constant)
                                               and isinstance(arg.value, str) for arg in header)
        # Leading global options may hide the actual subcommand; require an exception.
        if literal and header[0].value.startswith('-'):
            literal = False
        dynamic = (not literal or any(isinstance(arg, ast.Starred) for arg in args)
                   or bool(node.keywords))
        command = ' '.join(arg.value for arg in header) if literal else ''
        if dynamic:
            return callee + ':' + (command + ' ' if command else '') + '<dynamic>'
        if callee == 'gh':
            return 'gh:' + command
        if command == 'worktree' and len(args) > 1 and isinstance(args[1], ast.Constant) and args[1].value == 'prune':
            return callee + ':worktree prune'
        if command in PUBLICATIONS:
            if len(args) > 1 and isinstance(args[1], ast.Constant) and args[1].value == '--abort':
                return callee + ':merge --abort'
            return callee + ':' + command
    return None


def inventory(source, filename):
    sites = []
    def visit(node, scope='<module>', previous=None, assignments=None):
        assignments = {} if assignments is None else assignments
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
            assignments = {target.id: child.value for child in ast.walk(node)
                           if isinstance(child, ast.Assign) for target in child.targets
                           if isinstance(target, ast.Name)}
        if isinstance(node, ast.Call) and (filename == 'executable_workspace-tidy'
                                           or scope == 'prune_evidence_archive'):
            callee = name(node.func)
            literals = [a.value for a in node.args[1:] if isinstance(a, ast.Constant)]
            deletion = (callee in {'shutil.rmtree', 'item.unlink', 'item.rmdir'}
                        or callee == 'git' and literals and literals[0] in {'worktree', 'branch'}
                        and any(v in {'remove', '-D', '-d'} for v in literals))
        else:
            deletion = False
        if isinstance(node, ast.Call) and (kind := action(node) or (callee if deletion else None)):
            guarded = (isinstance(previous, ast.Expr) and isinstance(previous.value, ast.Call)
                       and name(previous.value.func) == 'admit' and not previous.value.args
                       and not previous.value.keywords)
            sites.append(((filename, scope, kind), node.lineno, guarded, name(node)))
            if kind.startswith(('subprocess.', 'os.exec', 'os.spawn')) and node.args:
                argv = node.args[0]
                if isinstance(argv, ast.Name): argv = assignments.get(argv.id, argv)
                executable = argv.elts[0] if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts else argv
                if isinstance(executable, ast.Constant) and isinstance(executable.value, str):
                    binary = executable.value.split()[0].rsplit('/', 1)[-1]
                    if binary in {'git', 'gh'} and TRANSPORTS.get((filename, scope)) != binary:
                        sites.append(((filename, scope, 'raw:' + binary), node.lineno, False, name(node)))
        for _, field in ast.iter_fields(node):
            if isinstance(field, list):
                prior = None
                for child in field:
                    if isinstance(child, ast.AST):
                        visit(child, scope, prior if isinstance(child, ast.stmt) else previous, assignments)
                        if isinstance(child, ast.stmt): prior = child
            elif isinstance(field, ast.AST):
                visit(field, scope, previous, assignments)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'RUNNER_WRAPPER'
                                               for t in node.targets):
            sites.extend(inventory(ast.literal_eval(node.value), filename + ':RUNNER_WRAPPER'))
    visit(ast.parse(source))
    return sites


class Coverage(unittest.TestCase):
    def assert_coverage(self, sources, verbose=False):
        exceptions = Counter()
        failures = []
        for filename, source in sources.items():
            for key, line, guarded, expression in inventory(source, filename):
                entry = ALLOWLIST.get(key)
                reason, expected = entry if isinstance(entry, tuple) else (entry, None)
                if verbose:
                    print(f'{key[0]}:{line} {key[1]} {key[2]}: '
                          f'{"admit" if guarded else reason or "UNGUARDED"}')
                if not guarded:
                    exceptions[key] += 1
                    if key not in ALLOWLIST or exceptions[key] > 1 or expected and expression != expected:
                        failures.append((key, line))
        self.assertFalse(failures, failures)
        self.assertEqual(set(exceptions), set(ALLOWLIST), 'Remove stale exceptions')

    def sources(self):
        return {filename: (ROOT / 'home/dot_local/bin' / filename).read_text()
                for filename in FILES}

    def test_production_coverage(self):
        self.assert_coverage(self.sources(), verbose=True)

    def test_synthetic_dynamic_publication_in_source_copy_fails_coverage(self):
        self.assert_coverage(self.sources())
        for setup, call in (("command = 'push'", "git(repo, command, 'origin')"),
                            ("args = ['push', 'origin']", 'run_git(repo, *args)'),
                            ("command = 'merge'", "gh(repo, 'pr', command, pr)")):
            with self.subTest(call=call):
                sources = self.sources()
                # Mutate a copy only; use the same full-source assertion as the gate.
                original = sources['executable_task']
                added = '\ndef synthetic_publication(repo, pr):\n    ' + setup + '\n    '
                sources['executable_task'] = original + added + call + '\n'
                with self.assertRaisesRegex(AssertionError, 'synthetic_publication'):
                    self.assert_coverage(sources)
                sources['executable_task'] = original + added + 'admit()\n    ' + call + '\n'
                self.assert_coverage(sources)

    def test_raw_git_and_gh_must_use_runtime_helpers_even_with_admission(self):
        self.assert_coverage(self.sources())
        for filename in FILES:
            for call in ("subprocess.run(['git', command, 'origin'])",
                         "subprocess.Popen(['/usr/bin/gh', 'pr', command, pr])",
                         "argv = ['git', *args]\n    subprocess.check_call(argv)"):
                for admission in ('', 'admit()\n    '):
                    with self.subTest(filename=filename, call=call, admission=admission):
                        sources = self.sources()
                        sources[filename] += '\ndef synthetic_raw(command, pr, args):\n    ' + admission + call + '\n'
                        with self.assertRaisesRegex(AssertionError, 'raw:'):
                            self.assert_coverage(sources)

    def test_new_launches_and_publications_fail_closed(self):
        calls = ('subprocess.Popen(cmd)', 'subprocess.run(cmd)', 'subprocess.call(cmd)',
                 'subprocess.check_call(cmd)', 'subprocess.check_output(cmd)',
                 'os.execv(path, argv)', 'os.spawnvp(mode, path, argv)',
                 "git(repo, 'push', 'origin')", "run_git(repo, 'commit-tree', tree)",
                 "gh(repo, 'pr', 'merge', pr)", "git(repo, 'merge', head)",
                 "git(repo, command, 'status')", 'git(repo, *args)',
                 'run_git(repo, command)', 'ok(repo, *args)',
                 "git(repo, 'push', *args)", "git(repo, '--no-optional-locks', command)",
                 "gh(repo, 'pr', command)",
                 "gh(repo, command, 'pr', 'view')", "gh(repo, 'pr', 'view', *args)")
        for call in calls:
            for prefix, expected in (('', False), ('admit()\n', True),
                                     ('admit()\ntime.sleep(1)\n', False),
                                     ('if ready:\n    admit()\n', False),
                                     ('admit()\nfcntl.flock(lock, flag)\n', False),
                                     ('admit()\nsubprocess.run(other)\n', False)):
                with self.subTest(call=call, prefix=prefix):
                    self.assertEqual(inventory(prefix + call, 'new')[-1][2], expected)

    def test_runtime_helpers_classify_forwarded_commands_at_transport(self):
        # Execute the actual wrapper ASTs, with inert transports: no real Git, HOME or /etc.
        for filename, helpers, queries in (
                ('executable_task', ('run_git', 'git', 'ok'),
                 [('status', '--porcelain'), ('remote', 'get-url', 'origin'),
                  ('worktree', 'list'), ('worktree', 'prune'), ('branch', '--show-current'),
                  ('merge', '--abort'),
                  ('worktree', 'remove', '--force', '/inert/.worktrees/.drill-aabbcc-0-group'),
                  ('worktree', 'remove', '--force', '/inert/.worktrees/.task-gate-t5-123'),
                  ('symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD')]),
                ('executable_task-batch', ('run_git', 'git'),
                 [('status', '--porcelain'), ('rev-parse', 'HEAD'),
                  ('merge-tree', '--write-tree', 'a', 'b')]),
                ('executable_workspace-tidy', ('git',),
                 [('reflog', 'show', 'main'), ('worktree', 'list'),
                  ('config', '--bool', 'core.sparseCheckout'),
                  ('--no-optional-locks', '-c', 'status.showUntrackedFiles=all', 'status', '--porcelain')])):
            selected = set(helpers) | ({'gh'} if filename == 'executable_task' else set())
            tree = ast.parse(self.sources()[filename])
            wrappers = ast.Module(body=[node for node in tree.body
                                       if isinstance(node, ast.FunctionDef) and node.name in selected],
                                  type_ignores=[])
            events = []
            held = True
            def admit():
                events.append('admit')
                if held: raise RuntimeError('refused: hold')
            def transport(*args, **kwargs):
                events.append('launch')
                return subprocess.CompletedProcess(args, 0, stdout='query\n', stderr='')
            namespace = dict(subprocess=SimpleNamespace(run=transport, CompletedProcess=subprocess.CompletedProcess,
                                                        TimeoutExpired=subprocess.TimeoutExpired, DEVNULL=subprocess.DEVNULL), Path=Path,
                             Stop=RuntimeError, admit=admit, shlex=shlex, re=re)
            exec(compile(wrappers, filename, 'exec'), namespace)
            for helper in helpers:
                for args in queries:
                    with self.subTest(filename=filename, helper=helper, query=args):
                        events.clear()
                        namespace[helper](Path('/inert'), *args)
                        self.assertEqual(events, ['launch'])
                for args in [(), ('push', 'origin'), ('merge', '--ff-only', 'topic'),
                             ('commit-tree', 'tree'), ('update-ref', 'refs/heads/main', 'sha'),
                             ('unknown-alias',), ('--no-optional-locks', 'push', 'origin'),
                             ('remote', 'set-url', 'origin', 'elsewhere'),
                             ('symbolic-ref', 'HEAD', 'refs/heads/main'),
                             ('branch', '-D', 'topic'), ('worktree', 'remove', 'path'),
                             ('worktree', 'remove', '--force', '/other/.worktrees/.drill-aabbcc-0-group'),
                             ('worktree', 'remove', '--force', '/inert/.worktrees/topic')]:
                    with self.subTest(filename=filename, helper=helper, publication=args):
                        held = True
                        events.clear()
                        with self.assertRaisesRegex(RuntimeError, 'refused: hold'):
                            namespace[helper](Path('/inert'), *args)
                        self.assertEqual(events, ['admit'])
                        held = False
                        events.clear()
                        namespace[helper](Path('/inert'), *args)
                        self.assertEqual(events, ['admit', 'launch'])
                held = True
            if 'gh' in selected:
                for args in [('pr', 'list'), ('pr', 'view', '1'), ('pr', 'create'),
                             ('pr', 'merge', '1'), ('api', 'endpoint'), ('unknown',)]:
                    query = args[:2] in [('pr', 'list'), ('pr', 'view')]
                    for held in (True, False):
                        events.clear()
                        if held and not query:
                            with self.assertRaisesRegex(RuntimeError, 'refused: hold'):
                                namespace['gh'](Path('/inert'), *args)
                        else:
                            namespace['gh'](Path('/inert'), *args)
                        self.assertEqual(events, ['launch'] if query else
                                         ['admit'] if held else ['admit', 'launch'])


if __name__ == '__main__':
    unittest.main()
