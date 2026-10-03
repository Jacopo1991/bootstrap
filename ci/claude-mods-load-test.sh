#!/usr/bin/env bash
# Run as the CI admin (passwordless sudo) after install.sh, last in the distro job.
# Proves the managed drop-in stops the agent's own mods from loading, with a control:
# the same mods DO load when the drop-in is temporarily moved aside (disposable
# container only). Needs a Claude Code release with mods (2.1.287 or later).
set -euo pipefail
dropin=/etc/claude-code/managed-settings.d/50-managed-mods-only.json
token=MODLOADED-7f3a
version=$(sudo -H -u agent bash -lc 'claude --version')
version=${version%% *}
[[ $(printf '%s\n2.1.287\n' "$version" | sort -V | head -n1) == 2.1.287 ]] || {
  echo "Claude Code $version predates mods (2.1.287); bump the pin." >&2; exit 1;
}
work=$(mktemp -d)
logs=$(sudo -u agent mktemp -d)
stash=$work/dropin.json
restore() {
  [[ ! -e $stash ]] || sudo install -o root -g root -m 0644 "$stash" "$dropin"
  sudo -H -u agent bash -lc 'claude plugin uninstall ci-modping@ci-mods; claude plugin marketplace remove ci-mods' \
    >/dev/null 2>&1 || true
  sudo rm -rf "$logs"
  rm -rf "$work"
}
trap restore EXIT
cp -r "$(dirname -- "$0")/fixtures/modping-marketplace" "$work/mkt"
chmod -R a+rX "$work"
chmod 0755 "$work"
plugin=$work/mkt/plugins/ci-modping

# ask <label> [extra claude args...]: run the mod's command, print combined output.
ask() {
  local label=$1
  shift
  # shellcheck disable=SC2016  # "$@" expands in the agent's shell, not here
  sudo -H -u agent bash -lc 'cd /tmp && exec claude -p /modping --debug-file "$@"' _ \
    "$logs/$label.log" "$@" 2>&1 || true
}
debug_log() { sudo cat "$logs/$1.log"; }
refused() { [[ $(debug_log "$1") == *allowManagedModsOnly* ]]; }

# Managed policy in force: a --plugin-dir mod must not answer.
out=$(ask sideload-managed --plugin-dir "$plugin")
[[ $out != *"$token"* ]] || { echo "sideloaded mod loaded under managed policy: $out" >&2; exit 1; }
refused sideload-managed || { echo 'debug log lacks the allowManagedModsOnly refusal' >&2; exit 1; }

# A mod the agent installs into its own plugin scope must not answer either.
sudo -H -u agent bash -lc "claude plugin marketplace add '$work/mkt' && claude plugin install ci-modping@ci-mods"
out=$(ask installed-managed)
[[ $out != *"$token"* ]] || { echo "installed mod loaded under managed policy: $out" >&2; exit 1; }
refused installed-managed || { echo 'debug log lacks the allowManagedModsOnly refusal for the installed mod' >&2; exit 1; }

# Controls: without the drop-in the same mods load, so the checks above can fail.
sudo mv "$dropin" "$stash"
out=$(ask sideload-control --plugin-dir "$plugin")
[[ $out == *"$token"* ]] || { echo "control: sideloaded mod did not load: $out" >&2; exit 1; }
out=$(ask installed-control)
[[ $out == *"$token"* ]] || { echo "control: installed mod did not load: $out" >&2; exit 1; }
sudo install -o root -g root -m 0644 "$stash" "$dropin"
rm -f "$stash"
echo 'user mods are refused under the managed drop-in and load without it'
