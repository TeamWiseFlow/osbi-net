#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
agent=none
dsh_home="${DSH_HOME:-$HOME/.dsh}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) [[ $# -ge 2 ]] || { printf 'Missing --agent value.\n' >&2; exit 2; }; agent="$2"; shift 2 ;;
    --dsh-home) [[ $# -ge 2 ]] || { printf 'Missing --dsh-home value.\n' >&2; exit 2; }; dsh_home="$2"; shift 2 ;;
    --help|-h) printf '%s\n' 'Usage: bash client/scripts/install.sh [--agent none|dsh] [--dsh-home PATH]' 'Installs backend, browser and assets; reuses or provisions Python/Node.' 'The optional dsh step installs OSBI skills, not the DSH host.'; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "$agent" == none || "$agent" == dsh ]] || { printf 'Only the dsh adapter is currently available.\n' >&2; exit 2; }
source "$PROJECT_ROOT/client/scripts/runtime.sh"
platform="$(osbi_platform)"
mkdir -p "$PROJECT_ROOT/.osbi/runtime" "$PROJECT_ROOT/.osbi/bin"
mkdir "$PROJECT_ROOT/.osbi/install.lock" 2>/dev/null || { printf 'Client installation is already running; inspect .osbi/install.lock if interrupted.\n' >&2; exit 1; }
trap 'rmdir "$PROJECT_ROOT/.osbi/install.lock"' EXIT
osbi_prepare_node "$PROJECT_ROOT" "$platform"
osbi_prepare_python "$PROJECT_ROOT" "$platform"
if [[ ! -e "$PROJECT_ROOT/.venv" ]]; then "$OSBI_INSTALL_PYTHON" -m venv "$PROJECT_ROOT/.venv"; fi
"$PROJECT_ROOT/.venv/bin/python" -m pip install -c "$PROJECT_ROOT/requirements.lock" -e "${PROJECT_ROOT}[client]"
npm ci --ignore-scripts --prefix "$PROJECT_ROOT/client/backend/browser"
npm run build --prefix "$PROJECT_ROOT/client/backend/browser"
node "$PROJECT_ROOT/client/backend/browser/scripts/install-assets.mjs"
if [[ "$agent" == dsh ]]; then "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/client/scripts/deepseek-harness/install.py" --home "$dsh_home"; fi
printf 'Client and browser installed. Start: bash "%s/client/scripts/run.sh"\n' "$PROJECT_ROOT"
