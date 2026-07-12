#!/usr/bin/env bash
# Mac / Linux setup. Run once:  bash setup.sh
set -e
cd "$(dirname "$0")"

echo "Creating a private Python environment (.venv)..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
echo "Installing dependencies (this can take a couple minutes)..."
.venv/bin/pip install -r requirements.txt

echo ""
echo "Let's configure JARVIS..."
.venv/bin/python configure.py

echo ""
echo "Setup done."
echo "   1) Start JARVIS:  bash run.sh"
echo "   2) Open:          http://localhost:8800   (use Chrome)"
