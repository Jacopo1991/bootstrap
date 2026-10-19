#!/usr/bin/env bash
# Sourced only by hosted CI when its disposable runner lacks pinned gate tools.
# Lanes and trusted local checks never enter this installation path.
tmp=${tmp:?ci/lint.sh must provide a disposable directory}
# shellcheck source=../system/common.sh
source "$ROOT/system/common.sh"
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
shellcheck_version=${SHELLCHECK_URL##*/download/}
shellcheck_version=${shellcheck_version%%/*}
ln -s "$tmp/shellcheck-$shellcheck_version/shellcheck" "$tmp/gate-bin/shellcheck"
export PATH="$tmp:$tmp/gate-bin:$PATH"
download_verified "$CHEZMOI_URL" "$CHEZMOI_SHA256" "$tmp/chezmoi.tar.gz"
tar -xzf "$tmp/chezmoi.tar.gz" -C "$tmp" chezmoi
