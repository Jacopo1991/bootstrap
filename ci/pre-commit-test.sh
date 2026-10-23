#!/usr/bin/env bash
# The standard pre-commit gate blocks a planted secret, a broken relative link and a lockfile
# without an offline vulnerability database, passes a clean change from a read-only store
# (as inside the agent sandbox), and pre-commit-enable installs it without overwriting.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
for tool in pre-commit gitleaks lychee osv-scanner; do
  command -v "$tool" >/dev/null || { echo "SKIP: $tool is not installed (installed by the chezmoi tools script)"; exit 0; }
done
real_osv="${XDG_CACHE_HOME:-$HOME/.cache}/osv-scalibr"
tmp=$(mktemp -d)
trap 'chmod -R u+w "$tmp"; rm -rf "$tmp"' EXIT
# The package cap gate command and its policy file come from this checkout, as chezmoi (which sets
# the executable bit on apply) and install.sh place them.
mkdir -p "$tmp/bin"
printf '#!/usr/bin/env bash\nexec /usr/bin/python3 %q "$@"\n' "$ROOT/ci/package-cap-check.py" > "$tmp/bin/package-cap-check"
chmod 0755 "$tmp/bin/package-cap-check"
export PATH="$tmp/bin:$PATH"
export HOME="$tmp/home" XDG_CACHE_HOME="$tmp/home/.cache" PRE_COMMIT_HOME="$tmp/home/.cache/pre-commit"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
mkdir -p "$HOME"
enable="$ROOT/home/dot_local/bin/executable_pre-commit-enable"
standard="$ROOT/home/dot_local/share/bootstrap/pre-commit/pre-commit-config.yaml"
repo="$tmp/repo"
git init -q -b main "$repo"

out=$(bash "$enable" "$repo")
grep -q "Next: commit .pre-commit-config.yaml" <<<"$out"
cmp -s "$standard" "$repo/.pre-commit-config.yaml"
grep -q "pre-commit" "$repo/.git/hooks/pre-commit"
[ -f "$PRE_COMMIT_HOME/db.db" ]
# Running it again is a no-op apart from reinstalling the hook.
out=$(bash "$enable" "$repo")
if grep -q "Next:" <<<"$out"; then echo "FAIL: second run rewrote the config"; exit 1; fi
git -C "$repo" add .pre-commit-config.yaml
git -C "$repo" commit -q -m "Add the standard pre-commit gate" >/dev/null 2>&1

# Inside the agent sandbox the store is read-only; local hooks must still run.
chmod -R a-w "$PRE_COMMIT_HOME"
commits() { git -C "$repo" rev-list --count HEAD; }
blocked() {  # blocked <description>: the staged change must be refused
  local before; before=$(commits)
  if git -C "$repo" commit -q -m "$1" >"$tmp/out" 2>&1; then
    cat "$tmp/out"; echo "FAIL: $1 was committed"; exit 1
  fi
  [ "$(commits)" = "$before" ]
  git -C "$repo" reset -q --hard HEAD
  git -C "$repo" clean -fdq
}

# Clean change: a valid relative link and a web link (never fetched).
mkdir -p "$repo/docs"
printf '# Guide\n\nSee [the notes](notes.md) and [upstream](https://example.invalid/page).\n' > "$repo/docs/guide.md"
printf '# Notes\n' > "$repo/docs/notes.md"
git -C "$repo" add docs
start=$(date +%s)
git -C "$repo" commit -q -m "Clean docs change" >"$tmp/out" 2>&1 || { cat "$tmp/out"; echo "FAIL: clean change refused"; exit 1; }
elapsed=$(( $(date +%s) - start ))
[ "$elapsed" -le 10 ] || { echo "FAIL: clean commit took ${elapsed}s"; exit 1; }

# Planted fake secret (synthetic GitHub token, never printed).
token="ghp_$(printf 'bootstrap pre-commit canary' | sha256sum | cut -c1-36)"
printf 'fixture_token=%s\n' "$token" > "$repo/settings.txt"
git -C "$repo" add settings.txt
blocked "planted fake secret"
grep -q gitleaks "$tmp/out"
if grep -qF "$token" "$tmp/out"; then echo "FAIL: secret value printed"; exit 1; fi

# Broken relative link.
printf '# Broken\n\nSee [missing](missing.md).\n' > "$repo/docs/broken.md"
git -C "$repo" add docs/broken.md
blocked "broken relative link"
grep -q missing.md "$tmp/out"

# A lockfile is scanned offline and fails closed when no offline database exists.
printf 'requests==2.19.0\n' > "$repo/requirements.txt"
git -C "$repo" add requirements.txt
blocked "lockfile without an offline database"
grep -q "no offline version of the OSV database" "$tmp/out"
# With the machine's offline PyPI database (osv-db-refresh), a known advisory is blocked.
osv_checked=0
if [ -f "$real_osv/PyPI/all.zip" ]; then
  mkdir -p "$XDG_CACHE_HOME/osv-scalibr"
  ln -s "$real_osv/PyPI" "$XDG_CACHE_HOME/osv-scalibr/PyPI"
  printf 'requests==2.19.0\n' > "$repo/requirements.txt"
  git -C "$repo" add requirements.txt
  blocked "lockfile with a known advisory"
  grep -q "PYSEC-2018-28" "$tmp/out"
  printf 'cfgv==3.5.0\n' > "$repo/requirements.txt"
  git -C "$repo" add requirements.txt
  git -C "$repo" commit -q -m "Clean lockfile" >"$tmp/out" 2>&1 || { cat "$tmp/out"; echo "FAIL: clean lockfile refused"; exit 1; }
  osv_checked=1
fi

# Package caps: a capped package above its cap, unpinned, or as a refused image tag is blocked
# in any tracked manifest; the allowed pin and tag pass (the lockfile hook sees no requirements file).
printf 'services:\n  proxy:\n    image: ghcr.io/berriai/litellm:main-latest\n' > "$repo/compose.yaml"
git -C "$repo" add compose.yaml
blocked "compose file with a refused litellm image tag"
grep -q "package-cap-check" "$tmp/out"
grep -q "compose.yaml:3: image tag main-latest is refused" "$tmp/out"
printf 'FROM ghcr.io/berriai/litellm:v1.106.2-stable\nRUN pip install litellm\n' > "$repo/Dockerfile"
git -C "$repo" add Dockerfile
blocked "Dockerfile installing litellm without a pin"
grep -qF "Dockerfile:2: litellm needs an exact pin (==) or upper bound (<=) at or below 1.106.2" "$tmp/out"
printf '[project]\nname = "demo"\ndependencies = ["litellm>=1.100"]\n' > "$repo/pyproject.toml"
git -C "$repo" add pyproject.toml
blocked "pyproject range without a lock file"
grep -qF "pyproject.toml:3: litellm needs an exact pin (==) or upper bound (<=) at or below 1.106.2" "$tmp/out"
printf '[project]\nname = "demo"\ndependencies = ["litellm==1.106.2"]\n' > "$repo/pyproject.toml"
printf 'services:\n  proxy:\n    image: ghcr.io/berriai/litellm:v1.106.2-stable\n' > "$repo/compose.yaml"
printf 'FROM ghcr.io/berriai/litellm:v1.106.2-stable\nRUN pip install litellm==1.106.2\n' > "$repo/Dockerfile"
git -C "$repo" add pyproject.toml compose.yaml Dockerfile
git -C "$repo" commit -q -m "Allowed litellm pin and image tag" >"$tmp/out" 2>&1 || { cat "$tmp/out"; echo "FAIL: allowed litellm pin refused"; exit 1; }
# A tracked manifest that drifts above the cap blocks every later commit, not only its own.
printf '[project]\nname = "demo"\ndependencies = ["litellm==1.107.0"]\n' > "$repo/pyproject.toml"
git -C "$repo" add pyproject.toml
git -C "$repo" commit -q -m "Above the cap" --no-verify >/dev/null 2>&1
printf '# Unrelated\n' > "$repo/docs/unrelated.md"
git -C "$repo" add docs/unrelated.md
blocked "unrelated change while a tracked manifest is above the cap"
grep -qF "pyproject.toml:3: litellm 1.107.0 is above the cap 1.106.2" "$tmp/out"
git -C "$repo" reset -q --hard HEAD~1

# pre-commit-enable never overwrites a different existing config.
chmod -R u+w "$PRE_COMMIT_HOME"
other="$tmp/other"
git init -q -b main "$other"
printf 'repos: []\n' > "$other/.pre-commit-config.yaml"
if bash "$enable" "$other" >/dev/null 2>&1; then echo "FAIL: existing config overwritten"; exit 1; fi
[ "$(cat "$other/.pre-commit-config.yaml")" = "repos: []" ]
[ ! -e "$other/.git/hooks/pre-commit" ]
if bash "$enable" "$tmp/not-a-repo" >/dev/null 2>&1; then echo "FAIL: non-repository accepted"; exit 1; fi
[ $osv_checked = 1 ] || echo "NOTE: no offline PyPI database at $real_osv; advisory control skipped (run osv-db-refresh)"
echo "PASS: pre-commit gate blocks a secret, a broken link, an unscanned lockfile and a capped package; clean change in ${elapsed}s; pre-commit-enable installs without overwriting"
