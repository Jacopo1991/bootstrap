"""Offline package-cap rules for resolver constraints and the repository gate.

Only callers supply paths. Production callers anchor policy/module paths to the
root-owned installation; there is deliberately no environment policy override.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import subprocess
import os
import tomllib

try:
    from packaging.utils import parse_sdist_filename, parse_wheel_filename
    from packaging.version import InvalidVersion, Version
except ModuleNotFoundError:
    # The isolated constraint generator cannot see user site-packages. Older bootstrap
    # installs carry packaging inside the distro's root-owned setuptools.
    sys.path.extend(['/usr/lib/python3/dist-packages',
                     '/usr/lib/python3/dist-packages/pkg_resources/_vendor'])
    from packaging.utils import parse_sdist_filename, parse_wheel_filename
    from packaging.version import InvalidVersion, Version

REQUIREMENTS = re.compile(r'^(?:requirements|constraints).*\.(?:txt|in)$', re.I)
SKIP_DIRS = {'.git', '.work', '.venv', 'venv', 'node_modules', '__pycache__', '.worktrees'}
DEFAULT_POLICY = Path('/usr/local/lib/agent-policy/package-caps.toml')


def normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def version_key(text: str) -> Version | None:
    try:
        version = Version(text)
        return None if version.local else version
    except (InvalidVersion, TypeError):
        return None


def image_name(reference: str) -> tuple[str, str | None]:
    name = reference.strip().strip('\"\'').split('@', 1)[0]
    tag = None
    colon = name.rfind(':')
    if colon > name.rfind('/'):
        name, tag = name[:colon], name[colon + 1:]
    name = name.lower()
    for prefix in ('docker.io/', 'index.docker.io/', 'registry-1.docker.io/'):
        name = name.removeprefix(prefix)
    return name.removeprefix('library/'), tag


class Cap:
    def __init__(self, name: str, table: dict):
        if not isinstance(table, dict) or set(table) - {
                'max_version', 'images', 'reason', 'source', 'added', 'approved_digests'}:
            raise ValueError(f'{name}: invalid policy keys/table')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
            raise ValueError('invalid package name')
        self.name = normalize(name)
        for field in ('max_version', 'reason', 'source'):
            if not isinstance(table.get(field), str) or not table[field].strip():
                raise ValueError(f'{name}: {field} must be a nonempty string')
        self.max_version = table['max_version']
        self.key = version_key(self.max_version)
        if self.key is None:
            raise ValueError(f'{name}: invalid max_version')
        if not table['source'].startswith('https://'):
            raise ValueError(f'{name}: source must be HTTPS')
        images = table.get('images', [])
        if not isinstance(images, list) or any(not isinstance(i, str) or not i or
                image_name(i) != (i, None) or '@' in i or '$' in i for i in images):
            raise ValueError(f'{name}: images must be an array of repository names')
        digests = table.get('approved_digests', [])
        if not isinstance(digests, list) or any(not isinstance(d, str) or
                not re.fullmatch(r'sha256:[a-f0-9]{64}', d) for d in digests):
            raise ValueError(f'{name}: invalid approved_digests')
        self.images, self.approved_digests = set(images), set(digests)
        self.reason, self.source = table['reason'], table['source']

    def version_reason(self, version: str | None) -> str | None:
        if version is None:
            return f'{self.name} needs an exact pin (==) or upper bound (<=) at or below {self.max_version}'
        key = version_key(version)
        if key is None:
            return f'{self.name} {version} is not an allowed exact version (cap {self.max_version}; local builds refused)'
        if key > self.key:
            return f'{self.name} {version} is above the cap {self.max_version}'
        return None

    def tag_reason(self, tag: str | None) -> str | None:
        if tag is None:
            return f'image {self.name} has no tag; cap v{self.max_version}'
        # Explicit vendor tag grammar; the whole version is compared, including
        # post/dev/pre releases. Never extract just a numeric substring.
        match = re.fullmatch(r'(?:litellm_stable_release_branch-)?(v?\d[^\s]*?)(?:-stable)?', tag)
        if not match or version_key(match[1]) is None:
            return f'image tag {tag} is refused; cap v{self.max_version}'
        return self.version_reason(match[1])


def load_policy(path: Path) -> dict[str, Cap]:
    data = tomllib.loads(path.read_text(encoding='utf-8'))
    packages = data.get('packages')
    if set(data) != {'packages'} or not isinstance(packages, dict) or not packages:
        raise ValueError('expected nonempty [packages.<name>] tables')
    caps = {normalize(name): Cap(name, table) for name, table in packages.items()}
    if len(caps) != len(packages):
        raise ValueError('duplicate normalized package names')
    return caps


def constraints(caps: dict[str, Cap]) -> list[str]:
    return [f'{cap.name}<={cap.max_version}' for cap in caps.values()]


class Checker:
    def __init__(self, caps: dict[str, Cap]):
        self.caps = caps
        self.findings: list[str] = []

    def error(self, path: Path, line: int, reason: str) -> None:
        rules = '; '.join(f'{c.name} cap {c.max_version}: {c.reason}; {c.source}' for c in self.caps.values())
        self.findings.append(f'{path}:{line}: {reason} ({rules})')

    @staticmethod
    def line_of(text: str, name: str) -> int:
        return next((n for n, row in enumerate(text.splitlines(), 1) if name.lower() in row.lower()), 1)

    def lock_file(self, path: Path) -> None:
        """Read each capped package's own resolved version and source.

        uv, Poetry and PDM use [[package]]; Pipenv keys entries by package name.
        No manifest lookup or lock compatibility walk is needed.
        """
        text = path.read_text(encoding='utf-8')
        if path.name.lower() == 'pipfile.lock':
            data = json.loads(text)
            entries = ((name, entry if isinstance(entry, dict) else {})
                       for section in ('default', 'develop')
                       for name, entry in data.get(section, {}).items())
        else:
            data = tomllib.loads(text)
            entries = ((entry.get('name'), entry)
                       for entry in data.get('package', []) if isinstance(entry, dict))
        for name, entry in entries:
            if not isinstance(name, str) or normalize(name) not in self.caps:
                continue
            cap = self.caps[normalize(name)]
            version = entry.get('version')
            if path.name.lower() == 'pipfile.lock':
                version = version[2:] if isinstance(version, str) and version.startswith('==') else None
            reason = cap.version_reason(version)
            if reason:
                self.error(path, self.line_of(text, name), reason)
            # uv uses source.registry; Poetry uses source.type=legacy for indexes.
            # PDM and Pipenv put direct-reference fields on the package itself.
            source = entry.get('source')
            registry = (isinstance(source, dict) and (
                set(source) == {'registry'} and isinstance(source['registry'], str) or
                path.name.lower() == 'poetry.lock' and source.get('type') == 'legacy'
                and set(source) <= {'type', 'url', 'reference'}))
            if (source is not None and not registry or
                    any(key in entry for key in ('git', 'hg', 'svn', 'bzr', 'path', 'file',
                                                  'url', 'directory', 'editable'))):
                self.error(path, self.line_of(text, name),
                           f'{cap.name} has a non-registry or unrecognized lock source; registry resolution required')

    def pinned_text(self, path: Path, text: str) -> None:
        """Require an allowed pin, upper bound or tag for every whole-token name.

        All non-lock formats, including comments, use the same literal rule.
        No manifest/shell/YAML parsing, expansion or referenced-file resolution.
        """
        for cap in self.caps.values():
            pattern = (r'(?<![A-Za-z0-9_.-])' + re.escape(cap.name) +
                       r'(?=-[0-9]|[^A-Za-z0-9_.-]|$)')
            for match in re.finditer(pattern, text, re.I | re.ASCII):
                line = text.count('\n', 0, match.start()) + 1
                if re.match(r'-[0-9]', text[match.end():]):
                    # Distribution filenames are versions too. Parse the entire
                    # filename with packaging, never a numeric prefix of it.
                    tail = re.match(r'''-[^\s"'`,;\[\]{}()<>|&\\/?#]+''', text[match.end():])[0]
                    filename = match[0] + tail
                    try:
                        parsed = (parse_wheel_filename(filename) if filename.endswith('.whl')
                                  else parse_sdist_filename(filename))
                        reason = cap.version_reason(str(parsed[1]))
                    except ValueError:
                        reason = f'{cap.name} distribution filename cannot be evaluated safely'
                    if reason:
                        self.error(path, line, reason)
                    continue
                # Read the entire adjacent value, never an allowed prefix of it.
                pin = re.match(
                    r'''(?:\[[A-Za-z0-9_,. -]+\])?(?:==|<=)(?P<version>[^\s"'`,;\[\]{}()<>|&\\]+)'''
                    r'''|:(?P<tag>[^\s"'`,;\[\]{}()<>|&\\]+)'''
                    r'''|[ \t]*=[ \t]*["'](?:==|<=)(?P<assigned>[^\s"'`,;\[\]{}()<>|&\\]+)''',
                    text[match.end():])
                if pin is None:
                    reason = cap.version_reason(None)
                elif not re.match(r'''["'\])}]*(?:\s|$|[,;<>|&])''', text[match.end() + pin.end():]):
                    # Quotes/escapes can concatenate a safe prefix into a different
                    # version. A closing delimiter must actually end the word.
                    reason = 'version token does not end the word; quote/escape concatenation refused'
                elif pin['version'] or pin['assigned']:
                    reason = cap.version_reason(pin['version'] or pin['assigned'])
                else:
                    tag, separator, digest = pin['tag'].partition('@')
                    reason = cap.tag_reason(tag)
                    if not reason and separator and digest not in cap.approved_digests:
                        reason = f'image digest is not approved in policy (cap {cap.max_version})'
                if reason:
                    self.error(path, line, reason)

    def check(self, path: Path) -> bool:
        name = path.name.lower()
        try:
            if name in {'uv.lock', 'poetry.lock', 'pipfile.lock', 'pdm.lock'}:
                self.lock_file(path)
            elif (REQUIREMENTS.match(name) or path.suffix.lower() == '.in' or
                  path.suffix.lower() == '.txt' and any(
                      part.lower() in {'requirements', 'constraints'} for part in path.parent.parts) or
                  name in {'pyproject.toml', 'setup.cfg', 'setup.py'} or
                  re.fullmatch(r'dockerfile(?:\..*)?|.*\.dockerfile|(?:docker-)?compose.*\.y.*ml', name)):
                self.pinned_text(path, path.read_text(encoding='utf-8'))
            elif path.suffix == '.py':
                text = path.read_text(encoding='utf-8')
                if not re.search(r'(?m)^# /// script[ \t]*\r?$', text):
                    return False
                self.pinned_text(path, text)
            else:
                return False
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self.error(path, 1, f'cannot evaluate manifest: {error}')
        return True


def repository_files(root: Path) -> list[Path]:
    try:
        top = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                             check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        if Path(top).resolve() != root.resolve():
            raise ValueError('scan root is not a repository root')
        listing = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], check=True,
                                 capture_output=True, text=True, timeout=30).stdout
        return [root / name for name in listing.split('\0') if name]
    except (OSError, ValueError, subprocess.SubprocessError):
        found = []
        for here, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            found.extend(Path(here) / name for name in sorted(files))
        return found


def main(argv=None, *, policy_path=DEFAULT_POLICY) -> int:
    """policy_path is a Python-only test seam, never an argv/environment setting."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--emit-constraints', action='store_true')
    parser.add_argument('--uv-project', metavar='NAME', help='emit a complete constrained project')
    parser.add_argument('paths', nargs='*', type=Path)
    args = parser.parse_args(argv)
    try:
        caps = load_policy(policy_path)
    except (OSError, ValueError, TypeError) as error:
        print(f'package-cap-check: cannot read the policy file {policy_path}: {error}; run install.sh', file=sys.stderr)
        return 2
    if args.emit_constraints:
        print('\n'.join(constraints(caps)))
        return 0
    if args.uv_project:
        print('[project]\nname = ' + json.dumps(args.uv_project))
        print('version = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = []\n')
        print('[tool.uv]\nconstraint-dependencies = ' + json.dumps(constraints(caps)))
        print('build-constraint-dependencies = ' + json.dumps(constraints(caps)))
        return 0
    checker = Checker(caps)
    checked = 0
    for path in args.paths or repository_files(args.root):
        if path.is_file() and checker.check(path):
            checked += 1
    for finding in checker.findings:
        print(finding)
    if checker.findings:
        print(f'package-cap-check: {len(checker.findings)} finding(s) against {policy_path}', file=sys.stderr)
        return 1
    print(f'package-cap-check: OK ({checked} manifest(s), {len(caps)} capped package(s))')
    return 0


if __name__ == '__main__':
    sys.exit(main())
