#!/usr/bin/env bash
# Root-owned part of the AgentDev shell connector: blocks Desktop Commander's vendor hosts
# before any npm ci runs its postinstall ping, and re-applies the block at every boot because
# WSL regenerates /etc/hosts when the distro starts.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
bash "$BOOTSTRAP_ROOT/system/vendor-block-hosts.sh" /etc/hosts
unit_dir=/etc/systemd/system
[[ ! -L $unit_dir ]] || { echo "Refusing symlinked $unit_dir." >&2; exit 1; }
install -d -o root -g root -m 0755 "$unit_dir"
[[ ! -L $unit_dir/vendor-block.service ]] || { echo 'Refusing symlinked vendor-block.service.' >&2; exit 1; }
install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/vendor-block.service" "$unit_dir/vendor-block.service"
if [[ -d /run/systemd/system ]]; then
  systemctl daemon-reload
  systemctl enable vendor-block.service
else
  # Hosted CI has no systemd PID 1; still verify that the unit is enabled.
  systemctl --root=/ enable vendor-block.service
fi
