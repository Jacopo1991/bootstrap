#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "$0")/common.sh"
require_root
exec /usr/bin/python3 -I "$BOOTSTRAP_ROOT/system/studio-layer-install.py" "$BOOTSTRAP_ROOT/system/studio-layer.release" /home/agent/dev_workspace/studio-layer
