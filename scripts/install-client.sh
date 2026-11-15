#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$PROJECT_ROOT/.venv"
"$PROJECT_ROOT/.venv/bin/pip" install -c "$PROJECT_ROOT/requirements.lock" -e "${PROJECT_ROOT}[client]"
npm ci --ignore-scripts --prefix "$PROJECT_ROOT/client/browser"
npm run build --prefix "$PROJECT_ROOT/client/browser"
node "$PROJECT_ROOT/client/browser/scripts/install-assets.mjs"
printf '%s\n' "Client installed. Start: $PROJECT_ROOT/.venv/bin/osbi-client serve"
