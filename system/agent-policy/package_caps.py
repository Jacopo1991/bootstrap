"""Offline package-cap rules for resolver constraints and the repository gate.

Only callers supply paths. Production callers anchor policy/module paths to the
root-owned installation; there is deliberately no environment policy override.
"""
from __future__ import annotations

import configparser
import argparse
import json
from pathlib import Path
import re
import sys
import subprocess
import os
import tomllib
from urllib.parse import unquote, urlsplit

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ModuleNotFoundError:
    # The isolated constraint generator cannot see user site-packages. Older bootstrap
    # installs carry packaging inside the distro's root-owned setuptools.
    sys.path.extend(['/usr/lib/python3/dist-packages',
                     '/usr/lib/python3/dist-packages/pkg_resources/_vendor'])
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

REQUIREMENTS = re.compile(r'^(?:requirements|constraints).*\.(?:txt|in)$', re.I)
SKIP_DIRS = {'.git', '.work', '.venv', 'venv', 'node_modules', '__pycache__', '.worktrees'}
DEFAULT_POLICY = Path('/usr/local/lib/agent-policy/package-caps.toml')


def normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def mentions_cap(text: str, caps: dict[str, Cap]) -> bool:
    """Recognize policy names in otherwise unevaluable dependency/command text."""
    text = unquote(text)
    return any(re.search(r'(?<![a-z0-9])' + re.escape(name).replace(r'\-', r'[-_.]+') +
                         r'(?![a-z0-9])', text, re.I) for name in caps)


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
            return f'{self.name} must be pinned exactly (==) at or below {self.max_version}'
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


def parse_requirement(text: str) -> tuple[str, str | None, bool] | None:
    try:
        req = Requirement(text.strip())
    except InvalidRequirement:
        return None
    specs = list(req.specifier)
    version = specs[0].version if len(specs) == 1 and specs[0].operator == '==' and '*' not in specs[0].version else None
    if req.url:
        version = None
    return normalize(req.name), version, version is not None


def poetry_spec(value) -> str | None:
    if isinstance(value, dict):
        if set(value) & {'git', 'path', 'url', 'file', 'source'}:
            return None
        value = value.get('version', '*')
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value == '*':
        return ''
    if version_key(value):
        return '==' + value
    if value.startswith(('^', '~')) and not value.startswith('~='):
        base = value[1:]
        key = version_key(base)
        if key is None:
            return None
        release = list(key.release)
        index = (next((i for i, n in enumerate(release) if n), len(release) - 1)
                 if value[0] == '^' else max(0, len(release) - 2))
        upper = release[:index + 1]
        upper[-1] += 1
        return f'>={base},<' + '.'.join(map(str, upper))
    try:
        SpecifierSet(value)
        return value
    except InvalidSpecifier:
        return None


def poetry_requirement(name: str, value) -> tuple[str, str | None, bool]:
    spec = poetry_spec(value)
    return parse_requirement(name + spec) if spec is not None else (normalize(name), None, False)


class Checker:
    def __init__(self, caps: dict[str, Cap]):
        self.caps = caps
        self.findings: list[str] = []
        self.active: set[Path] = set()

    def error(self, path: Path, line: int, reason: str) -> None:
        rules = '; '.join(f'{c.name} cap {c.max_version}: {c.reason}; {c.source}' for c in self.caps.values())
        self.findings.append(f'{path}:{line}: {reason} ({rules})')

    def requirement(self, path: Path, line: int, text: str) -> bool:
        """Check named policy packages; return whether an allowed pin was read."""
        if not mentions_cap(text, self.caps):
            return False
        is_artifact = ' @ ' not in text and (text.endswith(('.whl', '.tar.gz', '.zip')) or '://' in text)
        named = parse_requirement(text)
        parsed = named if not is_artifact or named and named[0] in self.caps else None
        if parsed is not None:
            name, version, _ = parsed
            if name in self.caps:
                reason = self.caps[name].version_reason(version)
                if reason:
                    self.error(path, line, reason)
                return reason is None
            return False
        # Wheel identity is verifiable from the standard filename, but named URL/
        # VCS requirements and unknown artifacts cannot establish trusted identity.
        filename = unquote(urlsplit(text).path).rsplit('/', 1)[-1]
        wheel = re.fullmatch(r'([A-Za-z0-9_.]+)-([^-]+)-(?:[^-]+-)?[^-]+-[^-]+-[^-]+\.whl', filename)
        if wheel:
            if normalize(wheel[1]) in self.caps:
                reason = self.caps[normalize(wheel[1])].version_reason(wheel[2])
                if reason:
                    self.error(path, line, reason)
                return reason is None
            return False
        self.error(path, line, 'unverifiable requirement/artifact; package must be pinned exactly')
        return False

    @staticmethod
    def logical_lines(path: Path) -> list[tuple[int, str]]:
        lines, current, start = [], '', 0
        for number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if not current:
                start = number
            if raw.rstrip().endswith('\\'):
                current += raw.rstrip()[:-1] + ' '
            else:
                lines.append((start, current + raw))
                current = ''
        if current:
            lines.append((start, current))
        return lines

    def include(self, path: Path, line: int, value: str) -> None:
        if not value or '$' in value or re.match(r'\w+://', value):
            if mentions_cap(value, self.caps):
                self.error(path, line, 'remote or unresolved capped requirements include refused')
            return
        target = (path.parent / value).resolve()
        if target in self.active:
            return
        if not target.is_file() and not mentions_cap(value, self.caps):
            return
        self.requirements_file(target)

    def requirements_file(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_file():
            self.error(path, 1, 'requirements include is unavailable')
            return
        if path in self.active:
            self.error(path, 1, 'requirements include cycle refused')
            return
        self.active.add(path)
        try:
            for number, text in self.logical_lines(path):
                text = re.split(r'(?:^|\s)#', text, 1)[0].strip()
                if not text:
                    continue
                include = re.match(r'^(?:-r\s*|-c\s*|--requirement(?:=|\s+)|--constraint(?:=|\s+))(.+)$', text)
                if include:
                    self.include(path, number, include[1].strip().strip('\"\''))
                elif text.startswith(('-e ', '--editable')):
                    if mentions_cap(text, self.caps):
                        self.error(path, number, 'unverifiable editable source; package must be pinned exactly')
                elif text.startswith('-'):
                    if mentions_cap(text, self.caps):
                        self.error(path, number, 'unmodelled capped requirements option')
                else:
                    self.requirement(path, number, text.split(' --')[0])
        except (OSError, UnicodeError) as error:
            self.error(path, 1, f'cannot read requirements: {error}')
        finally:
            self.active.remove(path)

    @staticmethod
    def line_of(text: str, name: str) -> int:
        return next((n for n, row in enumerate(text.splitlines(), 1) if name.lower() in row.lower()), 1)

    def lock_entries(self, path: Path) -> dict[str, list[dict]]:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
        result: dict[str, list[dict]] = {}
        for entry in data.get('package', []):
            if isinstance(entry, dict) and isinstance(entry.get('name'), str):
                result.setdefault(normalize(entry['name']), []).append(entry)
        return result

    def locked_reason(self, entry: dict) -> str | None:
        source = entry.get('source', {})
        if source and (not isinstance(source, dict) or set(source) != {'registry'} and
                       not (source.get('type') == 'legacy' and set(source) <= {'type', 'url', 'reference'})):
            return 'git/path/URL lock source refused; use a registry pin'
        if set(entry) & {'git', 'path', 'file', 'url', 'editable'}:
            return 'git/path/URL lock source refused; use a registry pin'
        return None

    def toml_lock(self, path: Path) -> None:
        text = path.read_text(encoding='utf-8')
        for name, entries in self.lock_entries(path).items():
            if name not in self.caps:
                continue
            for entry in entries:
                reason = self.caps[name].version_reason(entry.get('version')) or self.locked_reason(entry)
                if reason:
                    self.error(path, self.line_of(text, name), reason)

    def pipfile_lock(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding='utf-8'))
        for section in ('default', 'develop'):
            for name, entry in data.get(section, {}).items():
                if normalize(name) in self.caps:
                    if not isinstance(entry, dict) or set(entry) & {'git', 'path', 'file', 'url', 'editable'}:
                        self.error(path, 1, 'git/path/URL lock source refused; must be pinned exactly')
                    else:
                        self.requirement(path, 1, name + entry.get('version', ''))

    def pyproject_data(self, path: Path, data: dict) -> None:
        project = data.get('project', {})
        uv = data.get('tool', {}).get('uv', {})
        groups = [project.get('dependencies', []), data.get('build-system', {}).get('requires', [])]
        groups += list(project.get('optional-dependencies', {}).values())
        groups += list(data.get('dependency-groups', {}).values())
        groups += [uv.get('dev-dependencies', [])]
        declared: list[tuple[str, str | None]] = []
        for group in groups:
            for item in group:
                if isinstance(item, str):
                    try:
                        req = Requirement(item)
                        if normalize(req.name) in self.caps:
                            declared.append((normalize(req.name), None if req.url else str(req.specifier)))
                    except InvalidRequirement:
                        if any(c.name in item.lower() for c in self.caps.values()):
                            self.error(path, 1, 'unverifiable dependency declaration')
        poetry = data.get('tool', {}).get('poetry', {})
        tables = [poetry.get('dependencies', {}), poetry.get('dev-dependencies', {})]
        tables += [g.get('dependencies', {}) for g in poetry.get('group', {}).values()]
        for table in tables:
            declared += [(normalize(n), poetry_spec(v)) for n, v in table.items() if normalize(n) in self.caps]
        for name, source in uv.get('sources', {}).items():
            if normalize(name) in self.caps:
                self.error(path, 1, f'{name}: alternate uv source refused')
        locks = {n: self.lock_entries(path.with_name(n)) for n in ('uv.lock', 'poetry.lock') if path.with_name(n).is_file()}
        for name, spec in declared:
            cap = self.caps[name]
            if spec is None:
                self.error(path, 1, f'{name}: git/path/URL manifest source refused')
                continue
            parsed = parse_requirement(name + spec)
            version = parsed[1] if parsed else None
            if version:
                reason = cap.version_reason(version)
                if reason:
                    self.error(path, 1, reason)
            elif not locks:
                self.error(path, 1, f'{name} has a range and no uv.lock or poetry.lock pins it')
            # Check every matching entry, including duplicates/platform variants.
            for lock, packages in locks.items():
                entries = packages.get(name, [])
                if not entries:
                    self.error(path, 1, f'{name}: lock file does not pin it ({lock})')
                for entry in entries:
                    locked = entry.get('version')
                    reason = cap.version_reason(locked) or self.locked_reason(entry)
                    if reason:
                        self.error(path, 1, f'{lock} pins {locked}: {reason}')
                    elif not SpecifierSet(spec).contains(locked, prereleases=True):
                        self.error(path, 1, f'{name}: manifest/lock mismatch ({spec}, {lock} pins {locked})')
        # Constraints are caps rather than installation declarations, and do not
        # themselves need a lock. Reject rules that could relax this policy.
        for key in ('constraint-dependencies', 'build-constraint-dependencies', 'override-dependencies'):
            for text in uv.get(key, []):
                req = Requirement(text)
                name = normalize(req.name)
                if name in self.caps:
                    cap = self.caps[name]
                    if req.url or str(req.specifier) not in {f'<={cap.max_version}', f'=={cap.max_version}'}:
                        self.error(path, 1, f'{name}: unverified uv {key} rule')

    def container_text(self, path: Path) -> None:
        """Require an immediate allowed pin/tag for every whole-token policy name.

        Dockerfile and compose syntax, including comments, is deliberately text:
        no shell/YAML parsing, variable expansion or referenced-file resolution.
        """
        text = path.read_text(encoding='utf-8')
        for cap in self.caps.values():
            pattern = r'(?<![A-Za-z0-9_.-])' + re.escape(cap.name) + r'(?![A-Za-z0-9_.-])'
            for match in re.finditer(pattern, text, re.I | re.ASCII):
                line = text.count('\n', 0, match.start()) + 1
                # Read the entire adjacent value, never an allowed prefix of it.
                pin = re.match(r'''(==|:)([^\s"'`,;\[\]{}()<>|&\\]+)''', text[match.end():])
                if pin is None:
                    reason = cap.version_reason(None)
                elif pin[1] == '==':
                    reason = cap.version_reason(pin[2])
                else:
                    tag, separator, digest = pin[2].partition('@')
                    reason = cap.tag_reason(tag)
                    if not reason and separator and digest not in cap.approved_digests:
                        reason = f'image digest is not approved in policy (cap {cap.max_version})'
                if reason:
                    self.error(path, line, reason)

    def check(self, path: Path) -> bool:
        name = path.name.lower()
        text = None
        try:
            if REQUIREMENTS.match(name):
                self.requirements_file(path)
            elif name == 'pyproject.toml':
                self.pyproject_data(path, tomllib.loads(path.read_text(encoding='utf-8')))
            elif name in {'uv.lock', 'poetry.lock'}:
                self.toml_lock(path)
            elif name == 'pipfile.lock':
                self.pipfile_lock(path)
            elif re.fullmatch(r'dockerfile(?:\..*)?|.*\.dockerfile|(?:docker-)?compose.*\.y.*ml', name):
                self.container_text(path)
            elif name == 'setup.cfg':
                config = configparser.ConfigParser(interpolation=None)
                config.read(path)
                for section in config.sections():
                    for key, value in config[section].items():
                        if key in {'install_requires', 'setup_requires'} or section == 'options.extras_require':
                            for item in value.splitlines():
                                if item.strip():
                                    self.requirement(path, 1, item.strip())
            elif path.suffix == '.py':
                text = path.read_text(encoding='utf-8')
                if name == 'setup.py' and any(c.name in text.lower() for c in self.caps.values()):
                    self.error(path, 1, 'executable setup.py dependency declaration cannot be evaluated')
                for block in re.findall(r'(?m)^# /// script\s*\n(.*?)^# ///\s*$', text, re.S):
                    metadata = '\n'.join(row.removeprefix('#').removeprefix(' ') for row in block.splitlines())
                    self.pyproject_data(path, {'project': tomllib.loads(metadata)})
                if name != 'setup.py' and '# /// script' not in text:
                    return False
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
