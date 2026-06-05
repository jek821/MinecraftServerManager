"""TCP multiplexer: route port 25565 to Minecraft (internal) or Flask (HTTP/HTTPS).

Players connect to :25565 for the game. Minecraft clients download resource packs
over HTTP(S) on the same host:port, so those requests are forwarded to Flask while
game traffic goes to the internal MC server port.
"""

from __future__ import annotations

import select
import socket
import ssl
import threading

_HTTP_PREFIXES = (
    b'GET ', b'POST ', b'HEAD ', b'PUT ', b'DELETE ',
    b'OPTIONS ', b'PATCH ', b'CONNECT ', b'TRACE ',
)


def _is_http(sock: socket.socket) -> bool:
    try:
        peek = sock.recv(8, socket.MSG_PEEK)
        return any(peek.startswith(p) for p in _HTTP_PREFIXES)
    except OSError:
        return False


def _is_tls(sock: socket.socket) -> bool:
    try:
        peek = sock.recv(3, socket.MSG_PEEK)
        return len(peek) >= 3 and peek[0] == 0x16 and peek[1] == 0x03
    except OSError:
        return False


def _relay(client: socket.socket, backend: socket.socket) -> None:
    sockets = [client, backend]
    client.setblocking(False)
    backend.setblocking(False)
    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 120)
            if exceptional:
                break
            if not readable:
                break
            for src in readable:
                dst = backend if src is client else client
                try:
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        for s in sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


def _handle_client(
    client: socket.socket,
    mc_host: str,
    mc_port: int,
    http_host: str,
    http_port: int,
    ssl_context: ssl.SSLContext | None,
) -> None:
    try:
        if ssl_context and _is_tls(client):
            tls_client = ssl_context.wrap_socket(client, server_side=True)
            backend = socket.create_connection((http_host, http_port), timeout=15)
            _relay(tls_client, backend)
        elif _is_http(client):
            backend = socket.create_connection((http_host, http_port), timeout=15)
            _relay(client, backend)
        else:
            backend = socket.create_connection((mc_host, mc_port), timeout=15)
            _relay(client, backend)
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
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((public_host, public_port))
    listener.listen(256)
    while True:
        client, _addr = listener.accept()
        threading.Thread(
            target=_handle_client,
            args=(client, mc_host, mc_port, http_host, http_port, ssl_context),
            daemon=True,
        ).start()


def start_port_proxy(
    public_port: int = 25565,
    mc_port: int = 25566,
    http_host: str = '127.0.0.1',
    http_port: int = 5000,
    public_host: str = '0.0.0.0',
    ssl_context: ssl.SSLContext | None = None,
) -> threading.Thread:
    thread = threading.Thread(
        target=_listen_loop,
        args=(public_host, public_port, '127.0.0.1', mc_port, http_host, http_port, ssl_context),
        daemon=True,
        name='port-proxy',
    )
    thread.start()
    return thread
