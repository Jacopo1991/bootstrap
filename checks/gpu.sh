#!/usr/bin/env bash
# Run as admin after step 8, never as agent or root.
set -euo pipefail
[[ $EUID != 0 && $(id -un) == "$(cat /etc/bootstrap/admin-user)" ]] || {
  echo 'Run as the recorded admin.' >&2; exit 1;
}
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/lib/wsl/lib:$PATH"
nvidia-smi
sudo -H -u agent env PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin \
  bash -s -- "$ROOT" <<'SH'
set -euo pipefail
root=$1
venv="$HOME/.local/share/bootstrap/gpu-venv"
uv venv --python 3.12.14 --allow-existing "$venv"
uv pip sync --python "$venv/bin/python" --require-hashes "$root/checks/gpu-requirements.lock"
"$venv/bin/python" "$root/checks/gpu-smoke.py"
SH

# Exercise the runtime through the privileged admin; do not give agent access.
[[ $(sudo systemctl is-enabled docker.service || true) == disabled ]]
[[ $(sudo systemctl is-enabled docker.socket || true) == disabled ]]
sudo systemctl start docker.service
trap 'sudo systemctl stop docker.service docker.socket containerd.service' EXIT
uv_binary=$(sudo -H -u agent mise which uv)
# shellcheck source=gpu-image.env
source "$ROOT/checks/gpu-image.env"
sudo docker run --rm --gpus all "$GPU_IMAGE" nvidia-smi
sudo docker run --rm --gpus all \
  --mount "type=bind,src=$ROOT/checks,dst=/checks,readonly" \
  --mount "type=bind,src=$uv_binary,dst=/usr/local/bin/uv,readonly" \
  "$GPU_IMAGE" sh -ec '
    uv venv --python /usr/local/bin/python /tmp/gpu-venv
    uv pip sync --python /tmp/gpu-venv/bin/python --require-hashes /checks/gpu-requirements.lock
    /tmp/gpu-venv/bin/python /checks/gpu-smoke.py
  '
echo 'PASS: host and GPU container. Docker will be stopped and remains disabled at boot.'
