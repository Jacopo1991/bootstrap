#!/usr/bin/env bash
set -euo pipefail
trap 'echo "FAIL: distro check at line $LINENO" >&2' ERR
[[ $(id -un) == agent ]] || { echo 'Run as agent.' >&2; exit 1; }
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../home/.chezmoitemplates/pins.env
source "$ROOT/home/.chezmoitemplates/pins.env"
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin:/usr/lib/wsl/lib"
export DISABLE_AUTOUPDATER=1
for tool in chezmoi mise git gh jq rg fd cc c++ make node python uv claude codex ccusage bws secretspec; do
  command -v "$tool" >/dev/null || { echo "Missing: $tool" >&2; exit 1; }
done
[[ $(chezmoi --version) == "chezmoi version v$CHEZMOI_VERSION"* ]]
[[ $(mise --version) == "$MISE_VERSION "* ]]
[[ $(node --version) == "v$NODE_VERSION" ]]
[[ $(python --version) == "Python $PYTHON_VERSION" ]]
[[ $(uv --version) == "uv $UV_VERSION"* ]]
[[ $(claude --version) == "$CLAUDE_VERSION (Claude Code)" ]]
[[ $(codex --version) == "codex-cli $CODEX_VERSION" ]]
[[ $(ccusage --version) == "ccusage $CCUSAGE_VERSION" ]]
[[ $(bws --version) == "bws $BWS_VERSION" ]]
[[ $(secretspec --version) == "secretspec $SECRETSPEC_VERSION" ]]
for lock in "$ROOT/system/apt-base.lock" "$ROOT/system/apt-gh.lock"; do
  while IFS='=' read -r package version; do
    [[ $(dpkg-query -W -f='${Version}' "$package") == "$version" ]] || {
      echo "Version mismatch: $package" >&2; exit 1;
    }
  done < "$lock"
done
git config --global --get user.name >/dev/null
git config --global --get user.email >/dev/null
chezmoi verify
[[ -z $(chezmoi diff) ]]

# Compare content, mode, symlink targets and mtimes of managed files plus tool
# installations. Ignore application caches and chezmoi's own bookkeeping.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
snapshot() {
  python3 "$ROOT/checks/home-snapshot.py"
}
snapshot > "$tmp/before"
chezmoi apply
snapshot > "$tmp/after"
diff -u "$tmp/before" "$tmp/after"
chezmoi verify
[[ -z $(chezmoi diff) ]]
printf 'PASS: exact tool versions, clean verify, second apply changes nothing.\n'
