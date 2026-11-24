# Stage 1: Build Go WhatsApp Bridge
# Newer whatsmeow needs a recent Go; GOTOOLCHAIN=auto lets Go fetch a newer toolchain if required.
FROM golang:1.25-bookworm AS go-builder
ENV GOTOOLCHAIN=auto
WORKDIR /build
COPY whatsapp-bridge/ ./
# Change CACHEBUST (or build with --no-cache) to force a fresh whatsmeow download.
# WhatsApp rejects old clients with "Client outdated (405)".
ARG CACHEBUST=1
RUN go get go.mau.fi/whatsmeow@latest && go mod tidy && CGO_ENABLED=1 go build -o whatsapp-bridge main.go

# Stage 2: Runtime with Python + uv
FROM python:3.11-slim-bookworm
WORKDIR /app

# Install runtime dependencies (sqlite3, ffmpeg for voice notes, curl for uv)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy Go binary and create store directory (mount a persistent volume here on Northflank)
RUN mkdir -p /app/whatsapp-bridge/store
COPY --from=go-builder /build/whatsapp-bridge /app/whatsapp-bridge/whatsapp-bridge

# Copy Python MCP server and install dependencies
COPY whatsapp-mcp-server/ /app/whatsapp-mcp-server/
WORKDIR /app/whatsapp-mcp-server
RUN uv sync --frozen

# Fail the build early if a stale/broken main.py is being built
# (mcp.run() does not accept host/port; they must be set via mcp.settings)
RUN ! grep -q 'mcp.run(.*host=' main.py \
    || (echo "ERROR: main.py still calls mcp.run(host=...). Commit the fixed main.py." && exit 1)
RUN python -c "import ast,sys; ast.parse(open('main.py').read())"

# Copy startup script (strip Windows CRLF line endings, which break #!/bin/sh)
WORKDIR /app
COPY start.sh /app/start.sh
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
