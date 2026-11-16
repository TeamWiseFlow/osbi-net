#!/usr/bin/env bash
# Stable entry point for the independent local client backend.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$PROJECT_ROOT/client/scripts/run.sh" "$@"
