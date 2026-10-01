# shellcheck shell=bash
# Included in the chezmoi run-on-change script; exact versions, no credentials.
mise trust "$HOME/.config/mise/config.toml"
mise install
mise reshim
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
download() {
  curl --fail --silent --show-error --location --retry 3 "$1" -o "$3"
  printf '%s  %s\n' "$2" "$3" | sha256sum --check --status
}
mkdir -p "$HOME/.local/bin"
download "$GITLEAKS_URL" "$GITLEAKS_SHA256" "$tmp/gitleaks.tar.gz"
tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp" gitleaks
cmp -s "$tmp/gitleaks" "$HOME/.local/bin/gitleaks" || install -m 0755 "$tmp/gitleaks" "$HOME/.local/bin/gitleaks"
gitleaks_expected=${GITLEAKS_URL##*/download/v}
gitleaks_expected=${gitleaks_expected%%/*}
[[ $("$HOME/.local/bin/gitleaks" version) == *"$gitleaks_expected"* ]]
if [[ ! -x $HOME/.local/bin/claude ]] || [[ $("$HOME/.local/bin/claude" --version) != "$CLAUDE_VERSION (Claude Code)" ]]; then
  download "$CLAUDE_INSTALLER_URL" "$CLAUDE_INSTALLER_SHA256" "$tmp/claude-install.sh"
  bash "$tmp/claude-install.sh" "$CLAUDE_VERSION"
fi
[[ $("$HOME/.local/bin/claude" --version) == "$CLAUDE_VERSION (Claude Code)" ]]
download "$BWS_URL" "$BWS_SHA256" "$tmp/bws.zip"
unzip -q "$tmp/bws.zip" -d "$tmp/bws"
cmp -s "$tmp/bws/bws" "$HOME/.local/bin/bws" || install -m 0755 "$tmp/bws/bws" "$HOME/.local/bin/bws"
download "$SECRETSPEC_URL" "$SECRETSPEC_SHA256" "$tmp/secretspec.tar.xz"
tar -xJf "$tmp/secretspec.tar.xz" -C "$tmp"
secretspec_binary=$(find "$tmp" -type f -name secretspec -print -quit)
[[ -n $secretspec_binary ]]
cmp -s "$secretspec_binary" "$HOME/.local/bin/secretspec" || install -m 0755 "$secretspec_binary" "$HOME/.local/bin/secretspec"
npm_root="$HOME/.local/share/bootstrap/npm"
mise exec -- npm ci --prefix "$npm_root" --no-audit --no-fund
ln -sfn "$npm_root/node_modules/.bin/codex" "$HOME/.local/bin/codex"
ln -sfn "$npm_root/node_modules/.bin/ccusage" "$HOME/.local/bin/ccusage"
