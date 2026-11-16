#!/usr/bin/env bash
# Stable entry point for the client-owned installer.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$PROJECT_ROOT/client/scripts/install.sh" "$@"
