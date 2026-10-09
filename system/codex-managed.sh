#!/usr/bin/env bash
# Root-owned Codex requirements. Codex enforces /etc/codex/requirements.toml above
# user, project and session config: its managed hooks are trusted by policy and
# cannot be disabled, and its [rules] merge with the user's rules with the most
# restrictive decision winning. install.sh runs this from the published /opt revision.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
require_root_owned_policy
dir=/etc/codex
[[ ! -L $dir ]] || { echo "Refusing symlinked $dir." >&2; exit 1; }
install -d -o root -g root -m 0755 "$dir"
atomic_policy_install "$BOOTSTRAP_ROOT/system/codex-requirements.toml" \
  "$dir/requirements.toml"
