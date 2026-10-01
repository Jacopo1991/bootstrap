#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root

install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/sshd_config.agentdev" /etc/ssh/sshd_config.agentdev
install -d -o root -g root -m 0755 /etc/systemd/system/ssh.service.d
install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/ssh.service.override.conf" \
  /etc/systemd/system/ssh.service.d/agentdev.conf
ssh-keygen -A
/usr/sbin/sshd -t -f /etc/ssh/sshd_config.agentdev

# Ubuntu may enable socket activation at package install time. Mask it so the
# service cannot bind port 22 or an IPv6/wildcard address before our config.
if [[ -d /run/systemd/system ]]; then
  systemctl disable --now ssh.socket >/dev/null 2>&1 || true
  systemctl mask ssh.socket
  systemctl daemon-reload
  systemctl enable ssh.service
  systemctl restart ssh.service
else
  # Hosted CI installs into a disposable container without a systemd PID 1.
  systemctl --root=/ mask ssh.socket
  systemctl --root=/ enable ssh.service
fi
