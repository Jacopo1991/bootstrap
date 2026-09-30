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
cd "$ROOT"
mapfile -t scripts < <(find . -name '*.sh' -type f -not -path './.git/*')
"$tmp/shellcheck-v0.11.0/shellcheck" --external-sources --source-path=SCRIPTDIR "${scripts[@]}"
"$tmp/gitleaks" git --redact --no-banner .
"$tmp/gitleaks" dir --redact --no-banner .
