"""TCP multiplexer: port 25565 → Minecraft, resource-pack HTTP, or Flask web UI."""

from __future__ import annotations

import socket
import ssl
import threading
from pathlib import Path
from urllib.parse import urlparse

from pack_server import _world_from_path

_HTTP_PREFIXES = (
    b'GET ', b'POST ', b'HEAD ', b'PUT ', b'DELETE ',
    b'OPTIONS ', b'PATCH ', b'CONNECT ', b'TRACE ',
)


def _is_http_peek(sock: socket.socket) -> bool:
    try:
        peek = sock.recv(16, socket.MSG_PEEK)
        return any(peek.startswith(p) for p in _HTTP_PREFIXES) or peek.startswith(b'PRI ')
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


def _inject_forwarded_for(buf: bytes, client_ip: str) -> bytes:
    if not client_ip:
        return buf
    head, sep, rest = buf.partition(b'\r\n')
    if not sep:
        return buf
    headers_block = rest.split(b'\r\n\r\n', 1)[0]
    if b'x-forwarded-for:' in headers_block.lower():
        return buf
    return head + b'\r\n' + f'X-Forwarded-For: {client_ip}\r\n'.encode() + rest


def _parse_http(buf: bytes) -> tuple[str, str]:
    line = buf.split(b'\r\n', 1)[0].decode('utf-8', errors='replace')
    parts = line.split()
    if len(parts) < 2:
        return '', ''
    return parts[0].upper(), parts[1]


def _relay_bidirectional(a: socket.socket, b: socket.socket) -> None:
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


def _http_backend_port(path: str, pack_port: int, flask_port: int) -> int:
    if _world_from_path(path):
        return pack_port
    # Absolute URI in request line
    if path.startswith('http://') or path.startswith('https://'):
        if _world_from_path(urlparse(path).path):
            return pack_port
    return flask_port


def _handle_client(
    client: socket.socket,
    client_addr: tuple[str, int],
    mc_host: str,
    mc_port: int,
    flask_host: str,
    flask_port: int,
    pack_port: int,
    ssl_context: ssl.SSLContext | None,
) -> None:
    try:
        client.settimeout(120)

        if ssl_context and _is_tls_peek(client):
            client = ssl_context.wrap_socket(client, server_side=True)
            client.settimeout(120)

        if _is_http_peek(client) or isinstance(client, ssl.SSLSocket):
            buf = _read_http_request(client)
            buf = _inject_forwarded_for(buf, client_addr[0])
            _method, path = _parse_http(buf)
            dest_port = _http_backend_port(path, pack_port, flask_port)
            backend = socket.create_connection((flask_host, dest_port), timeout=30)
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
    flask_host: str,
    flask_port: int,
    pack_port: int,
    ssl_context: ssl.SSLContext | None,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((public_host, public_port))
    listener.listen(256)
    while True:
        client, addr = listener.accept()
        threading.Thread(
            target=_handle_client,
            args=(client, addr, mc_host, mc_port, flask_host, flask_port, pack_port, ssl_context),
            daemon=True,
        ).start()


def start_port_proxy(
    public_port: int = 25565,
    mc_port: int = 25566,
    flask_host: str = '127.0.0.1',
    flask_port: int = 17891,
    pack_port: int = 17892,
    public_host: str = '0.0.0.0',
    ssl_context: ssl.SSLContext | None = None,
    worlds_dir: Path | None = None,
) -> threading.Thread:
    if worlds_dir is None:
        raise ValueError('worlds_dir is required')
    from pack_server import start_pack_server
    start_pack_server(worlds_dir, flask_host, pack_port)
    thread = threading.Thread(
        target=_listen_loop,
        args=(public_host, public_port, '127.0.0.1', mc_port, flask_host, flask_port, pack_port, ssl_context),
        daemon=True,
        name='port-proxy',
    )
    thread.start()
    return thread
