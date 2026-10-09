#!/usr/bin/env bash
# Root-owned Claude Code managed settings. Claude Code reads
# /etc/claude-code/managed-settings.d/*.json above every user, project and local
# setting, so the agent account can neither edit nor override these entries.
# install.sh runs this from the published, root-owned /opt revision.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
require_root_owned_policy
dir=/etc/claude-code
dropins=$dir/managed-settings.d
for path in "$dir" "$dropins"; do
  [[ ! -L $path ]] || { echo "Refusing symlinked $path." >&2; exit 1; }
done
install -d -o root -g root -m 0755 "$dir" "$dropins"
# Guardrails: the permission deny rules and the policy hooks (root-owned scripts under
# /opt/machine-bootstrap/current). Nothing here limits bypass or auto mode.
install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/claude-managed-guardrails.json" \
  "$dropins/10-agent-guardrails.json"
install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/claude-managed-mods.json" \
  "$dropins/50-managed-mods-only.json"
