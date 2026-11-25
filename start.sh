#!/bin/sh
# Run the Go WhatsApp bridge in a restart loop so a fresh pairing QR code is
# printed every few minutes until the device is linked, and so the bridge
# recovers if it ever exits.
cd /app/whatsapp-bridge
(
  while true; do
    echo "[start.sh] starting WhatsApp bridge..."
    ./whatsapp-bridge
    echo "[start.sh] bridge exited (code $?), restarting in 5s..." >&2
    sleep 5
  done
) &

# Give the bridge a moment, then start the Python MCP server on port 8000
sleep 3
cd /app/whatsapp-mcp-server
exec /app/whatsapp-mcp-server/.venv/bin/python main.py
