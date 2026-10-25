# shellcheck shell=bash
# Restore agy only from the release archive verified by download().
# `tmp` is initialized by the including tools.sh template.
# shellcheck disable=SC2154
install_agy() {
  download "$AGY_URL" "$AGY_SHA256" "$tmp/agy.tar.gz"
  tar -xzf "$tmp/agy.tar.gz" -C "$tmp" antigravity
  agy_root="$HOME/.local/share/bootstrap/agy/$AGY_VERSION"
  mkdir -p "$agy_root"
  local expected_binary_sha
  expected_binary_sha=$(sha256sum "$tmp/antigravity" | cut -d' ' -f1)
  if ! cmp -s "$tmp/antigravity" "$agy_root/antigravity" ||
     [[ ! -f $agy_root/antigravity.sha256 ]] ||
     [[ $(cat "$agy_root/antigravity.sha256" 2>/dev/null) != "$expected_binary_sha" ]]; then
    chmod u+w "$agy_root"
    if [[ -e $agy_root/antigravity ]]; then chmod u+w "$agy_root/antigravity"; fi
    if [[ -e $agy_root/antigravity.sha256 ]]; then chmod u+w "$agy_root/antigravity.sha256"; fi
    install -m 0755 "$tmp/antigravity" "$agy_root/antigravity"
    printf '%s\n' "$expected_binary_sha" > "$agy_root/antigravity.sha256"
  fi
  chmod a-w "$agy_root/antigravity" "$agy_root/antigravity.sha256" "$agy_root"
  # shellcheck disable=SC2016
  printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
    'export AGY_CLI_DISABLE_AUTO_UPDATE=true' \
    'exec "$HOME/.local/share/bootstrap/agy/'"$AGY_VERSION"'/antigravity" "$@"' > "$tmp/agy-wrapper"
  rm -f "$HOME/.local/bin/agy"
  install -m 0755 "$tmp/agy-wrapper" "$HOME/.local/bin/agy"
  [[ $("$HOME/.local/bin/agy" --version) == *"$AGY_VERSION"* ]]
}
