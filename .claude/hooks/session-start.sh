#!/bin/bash
# SessionStart hook for Claude Code on the web: installs the landing site's
# dependencies so tests, typecheck, lint, build and a headless browser check
# work in a fresh cloud session. Local sessions are left alone.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR/landing"

# `npm install` rather than `npm ci`: it reuses the node_modules cached with the
# container instead of deleting it on every start. It honours the lockfile.
npm install --no-audit --no-fund --loglevel=error

# The container ships Playwright globally with Chromium in /opt/pw-browsers;
# exposing the global root lets a CommonJS script `require('playwright')`
# without adding it to package.json.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export NODE_PATH=\"$(npm root -g)\"" >> "$CLAUDE_ENV_FILE"
  echo 'export NEXT_TELEMETRY_DISABLED=1' >> "$CLAUDE_ENV_FILE"
fi
