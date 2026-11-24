# Stage 1: Build Go WhatsApp Bridge
FROM golang:bookworm AS go-builder
WORKDIR /build
COPY whatsapp-bridge/ ./
RUN go get go.mau.fi/whatsmeow@latest && go mod tidy && CGO_ENABLED=1 go build -o whatsapp-bridge main.go


# Stage 2: Runtime with Python + uv
FROM python:3.11-slim-bookworm
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN mkdir -p /app/whatsapp-bridge/store
COPY --from=go-builder /build/whatsapp-bridge /app/whatsapp-bridge/whatsapp-bridge

COPY whatsapp-mcp-server/ /app/whatsapp-mcp-server/
WORKDIR /app/whatsapp-mcp-server
RUN uv sync && .venv/bin/python -c "from mcp.server.fastmcp import FastMCP; import mcp, importlib.metadata as m; print('mcp', m.version('mcp'))"

WORKDIR /app
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
