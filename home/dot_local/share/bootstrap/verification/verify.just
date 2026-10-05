# Run the acceptance suite against the running app: `just verify http://localhost:3000`,
# or one criterion: `just verify http://localhost:3000 @T-4-AC2`.
# Evidence is written to ~/project-data/@PROJECT@/verification/<run>/ (outside this repository).
verify url tag='':
    #!/usr/bin/env bash
    set -euo pipefail
    out="$HOME/project-data/@PROJECT@/verification/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$out"
    echo "Evidence: $out"
    export VERIFY_URL='{{url}}' VERIFY_OUT="$out"
    if [ -n '{{tag}}' ]; then
      playwright test --config acceptance/playwright.config.ts --grep '{{tag}}'
    else
      playwright test --config acceptance/playwright.config.ts
    fi
