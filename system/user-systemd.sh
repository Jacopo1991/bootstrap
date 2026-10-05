#!/usr/bin/env bash
# Make sure agent's systemd user manager and bus are running (lingering is set by
# users.sh), so install.sh can enable the qmd index timer on a fresh distro.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
[[ -d /run/systemd/system ]] || { echo 'systemd is not running; restart the distro and re-run install.sh.' >&2; exit 1; }
uid=$(id -u agent)
loginctl enable-linger agent
systemctl start "user@$uid.service"
for _ in $(seq 1 30); do
  [[ -S /run/user/$uid/bus ]] && exit 0
  sleep 1
done
echo "agent's user bus /run/user/$uid/bus did not appear within 30 s." >&2
exit 1
