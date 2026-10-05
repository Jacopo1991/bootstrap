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
# Local quality gate (pre-commit-enable): lychee, osv-scanner and pre-commit from a hashed lock.
download "$LYCHEE_URL" "$LYCHEE_SHA256" "$tmp/lychee.tar.gz"
tar -xzf "$tmp/lychee.tar.gz" -C "$tmp"
lychee_binary=$(find "$tmp" -type f -name lychee -print -quit)
[[ -n $lychee_binary ]]
cmp -s "$lychee_binary" "$HOME/.local/bin/lychee" || install -m 0755 "$lychee_binary" "$HOME/.local/bin/lychee"
lychee_expected=${LYCHEE_URL##*/download/lychee-v}
[[ $("$HOME/.local/bin/lychee" --version) == "lychee ${lychee_expected%%/*}" ]]
download "$OSV_SCANNER_URL" "$OSV_SCANNER_SHA256" "$tmp/osv-scanner"
cmp -s "$tmp/osv-scanner" "$HOME/.local/bin/osv-scanner" || install -m 0755 "$tmp/osv-scanner" "$HOME/.local/bin/osv-scanner"
osv_expected=${OSV_SCANNER_URL##*/download/v}
[[ $("$HOME/.local/bin/osv-scanner" --version) == *"osv-scanner version: ${osv_expected%%/*}"* ]]
# just runs the `verify` recipe that verify-enable adds to knowledge repositories.
download "$JUST_URL" "$JUST_SHA256" "$tmp/just.tar.gz"
tar -xzf "$tmp/just.tar.gz" -C "$tmp" just
cmp -s "$tmp/just" "$HOME/.local/bin/just" || install -m 0755 "$tmp/just" "$HOME/.local/bin/just"
just_expected=${JUST_URL##*/download/}
[[ $("$HOME/.local/bin/just" --version) == "just ${just_expected%%/*}" ]]
pre_commit_root="$HOME/.local/share/bootstrap/pre-commit"
mise exec -- uv venv --quiet --allow-existing --python "$(mise which python)" "$pre_commit_root/venv"
mise exec -- uv pip sync --quiet --require-hashes --python "$pre_commit_root/venv/bin/python" "$pre_commit_root/requirements.lock"
ln -sfn "$pre_commit_root/venv/bin/pre-commit" "$HOME/.local/bin/pre-commit"
pre_commit_expected=$(sed -n 's/^pre-commit==//p' "$pre_commit_root/requirements.in")
[[ $("$HOME/.local/bin/pre-commit" --version) == "pre-commit $pre_commit_expected" ]]
# Create pre-commit's store here: inside the agent sandbox ~/.cache is read-only and
# pre-commit then runs local hooks from the existing store without writing to it.
"$HOME/.local/bin/pre-commit" gc >/dev/null
osv-db-refresh >/dev/null
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
# Playwright (verification pack): one pinned version and one shared browser cache that agent
# sessions, the MCP server and test runs all use. The wrappers fix the cache path; the browser
# is downloaded once by verify-setup.sh (install.sh, outside the sandbox), never when tests run.
rm -f "$HOME/.local/bin/playwright" "$HOME/.local/bin/playwright-mcp"
cat > "$HOME/.local/bin/playwright" <<'PLAYWRIGHT'
#!/usr/bin/env bash
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"
exec "$HOME/.local/share/bootstrap/npm/node_modules/.bin/playwright" "$@"
PLAYWRIGHT
cat > "$HOME/.local/bin/playwright-mcp" <<'PLAYWRIGHT_MCP'
#!/usr/bin/env bash
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"
exec "$HOME/.local/share/bootstrap/npm/node_modules/.bin/playwright-mcp" --browser chromium --headless --isolated "$@"
PLAYWRIGHT_MCP
chmod 0755 "$HOME/.local/bin/playwright" "$HOME/.local/bin/playwright-mcp"
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
