# Stage 1: Build Go WhatsApp Bridge
FROM golang:1.24-bookworm AS go-builder
WORKDIR /build
COPY whatsapp-bridge/ ./
RUN CGO_ENABLED=1 go build -o whatsapp-bridge main.go

# Stage 2: Runtime with Python + uv
FROM python:3.11-slim-bookworm
WORKDIR /app

# Install runtime dependencies (sqlite3, ffmpeg for voice notes, curl for uv)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy Go binary and create store directory
RUN mkdir -p /app/whatsapp-bridge/store
COPY --from=go-builder /build/whatsapp-bridge /app/whatsapp-bridge/whatsapp-bridge

# Copy Python MCP server and install dependencies
COPY whatsapp-mcp-server/ /app/whatsapp-mcp-server/
WORKDIR /app/whatsapp-mcp-server
RUN uv sync --frozen

# Copy startup script
WORKDIR /app
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
