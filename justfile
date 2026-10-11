# `just check` is the gate: what lanes and `task check` run before a merge.
# Full lint in a clean environment, as required before every bootstrap merge.
check:
    env -i PATH="$PATH" HOME="$HOME" NO_PROXY="$NO_PROXY" no_proxy="$no_proxy" bash ci/lint.sh
