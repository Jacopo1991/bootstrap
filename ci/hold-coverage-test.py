#!/usr/bin/env python3
"""Every launch and publication needs admission at the action, or a reviewed exception."""
import ast
from collections import Counter
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILES = ('executable_task', 'executable_task-batch', 'executable_gate-browser',
         'executable_workspace-tidy')
STARTS = {'Popen', 'run', 'call', 'check_call', 'check_output'}
PUBLICATIONS = {'push', 'commit-tree', 'merge', 'update-ref'}

# One occurrence per entry; duplicates cannot borrow the same exception.
# The Git/GitHub transports are covered again at every publication caller below.
ALLOWLIST = {
    ('executable_task', 'run_git', 'subprocess.run'): 'Git transport; publication callers audited separately; other calls query or maintain local checkouts.',
    ('executable_task', 'advisory_pre_commit_environment', 'subprocess.run'): 'Read-tree populates a private disposable index for the current gate step.',
    ('executable_task', 'runner_service', 'subprocess.run'): 'Read-only systemctl show query.',
    ('executable_task', 'start_locked', 'subprocess.run'): 'Read-only command availability query.',
    ('executable_task', 'sync_dependencies_after_merge', 'subprocess.run'): 'Read-only just --show sync query.',
    ('executable_task', 'gh', 'subprocess.run'): 'GitHub transport; every caller is audited below.',
    ('executable_task', 'open_pr', 'subprocess.run'): 'Read-only gh pr list query.',
    ('executable_task', 'cancel', 'subprocess.run'): 'Operator cancellation must remain available during hold.',
    ('executable_task:RUNNER_WRAPPER', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_task-batch', 'git', 'subprocess.run'): 'Read-only Git queries; batch publication delegates to guarded task commands.',
    ('executable_task-batch', 'run_git', 'subprocess.run'): 'Read-only Git history and merge-tree queries.',
    ('executable_task-batch', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_gate-browser', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_workspace-tidy', 'git', 'subprocess.run'): 'Git transport; deletion callers audited separately, other calls are queries.',
    ('executable_workspace-tidy', 'admit', 'subprocess.run'): 'Absolute installed hold checker itself.',
    ('executable_task', 'merge', 'gh:pr view'): 'Read-only publication read-back.',
    ('executable_task', 'close_pr', 'gh:pr list'): 'Read-only PR query.',
    ('executable_task', 'close_with_pr', 'gh:pr view'): 'Read-only publication read-back.',
    ('executable_task', 'gate_merge_result', 'run_git:merge --abort'): 'Cancel a conflicted disposable gate merge.',
}


def name(node):
    return ast.unparse(node)


def action(node):
    """Classify calls without relying on source line numbers or command variable names."""
    callee = name(node.func)
    if (callee.startswith('subprocess.') and callee.split('.')[-1] in STARTS
            or callee.startswith('os.') and callee.split('.')[-1].startswith(('exec', 'spawn'))):
        return callee
    literals = [arg.value for arg in node.args[1:] if isinstance(arg, ast.Constant)]
    if callee == 'gh':
        return 'gh:' + ' '.join(str(value) for value in literals[:2])
    if callee in {'git', 'run_git', 'ok'} and literals:
        if literals[0] in PUBLICATIONS:
            if literals[:2] == ['merge', '--abort']: return callee + ':merge --abort'
            return callee + ':' + str(literals[0])
    return None


def inventory(source, filename):
    sites = []
    def visit(node, scope='<module>', previous=None):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
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
            sites.append(((filename, scope, kind), node.lineno, guarded))
        for _, field in ast.iter_fields(node):
            if isinstance(field, list):
                prior = None
                for child in field:
                    if isinstance(child, ast.AST):
                        visit(child, scope, prior if isinstance(child, ast.stmt) else previous)
                        if isinstance(child, ast.stmt): prior = child
            elif isinstance(field, ast.AST):
                visit(field, scope, previous)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'RUNNER_WRAPPER'
                                               for t in node.targets):
            sites.extend(inventory(ast.literal_eval(node.value), filename + ':RUNNER_WRAPPER'))
    visit(ast.parse(source))
    return sites


class Coverage(unittest.TestCase):
    def test_production_coverage(self):
        exceptions = Counter()
        failures = []
        for filename in FILES:
            source = (ROOT / 'home/dot_local/bin' / filename).read_text()
            for key, line, guarded in inventory(source, filename):
                print(f'{key[0]}:{line} {key[1]} {key[2]}: '
                      f'{"admit" if guarded else ALLOWLIST.get(key, "UNGUARDED")}')
                if not guarded:
                    exceptions[key] += 1
                    if key not in ALLOWLIST or exceptions[key] > 1:
                        failures.append((key, line))
        self.assertFalse(failures, failures)
        self.assertEqual(set(exceptions), set(ALLOWLIST), 'Remove stale exceptions')

    def test_new_launches_and_publications_fail_closed(self):
        calls = ('subprocess.Popen(cmd)', 'subprocess.run(cmd)', 'subprocess.call(cmd)',
                 'subprocess.check_call(cmd)', 'subprocess.check_output(cmd)',
                 'os.execv(path, argv)', 'os.spawnvp(mode, path, argv)',
                 "git(repo, 'push', 'origin')", "run_git(repo, 'commit-tree', tree)",
                 "gh(repo, 'pr', 'merge', pr)", "git(repo, 'merge', head)")
        for call in calls:
            for prefix, expected in (('', False), ('admit()\n', True),
                                     ('admit()\ntime.sleep(1)\n', False),
                                     ('if ready:\n    admit()\n', False),
                                     ('admit()\nfcntl.flock(lock, flag)\n', False),
                                     ('admit()\nsubprocess.run(other)\n', False)):
                with self.subTest(call=call, prefix=prefix):
                    self.assertEqual(inventory(prefix + call, 'new')[-1][2], expected)


if __name__ == '__main__':
    unittest.main()
