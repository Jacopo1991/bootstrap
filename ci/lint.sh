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
# The pre-commit gate's pinned tools, as the chezmoi tools script installs them.
download_verified "$LYCHEE_URL" "$LYCHEE_SHA256" "$tmp/lychee.tar.gz"
tar -xzf "$tmp/lychee.tar.gz" -C "$tmp" --strip-components=1 lychee-x86_64-unknown-linux-musl/lychee
download_verified "$OSV_SCANNER_URL" "$OSV_SCANNER_SHA256" "$tmp/osv-scanner"
chmod 0755 "$tmp/osv-scanner"
python3 -m venv "$tmp/pre-commit"
"$tmp/pre-commit/bin/pip" install --quiet --disable-pip-version-check --require-hashes \
  -r "$ROOT/home/dot_local/share/bootstrap/pre-commit/requirements.lock"
mkdir -p "$tmp/gate-bin"
ln -s "$tmp/pre-commit/bin/pre-commit" "$tmp/gate-bin/pre-commit"
export PATH="$tmp:$tmp/gate-bin:$PATH"
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
bash ci/new-project-test.sh
bash ci/pre-commit-test.sh
mapfile -t scripts < <(find . -name '*.sh' -type f -not -path './.git/*')
scripts+=(home/dot_local/bin/executable_qmd-refresh home/dot_local/bin/executable_new-project
  home/dot_local/bin/executable_pre-commit-enable home/dot_local/bin/executable_osv-db-refresh)
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
python3 ci/qmd-test.py "$tmp/chezmoi"
"$tmp/gitleaks" git --redact --no-banner .
"$tmp/gitleaks" dir --redact --no-banner .
