#!/usr/bin/env python3
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parents[1] / 'system/studio-layer-install.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid', GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
    return subprocess.run(['/usr/bin/git', '-C', str(repo), *args], env=env, check=True, capture_output=True).stdout.decode().strip()

def refused(call):
    try:
        call()
    except (ValueError, OSError):
        return
    raise AssertionError('unsafe installation accepted')

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    checkout = base / 'layer'
    checkout.mkdir()
    git(checkout, 'init', '-q')
    (checkout / 'bin').mkdir()
    (checkout / 'bin/layer-core').write_text('fixture\n')
    git(checkout, 'add', '.')
    git(checkout, 'commit', '-qm', 'fixture')
    revision = git(checkout, 'rev-parse', 'HEAD')
    pins = base / 'bootstrap/system'
    pins.mkdir(parents=True)
    pin = pins / 'studio-layer.release'
    pin.write_text(revision + '\n')
    binary = base / 'source-archon'
    binary.write_bytes(b'fixture binary')
    (pins / 'studio-layer-archon.sha256').write_text(hashlib.sha256(binary.read_bytes()).hexdigest() + '  ' + str(binary) + '\n')
    root = base / 'root'
    root.mkdir()
    refused(lambda: mod.install(pin, checkout, root))
    real_check = mod.check_custody
    def fixture_check(fd, path, agent, groups):
        # /tmp is necessarily world-writable; model only its ancestors as trusted.
        if path == base or base in path.parents:
            real_check(fd, path, agent, groups)
    with patch.object(mod.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=987654, pw_gid=987654)), patch.object(mod.os, 'getgrouplist', return_value=[987654]), patch.object(mod, 'check_custody', side_effect=fixture_check), patch.object(mod, 'runtime_paths', return_value={'codex': '/fixture/codex', 'node': '/fixture/node', 'uv': '/fixture/uv'}):
        (base / 'bootstrap').chmod(0o777)
        refused(lambda: mod.install(pin, checkout, root))
        (base / 'bootstrap').chmod(0o755)
        entry = base / 'bootstrap/install.sh'
        entry.write_text('fixture\n')
        script = pins / 'studio-layer-install.py'
        script.write_text('fixture\n')
        (base / 'bootstrap').chmod(0o777)
        refused(lambda: mod.open_custodied(entry))
        refused(lambda: mod.open_custodied(script))
        (base / 'bootstrap').chmod(0o755)
        entry.unlink()
        entry.symlink_to(script)
        refused(lambda: mod.open_custodied(entry))
        entry.unlink()
        pin.unlink()
        pin.symlink_to(pins / 'other')
        refused(lambda: mod.install(pin, checkout, root))
        pin.unlink()
        pin.write_text(revision + '\n')
        # Replace the checkout after opening its pin directory; descriptor reads
        # must retain the original pins even when the path now names a symlink.
        replacement = base / 'replacement/system'
        replacement.mkdir(parents=True)
        (replacement / pin.name).write_text('0' * 40 + '\n')
        (replacement / 'studio-layer-archon.sha256').write_text('bad pin\n')
        original_open = mod.open_custodied
        def swap_after_open(path, directory=False):
            fd = original_open(path, directory)
            (base / 'bootstrap').rename(base / 'preserved')
            (base / 'bootstrap').symlink_to(base / 'replacement', target_is_directory=True)
            return fd
        with patch.object(mod, 'open_custodied', side_effect=swap_after_open):
            release, digest = mod.read_pins(pin)
        assert release.strip() == revision and digest.startswith(hashlib.sha256(binary.read_bytes()).hexdigest())
        (base / 'bootstrap').unlink()
        (base / 'preserved').rename(base / 'bootstrap')
        original = mod.subprocess.run
        def wrong_tree(argv, **kwargs):
            result = original(argv, **kwargs)
            if 'rev-parse' in argv:
                result.stdout = b'0' * 40 + b'\n'
            return result
        with patch.object(mod.subprocess, 'run', side_effect=wrong_tree):
            refused(lambda: mod.install(pin, checkout, root))
        mod.install(pin, checkout, root)
        mod.install(pin, checkout, root)
        assert '"node":"/fixture/node"' in (root / 'etc/studio-layer/runtime-paths.json').read_text()
        assert (root / 'usr/local/lib/studio-layer/current/archon').read_bytes() == binary.read_bytes()
        qualified = root / 'etc/studio-layer/qualified'
        qualified.write_text('release=' + revision + '\n')
        with patch.object(mod, 'runtime_paths', return_value={'codex': '/fixture/changed', 'node': '/fixture/node', 'uv': '/fixture/uv'}):
            mod.install(pin, checkout, root)
        assert not qualified.exists()
        qualified.write_text('release=' + revision + '\n')
        hold_config = root / 'etc/studio-layer/hold-config.json'
        hold_config.write_text('invalid')
        before = (root / 'etc/studio-layer/runtime-paths.json').read_bytes()
        with patch.object(mod, 'runtime_paths', return_value={'codex': '/fixture/another', 'node': '/fixture/node', 'uv': '/fixture/uv'}):
            refused(lambda: mod.install(pin, checkout, root))
        assert (root / 'etc/studio-layer/runtime-paths.json').read_bytes() == before
        hold_config.unlink()
        (checkout / 'bin/layer-core').write_text('new release\n')
        git(checkout, 'add', '.')
        git(checkout, 'commit', '-qm', 'upgrade')
        pin.write_text(git(checkout, 'rev-parse', 'HEAD') + '\n')
        mod.install(pin, checkout, root)
        assert not qualified.exists()
print('studio-layer installer: PASS')
