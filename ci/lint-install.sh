#!/usr/bin/env bash
# Download only a missing tool, into the caller's disposable gate directory.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=../system/common.sh
source "$ROOT/system/common.sh"
tmp=${1:?gate directory required}
tool=${2:?tool required}
mkdir -p "$tmp/gate-bin"
case "$tool" in
  shellcheck)
    download_verified "$SHELLCHECK_URL" "$SHELLCHECK_SHA256" "$tmp/shellcheck.tar.xz"
    tar -xJf "$tmp/shellcheck.tar.xz" -C "$tmp"
    version=${SHELLCHECK_URL##*/download/}
    version=${version%%/*}
    ln -s "$tmp/shellcheck-$version/shellcheck" "$tmp/gate-bin/shellcheck"
    ;;
  gitleaks)
    download_verified "$GITLEAKS_URL" "$GITLEAKS_SHA256" "$tmp/gitleaks.tar.gz"
    tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp/gate-bin" gitleaks
    ;;
  lychee)
    download_verified "$LYCHEE_URL" "$LYCHEE_SHA256" "$tmp/lychee.tar.gz"
    tar -xzf "$tmp/lychee.tar.gz" -C "$tmp/gate-bin" --strip-components=1 lychee-x86_64-unknown-linux-musl/lychee
    ;;
  osv-scanner)
    download_verified "$OSV_SCANNER_URL" "$OSV_SCANNER_SHA256" "$tmp/gate-bin/osv-scanner"
    chmod 0755 "$tmp/gate-bin/osv-scanner"
    ;;
  pre-commit)
    python3 -m venv "$tmp/pre-commit"
    "$tmp/pre-commit/bin/pip" install --quiet --disable-pip-version-check --require-hashes \
      -r "$ROOT/home/dot_local/share/bootstrap/pre-commit/requirements.lock"
    ln -s "$tmp/pre-commit/bin/pre-commit" "$tmp/gate-bin/pre-commit"
    ;;
  chezmoi)
    download_verified "$CHEZMOI_URL" "$CHEZMOI_SHA256" "$tmp/chezmoi.tar.gz"
    tar -xzf "$tmp/chezmoi.tar.gz" -C "$tmp/gate-bin" chezmoi
    ;;
  *) echo "Unknown gate tool: $tool" >&2; exit 1 ;;
esac
