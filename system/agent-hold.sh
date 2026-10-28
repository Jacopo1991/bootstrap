#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "$0")/common.sh"
require_root
install -o root -g root -m 0644 "$BOOTSTRAP_ROOT/home/dot_local/bin/task_hold_lock.py" /usr/local/bin/task_hold_lock.py
python3 /usr/local/bin/task_hold_lock.py
install -o root -g root -m 0755 "$BOOTSTRAP_ROOT/system/agent-hold" /usr/local/bin/agent-hold
