"""Minimal HTTP server for /resourcepack/<world>.zip — uses stdlib for reliable downloads."""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_PACK_PATH = re.compile(r'^/resourcepack/([A-Za-z0-9_\-]+)\.zip$')


def _world_from_path(path: str) -> str | None:
    # Handle absolute URI: GET http://host:port/resourcepack/x.zip
    if path.startswith('http://') or path.startswith('https://'):
        path = urlparse(path).path
    path = path.split('?', 1)[0]
    m = _PACK_PATH.match(path)
    return m.group(1) if m else None


def _make_handler(worlds_dir: Path) -> type[BaseHTTPRequestHandler]:
    class PackHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args) -> None:
            pass

        def _send_pack(self, send_body: bool) -> None:
            world = _world_from_path(self.path)
            if not world:
                self.send_error(404)
                return
            pack = worlds_dir / world / '.resource_pack.zip'
            if not pack.is_file():
                self.send_error(404)
                return
            data = pack.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition', f'attachment; filename="{world}_paintings.zip"')
            self.send_header('Connection', 'close')
            self.end_headers()
            if send_body:
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._send_pack(send_body=True)

        def do_HEAD(self) -> None:
            self._send_pack(send_body=False)

    return PackHandler


def start_pack_server(worlds_dir: Path, host: str = '127.0.0.1', port: int = 17892) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _make_handler(worlds_dir))
    threading.Thread(target=server.serve_forever, daemon=True, name='pack-server').start()
    return server
