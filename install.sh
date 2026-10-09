#!/usr/bin/env bash
set -euo pipefail
umask 022
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $EUID != 0 && $(id -un) != agent ]] || { echo 'Run as the non-root admin user.' >&2; exit 1; }
[[ -z $(git -C "$ROOT" status --porcelain) ]] || {
  echo 'Commit bootstrap changes before running install.sh.' >&2; exit 1;
}
sudo -v
bash_cmd=/bin/bash
sudo "$bash_cmd" "$ROOT/system/base.sh"
sudo "$bash_cmd" "$ROOT/system/users.sh" "$(id -un)"
sudo "$bash_cmd" "$ROOT/system/inventory-mirror.sh"
# shellcheck source=system/common.sh
source "$ROOT/system/common.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
download_verified "$CHEZMOI_URL" "$CHEZMOI_SHA256" "$tmp/chezmoi.tar.gz"
tar -xzf "$tmp/chezmoi.tar.gz" -C "$tmp" chezmoi
if ! cmp -s "$tmp/chezmoi" /usr/local/bin/chezmoi; then
  sudo install -m 0755 "$tmp/chezmoi" /usr/local/bin/chezmoi
fi
download_verified "$MISE_URL" "$MISE_SHA256" "$tmp/mise.tar.xz"
tar -xJf "$tmp/mise.tar.xz" -C "$tmp" mise/bin/mise
if ! cmp -s "$tmp/mise/bin/mise" /usr/local/bin/mise; then
  sudo install -m 0755 "$tmp/mise/bin/mise" /usr/local/bin/mise
fi
# The managed policy hook's secret scanner: root-owned, found first on the hook's fixed PATH.
download_verified "$GITLEAKS_URL" "$GITLEAKS_SHA256" "$tmp/gitleaks.tar.gz"
tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp" gitleaks
if ! cmp -s "$tmp/gitleaks" /usr/local/bin/gitleaks; then
  sudo install -m 0755 "$tmp/gitleaks" /usr/local/bin/gitleaks
fi

# Publish only committed public files, never the admin's .git config or home.
revision=$(git -C "$ROOT" rev-parse HEAD)
public_source="/opt/machine-bootstrap/revisions/$revision"
sudo install -d -m 0755 "$public_source"
git -C "$ROOT" archive HEAD | sudo tar -xf - -C "$public_source"
# chezmoi init expects a Git source. Create fresh metadata without copying the
# admin's repository config, credentials, templates or hooks.
sudo git -C "$public_source" init --quiet --initial-branch=main --template=
sudo chown -R root:root "$public_source"
sudo chmod -R go-w "$public_source"
# Keep policy scripts available even when current is rolled back to an older revision.
policy_dir=/usr/local/lib/agent-policy
[[ ! -L $policy_dir ]] || { echo "Refusing symlinked $policy_dir." >&2; exit 1; }
sudo install -d -o root -g root -m 0755 "$policy_dir"
for script in pre_tool_use.py context_reminder.py; do
  policy_temp=$(sudo mktemp "$policy_dir/.agent-policy.XXXXXX")
  sudo install -o root -g root -m 0644 "$public_source/system/agent-policy/$script" "$policy_temp"
  sudo mv -f -- "$policy_temp" "$policy_dir/$script"
done
current_temp=$(sudo mktemp -d /opt/machine-bootstrap/.current.XXXXXX)
sudo ln -s "$public_source" "$current_temp/current"
sudo mv -T -- "$current_temp/current" /opt/machine-bootstrap/current
sudo rmdir "$current_temp"

# Agent guardrails (deny rules, forbidden/prompt command rules, policy hooks) go into the
# root-owned managed settings of Claude Code and Codex, written from the published revision
# so the hooks run root-owned scripts. Preferences stay in the agent's own config.
sudo "$bash_cmd" /opt/machine-bootstrap/current/system/claude-managed.sh
sudo "$bash_cmd" /opt/machine-bootstrap/current/system/codex-managed.sh

# chezmoi's existing-source init applies .chezmoiroot and prompts once for the
# single Git identity. The source remains public and owned by root.
cd /tmp
sudo -H -u agent env PATH=/usr/local/bin:/usr/bin:/bin \
  CHEZMOI_GIT_NAME="${CHEZMOI_GIT_NAME:-}" CHEZMOI_GIT_EMAIL="${CHEZMOI_GIT_EMAIL:-}" \
  chezmoi --source /opt/machine-bootstrap/current init --apply

# qmd needs the agent's user systemd (timer) and runs outside agent sessions:
# render its config, pull models, build the index, start the timer, register the MCP server.
sudo "$bash_cmd" "$ROOT/system/user-systemd.sh"
agent_uid=$(id -u agent)
sudo -H -u agent env PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin \
  XDG_RUNTIME_DIR="/run/user/$agent_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$agent_uid/bus" \
  BOOTSTRAP_SOURCE=/opt/machine-bootstrap/current \
  bash /home/agent/.local/share/bootstrap/qmd-setup.sh
# The verification pack's Chromium download and Playwright MCP registration run the same way.
sudo -H -u agent env PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin \
  bash /home/agent/.local/share/bootstrap/verify-setup.sh
# Context7 (current library docs) is the one remote MCP server lanes get; claude.ai connectors are off.
sudo -H -u agent env PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin \
  bash /home/agent/.local/share/bootstrap/context7-setup.sh
# The cortex-core skills come from GitHub with `gh skill` (user scope, Claude Code and Codex);
# gh-skill-update.timer keeps them current. Needs agent's gh login, so it runs outside any session.
sudo -H -u agent env PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin \
  XDG_RUNTIME_DIR="/run/user/$agent_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$agent_uid/bus" \
  bash /home/agent/.local/share/bootstrap/skills-setup.sh
echo 'Bootstrap applied. Restart this distro before the WSL boundary checks.'
