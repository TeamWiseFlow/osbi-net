#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export PATH="$PROJECT_ROOT/.osbi/bin:$PROJECT_ROOT/.venv/bin:$PATH"
exec npx --yes @deepseek-ai/dsh@0.2.2-rc.5 --profile web --patch "$PROJECT_ROOT/client/scripts/deepseek-harness/cordis.patch.yml" "$@"
