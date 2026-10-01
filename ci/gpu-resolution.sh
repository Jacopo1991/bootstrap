#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../checks/gpu-image.env
source "$ROOT/checks/gpu-image.env"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/uv" "$tmp/simulated-admin/home/jacopo/bootstrap0700"
curl --fail --silent --show-error --location --retry 3 \
  https://releases.astral.sh/github/uv/releases/download/0.12.21/uv-x86_64-unknown-linux-gnu.tar.gz \
  -o "$tmp/uv.tar.gz"
printf '%s  %s\n' \
  23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0 \
  "$tmp/uv.tar.gz" | sha256sum --check --status
tar -xzf "$tmp/uv.tar.gz" --strip-components=1 -C "$tmp/uv"
grep -q '^uv 0\.12\.21' <("$tmp/uv/uv" --version)
export PATH="$tmp/uv:$PATH"
export UV_NO_CONFIG=1

# Match the pinned host Python, then start from a restricted-style cwd with
# hostile uv configuration while exercising the same resolver entry point.
cd /
uv python install 3.12.14
uv venv --python 3.12.14 "$tmp/host-venv"
host_python="$tmp/host-venv/bin/python"
printf '[invalid]\nthis-is-not-a-valid-uv-setting = true\n' > "$tmp/simulated-admin/home/jacopo/bootstrap0700/uv.toml"
(
  cd "$tmp/simulated-admin/home/jacopo/bootstrap0700"
  bash "$ROOT/checks/gpu.sh" --resolve-only "$host_python" "$ROOT/checks/gpu-requirements.lock"
)

# Resolve the exact runtime lock against the pinned container interpreter too.
docker run --rm \
  --workdir /home/jacopo/bootstrap0700 \
  --mount "type=bind,src=$tmp/simulated-admin/home/jacopo/bootstrap0700,dst=/home/jacopo/bootstrap0700,readonly" \
  --mount "type=bind,src=$ROOT/checks,dst=/checks,readonly" \
  --mount "type=bind,src=$tmp/uv/uv,dst=/usr/local/bin/uv,readonly" \
  "$GPU_IMAGE" \
  bash -ec 'UV_NO_CONFIG=1 bash /checks/gpu.sh --resolve-only /usr/local/bin/python /checks/gpu-requirements.lock'
