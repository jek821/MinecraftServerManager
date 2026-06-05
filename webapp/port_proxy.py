"""TCP multiplexer: route port 25565 to Minecraft (internal) or HTTP handlers.

Resource pack downloads are served directly (avoids proxy truncation that causes
Minecraft's "Premature EOF"). Other HTTP traffic is forwarded to Flask.
"""

from __future__ import annotations

import re
import socket
import ssl
import threading
from pathlib import Path

_HTTP_PREFIXES = (
    b'GET ', b'POST ', b'HEAD ', b'PUT ', b'DELETE ',
    b'OPTIONS ', b'PATCH ', b'CONNECT ', b'TRACE ',
)

_PACK_PATH = re.compile(r'^/resourcepack/([A-Za-z0-9_\-]+)\.zip$')


def _is_http_peek(sock: socket.socket) -> bool:
    try:
        peek = sock.recv(8, socket.MSG_PEEK)
        return any(peek.startswith(p) for p in _HTTP_PREFIXES)
    except OSError:
        return False


def _is_tls_peek(sock: socket.socket) -> bool:
    try:
        peek = sock.recv(3, socket.MSG_PEEK)
        return len(peek) >= 3 and peek[0] == 0x16 and peek[1] == 0x03
    except OSError:
        return False


def _read_http_request(sock: socket.socket) -> bytes:
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = sock.recv(8192)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    return buf


def _parse_http(buf: bytes) -> tuple[str, str]:
    line = buf.split(b'\r\n', 1)[0].decode('utf-8', errors='replace')
    parts = line.split()
    if len(parts) < 2:
        return '', ''
    return parts[0].upper(), parts[1]


def _serve_resource_pack(sock: socket.socket, worlds_dir: Path, path: str, method: str) -> bool:
    m = _PACK_PATH.match(path)
    if not m:
        return False
    world_name = m.group(1)
    pack = worlds_dir / world_name / '.resource_pack.zip'
    if not pack.is_file():
        sock.sendall(b'HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
        return True
    data = pack.read_bytes()
    header = (
        'HTTP/1.1 200 OK\r\n'
        'Content-Type: application/zip\r\n'
        f'Content-Length: {len(data)}\r\n'
        f'Content-Disposition: attachment; filename="{world_name}_paintings.zip"\r\n'
        'Connection: close\r\n'
        '\r\n'
    ).encode()
    sock.sendall(header)
    if method != 'HEAD':
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            sent = sock.send(view[offset:offset + 262144])
            if sent == 0:
                break
            offset += sent
    return True


def _relay_bidirectional(a: socket.socket, b: socket.socket) -> None:
    """Threaded relay — more reliable than single-thread select for large downloads."""

    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=300)
    t2.join(timeout=300)
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


def _handle_client(
    client: socket.socket,
    mc_host: str,
    mc_port: int,
    http_host: str,
    http_port: int,
    ssl_context: ssl.SSLContext | None,
    worlds_dir: Path,
) -> None:
    try:
        client.settimeout(120)

        if ssl_context and _is_tls_peek(client):
            client = ssl_context.wrap_socket(client, server_side=True)
            client.settimeout(120)

        if _is_http_peek(client) or isinstance(client, ssl.SSLSocket):
            buf = _read_http_request(client)
            method, path = _parse_http(buf)
            if method in ('GET', 'HEAD') and _serve_resource_pack(client, worlds_dir, path, method):
                try:
                    client.close()
                except OSError:
                    pass
                return
            backend = socket.create_connection((http_host, http_port), timeout=30)
            backend.sendall(buf)
            _relay_bidirectional(client, backend)
            return

        backend = socket.create_connection((mc_host, mc_port), timeout=30)
        _relay_bidirectional(client, backend)
    except OSError:
        try:
            client.close()
        except OSError:
            pass


def _listen_loop(
    public_host: str,
    public_port: int,
    mc_host: str,
    mc_port: int,
    http_host: str,
    http_port: int,
    ssl_context: ssl.SSLContext | None,
    worlds_dir: Path,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((public_host, public_port))
    listener.listen(256)
    while True:
        client, _addr = listener.accept()
        threading.Thread(
            target=_handle_client,
            args=(client, mc_host, mc_port, http_host, http_port, ssl_context, worlds_dir),
            daemon=True,
        ).start()


def start_port_proxy(
    public_port: int = 25565,
    mc_port: int = 25566,
    http_host: str = '127.0.0.1',
    http_port: int = 5000,
    public_host: str = '0.0.0.0',
    ssl_context: ssl.SSLContext | None = None,
    worlds_dir: Path | None = None,
) -> threading.Thread:
    if worlds_dir is None:
        raise ValueError('worlds_dir is required')
    thread = threading.Thread(
        target=_listen_loop,
        args=(public_host, public_port, '127.0.0.1', mc_port, http_host, http_port, ssl_context, worlds_dir),
        daemon=True,
        name='port-proxy',
    )
    thread.start()
    return thread
