#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$PROJECT_ROOT/.venv/bin:$PATH"
exec npx --yes @deepseek-ai/dsh@0.2.2-rc.3 --profile web --patch "$PROJECT_ROOT/client/deepseek-harness/cordis.patch.yml" "$@"
