#!/usr/bin/env python3
"""Exercise qualification cleanup after an owner replaces the probe marker."""
import ast
import contextlib
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from unittest.mock import patch

script = (Path(__file__).resolve().parents[1] / 'system/studio-layer-qualify.sh').read_text()
source = script.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
tree = ast.parse(source)
cleanup = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'cleanup')
with tempfile.TemporaryDirectory() as tmp:
    marker = Path(tmp) / 'agent-hold'
    marker.write_text('probe hold\n')
    marker.write_text('owner hold\n')
    calls = []
    namespace = {'held_fd': None, 'release_owned_children': lambda: calls.append('children'),
                 'owner': lambda *args: calls.append(args), '__builtins__': __builtins__}
    exec(compile(ast.Module(body=[cleanup], type_ignores=[]), '<cleanup>', 'exec'), namespace)
    namespace['cleanup']()
    assert marker.read_text() == 'owner hold\n'
    assert calls == ['children']
    assert "'release', '--force'" not in source

# Exercise the actual exception, cleanup and publication control flow with
# synthetic paths, including failures after both individual checks passed.
for failure in ('final-metadata', 'interrupt', 'cleanup', None):
    tree = ast.parse(source)
    experiment = next(node for node in tree.body if isinstance(node, ast.Try))
    body = "checks['a-break-glass'] = checks['b-reader-wait'] = True\n"
    body += "require(False, 'metadata')\n" if failure == 'final-metadata' else 'experiment_ok = True\n'
    if failure == 'interrupt':
        body += 'raise KeyboardInterrupt()\n'
    experiment.body = ast.parse(body).body
    with tempfile.TemporaryDirectory() as tmp:
        etc = Path(tmp) / 'etc'
        etc.mkdir()
        marker = etc / 'qualified'
        class MapPaths(ast.NodeTransformer):
            def visit_Constant(self, node):
                paths = {'/etc/studio-layer': str(etc), '/etc/studio-layer/qualified': str(marker),
                         '/usr/local/lib/studio-layer/current': str(Path(tmp) / ('a' * 40))}
                return ast.copy_location(ast.Constant(paths[node.value]), node) if isinstance(node.value, str) and node.value in paths else node
        program = ast.fix_missing_locations(MapPaths().visit(ast.Module(body=tree.body[tree.body.index(experiment):], type_ignores=[])))
        def require(ok, message):
            if not ok:
                raise RuntimeError(message)
        def clean():
            if failure == 'cleanup':
                raise RuntimeError('cleanup')
        namespace = dict(checks={'a-break-glass': False, 'b-reader-wait': False}, experiment_ok=False,
                         cleanup_ok=False, require=require, cleanup=clean, initial_identity=(1, 2),
                         protected=lambda path: None, os=os, signal=signal, tempfile=tempfile, time=time, sys=sys)
        output = io.StringIO()
        with patch.object(os, 'fchown', lambda *args: None), patch.object(signal, 'signal', lambda *args: None), contextlib.redirect_stdout(output):
            try:
                exec(compile(program, '<qualification>', 'exec'), namespace)
            except SystemExit as error:
                code = error.code
        assert code == (1 if failure else 0)
        assert marker.exists() is (failure is None)
        assert ('FAIL ' in output.getvalue()) is (failure is not None)
print('qualification cleanup: PASS')
