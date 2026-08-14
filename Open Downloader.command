#!/usr/bin/env bash
# Double-click this file in Finder to open the downloader in your browser.
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

if [[ ! -x .venv/bin/python ]]; then
  echo "First-time setup..."
  ./setup.sh || { echo; echo "Setup failed. Press any key to close."; read -r -n 1; exit 1; }
fi

exec ./.venv/bin/python -m grabber --web
