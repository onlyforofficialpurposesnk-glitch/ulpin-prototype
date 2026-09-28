#!/usr/bin/env bash
# scripts/reset_demo.sh – One-command demo reset and pipeline rebuild
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

# Activate virtualenv if present
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Run the cross-platform reset script
python3 scripts/reset_demo.py "$@"
