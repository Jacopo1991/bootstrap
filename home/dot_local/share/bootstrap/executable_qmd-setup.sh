#!/usr/bin/env bash
# One-time and re-run qmd setup, run as agent by install.sh after chezmoi apply,
# outside agent sessions and the Claude sandbox. Every step must succeed; nothing
# is silenced. Needs the agent's user systemd (XDG_RUNTIME_DIR and
# DBUS_SESSION_BUS_ADDRESS, which install.sh provides).
set -euo pipefail
[[ $(id -un) == agent && $EUID != 0 ]] || { echo 'Run as agent.' >&2; exit 1; }
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"
source_dir=${BOOTSTRAP_SOURCE:-/opt/machine-bootstrap/current}
step() { printf '\n== qmd setup: %s\n' "$*"; }

step 'render ~/.config/qmd/index.yml'
chezmoi --source "$source_dir" apply "$HOME/.config/qmd/index.yml"
grep -q '^  cortex-' "$HOME/.config/qmd/index.yml" || {
  echo "index.yml has no collections; clone the Cortex repositories into ~/cortex and re-run." >&2
  exit 1
}

step 'GPU'
gpu_report=$("$HOME/.local/share/bootstrap/npm/node_modules/.bin/node-llama-cpp" inspect gpu 2>&1)
printf '%s\n' "$gpu_report" | grep -E '^(CUDA|Vulkan):' || true
if [[ -e /usr/lib/wsl/lib/libcuda.so.1 ]] && ! grep -q '^CUDA: available' <<<"$gpu_report"; then
  echo 'WARNING: an NVIDIA driver is present but node-llama-cpp reports no CUDA; qmd will run on the CPU.' >&2
fi

step 'models, index and embeddings'
qmd pull
qmd update
# Embedding is not worth aborting the install for (a GPU out of memory, a driver
# fault): retry once on the CPU, otherwise leave it to qmd-index.timer.
if ! qmd embed; then
  echo 'WARNING: qmd embed failed; retrying once on the CPU (QMD_FORCE_CPU=1).' >&2
  if ! QMD_FORCE_CPU=1 qmd embed; then
    echo 'WARNING: qmd embed failed on the CPU too; continuing. qmd-index.timer retries every 15 minutes; vector search stays incomplete until it succeeds.' >&2
  fi
fi

step 'user systemd timers'
if [[ ! -S ${XDG_RUNTIME_DIR:-/nonexistent}/bus ]]; then
  echo "No user systemd bus at \$XDG_RUNTIME_DIR/bus; cannot start qmd-index.timer." >&2
  exit 1
fi
systemctl --user daemon-reload
systemctl --user enable --now qmd-index.timer
systemctl --user is-active --quiet qmd-index.timer
systemctl --user list-timers qmd-index.timer --no-pager
# The pre-commit gate's offline vulnerability data (osv-db-refresh) on the same user manager.
systemctl --user enable --now osv-db-refresh.timer
systemctl --user is-active --quiet osv-db-refresh.timer
systemctl --user list-timers osv-db-refresh.timer --no-pager

step 'shared qmd MCP server'
# One HTTP server keeps the models loaded once; Claude Code sessions and the
# Claude app connect to it instead of each starting a stdio server on the GPU.
systemctl --user enable --now qmd-mcp.service
systemctl --user is-active --quiet qmd-mcp.service

step 'Claude Code MCP server'
claude=$HOME/.local/bin/claude
# Replace a stale entry; `mcp get` fails only when there is none.
if "$claude" mcp get qmd >/dev/null 2>&1; then
  "$claude" mcp remove --scope user qmd
fi
"$claude" mcp add --scope user --transport http qmd http://localhost:8181/mcp
"$claude" mcp get qmd
echo 'qmd setup complete.'
