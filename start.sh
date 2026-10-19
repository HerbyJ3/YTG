#!/usr/bin/env bash
# macOS / Linux launcher
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up... this takes a minute."
  python3 -m venv .venv || { echo "Python 3.9+ is required."; exit 1; }
fi
.venv/bin/python -m pip install -q --upgrade -r requirements.txt
exec .venv/bin/python app.py "$@"
