#!/usr/bin/env bash
# Create the virtualenv and install dependencies. Safe to re-run; also upgrades
# yt-dlp, which you should do whenever downloads suddenly start failing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi

if [[ -z "$PYTHON" ]]; then
  echo "No python3 found. Install it with: brew install python" >&2
  exit 1
fi

echo "==> Using $PYTHON ($("$PYTHON" --version))"

if [[ ! -d .venv ]]; then
  echo "==> Creating .venv"
  "$PYTHON" -m venv .venv
fi

echo "==> Installing/upgrading dependencies"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet --upgrade -r requirements.txt

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "==> ffmpeg is missing (needed to merge video and audio)."
  if command -v brew >/dev/null 2>&1; then
    echo "    Installing via Homebrew..."
    brew install ffmpeg
  else
    echo "    Install it manually: https://ffmpeg.org/download.html" >&2
  fi
fi

chmod +x "$HERE/grab"

echo
echo "==> Done. Try:"
echo "    ./grab --doctor"
echo "    ./grab --web"
