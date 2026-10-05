# shellcheck shell=bash
# Included in the chezmoi run-on-change script; exact versions, no credentials.
mise trust "$HOME/.config/mise/config.toml"
mise install
mise reshim
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
download() {
  curl --fail --silent --show-error --location --retry 3 "$1" -o "$3"
  printf '%s  %s\n' "$2" "$3" | sha256sum --check --status
}
mkdir -p "$HOME/.local/bin"
# uv's cache is the one sandbox-writable path outside the repository.
mkdir -p "$HOME/.cache/uv" "$HOME/.npm"
download "$GITLEAKS_URL" "$GITLEAKS_SHA256" "$tmp/gitleaks.tar.gz"
tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp" gitleaks
cmp -s "$tmp/gitleaks" "$HOME/.local/bin/gitleaks" || install -m 0755 "$tmp/gitleaks" "$HOME/.local/bin/gitleaks"
gitleaks_expected=${GITLEAKS_URL##*/download/v}
gitleaks_expected=${gitleaks_expected%%/*}
[[ $("$HOME/.local/bin/gitleaks" version) == *"$gitleaks_expected"* ]]
if [[ ! -x $HOME/.local/bin/claude ]] || [[ $("$HOME/.local/bin/claude" --version) != "$CLAUDE_VERSION (Claude Code)" ]]; then
  download "$CLAUDE_INSTALLER_URL" "$CLAUDE_INSTALLER_SHA256" "$tmp/claude-install.sh"
  bash "$tmp/claude-install.sh" "$CLAUDE_VERSION"
fi
[[ $("$HOME/.local/bin/claude" --version) == "$CLAUDE_VERSION (Claude Code)" ]]
download "$BWS_URL" "$BWS_SHA256" "$tmp/bws.zip"
unzip -q "$tmp/bws.zip" -d "$tmp/bws"
cmp -s "$tmp/bws/bws" "$HOME/.local/bin/bws" || install -m 0755 "$tmp/bws/bws" "$HOME/.local/bin/bws"
download "$SECRETSPEC_URL" "$SECRETSPEC_SHA256" "$tmp/secretspec.tar.xz"
tar -xJf "$tmp/secretspec.tar.xz" -C "$tmp"
secretspec_binary=$(find "$tmp" -type f -name secretspec -print -quit)
[[ -n $secretspec_binary ]]
cmp -s "$secretspec_binary" "$HOME/.local/bin/secretspec" || install -m 0755 "$secretspec_binary" "$HOME/.local/bin/secretspec"
npm_root="$HOME/.local/share/bootstrap/npm"
mise exec -- npm ci --prefix "$npm_root" --no-audit --no-fund
ln -sfn "$npm_root/node_modules/.bin/codex" "$HOME/.local/bin/codex"
ln -sfn "$npm_root/node_modules/.bin/ccusage" "$HOME/.local/bin/ccusage"
ln -sfn "$npm_root/node_modules/.bin/backlog" "$HOME/.local/bin/backlog"
# qmd runs through a small wrapper so every caller (CLI, timer, MCP server) uses the GPU:
# node-llama-cpp only detects the CUDA runtime through LD_LIBRARY_PATH and the WSL
# driver tools on PATH, not through the backend's own $ORIGIN runpath.
rm -f "$HOME/.local/bin/qmd"
cat > "$HOME/.local/bin/qmd" <<'QMD'
#!/usr/bin/env bash
cuda="$HOME/.local/share/bootstrap/npm/node_modules/@node-llama-cpp/linux-x64-cuda/bins/linux-x64-cuda"
export LD_LIBRARY_PATH="$cuda${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
case ":$PATH:" in *:/usr/lib/wsl/lib:*) ;; *) export PATH="$PATH:/usr/lib/wsl/lib" ;; esac
exec "$HOME/.local/share/bootstrap/npm/node_modules/.bin/qmd" "$@"
QMD
chmod 0755 "$HOME/.local/bin/qmd"
# qmd GPU: node-llama-cpp's prebuilt CUDA backend links libcudart.so.13 and
# libcublas.so.13 and finds them through its $ORIGIN runpath. Put the hash-pinned
# NVIDIA runtime libraries from the PyPI wheels next to it (no root, no env vars;
# the driver's libcuda comes from /usr/lib/wsl/lib). npm ci above wiped the
# directory, so restore it on every run from the wheel cache.
cuda_dir="$npm_root/node_modules/@node-llama-cpp/linux-x64-cuda/bins/linux-x64-cuda"
wheels="$HOME/.cache/bootstrap/cuda-wheels"
mkdir -p "$wheels"
cuda_wheel() {
  local file="$wheels/$2.whl"
  if [[ ! -f $file ]] || ! printf '%s  %s\n' "$3" "$file" | sha256sum --check --status; then
    download "$1" "$3" "$file.part"
    mv "$file.part" "$file"
  fi
  shift 3
  unzip -q -j -o "$file" "$@" -d "$cuda_dir"
}
cuda_wheel "$CUDA_RUNTIME_URL" cuda-runtime "$CUDA_RUNTIME_SHA256" nvidia/cu13/lib/libcudart.so.13
cuda_wheel "$CUBLAS_URL" cublas "$CUBLAS_SHA256" nvidia/cu13/lib/libcublas.so.13 nvidia/cu13/lib/libcublasLt.so.13
if ldd "$cuda_dir/libggml-cuda.so" | grep -Eq 'libcu(dart|blas)[^ ]* => not found'; then
  ldd "$cuda_dir/libggml-cuda.so" >&2
  echo 'qmd CUDA backend still has unresolved CUDA runtime libraries.' >&2
  exit 1
fi
# Models, the first index, the timer and the MCP registration need the agent's
# user systemd; install.sh runs ~/.local/share/bootstrap/qmd-setup.sh for them.
