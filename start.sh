#!/bin/sh
set -e

# Start the Go WhatsApp bridge in the background
cd /app/whatsapp-bridge
./whatsapp-bridge &

# Wait briefly for the bridge to initialize
sleep 3

# Start the Python MCP server on port 8000
cd /app/whatsapp-mcp-server
exec uv run main.py
