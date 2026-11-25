"""Share downloaded WhatsApp media over short-lived, unguessable HTTP links.

Media is downloaded onto the server's disk. This lets an MCP client (e.g. OpenCode)
fetch the bytes with a plain HTTP GET and save them on its own machine.
"""
import mimetypes
import os
import re
import secrets
import time
from typing import Any, Dict, Tuple

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse

TTL_SECONDS = 20 * 60
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://p01--harries-mcp--w8b4bhgbgzmd.code.run"
).rstrip("/")

# token -> (absolute path, filename, mime, expires_at)
_SHARES: Dict[str, Tuple[str, str, str, float]] = {}


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"


def _purge() -> None:
    now = time.time()
    for token in [t for t, v in _SHARES.items() if v[3] < now]:
        _SHARES.pop(token, None)


def share(path: str) -> Dict[str, Any]:
    """Register a server-side file and return a temporary download link for it."""
    _purge()
    filename = _safe_name(os.path.basename(path))
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    token = secrets.token_urlsafe(24)
    _SHARES[token] = (os.path.abspath(path), filename, mime, time.time() + TTL_SECONDS)
    url = f"{PUBLIC_BASE_URL}/media/{token}/{filename}"
    return {
        "url": url,
        "filename": filename,
        "mime_type": mime,
        "size_bytes": os.path.getsize(path),
        "expires_in_minutes": TTL_SECONDS // 60,
        "download_with": f'curl.exe -L -o "{filename}" "{url}"',
    }


async def serve_media(request: Request):
    """GET /media/{token}/{filename}"""
    _purge()
    entry = _SHARES.get(request.path_params.get("token", ""))
    if not entry or not os.path.isfile(entry[0]):
        return PlainTextResponse("Link expired or not found", status_code=404)
    path, filename, mime, _ = entry
    return FileResponse(path, media_type=mime, filename=filename)
