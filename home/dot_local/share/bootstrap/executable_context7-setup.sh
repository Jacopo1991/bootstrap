#!/usr/bin/env bash
# Registers the Context7 documentation MCP server for Claude Code (user scope), run as
# agent by install.sh after chezmoi apply, outside agent sessions. It is a remote HTTP
# server with no key (anonymous rate limits); claude.ai connectors stay off through
# ENABLE_CLAUDEAI_MCP_SERVERS=false, so this is the only way lanes get library docs.
set -euo pipefail
[[ $(id -un) == agent && $EUID != 0 ]] || { echo 'Run as agent.' >&2; exit 1; }
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin"
claude=$HOME/.local/bin/claude

echo '== context7 setup: Claude Code MCP server'
# Replace a stale entry; `mcp get` fails only when there is none.
if "$claude" mcp get context7 >/dev/null 2>&1; then
  "$claude" mcp remove --scope user context7
fi
"$claude" mcp add --scope user --transport http context7 https://mcp.context7.com/mcp
"$claude" mcp get context7
echo 'context7 setup complete.'
