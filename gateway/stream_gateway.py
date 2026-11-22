#!/usr/bin/env python3
"""Stable playback gateway for Jellyfin `.strm` files.

The public URL is content-keyed:

    /play/movie/{tmdb}
    /play/tv/{tmdb}/{season}/{episode}

It does not name a provider. It asks the configured source services in order
and returns the first playable HLS manifest. A source can be replaced or
reimplemented behind its service URL without changing any `.strm` files.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_PORT = 8790
USER_AGENT = "framex-gateway/1.0"
MEDIA_PATH_RE = re.compile(r"^/play/(?:movie/\d+|tv/\d+/\d+/\d+)/?$")
SOURCES = (
    ("framex", os.environ.get("FRAMEX_LIBRARY_URL", "http://framex-library:8789")),
    ("cinecat", os.environ.get("CINECAT_LIBRARY_URL", "http://cinecat-library:8788")),
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


class Gateway:
    def __init__(self, sources):
        self.sources = [
            (name, base.rstrip("/"))
            for name, base in sources
            if base and base.rstrip("/")
        ]

    def resolve(self, path: str):
        """Return (status, body, source) for the first usable source."""
        headers = {
            "Accept": "application/vnd.apple.mpegurl,*/*",
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
        }
        failures = []
        for name, base in self.sources:
            try:
                with urlopen(
                    Request(base + path, headers=headers), timeout=30
                ) as response:
                    body = response.read()
                    if response.status == 200 and body.lstrip().startswith(b"#EXTM3U"):
                        return 200, body, name
                    failures.append(f"{name}: HTTP {response.status}")
            except HTTPError as exc:
                failures.append(f"{name}: HTTP {exc.code}")
            except (URLError, OSError, ValueError) as exc:
                failures.append(f"{name}: {type(exc).__name__}")
        log(f"no source for {path}: {'; '.join(failures) or 'none configured'}")
        return 404, b"", ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StreamGateway/1.0"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle(head_only=True)

    def _send_bytes(self, status: int, body: bytes, head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, head_only=(self.command == "HEAD"))

    def _handle(self, head_only: bool = False) -> None:
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True})
        if not MEDIA_PATH_RE.match(path):
            return self._json(404, {"error": "not found"})
        status, body, source = self.server.app.resolve(path)
        if status != 200:
            return self._json(404, {"error": "no playable source"})
        log(f"{path} -> {source}")
        self._send_bytes(200, body, head_only=head_only)


class GatewayServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, app: Gateway):
        self.app = app
        super().__init__(address, Handler)


def main() -> int:
    port = int(os.environ.get("GATEWAY_PORT", DEFAULT_PORT))
    app = Gateway(SOURCES)
    if not app.sources:
        log("no source services configured")
    server = GatewayServer(("0.0.0.0", port), app)
    print(f"stream gateway on :{port}", flush=True)
    print(f"  sources: {', '.join(name for name, _ in app.sources)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
