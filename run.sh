#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

if [[ -n "${SISTERS_MASCOT_PLATFORM:-}" ]]; then
    export QT_QPA_PLATFORM="$SISTERS_MASCOT_PLATFORM"
elif [[ -z "${QT_QPA_PLATFORM:-}" && -n "${DISPLAY:-}" ]]; then
    export QT_QPA_PLATFORM="xcb"
fi

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/.venv/bin/python" -m sisters_mascot.main "$@"
