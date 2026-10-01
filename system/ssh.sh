#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root

stage_policy() {
  install -d -o root -g root -m 0755 /etc/ssh
  install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/sshd_config.agentdev" /etc/ssh/sshd_config.agentdev
  install -d -o root -g root -m 0755 /etc/systemd/system/ssh.service.d
  install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/system/ssh.service.override.conf" \
    /etc/systemd/system/ssh.service.d/agentdev.conf

  # Stop a previously enabled socket first, then make masking effective before
  # package post-install scripts can enable or start Ubuntu socket activation.
  if [[ -d /run/systemd/system ]]; then
    systemctl disable --now ssh.socket >/dev/null 2>&1 || true
  fi
  install -d -o root -g root -m 0755 /etc/systemd/system
  ln -sfn /dev/null /etc/systemd/system/ssh.socket
  if [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
  fi
}

activate_policy() {
  install -d -o root -g root -m 0755 /run/sshd
  ssh-keygen -A
  /usr/sbin/sshd -t -f /etc/ssh/sshd_config.agentdev
  if [[ -d /run/systemd/system ]]; then
    systemctl daemon-reload
    systemctl enable ssh.service
    systemctl restart ssh.service
  else
    # Hosted CI has no systemd PID 1; still verify that the service is enabled.
    systemctl --root=/ enable ssh.service
  fi
}

case ${1:-activate} in
  prepare) stage_policy ;;
  activate) activate_policy ;;
  *) echo 'Use prepare or activate.' >&2; exit 2 ;;
esac
