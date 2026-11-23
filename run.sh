#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

# Platform selection: explicit override > existing QT_QPA_PLATFORM > autodetect.
# Wayland sessions (e.g. KDE Plasma) prefer the native Wayland backend so IME
# (Mozc via fcitx5/ibus) works through Qt's text-input protocol. Pure X11 keeps
# xcb for free dragging and coordinate persistence.
if [[ -n "${SISTERS_MASCOT_PLATFORM:-}" ]]; then
    export QT_QPA_PLATFORM="$SISTERS_MASCOT_PLATFORM"
elif [[ -z "${QT_QPA_PLATFORM:-}" ]]; then
    if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
        export QT_QPA_PLATFORM="wayland"
    elif [[ -n "${DISPLAY:-}" ]]; then
        export QT_QPA_PLATFORM="xcb"
    fi
fi

# On native Wayland a leftover X11 QT_IM_MODULE (fcitx/ibus) breaks Japanese
# input; Qt handles IME via the text-input protocol instead. Set
# SISTERS_MASCOT_KEEP_IM_MODULE=1 to keep an explicitly chosen IM module.
if [[ "${QT_QPA_PLATFORM:-}" == wayland* && -z "${SISTERS_MASCOT_KEEP_IM_MODULE:-}" ]]; then
    unset QT_IM_MODULE
fi

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/.venv/bin/python" -m sisters_mascot.main "$@"
