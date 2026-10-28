#!/usr/bin/env python3
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'system/studio-layer-install.py'
spec = importlib.util.spec_from_file_location('installer', source)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def archive(target):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w') as tar:
        for name in ('bin', 'core'):
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for name, body in [('bin/layer-core', None), ('core/run', b'#!/bin/sh\n')]:
            info = tarfile.TarInfo(name)
            if body is None:
                info.type, info.linkname = tarfile.SYMTYPE, target
                tar.addfile(info)
            else:
                info.mode, info.size = 0o755, len(body)
                tar.addfile(info, io.BytesIO(body))
    return data.getvalue()

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    pin = root / 'pin'
    pin.write_text('PENDING\n')
    try:
        mod.install(pin, root, root)
        raise AssertionError('PENDING accepted')
    except ValueError:
        pass
    pin.write_text('a' * 40 + '\n')
    class Result:
        stdout = archive('../core/run')
    with patch.object(mod.subprocess, 'run', return_value=Result()):
        mod.install(pin, root, root)
        mod.install(pin, root, root)
        assert (root / 'usr/local/lib/studio-layer/current/bin/layer-core').exists()
        assert (root / 'etc/studio-layer/hold-config.json').exists()
        (root / 'etc/studio-layer/hold-config.json').write_text('{}')
        try:
            mod.install(pin, root, root)
            raise AssertionError('different config accepted')
        except ValueError:
            pass
        (root / 'etc/studio-layer/hold-config.json').unlink()
        (root / 'usr/local/lib/studio-layer' / ('a' * 40) / 'core/run').chmod(0o755)
        (root / 'usr/local/lib/studio-layer' / ('a' * 40) / 'core/run').write_text('changed')
        try:
            mod.install(pin, root, root)
            raise AssertionError('different release accepted')
        except ValueError:
            pass
    Result.stdout = archive('/outside')
    pin.write_text('b' * 40 + '\n')
    with patch.object(mod.subprocess, 'run', return_value=Result()):
        try:
            mod.install(pin, root, root)
            raise AssertionError('external link accepted')
        except ValueError:
            pass
print('studio-layer installer: PASS')
