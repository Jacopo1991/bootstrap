#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$ROOT"
# Policy fixtures create independent temporary repositories and supply their own
# lane anchors. Do not make them inherit the enclosing build lane's boundary.
unset TASK_WORKTREE
# Missing tools use the same checksum-pinned fallback on hosts and fresh CI.
export PATH="$tmp/gate-bin:$PATH"
bash ci/gate-tools.sh "$tmp"
failed=0
skipped=0
suite_results="${TMPDIR:-/tmp}/gate-suites.tsv"
: > "$suite_results"
suite_timings=()
lane_skippable_suite() {
  # Socket/browser suites may skip in lanes, never in a trusted gate.
  if [[ -n ${TASK_GATE_RUN_ID:-} ]]; then return 1; fi
  [[ $# == 2 ]] && {
    [[ $1 == bash && $2 == ci/boundary-test.sh ]] ||
      [[ $1 == python3 && $2 == ci/gate-browser-test.py ]]
  }
}
run() {
  local command="$*" started ended elapsed status=0 result
  started=$(date +%s%N)
  echo "RUN: $command" >&2
  "$@" || status=$?
  if (( status == 77 )) && lane_skippable_suite "$@"; then
    result=skipped
    echo "SKIP: suite $command" >&2
    skipped=$((skipped + 1))
  elif (( status )); then
    result=failed
    echo "FAIL: $command (exit $status)" >&2
    failed=$((failed + 1))
  else
    result=passed
    echo "PASS: suite $command" >&2
  fi
  ended=$(date +%s%N)
  elapsed=$((ended - started))
  printf '%s\t%s\n' "$result" "$command" >> "$suite_results"
  suite_timings+=("$elapsed"$'\t'"$command")
}
run bash ci/gate-tools.sh
run python3 ci/gate-tools-test.py
run python3 ci/gitleaks-canary.py "$(command -v gitleaks || true)"
run bash ci/gitleaks-working-tree-test.sh
run bash ci/boundary-test.sh
run bash -n system/ssh.sh system/authorize-agent-key.sh ci/vscode-ssh-test.sh
run python3 ci/monthly_pins_test.py
run python3 ci/agy-install-test.py
run python3 ci/agent-config-test.py
run python3 ci/claude-lean-session-test.py
run python3 ci/claude-managed-mods-test.py
run python3 ci/managed-policy-test.py
run python3 ci/rootless-docker-test.py
run python3 ci/agent-drift-test.py
run python3 ci/inventory-mirror-test.py
run python3 ci/prune-revisions-test.py
run bash ci/new-project-test.sh
run bash ci/pre-commit-test.sh
run python3 ci/package-caps-test.py
run python3 ci/package-cap-resolver-test.py
run python3 ci/osv-db-test.py
run python3 ci/security-watch-test.py
run python3 ci/verification-pack-test.py
run python3 ci/gate-browser-test.py
run python3 ci/skills-hooks-test.py
run python3 ci/workspace-tidy-test.py
run python3 ci/task-test.py
run python3 ci/task-usage-test.py
run python3 ci/task-batch-test.py
run python3 ci/task-line-budget-test.py
run python3 ci/task-line-budget.py
run python3 ci/backrest-test.py
run python3 ci/job-ping-test.py
run python3 ci/shell-connector-test.py
run python3 ci/backlog-mcp-test.py
mapfile -t scripts < <(find . -name '*.sh' -type f -not -path './.git/*')
scripts+=(home/dot_local/bin/executable_qmd-refresh home/dot_local/bin/executable_new-project
  home/dot_local/bin/executable_pre-commit-enable home/dot_local/bin/executable_osv-db-refresh
  home/dot_local/bin/executable_verify-enable home/dot_local/bin/executable_gh-skill-update
  home/dot_local/bin/executable_job-ping home/dot_local/bin/executable_agentdev-shell-mcp
  home/dot_local/bin/executable_agentdev-backlog-mcp)
run shellcheck --external-sources --source-path=SCRIPTDIR "${scripts[@]}"
# Render the actual chezmoi script too; shared pin constants are intentionally
# unused in each individual consumer, hence only SC2034 is excluded here.
mkdir -p "$tmp/home"
CHEZMOI_GIT_NAME='Bootstrap CI' CHEZMOI_GIT_EMAIL='bootstrap-ci@example.invalid' \
  run chezmoi --source "$ROOT" --destination "$tmp/home" --config "$tmp/chezmoi.toml" init
run chezmoi --source "$ROOT" --destination "$tmp/home" --config "$tmp/chezmoi.toml" \
  execute-template < home/run_onchange_after_10-tools.sh.tmpl > "$tmp/tools.sh"
run shellcheck --exclude=SC2034 "$tmp/tools.sh"
run chezmoi --source "$ROOT" --destination "$tmp/home" --config "$tmp/chezmoi.toml" \
  execute-template < home/dot_gitconfig.tmpl > "$tmp/gitconfig"
run python3 ci/git-credentials-test.py "$tmp/gitconfig"
run bash ci/git-ignore-test.sh
run bash ci/compile-cache-test.sh
run python3 ci/qmd-test.py "$(command -v chezmoi || true)"
run gitleaks git --redact --no-banner .
run bash ci/gitleaks-working-tree.sh gitleaks "$ROOT" "$tmp/working-tree-scan"

echo "Suite timings (seconds, slowest first):" >&2
while IFS=$'\t' read -r elapsed command; do
  printf 'TIME: %s s  %s\n' "$(awk -v ns="$elapsed" 'BEGIN { printf "%.3f", ns / 1000000000 }')" "$command" >&2
done < <(printf '%s\n' "${suite_timings[@]}" | sort -rn -k1,1)
echo "Gate suites finished (failed=$failed, skipped=$skipped)"
(( failed == 0 ))
