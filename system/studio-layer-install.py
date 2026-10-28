#!/usr/bin/env python3
"""Install a pinned, self-contained studio layer. Root entry point has no overrides."""
import io
import hashlib
import errno
import json
import os
import pwd
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


def runtime_paths():
    # Resolve in the agent's login environment, never the invoking admin's PATH.
    script = 'command -v codex; mise which node; mise which uv'
    result = subprocess.run(['/usr/sbin/runuser', '-u', 'agent', '--', '/bin/bash', '-lc', script],
                            check=True, capture_output=True, text=True, timeout=30)
    lines = result.stdout.splitlines()
    if len(lines) != 3:
        raise ValueError('runtime resolution failed')
    paths = {name: str(Path(value).resolve(strict=True)) for name, value in zip(('codex', 'node', 'uv'), lines)}
    if any(not Path(value).is_absolute() or not Path(value).is_file() or not os.access(value, os.X_OK) for value in paths.values()):
        raise ValueError('runtime path unavailable')
    return paths


def check_custody(fd, path, agent, agent_groups):
    info = os.fstat(fd)
    if (info.st_uid == agent.pw_uid or info.st_mode & 0o002
            or (info.st_mode & 0o020 and info.st_gid in agent_groups)):
        raise ValueError('unsafe pin custody: ' + str(path))
    try:
        os.getxattr(fd, 'system.posix_acl_access')
    except OSError as exc:
        if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
            raise
    else:
        raise ValueError('pin custody ACL requires review: ' + str(path))


def open_custodied(path, directory=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('custody path must be absolute and normalized')
    agent = pwd.getpwnam('agent')
    agent_groups = set(os.getgrouplist('agent', agent.pw_gid))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = Path('/')
        check_custody(fd, current, agent, agent_groups)
        for i, part in enumerate(path.parts[1:]):
            is_dir = i < len(path.parts[1:]) - 1 or directory
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_dir else os.O_NONBLOCK)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            current /= part
            check_custody(fd, current, agent, agent_groups)
            if not is_dir and not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('custody file is not regular: ' + str(current))
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_pins(pin):
    pin = Path(pin)
    agent = pwd.getpwnam('agent')
    groups = set(os.getgrouplist('agent', agent.pw_gid))
    directory = open_custodied(pin.parent, directory=True)
    try:
        values = []
        for name in (pin.name, 'studio-layer-archon.sha256'):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'r') as source:
                check_custody(source.fileno(), pin.parent / name, agent, groups)
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError('pin is not a regular file')
                values.append(source.read())
        return values
    finally:
        os.close(directory)


def install(pin, checkout, root=Path('/')):
    revision, digest = read_pins(pin)
    fields = digest.strip().split(maxsplit=1)
    if len(fields) != 2 or not re.fullmatch(r'[0-9a-f]{64}', fields[0]) or not Path(fields[1]).is_absolute():
        raise ValueError('invalid Archon pin')
    archon_bytes = Path(fields[1]).read_bytes()
    if hashlib.sha256(archon_bytes).hexdigest() != fields[0]:
        raise ValueError('Archon digest mismatch')
    def protected_dir(path):
        path.mkdir(parents=True, exist_ok=True)
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or (root == Path('/') and info.st_uid != 0):
                raise ValueError('unsafe installation directory: ' + str(current))
    revision = revision.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('release pin must be a 40-hex commit; PENDING cannot be installed')
    git_env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
               'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null'}
    archive = subprocess.run(['/usr/bin/git', '-c', 'safe.directory=' + str(checkout), '-C', str(checkout), 'archive', '--format=tar', revision], check=True, stdout=subprocess.PIPE, env=git_env).stdout
    expected_tree = subprocess.run(['/usr/bin/git', '-c', 'safe.directory=' + str(checkout), '-C', str(checkout), 'rev-parse', revision + '^{tree}'], check=True, stdout=subprocess.PIPE, env=git_env).stdout.decode().strip()
    lib = root / 'usr/local/lib/studio-layer'
    protected_dir(lib)
    with tempfile.TemporaryDirectory(prefix='.release-', dir=lib) as temp:
        stage = Path(temp) / 'tree'
        stage.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            members = tar.getmembers()
            for m in members:
                p = PurePosixPath(m.name)
                if p.is_absolute() or '..' in p.parts or not (m.isdir() or m.isfile() or m.issym()):
                    raise ValueError('unsafe archive member')
                if m.issym():
                    target = PurePosixPath(m.linkname)
                    if target.is_absolute():
                        raise ValueError('archive symlink points outside release')
                    parts = list(p.parent.parts)
                    for part in target.parts:
                        if part == '..':
                            if not parts:
                                raise ValueError('archive symlink points outside release')
                            parts.pop()
                        elif part != '.':
                            parts.append(part)
                # A later member must never traverse an archive symlink.
                if any(parent.name and parent.as_posix() in {x.name.rstrip('/') for x in members if x.issym()} for parent in p.parents):
                    raise ValueError('archive member beneath symlink')
            tar.extractall(stage, members=members, filter='data')
        index = Path(temp) / 'index'
        git_dir = Path(temp) / 'verify.git'
        subprocess.run(['/usr/bin/git', 'init', '--bare', '--quiet', str(git_dir)], check=True, env=git_env)
        index_env = dict(git_env, GIT_INDEX_FILE=str(index), GIT_WORK_TREE=str(stage), GIT_DIR=str(git_dir))
        git_cmd = ['/usr/bin/git']
        subprocess.run(git_cmd + ['read-tree', '--empty'], check=True, env=index_env)
        subprocess.run(git_cmd + ['-c', 'core.autocrlf=false', 'add', '-f', '--all', '--', '.'], check=True, env=index_env)
        actual_tree = subprocess.run(git_cmd + ['write-tree'], check=True, stdout=subprocess.PIPE, env=index_env).stdout.decode().strip()
        if actual_tree != expected_tree:
            raise ValueError('archived tree mismatch')
        if (stage / 'archon').exists() or (stage / 'archon').is_symlink():
            raise ValueError('archive collides with installed Archon')
        (stage / 'archon').write_bytes(archon_bytes)
        (stage / 'archon').chmod(0o755)
        for p in stage.rglob('*'):
            if not p.is_symlink():
                p.chmod((0o555 if p.is_dir() or p.stat().st_mode & 0o111 else 0o444))
                if root == Path('/'):
                    os.chown(p, 0, 0)
        dest = lib / revision
        if dest.exists() or dest.is_symlink():
            def snapshot(base):
                return {str(p.relative_to(base)): ('link', os.readlink(p)) if p.is_symlink() else ('dir', p.stat().st_mode & 0o777) if p.is_dir() else ('file', p.read_bytes(), p.stat().st_mode & 0o777) for p in base.rglob('*')}
            if dest.is_symlink() or not dest.is_dir() or snapshot(dest) != snapshot(stage) or dest.stat().st_mode & 0o222:
                raise ValueError('existing release differs')
            if root == Path('/') and any(not p.is_symlink() and p.stat().st_uid != 0 for p in [dest, *dest.rglob('*')]):
                raise ValueError('existing release is not root-owned')
            for p in stage.rglob('*'):
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
        else:
            os.rename(stage, dest)
            dest.chmod(0o555)
    config = {'version': 1, 'actor': 'seat', 'test_mode': False, 'bootstrap_hold_file': '/etc/agent-hold',
              'executor': {'argv': ['/usr/local/bin/layer-executor'], 'state': '/home/agent/.local/state/studio-layer/state'},
              'runner': {'argv': ['/usr/local/bin/layer-agent-runners'], 'state': '/home/agent/.local/state/studio-layer/state'}}
    etc = root / 'etc/studio-layer'
    protected_dir(etc)
    runtimes = (json.dumps(runtime_paths(), separators=(',', ':')) + '\n').encode()
    runtime_target = etc / 'runtime-paths.json'
    if runtime_target.is_symlink() or (runtime_target.exists() and (not runtime_target.is_file() or runtime_target.stat().st_mode & 0o777 != 0o644 or (root == Path('/') and runtime_target.stat().st_uid != 0))):
        raise ValueError('unsafe runtime configuration')
    with tempfile.NamedTemporaryFile(dir=etc, delete=False) as f:
        f.write(runtimes)
        os.fchmod(f.fileno(), 0o644)
        runtime_tmp = f.name
    target = etc / 'hold-config.json'
    data = (json.dumps(config, separators=(',', ':')) + '\n').encode()
    if target.is_symlink() or (target.exists() and target.read_bytes() != data):
        raise ValueError('existing hold config differs')
    if target.exists() and (not target.is_file() or target.stat().st_mode & 0o777 != 0o644 or (root == Path('/') and target.stat().st_uid != 0)):
        raise ValueError('existing hold config has unsafe metadata')
    bindir = root / 'usr/local/bin'
    protected_dir(bindir)
    wrappers = []
    for entry in (dest / 'bin').iterdir():
        if not entry.name.startswith('layer-'):
            continue
        wrapper = bindir / entry.name
        content = '#!/bin/sh\nexec /usr/local/lib/studio-layer/current/bin/' + entry.name + ' "$@"\n'
        with tempfile.NamedTemporaryFile(mode='w', dir=bindir, delete=False) as f:
            f.write(content)
            os.fchmod(f.fileno(), 0o755)
            name = f.name
        wrappers.append((name, wrapper))
    link = lib / ('.current-' + str(os.getpid()))
    current = lib / 'current'
    runtime_changed = not runtime_target.exists() or runtime_target.read_bytes() != runtimes
    if runtime_changed or not current.is_symlink() or os.readlink(current) != revision:
        qualified = etc / 'qualified'
        if qualified.is_symlink():
            raise ValueError('unsafe qualification marker')
        if qualified.exists():
            qualified.unlink()
    # All inputs and destinations have been validated and staged before publishing.
    os.symlink(revision, link)
    if not target.exists():
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
    for name, wrapper in wrappers:
        os.replace(name, wrapper)
    os.replace(runtime_tmp, runtime_target)
    os.replace(link, current)


if __name__ == '__main__':
    try:
        script = Path(__file__).absolute()
        entry = Path(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == '--check-custody' else script.parent.parent / 'install.sh'
        for path in (script, entry):
            fd = open_custodied(path)
            os.close(fd)
        if sys.argv[1] != '--check-custody':
            install(sys.argv[1], sys.argv[2])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('studio-layer install refused:', exc, file=sys.stderr)
        sys.exit(1)
