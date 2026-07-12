#!/usr/bin/env bash
# Reach JARVIS from your phone, anywhere, over your private Tailscale network.
# Do this AFTER JARVIS is running (run.sh) and Tailscale is installed + signed in
# on BOTH this computer and your phone (same account).
PORT="${JARVIS_PORT:-8800}"
TS="tailscale"
if ! command -v "$TS" >/dev/null 2>&1; then
  if [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]; then
    TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  else
    echo "❌ Tailscale isn't installed. Get it at https://tailscale.com/download, sign in, then run this again."
    exit 1
  fi
fi
echo "→ Publishing http://localhost:$PORT to your private Tailscale network…"
"$TS" serve --bg --https=443 "http://127.0.0.1:$PORT"
echo ""
echo "✅ Live. Open this on your phone (Chrome, same Tailscale account):"
"$TS" serve status 2>/dev/null | grep -i https || echo "  (run: $TS serve status  to see your https://….ts.net link)"
echo "To stop sharing later:  $TS serve --https=443 off"
