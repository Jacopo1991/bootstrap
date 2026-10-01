#!/usr/bin/env bash
# Run as admin after step 8, never as agent or root.
set -euo pipefail

gpu_requirements() (
  local mode=$1 python=$2 lock=$3
  [[ $mode == --install-only || $mode == --resolve-only ]]
  [[ -x $python && -f $lock ]]
  cd /
  export UV_NO_CONFIG=1
  unset UV_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL UV_INDEX_STRATEGY UV_DEFAULT_INDEX UV_FIND_LINKS

  local tmp
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  python3 - "$lock" "$tmp/pypi.txt" "$tmp/torch.txt" <<'PY'
import re
import sys
from pathlib import Path

source, pypi_path, torch_path = map(Path, sys.argv[1:])
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
blocks = []
current = []
requirement = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")

def finish():
    if current:
        blocks.append("".join(current))
        current.clear()

for line in lines:
    if requirement.match(line):
        finish()
        current.append(line)
    elif current:
        current.append(line)
    elif line.startswith(("--index-url", "--extra-index-url", "--index-strategy", "--index ")):
        continue

finish()
if not blocks:
    raise SystemExit("GPU lock contains no exact requirement blocks")

torch_names = {"torch", "torchvision", "torchaudio", "triton"}
torch_blocks = []
pypi_blocks = []
seen = set()
for block in blocks:
    match = requirement.match(block)
    if not match:
        raise SystemExit("GPU lock contains a malformed requirement block")
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    if name in seen:
        raise SystemExit(f"GPU lock contains duplicate requirement: {name}")
    seen.add(name)
    if "--hash=sha256:" not in block:
        raise SystemExit(f"GPU lock requirement has no SHA-256 hashes: {name}")
    (torch_blocks if name in torch_names else pypi_blocks).append(block)

if "torch" not in seen:
    raise SystemExit("GPU lock does not pin torch")
if len(torch_blocks) + len(pypi_blocks) != len(blocks):
    raise SystemExit("GPU lock split dropped a requirement block")
if not pypi_blocks:
    raise SystemExit("GPU lock contains no PyPI requirements")

pypi_path.write_text("".join(pypi_blocks), encoding="utf-8")
torch_path.write_text("".join(torch_blocks), encoding="utf-8")
print(f"Split {len(blocks)} hash-pinned requirements: {len(torch_blocks)} PyTorch, {len(pypi_blocks)} PyPI")
PY

  local -a dry_run=()
  [[ $mode == --resolve-only ]] && dry_run+=(--dry-run)
  uv pip install --python "$python" --index-url https://download.pytorch.org/whl/cu128 \
    --no-deps --require-hashes "${dry_run[@]}" -r "$tmp/torch.txt"
  uv pip install --python "$python" --index-url https://pypi.org/simple \
    --no-deps --require-hashes "${dry_run[@]}" -r "$tmp/pypi.txt"
  if [[ $mode == --install-only ]]; then
    uv pip check --python "$python"
  fi
)

if [[ ${1:-} == --install-only || ${1:-} == --resolve-only ]]; then
  [[ $# == 3 ]] || { echo 'Usage: gpu.sh --install-only|--resolve-only PYTHON LOCK' >&2; exit 2; }
  gpu_requirements "$1" "$2" "$3"
  exit
fi

[[ $EUID != 0 && $(id -un) == "$(cat /etc/bootstrap/admin-user)" ]] || {
  echo 'Run as the recorded admin.' >&2; exit 1;
}
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/lib/wsl/lib:$PATH"
nvidia-smi

# Leave the restricted admin home before invoking uv as the agent.
cd /
sudo -H -u agent env UV_NO_CONFIG=1 PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin \
  bash -s -- "$ROOT" <<'SH'
set -euo pipefail
root=$1
cd /
venv="/home/agent/.local/share/bootstrap/gpu-venv"
uv venv --python 3.12.14 --allow-existing "$venv"
bash "$root/checks/gpu.sh" --install-only "$venv/bin/python" "$root/checks/gpu-requirements.lock"
"$venv/bin/python" "$root/checks/gpu-smoke.py"
SH

# Exercise the runtime through the privileged admin; do not give agent access.
[[ $(sudo systemctl is-enabled docker.service || true) == disabled ]]
[[ $(sudo systemctl is-enabled docker.socket || true) == disabled ]]
sudo systemctl start docker.service
trap 'sudo systemctl stop docker.service docker.socket containerd.service' EXIT
uv_binary=$(sudo -H -u agent env UV_NO_CONFIG=1 PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin mise which uv)
# shellcheck source=gpu-image.env
source "$ROOT/checks/gpu-image.env"
sudo docker run --rm --gpus all "$GPU_IMAGE" nvidia-smi
sudo docker run --rm --gpus all \
  --mount "type=bind,src=$ROOT/checks,dst=/checks,readonly" \
  --mount "type=bind,src=$uv_binary,dst=/usr/local/bin/uv,readonly" \
  "$GPU_IMAGE" sh -ec '
    uv venv --python /usr/local/bin/python /tmp/gpu-venv
    bash /checks/gpu.sh --install-only /tmp/gpu-venv/bin/python /checks/gpu-requirements.lock
    /tmp/gpu-venv/bin/python /checks/gpu-smoke.py
  '
echo 'PASS: host and GPU container. Docker will be stopped and remains disabled at boot.'
