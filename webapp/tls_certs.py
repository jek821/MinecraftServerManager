"""Self-signed TLS certificate for HTTPS resource-pack downloads on the public port."""

from __future__ import annotations

import ipaddress
import json
import ssl
import subprocess
from pathlib import Path


def _san_for_host(host: str) -> str:
    try:
        ipaddress.ip_address(host)
        return f'IP:{host}'
    except ValueError:
        return f'DNS:{host}'


def ensure_tls_context(cert_dir: Path, host: str) -> ssl.SSLContext | None:
    host = host.strip()
    if not host:
        return None

    cert_dir.mkdir(parents=True, exist_ok=True)
    meta_path = cert_dir / 'meta.json'
    cert_path = cert_dir / 'server.crt'
    key_path = cert_dir / 'server.key'
    san = _san_for_host(host)

    need_gen = True
    if meta_path.exists() and cert_path.exists() and key_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            need_gen = meta.get('san') != san
        except Exception:
            need_gen = True

    if need_gen:
        subprocess.run(
            [
                'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                '-keyout', str(key_path),
                '-out', str(cert_path),
                '-days', '825',
                '-nodes',
                '-subj', '/CN=minecraft-resource-pack',
                '-addext', f'subjectAltName={san}',
            ],
            check=True,
            capture_output=True,
        )
        meta_path.write_text(json.dumps({'host': host, 'san': san}, indent=2))

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx
