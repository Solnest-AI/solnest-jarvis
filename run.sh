#!/usr/bin/env bash
# Mac / Linux: start JARVIS.  bash run.sh
cd "$(dirname "$0")"
if [ ! -d .venv ]; then echo "Run 'bash setup.sh' first."; exit 1; fi
.venv/bin/python server.py
