# `task check` is the lane gate entry; it runs this recipe before a merge.
# Full lint in a clean environment, as required before every bootstrap merge.
check:
    env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" TASK_GATE_RUN_ID="${TASK_GATE_RUN_ID:-}" TASK_GATE_PORT_BASE="${TASK_GATE_PORT_BASE:-}" bash ci/lint.sh
