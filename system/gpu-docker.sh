#!/usr/bin/env bash
# Separate step 8 only: container runtime, never a Linux GPU driver/toolkit.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
[[ -e /etc/bootstrap/admin-user ]] || { echo 'Run install.sh first.' >&2; exit 1; }
apt_key docker "$DOCKER_KEY_URL" "$DOCKER_KEY_SHA256" armored
apt_key nvidia-container-toolkit "$NVIDIA_KEY_URL" "$NVIDIA_KEY_SHA256" armored
cat > /etc/apt/sources.list.d/bootstrap-docker.list <<'EOF'
deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable
EOF
cat > /etc/apt/sources.list.d/bootstrap-nvidia.list <<'EOF'
deb [arch=amd64 signed-by=/etc/apt/keyrings/nvidia-container-toolkit.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /
EOF

# Mask before apt so package post-install actions cannot activate Docker.
systemctl mask --now docker.service docker.socket
trap 'systemctl disable --now docker.service docker.socket; systemctl unmask docker.service docker.socket' EXIT
apt-get update
apt_locked "$BOOTSTRAP_ROOT/system/apt-docker.lock"
apt_locked "$BOOTSTRAP_ROOT/system/apt-nvidia.lock"
nvidia-ctk runtime configure --runtime=docker
systemctl unmask docker.service docker.socket
systemctl disable --now docker.service docker.socket containerd.service
trap - EXIT
[[ $(systemctl is-enabled docker.service || true) == disabled ]]
[[ $(systemctl is-enabled docker.socket || true) == disabled ]]
if id -nG agent | tr ' ' '\n' | grep -qx docker; then
  echo 'Agent must not have Docker access.' >&2; exit 1;
fi
echo 'Step 8 installed. Admin can explicitly start Docker for checks; it stays disabled at boot.'
