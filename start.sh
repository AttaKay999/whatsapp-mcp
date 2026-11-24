#!/bin/sh
set -e

# Start the Go WhatsApp bridge in the background
cd /app/whatsapp-bridge
./whatsapp-bridge &
BRIDGE_PID=$!

# Wait briefly for the bridge to initialize
sleep 3

if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
  echo "WARNING: WhatsApp bridge exited during startup. Check the bridge logs above." >&2
fi

# Start the Python MCP server on port 8000
cd /app/whatsapp-mcp-server
exec uv run main.py
