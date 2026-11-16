#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$PROJECT_ROOT/.osbi/bin:$PROJECT_ROOT/.venv/bin:$PATH"
if [[ $# -eq 0 ]]; then set -- serve; fi
exec "$PROJECT_ROOT/.venv/bin/osbi-client" "$@"
