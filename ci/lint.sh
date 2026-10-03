#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../system/common.sh
source "$ROOT/system/common.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
download_verified "$SHELLCHECK_URL" "$SHELLCHECK_SHA256" "$tmp/shellcheck.tar.xz"
tar -xJf "$tmp/shellcheck.tar.xz" -C "$tmp"
download_verified "$GITLEAKS_URL" "$GITLEAKS_SHA256" "$tmp/gitleaks.tar.gz"
tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp" gitleaks
export PATH="$tmp:$PATH"
python3 "$ROOT/ci/gitleaks-canary.py" "$tmp/gitleaks"
cd "$ROOT"
bash ci/boundary-test.sh
bash -n system/ssh.sh system/authorize-agent-key.sh ci/vscode-ssh-test.sh
python3 ci/monthly_pins_test.py
python3 ci/agent-config-test.py
python3 ci/claude-managed-mods-test.py
python3 ci/git-default-merge-test.py
python3 ci/agent-drift-test.py
python3 ci/inventory-mirror-test.py
mapfile -t scripts < <(find . -name '*.sh' -type f -not -path './.git/*')
"$tmp/shellcheck-v0.11.0/shellcheck" --external-sources --source-path=SCRIPTDIR "${scripts[@]}"
# Render the actual chezmoi script too; shared pin constants are intentionally
# unused in each individual consumer, hence only SC2034 is excluded here.
download_verified "$CHEZMOI_URL" "$CHEZMOI_SHA256" "$tmp/chezmoi.tar.gz"
tar -xzf "$tmp/chezmoi.tar.gz" -C "$tmp" chezmoi
mkdir -p "$tmp/home"
CHEZMOI_GIT_NAME='Bootstrap CI' CHEZMOI_GIT_EMAIL='bootstrap-ci@example.invalid' \
  "$tmp/chezmoi" --source "$ROOT" --destination "$tmp/home" --config "$tmp/chezmoi.toml" init
"$tmp/chezmoi" --source "$ROOT" --destination "$tmp/home" --config "$tmp/chezmoi.toml" \
  execute-template < home/run_onchange_after_10-tools.sh.tmpl > "$tmp/tools.sh"
"$tmp/shellcheck-v0.11.0/shellcheck" --exclude=SC2034 "$tmp/tools.sh"
"$tmp/chezmoi" --source "$ROOT" --destination "$tmp/home" --config "$tmp/chezmoi.toml" \
  execute-template < home/dot_gitconfig.tmpl > "$tmp/gitconfig"
python3 ci/git-credentials-test.py "$tmp/gitconfig"
"$tmp/gitleaks" git --redact --no-banner .
"$tmp/gitleaks" dir --redact --no-banner .
