#!/usr/bin/env bash
# Starts a cloudflared quick tunnel pointed at the local backend
# (http://localhost:8000) and prints the generated
# https://*.trycloudflare.com URL, formatted for pasting into Retell's
# webhook config.
#
# This only starts the tunnel and shows you the URL. Pasting that URL into
# the Retell dashboard's webhook field is a manual step you still have to
# do yourself -- that requires your own Retell account login, which no
# script can do on your behalf.
#
# Keeps running in the foreground so the tunnel stays alive for your dev
# session. Press Ctrl+C to stop it (this also stops the underlying
# cloudflared process, so you don't end up with an orphaned tunnel).
#
# Requires cloudflared to be installed and on PATH. See:
# https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

set -uo pipefail

BACKEND_URL="http://localhost:8000"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-cloudflared}"
LOG_FILE="/tmp/ai_calling_agent_tunnel_$$.log"

if ! command -v "$CLOUDFLARED_BIN" >/dev/null 2>&1; then
  echo "Couldn't find '$CLOUDFLARED_BIN' on PATH. Is cloudflared installed?" >&2
  echo "See: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" >&2
  exit 1
fi

echo "Starting cloudflared tunnel -> $BACKEND_URL ..."

"$CLOUDFLARED_BIN" tunnel --url "$BACKEND_URL" > "$LOG_FILE" 2>&1 &
PID=$!

CLEANED_UP=0
cleanup() {
  if [ "$CLEANED_UP" -eq 1 ]; then
    return
  fi
  CLEANED_UP=1
  if kill -0 "$PID" 2>/dev/null; then
    echo ""
    echo "Stopping tunnel (PID $PID)..."
    kill "$PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

TUNNEL_URL=""
TIMEOUT=30
ELAPSED=0
while [ -z "$TUNNEL_URL" ] && [ "$ELAPSED" -lt "$TIMEOUT" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "cloudflared exited unexpectedly. Check log: $LOG_FILE" >&2
    exit 1
  fi
  TUNNEL_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -n1 || true)
  sleep 1
  ELAPSED=$((ELAPSED + 1))
done

if [ -z "$TUNNEL_URL" ]; then
  echo "Timed out after ${TIMEOUT}s waiting for cloudflared to print a tunnel URL. Check log: $LOG_FILE" >&2
  exit 1
fi

WEBHOOK_URL="${TUNNEL_URL}/events"

echo ""
echo "============================================"
echo "Webhook tunnel is up. Paste this into the"
echo "Retell dashboard's webhook URL field:"
echo ""
echo "$WEBHOOK_URL"
echo "============================================"
echo ""
echo "NOTE: pasting this URL into the Retell dashboard is a manual step -- this"
echo "script cannot do it for you (that requires your own Retell account login)."
echo "A fresh URL is generated every time this script (re)starts, so re-paste"
echo "it into the dashboard whenever you restart the tunnel."
echo ""
echo "Tunnel is running in the foreground (PID $PID). Press Ctrl+C to stop it."
echo ""

wait "$PID"