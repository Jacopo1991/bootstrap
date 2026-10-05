# Run the acceptance suite against the running app: `just verify http://localhost:3000`,
# or one criterion: `just verify http://localhost:3000 @T-4-AC2`.
# Evidence is written to acceptance/evidence/<run>/ (gitignored).
# A plain line recipe, not a shebang one: just writes shebang scripts to XDG_RUNTIME_DIR,
# which is read-only inside the agent sandbox.
verify url tag='':
    @out="$PWD/acceptance/evidence/$(date -u +%Y%m%dT%H%M%SZ)" && \
      mkdir -p "$out" && \
      echo "Evidence: $out" && \
      export VERIFY_URL='{{url}}' VERIFY_OUT="$out" && \
      tag='{{tag}}' && \
      playwright test --config acceptance/playwright.config.ts ${tag:+--grep "$tag"}
