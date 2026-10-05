#!/usr/bin/env bash
# Install the cortex-core skills with `gh skill`, run as agent by install.sh after chezmoi
# apply, outside agent sessions and the Claude sandbox (it needs github.com and writes the
# skill folders). User scope, for Claude Code and Codex. The first run uses --force once to
# replace the hand-copied slim-workflow folders; later runs only update. Every step must
# succeed; nothing is silenced. Needs the agent's user systemd (XDG_RUNTIME_DIR and
# DBUS_SESSION_BUS_ADDRESS, which install.sh provides).
set -euo pipefail
[[ $(id -un) == agent && $EUID != 0 ]] || { echo 'Run as agent.' >&2; exit 1; }
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"
repository=Jacopo1991/cortex-core
stamp="${XDG_STATE_HOME:-$HOME/.local/state}/bootstrap/skills-installed"
step() { printf '\n== skills setup: %s\n' "$*"; }

step 'GitHub login'
gh auth status >/dev/null || { echo "gh is not logged in as agent; cannot read $repository." >&2; exit 1; }

if [[ ! -e $stamp ]]; then
  step "first install of $repository (--force replaces the hand-copied folders)"
  for agent in claude-code codex; do
    gh skill install "$repository" --all --agent "$agent" --scope user --force
  done
  mkdir -p "${stamp%/*}"
  touch "$stamp"
else
  step 'already installed; updating'
  gh-skill-update
fi

step 'user systemd timer'
if [[ ! -S ${XDG_RUNTIME_DIR:-/nonexistent}/bus ]]; then
  echo "No user systemd bus at \$XDG_RUNTIME_DIR/bus; cannot start gh-skill-update.timer." >&2
  exit 1
fi
systemctl --user daemon-reload
systemctl --user enable --now gh-skill-update.timer
systemctl --user is-active --quiet gh-skill-update.timer
systemctl --user list-timers gh-skill-update.timer --no-pager
