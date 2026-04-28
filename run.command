#!/usr/bin/env bash
# Dev-mode launcher: start the tray app from source (no PyInstaller build).
# Useful for hacking on the code. Coworkers should use the prebuilt .app.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

exec ./.venv/bin/python whip_app.py
