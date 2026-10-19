#!/usr/bin/env bash
# Offline preflight shared by lint and its regression test.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../home/.chezmoitemplates/pins.env
source "$ROOT/home/.chezmoitemplates/pins.env"
failed=0
check_tool() {
  local tool=$1 expected=$2 output
  shift 2
  if ! command -v "$tool" >/dev/null; then
    echo "FAIL: missing $tool; apply bootstrap with install.sh outside the lane sandbox" >&2
    failed=1
  elif ! output=$("$tool" "$@" 2>&1) || [[ $output != *"$expected"* ]]; then
    echo "FAIL: $tool must report $expected; apply bootstrap with install.sh outside the lane sandbox" >&2
    failed=1
  else
    echo "PASS: installed $tool ($expected), no download"
  fi
}
version_from_url() {
  local version=${1##*/download/}
  version=${version%%/*}
  printf '%s' "${version#v}"
}
check_tool shellcheck "version: $(version_from_url "$SHELLCHECK_URL")" --version
check_tool gitleaks "$(version_from_url "$GITLEAKS_URL")" version
lychee_version=${LYCHEE_URL##*/download/lychee-v}
check_tool lychee "lychee ${lychee_version%%/*}" --version
check_tool osv-scanner "osv-scanner version: $(version_from_url "$OSV_SCANNER_URL")" --version
check_tool chezmoi "v$CHEZMOI_VERSION" --version
check_tool pre-commit "pre-commit $(sed -n 's/^pre-commit==//p' "$ROOT/home/dot_local/share/bootstrap/pre-commit/requirements.in")" --version
exit "$failed"
