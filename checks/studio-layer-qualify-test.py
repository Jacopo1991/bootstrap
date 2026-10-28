#!/usr/bin/env python3
"""Exercise qualification cleanup after an owner replaces the probe marker."""
import ast
from pathlib import Path
import tempfile

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
print('qualification cleanup: PASS')
