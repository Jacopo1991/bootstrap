#!/usr/bin/env bash
# Root-owned systemd timer that copies the Windows-written inventory snapshot into
# /home/agent/project-data/inventory/latest.json. Nothing here lets Windows start
# or reach into the distro; the copy only happens while AgentDev already runs.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
unit_dir=/etc/systemd/system
[[ ! -L $unit_dir ]] || { echo "Refusing symlinked $unit_dir." >&2; exit 1; }
install -d -o root -g root -m 0755 "$unit_dir"
for unit in inventory-mirror.service inventory-mirror.timer; do
  [[ ! -L $unit_dir/$unit ]] || { echo "Refusing symlinked $unit." >&2; exit 1; }
  install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/$unit" "$unit_dir/$unit"
done
if [[ -d /run/systemd/system ]]; then
  systemctl daemon-reload
  systemctl enable --now inventory-mirror.timer
else
  # Hosted CI has no systemd PID 1; still verify that the timer is enabled.
  systemctl --root=/ enable inventory-mirror.timer
fi
