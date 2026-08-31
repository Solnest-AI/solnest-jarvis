#!/usr/bin/env bash
# setup-keys.sh — reads your .env and wires up your keys.
#
# Your keys go from the .env file straight into the tool. They are never printed,
# never sent to the AI, and never written into a chat. This script only ever
# reports OK or MISSING.
set -uo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "❌ No .env file found in this folder."
  echo "   Copy .env.template to .env, paste your keys into it, and run this again."
  exit 1
fi

# Load the .env WITHOUT printing anything.
set -a; . ./.env; set +a

missing=0
found=0
need() {  # need VAR "Human name"
  if [ -z "${!1:-}" ]; then echo "  ⬜ $2 — blank in .env ($1), skipping"; missing=$((missing+1))
  else echo "  ✅ $2"; found=$((found+1)); fi
}

echo "Checking your .env..."

need ANTHROPIC_API_KEY "JARVIS's brain (required)"
need ELEVENLABS_API_KEY "JARVIS's premium voice (optional)"

# The premium voice needs BOTH the key and a voice id. A blank voice id turns
# TTS off silently (see jarvis/voice_tts.py), so call that out here.
if [ -n "${ELEVENLABS_API_KEY:-}" ] && [ -z "${ELEVENLABS_VOICE_ID:-}" ]; then
  echo "  ⚠️  ELEVENLABS_VOICE_ID is blank, so the premium voice stays OFF."
  echo "     Copy the ELEVENLABS_VOICE_ID line from .env.template into your .env."
fi

if [ "$found" -eq 0 ]; then
  echo
  echo "❌ Nothing is filled in yet. Open the .env file, paste your key(s), save, and run this again."
  echo "   (See KEYS.md for where to get each one.)"
  exit 1
fi
echo

echo; echo "Done. Now start JARVIS:  bash run.sh"
echo "(He reads .env on startup, so restart him after any change to it.)"
