#!/usr/bin/env python3
"""Production entrypoint for vu-handbook-mcp over Streamable HTTP.

Wraps the unchanged FastMCP server (../mcp_server.py) with:
  * bearer-token auth on every request except GET /healthz
  * per-client-IP token-bucket rate limiting
  * DNS-rebinding protection limited to MCP_PUBLIC_HOST

Environment (see deploy/.env.example):
  MCP_BEARER_TOKENS      comma-separated accepted tokens (required unless MCP_ALLOW_ANONYMOUS=1)
  MCP_ALLOW_ANONYMOUS    "1" to serve without a token (public data; rate limit still applies)
  MCP_PUBLIC_HOST        public hostname, e.g. vumcp.example.com (added to allowed Host headers)
  MCP_ALLOWED_ORIGINS    optional comma-separated browser origins
  MCP_BIND / MCP_PORT    bind address/port (default 127.0.0.1:8000)
  MCP_RATE_PER_MIN       sustained requests per minute per IP (default 60)
  MCP_RATE_BURST         burst size (default 30)
  MCP_TRUSTED_PROXIES    proxies whose X-Forwarded-For is trusted (default 127.0.0.1)
  MCP_STATELESS          "1" for stateless HTTP sessions
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402  (installed with mcp)
from mcp.server.streamable_http import TransportSecuritySettings  # noqa: E402

import mcp_server  # noqa: E402


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


TOKENS = [t.encode() for t in _csv("MCP_BEARER_TOKENS")]
ANON = os.environ.get("MCP_ALLOW_ANONYMOUS") == "1"
PUBLIC_HOST = os.environ.get("MCP_PUBLIC_HOST", "").strip()
RATE = float(os.environ.get("MCP_RATE_PER_MIN", "60")) / 60.0
BURST = float(os.environ.get("MCP_RATE_BURST", "30"))
OPEN_PATHS = {"/healthz"}


async def _send_json(send, status: int, body: dict, extra: list | None = None) -> None:
    data = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": data})


class Guard:
    """ASGI middleware: rate limit, then bearer auth."""

    def __init__(self, app):
        self.app = app
        self.buckets: dict[str, tuple[float, float]] = {}

    def _allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self.buckets.get(key, (BURST, now))
        tokens = min(BURST, tokens + (now - last) * RATE)
        if tokens < 1:
            self.buckets[key] = (tokens, now)
            return False
        self.buckets[key] = (tokens - 1, now)
        if len(self.buckets) > 10_000:  # bound memory
            self.buckets.clear()
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in OPEN_PATHS:
            return await self.app(scope, receive, send)
        client = (scope.get("client") or ("?", 0))[0]
        if not self._allow(client):
            return await _send_json(send, 429, {"error": "rate_limited"}, [(b"retry-after", b"10")])
        if not ANON:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            token = auth[7:].strip() if auth[:7].lower() == b"bearer " else b""
            if not token or not any(hmac.compare_digest(token, t) for t in TOKENS):
                return await _send_json(send, 401, {"error": "unauthorized"},
                                        [(b"www-authenticate", b'Bearer realm="vu-handbook-mcp"')])
        return await self.app(scope, receive, send)


def build_app():
    mcp = mcp_server.mcp
    sec = mcp.settings.transport_security
    hosts = list(sec.allowed_hosts)
    origins = list(sec.allowed_origins) + _csv("MCP_ALLOWED_ORIGINS")
    if PUBLIC_HOST:
        hosts += [PUBLIC_HOST, f"{PUBLIC_HOST}:*"]
        origins += [f"https://{PUBLIC_HOST}"]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(hosts)),
        allowed_origins=list(dict.fromkeys(origins)),
    )
    mcp.settings.stateless_http = os.environ.get("MCP_STATELESS") == "1"
    return Guard(mcp.streamable_http_app())


def main() -> None:
    if not TOKENS and not ANON:
        sys.exit("refusing to start: set MCP_BEARER_TOKENS (or MCP_ALLOW_ANONYMOUS=1)")
    uvicorn.run(
        build_app(),
        host=os.environ.get("MCP_BIND", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("MCP_TRUSTED_PROXIES", "127.0.0.1"),
        server_header=False,
        log_level=os.environ.get("MCP_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
