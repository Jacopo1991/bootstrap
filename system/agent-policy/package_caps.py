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
import shlex
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

    def image_reason(self, reference: str) -> str | None:
        name, tag = image_name(reference)
        if name not in self.images:
            return None
        reason = self.tag_reason(tag)
        if reason:
            return reason
        if '@' in reference and reference.split('@', 1)[1] not in self.approved_digests:
            return f'image digest is not approved in policy (cap {self.max_version})'
        return None


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

    def requirement(self, path: Path, line: int, text: str, *, artifact=False) -> None:
        if '$' in text or '`' in text:
            self.error(path, line, 'unresolved installation operand')
            return
        artifact = ' @ ' not in text and (text.endswith(('.whl', '.tar.gz', '.zip')) or '://' in text)
        parsed = None if artifact else parse_requirement(text)
        if parsed is not None:
            name, version, _ = parsed
            if name in self.caps:
                reason = self.caps[name].version_reason(version)
                if reason:
                    self.error(path, line, reason)
            return
        # Wheel identity is verifiable from the standard filename, but named URL/
        # VCS requirements and unknown artifacts cannot establish trusted identity.
        filename = unquote(urlsplit(text).path).rsplit('/', 1)[-1]
        wheel = re.fullmatch(r'([A-Za-z0-9_.]+)-([^-]+)-(?:[^-]+-)?[^-]+-[^-]+-[^-]+\.whl', filename)
        if wheel and normalize(wheel[1]) in self.caps:
            reason = self.caps[normalize(wheel[1])].version_reason(wheel[2])
            if reason:
                self.error(path, line, reason)
            return
        # Refuse any unmodelled operand, even one whose filename hides the name.
        self.error(path, line, 'unverifiable requirement/artifact; use an exact named registry pin')

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
            self.error(path, line, 'remote or unresolved requirements include refused')
            return
        target = (path.parent / value).resolve()
        if target in self.active:
            self.error(path, line, 'requirements include cycle refused')
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
                    self.error(path, number, 'unverifiable editable source; package must be pinned exactly')
                elif text.startswith('-'):
                    if not text.startswith(('--index-url ', '--extra-index-url ', '--find-links ', '--no-index', '--trusted-host ')):
                        self.error(path, number, 'unmodelled requirements option')
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

    @staticmethod
    def interpolate(reference: str, variables: dict[str, str]) -> str:
        pattern = r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)'
        def replace(match):
            name = match[1] or match[3]
            value = variables.get(name, match[2])
            return value if value is not None else match[0]
        # Do not consult the invoking process environment.
        return re.sub(pattern, replace, reference)

    def image(self, path: Path, line: int, reference: str, variables=None) -> None:
        reference = self.interpolate(reference, variables or {})
        if '$' in reference:
            self.error(path, line, 'unresolved image reference refused')
            return
        for cap in self.caps.values():
            reason = cap.image_reason(reference)
            if reason:
                self.error(path, line, reason)

    def dockerfile(self, path: Path) -> None:
        variables: dict[str, str] = {}
        for number, text in self.logical_lines(path):
            words = shlex.split(text, comments=True)
            if not words:
                continue
            verb = words.pop(0).upper()
            if verb == 'ARG' and words:
                name, _, value = words[0].partition('=')
                if value:
                    variables[name] = self.interpolate(value, variables)
            elif verb == 'FROM':
                operands = [w for w in words if not w.startswith('--')]
                if operands:
                    self.image(path, number, operands[0], variables)
            elif verb == 'RUN':
                body = text.strip()[3:].strip()
                segments = [json.loads(body)] if body.startswith('[') else [shlex.split(s) for s in re.split(r'&&|\|\||;|\|', body)]
                for tokens in segments:
                    reason = install_reason(tokens, path.parent, self.caps)
                    if reason:
                        self.error(path, number, reason)

    def compose(self, path: Path) -> None:
        import yaml
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
        visited = set()
        def visit(value):
            if isinstance(value, (dict, list)):
                if id(value) in visited:
                    return
                visited.add(id(value))
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'image':
                        if isinstance(item, str):
                            self.image(path, self.line_of(path.read_text(), item), item)
                        else:
                            self.error(path, 1, 'invalid image reference')
                    elif key == 'dockerfile' and isinstance(item, str):
                        target = path.parent / item
                        if target.is_file():
                            self.dockerfile(target)
                        else:
                            self.error(path, 1, 'referenced Dockerfile unavailable')
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(data)

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
            elif 'dockerfile' in name or 'containerfile' in name:
                self.dockerfile(path)
            elif path.suffix.lower() in {'.yml', '.yaml'}:
                # Arbitrary compose filenames: structural detection, not a name regex.
                text = path.read_text(encoding='utf-8')
                if 'compose' in name or re.search(r'\bservices\s*:', text):
                    self.compose(path)
                else:
                    return False
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
        # YAML errors do not derive from ValueError.
        except Exception as error:
            self.error(path, 1, f'cannot parse manifest: {error}')
        return True


# Dockerfile RUN checks supply argv to these gate-only installer helpers.
GLOBAL_VALUES = {'--python', '-p', '--directory', '--project', '--config-file', '--color',
                 '--cache-dir', '--log', '--proxy', '--timeout', '--retries', '--cert', '--client-cert'}
FLAGS = {'-q', '--quiet', '-v', '--verbose', '--offline', '--no-progress', '--no-cache',
         '--no-cache-dir', '--disable-pip-version-check', '--no-input', '--isolated', '-I', '-s', '-E'}
INSTALL_VALUES = {'--index-url', '-i', '--extra-index-url', '--find-links', '-f', '--trusted-host',
                  '--python', '-p', '--cache-dir', '--group', '--only', '--extras', '--extra',
                  '--target', '-t', '--prefix', '--root', '--platform', '--python-version',
                  '--implementation', '--abi', '--keyring-provider', '--link-mode'}
INSTALL_FLAGS = FLAGS | {'-U', '--upgrade', '--no-deps', '--pre', '--user', '--system',
                        '--dry-run', '--ignore-installed', '--force-reinstall', '--no-build',
                        '--no-index', '--no-build-isolation', '--dev', '--optional', '--exact',
                        '--reinstall', '--force', '--require-hashes', '--no-warn-script-location'}


def strip_globals(words: list[str]) -> list[str] | None:
    words = words[:]
    while words and words[0].startswith('-'):
        word = words.pop(0)
        key = word.split('=', 1)[0]
        if key in GLOBAL_VALUES:
            if '=' not in word:
                if not words:
                    return None
                words.pop(0)
        elif key not in FLAGS and not re.fullmatch(r'-[qv]+', key):
            return None
    return words


def install_reason(tokens: list[str], cwd: Path, caps: dict[str, Cap]) -> str | None:
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    rest = tokens[1:]
    if re.fullmatch(r'python[0-9.]*', executable):
        while rest and rest[0] in {'-I', '-s', '-E', '-B', '-u'}:
            rest = rest[1:]
        if rest[:2] != ['-m', 'pip']:
            return None
        executable, rest = 'pip', rest[2:]
    installers = {'pip', 'pip3', 'uv', 'uvx', 'pipx', 'poetry', 'conda', 'mamba', 'micromamba'}
    if executable not in installers:
        return None
    rest = strip_globals(rest)
    if rest is None:
        return 'installer options cannot be resolved safely'
    if executable in {'conda', 'mamba', 'micromamba'}:
        return 'conda/mamba resolution is unsupported; use the capped pip/uv paths'
    if executable == 'uvx':
        return 'uvx executes downloaded code; use an explicitly pinned uv tool install'
    if not rest:
        return None
    sub = rest.pop(0)
    if executable == 'uv' and sub in {'pip', 'tool'}:
        rest = strip_globals(rest)
        if not rest:
            return 'unresolved uv installer subcommand'
        sub = rest.pop(0)
    if (executable == 'uv' and sub in {'sync', 'run', 'lock'}) or (executable == 'poetry' and sub == 'install') or (executable == 'pipx' and sub == 'run'):
        return 'project/tool resolution is refused here; the gate must check its lock and sources'
    if sub not in {'install', 'add', 'inject', 'compile', 'sync'}:
        return None
    checker = Checker(caps)
    path = cwd / '<install>'
    index = 0
    while index < len(rest):
        word = rest[index]
        key, equal, value = word.partition('=')
        include = key in {'-r', '-c', '--requirement', '--constraint', '--constraints', '--build-constraint', '--build-constraints', '--with-requirements'}
        if not include and re.match(r'^-[rc].+', word):
            include, key, equal, value = True, word[:2], '=', word[2:]
        if '$' in word or '`' in word or any(c in word for c in '*?'):
            return 'unresolved shell expansion in installer input refused'
        if include or key in INSTALL_VALUES:
            if not equal:
                index += 1
                if index >= len(rest):
                    return 'installer option value is missing'
                value = rest[index]
            if '$' in value or '`' in value:
                return 'unresolved installer option value refused'
            if include:
                checker.include(path, 1, value)
        elif word == '--':
            pass
        elif word.startswith('-'):
            if word not in INSTALL_FLAGS and not re.fullmatch(r'-[qv]+', word):
                return f'unmodelled installer option {key} refused'
        else:
            if executable == 'poetry' and '@' in word and '://' not in word:
                name, version = word.split('@', 1)
                word = name + '==' + version.removeprefix('==')
            checker.requirement(path, 1, word, artifact=True)
        index += 1
    return checker.findings[0] if checker.findings else None


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
    parser.add_argument('--uv-project', action='store_true')
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
