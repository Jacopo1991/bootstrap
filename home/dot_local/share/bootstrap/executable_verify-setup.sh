#!/usr/bin/env bash
# One-time and re-run verification pack setup, run as agent by install.sh after chezmoi apply,
# outside agent sessions and the Claude sandbox (the sandbox cannot reach the browser CDN).
# Every step must succeed; nothing is silenced.
set -euo pipefail
[[ $(id -un) == agent && $EUID != 0 ]] || { echo 'Run as agent.' >&2; exit 1; }
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"
step() { printf '\n== verify setup: %s\n' "$*"; }

step 'Chromium for the pinned Playwright version'
# The shared cache is fixed by the playwright wrapper. A browser build is keyed by the
# Playwright version, so a pin bump downloads the new build here and nothing at test time.
playwright install chromium

step 'Claude Code MCP server'
claude=$HOME/.local/bin/claude
# Replace a stale entry; `mcp get` fails only when there is none.
if "$claude" mcp get playwright >/dev/null 2>&1; then
  "$claude" mcp remove --scope user playwright
fi
"$claude" mcp add --scope user playwright -- "$HOME/.local/bin/playwright-mcp"
"$claude" mcp get playwright
echo 'verify setup complete.'
