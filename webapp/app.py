import hashlib
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, session
from PIL import Image as PILImage

from port_proxy import start_port_proxy
from tls_certs import ensure_tls_context

app = Flask(__name__)

# BASE_DIR must be defined before _load_secret_key so the .secret_key path resolves
BASE_DIR = Path(__file__).resolve().parent.parent
WORLDS_DIR = BASE_DIR / 'worldFiles'
JARS_DIR = BASE_DIR / 'jars'
CONFIG_FILE = BASE_DIR / 'config.json'
FLASK_HOST = '127.0.0.1'
FLASK_PORT = 17891  # internal only — proxied via public_port
PACK_PORT = 17892     # dedicated resource-pack HTTP server
DEFAULT_PUBLIC_PORT = 25565
DEFAULT_MC_INTERNAL_PORT = 25566
_IMAGE_EXTS = ('.png', '.jpg', '.jpeg')
SERVER_ICON_FILE = BASE_DIR / 'server-icon.png'
ICON_SIZE = 64

def _load_secret_key() -> str:
    if key := os.environ.get('SECRET_KEY'):
        return key
    key_file = BASE_DIR / '.secret_key'
    if key_file.exists():
        return key_file.read_text().strip()
    import secrets
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key

app.secret_key = _load_secret_key()
PASSWORD = os.environ.get('MC_PASSWORD', 'admin')
SERVER_NAME = os.environ.get('SERVER_NAME', 'MC')

PAINTINGS_NS = 'mcpainting'
# Broad supported_formats range so the pack loads across many MC versions
_RP_MCMETA = json.dumps({
    "pack": {
        "pack_format": 46,
        "supported_formats": {"min_inclusive": 34, "max_inclusive": 9999},
        "description": "Custom Paintings"
    }
})
_DP_MCMETA = json.dumps({
    "pack": {
        "pack_format": 61,
        "supported_formats": {"min_inclusive": 48, "max_inclusive": 9999},
        "description": "Custom Paintings"
    }
})

_jobs: dict = {}
_jobs_lock = threading.Lock()

_pregen_handles: dict[str, dict] = {}
_pregen_handles_lock = threading.Lock()

_server_proc: 'subprocess.Popen | None' = None
_server_start_time: float | None = None
_server_lock = threading.Lock()
_config_lock = threading.Lock()
_rcon_write_lock = threading.Lock()
_props_write_lock = threading.Lock()
_rcon_route_lock = threading.RLock()
_whitelist_lock = threading.Lock()
_world_locks: dict[str, threading.Lock] = {}
_world_locks_guard = threading.Lock()
_rebuild_busy: set[str] = set()
_rebuild_busy_lock = threading.Lock()
_icon_lock = threading.Lock()

_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_COOLDOWN_SEC = 10 * 60 * 60
_login_lockouts: dict[str, dict] = {}
_login_lockouts_lock = threading.Lock()


# ─── Config ──────────────────────────────────────────────────────────────────

_AIKAR_FLAGS = (
    '-Xms12G -Xmx12G -XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200'
    ' -XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:+AlwaysPreTouch'
    ' -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M'
    ' -XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4'
    ' -XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90'
    ' -XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32'
    ' -XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1'
)


def load_config() -> dict:
    defaults = {
        'active_world': None,
        'java_cmd': 'java',
        'jvm_args': _AIKAR_FLAGS,
        'public_port': DEFAULT_PUBLIC_PORT,
        'mc_internal_port': DEFAULT_MC_INTERNAL_PORT,
    }
    dirty = False
    if CONFIG_FILE.exists():
        saved = json.loads(CONFIG_FILE.read_text())
        defaults.update(saved)
        # Legacy "port" was the old Flask/resource-pack port — not the public MC port.
        if 'port' in saved:
            defaults.pop('port', None)
            dirty = True
    public = int(defaults.get('public_port', DEFAULT_PUBLIC_PORT))
    internal = int(defaults.get('mc_internal_port', DEFAULT_MC_INTERNAL_PORT))
    reserved = {FLASK_PORT, PACK_PORT, internal}
    if public in reserved or public == 5000:
        defaults['public_port'] = DEFAULT_PUBLIC_PORT
        dirty = True
    if internal == defaults['public_port']:
        defaults['mc_internal_port'] = DEFAULT_MC_INTERNAL_PORT
        dirty = True
    if dirty:
        CONFIG_FILE.write_text(json.dumps(
            {k: v for k, v in defaults.items() if k != 'port'}, indent=2,
        ))
    return defaults


def _mc_internal_port() -> int:
    return int(load_config().get('mc_internal_port', DEFAULT_MC_INTERNAL_PORT))


def _public_port() -> int:
    return int(load_config().get('public_port', DEFAULT_PUBLIC_PORT))


def _is_private_host(host: str) -> bool:
    host = host.strip()
    if host in ('localhost', '127.0.0.1', '::1'):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _resource_pack_scheme(host: str) -> str:
    """Use HTTPS only when explicitly enabled (requires a real TLS cert, not self-signed)."""
    if load_config().get('resource_pack_https') and not _is_private_host(host):
        return 'https'
    return 'http'


def _resource_pack_url(world_name: str, host: str) -> str:
    scheme = _resource_pack_scheme(host)
    return f'{scheme}://{host}:{_public_port()}/resourcepack/{world_name}.zip'


def _ensure_pack_cache(world_dir: Path) -> Path:
    cached = world_dir / '.resource_pack.zip'
    if not cached.exists():
        cached.write_bytes(_build_resource_pack_zip(_paintings_dir(world_dir)))
    return cached


def _pack_file_response(world_dir: Path) -> Response:
    cached = _ensure_pack_cache(world_dir)
    data = cached.read_bytes()
    return Response(
        data,
        mimetype='application/zip',
        headers={
            'Content-Length': str(len(data)),
            'Content-Disposition': f'attachment; filename="{world_dir.name}_paintings.zip"',
            'Cache-Control': 'no-store',
        },
    )


def _test_resource_pack_url(url: str) -> dict:
    """Verify the pack is reachable through the public port proxy."""
    try:
        verify = not url.startswith('https://')
        resp = requests.get(url, timeout=15, verify=verify)
        ct = resp.headers.get('Content-Type', '')
        ok = (
            resp.status_code == 200
            and len(resp.content) > 22
            and ('zip' in ct or 'octet-stream' in ct or resp.content[:2] == b'PK')
        )
        return {
            'ok': ok,
            'status': resp.status_code,
            'bytes': len(resp.content),
            'content_type': ct,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def save_config(config: dict) -> None:
    with _config_lock:
        CONFIG_FILE.write_text(json.dumps(config, indent=2))


def _world_op_lock(world_name: str) -> threading.Lock:
    with _world_locks_guard:
        if world_name not in _world_locks:
            _world_locks[world_name] = threading.Lock()
        return _world_locks[world_name]


def _background_mc_jobs_running() -> bool:
    with _jobs_lock:
        return any(
            j.get('status') == 'running' and j.get('type') in ('pregen', 'generate')
            for j in _jobs.values()
        )


def _active_background_mc_job() -> dict | None:
    with _jobs_lock:
        for job in _jobs.values():
            if job.get('status') == 'running' and job.get('type') in ('pregen', 'generate'):
                return {'type': job['type'], 'world': job.get('world')}
    return None


def _managed_server_running() -> bool:
    with _server_lock:
        return _server_proc is not None and _server_proc.poll() is None


def _mc_port_busy() -> bool:
    return _managed_server_running() or _background_mc_jobs_running()


def _try_reserve_job(world: str | None, job_type: str) -> str | None:
    """Atomically register a background job, or return None if busy."""
    with _server_lock:
        managed_running = _server_proc is not None and _server_proc.poll() is None
    with _jobs_lock:
        if job_type in ('pregen', 'generate') and managed_running:
            return None
        if job_type in ('pregen', 'generate'):
            if any(
                j.get('status') == 'running' and j.get('type') in ('pregen', 'generate')
                for j in _jobs.values()
            ):
                return None
        if world is not None:
            if any(
                j.get('status') == 'running' and j.get('world') == world
                for j in _jobs.values()
            ):
                return None
        job_id = str(uuid.uuid4())
        entry: dict = {
            'status': 'running',
            'log': [],
            'error': None,
            'type': job_type,
            'cancel_requested': False,
        }
        if world is not None:
            entry['world'] = world
        _jobs[job_id] = entry
        return job_id


# ─── Helpers ─────────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def safe_child(base: Path, name: str) -> Path:
    resolved = (base / name).resolve()
    if not str(resolved).startswith(str(base.resolve()) + os.sep):
        raise ValueError('Path traversal detected')
    return resolved


def dir_size(path: Path) -> int:
    try:
        result = subprocess.run(
            ['du', '-sb', str(path)],
            capture_output=True, text=True, timeout=15
        )
        return int(result.stdout.split()[0])
    except Exception:
        return 0


def get_level_name(world_dir: Path) -> str:
    props = world_dir / 'server.properties'
    if props.exists():
        for line in props.read_text().splitlines():
            if line.startswith('level-name='):
                return line.split('=', 1)[1].strip()
    return 'world'


# Paper extracts these into the world folder on start — not part of the save.
_DOWNLOAD_SKIP_DIRS = frozenset({
    'libraries', 'versions', 'logs', 'crash-reports', 'cache',
})
# Regenerated by the manager from paintings/ — skip to avoid huge duplicate blobs.
_DOWNLOAD_SKIP_FILES = frozenset({
    '.resource_pack.zip', '.resource_pack.zip.tmp',
})
# Region/chunk data is already packed; compressing it is very slow with little benefit.
_DOWNLOAD_STORE_SUFFIXES = frozenset({
    '.mca', '.mcc', '.png', '.jpg', '.jpeg', '.zip', '.jar', '.dat', '.nbt',
})


def _world_download_arcname(world_dir: Path, world_name: str, fp: Path) -> str | None:
    try:
        rel = fp.relative_to(world_dir)
    except ValueError:
        return None
    if fp.name == 'session.lock' or fp.name in _DOWNLOAD_SKIP_FILES:
        return None
    if rel.parts and rel.parts[0] in _DOWNLOAD_SKIP_DIRS:
        return None
    return f'{world_name}/{rel.as_posix()}'


def _zip_compress_type(fp: Path) -> int:
    """Store large/binary files as-is; only deflate small text configs."""
    try:
        size = fp.stat().st_size
    except OSError:
        return zipfile.ZIP_STORED
    if size > 256 * 1024 or fp.suffix.lower() in _DOWNLOAD_STORE_SUFFIXES:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _build_world_zip(
    world_dir: Path,
    world_name: str,
    dest: Path,
    log=None,
) -> int:
    """Zip world save data; returns number of files included."""
    count = 0
    bytes_done = 0
    last_log = time.time()
    with zipfile.ZipFile(dest, 'w') as zf:
        for fp in world_dir.rglob('*'):
            if not fp.is_file():
                continue
            arcname = _world_download_arcname(world_dir, world_name, fp)
            if not arcname:
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            zf.write(fp, arcname, compress_type=_zip_compress_type(fp))
            count += 1
            bytes_done += size
            if log and time.time() - last_log >= 2.0:
                log(f'Zipping… {count} files, {bytes_done / (1024 ** 3):.2f} GB scanned')
                last_log = time.time()
    return count


def valid_world_name(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    return not any(c in name for c in ('/', '\\', '..', '\0', ':'))


# ─── IP detection ────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''


def _proc_metrics(pid: int) -> dict | None:
    """Read RAM and avg-CPU for a process from /proc — pure file reads, zero overhead."""
    try:
        rss_kb = 0
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    rss_kb = int(line.split()[1])
                    break

        with open(f'/proc/{pid}/stat') as f:
            stat_raw = f.read()
        # comm field is wrapped in parens and may contain spaces; parse after the closing ')'
        stat_after_comm = stat_raw[stat_raw.rfind(')') + 2:]
        stat_fields = stat_after_comm.split()
        # After comm: state(0) ppid(1) ... utime(11) stime(12) ... starttime(19)
        utime, stime, start_ticks = int(stat_fields[11]), int(stat_fields[12]), int(stat_fields[19])

        with open('/proc/uptime') as f:
            sys_uptime = float(f.read().split()[0])

        hz = os.sysconf('SC_CLK_TCK')
        ncpus = os.cpu_count() or 1
        proc_uptime = sys_uptime - start_ticks / hz
        cpu_pct = round(((utime + stime) / hz) / proc_uptime / ncpus * 100, 1) if proc_uptime > 0 else 0.0

        mem_total = mem_avail = None
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    mem_avail = int(line.split()[1])
                if mem_total is not None and mem_avail is not None:
                    break
        mem_total = mem_total or 0
        mem_avail = mem_avail or 0

        return {
            'rss_kb': rss_kb,
            'cpu_pct': cpu_pct,
            'sys_mem_total_kb': mem_total,
            'sys_mem_used_kb': mem_total - mem_avail,
        }
    except Exception:
        return None


def _mc_status_ping(host: str, port: int, timeout: float = 1.5) -> dict:
    """Server List Ping — returns version, players, MOTD or raises on failure."""
    def pack_varint(v: int) -> bytes:
        out = b''
        while True:
            part = v & 0x7F
            v >>= 7
            out += bytes([part | (0x80 if v else 0)])
            if not v:
                break
        return out

    def read_varint(s: socket.socket) -> int:
        result = shift = 0
        while True:
            b = s.recv(1)
            if not b:
                raise ConnectionError('Connection closed')
            byte = b[0]
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
        return result

    def read_n(s: socket.socket, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('Connection closed')
            buf += chunk
        return buf

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect((host, port))

        host_b = host.encode('utf-8')
        body = (
            pack_varint(47) +
            pack_varint(len(host_b)) + host_b +
            struct.pack('>H', port) +
            pack_varint(1)
        )
        packet = pack_varint(0x00) + body
        s.sendall(pack_varint(len(packet)) + packet)
        s.sendall(b'\x01\x00')

        resp_len = read_varint(s)
        raw = read_n(s, resp_len)

        off = 0
        while raw[off] & 0x80:
            off += 1
        off += 1

        str_len = shift = 0
        while True:
            b = raw[off]; off += 1
            str_len |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break

        status = json.loads(raw[off:off + str_len])
        players = status.get('players', {})
        desc = status.get('description', {})
        if isinstance(desc, str):
            motd = desc
        elif isinstance(desc, dict):
            motd = desc.get('text', '')
            for extra in desc.get('extra', []):
                motd += extra.get('text', '') if isinstance(extra, dict) else str(extra)
        else:
            motd = ''
        motd = re.sub(r'§.', '', motd).strip()

        return {
            'players_online': players.get('online', 0),
            'players_max': players.get('max', 20),
            'version': status.get('version', {}).get('name', ''),
            'motd': motd,
        }


# ─── RCON ─────────────────────────────────────────────────────────────────────

class RCONError(Exception):
    pass


def _rcon_exec(host: str, port: int, password: str, command: str) -> str:
    with _rcon_route_lock:
        return _rcon_exec_unlocked(host, port, password, command)


def _rcon_exec_unlocked(host: str, port: int, password: str, command: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        try:
            s.connect((host, int(port)))
        except ConnectionRefusedError:
            raise RCONError('Connection refused — is the server running with RCON enabled?')
        except OSError as e:
            raise RCONError(str(e))

        def send_pkt(req_id: int, ptype: int, payload: str) -> None:
            data = payload.encode('utf-8') + b'\x00\x00'
            s.sendall(struct.pack('<iii', len(data) + 8, req_id, ptype) + data)

        def recv_pkt() -> tuple[int, int, str]:
            raw = b''
            while len(raw) < 4:
                chunk = s.recv(4 - len(raw))
                if not chunk:
                    raise RCONError('Connection closed by server')
                raw += chunk
            length = struct.unpack('<i', raw)[0]
            raw = b''
            while len(raw) < length:
                chunk = s.recv(length - len(raw))
                if not chunk:
                    raise RCONError('Connection closed by server')
                raw += chunk
            return (
                struct.unpack('<i', raw[0:4])[0],
                struct.unpack('<i', raw[4:8])[0],
                raw[8:-2].decode('utf-8', errors='replace'),
            )

        send_pkt(1, 3, password)
        resp_id, _, _ = recv_pkt()
        if resp_id == -1:
            raise RCONError('Authentication failed — check rcon.password in server.properties')

        send_pkt(2, 2, command)
        _, _, response = recv_pkt()
        return response


def _rcon_settings(world_dir: Path) -> dict:
    cfg = {'enabled': False, 'port': 25575, 'password': ''}
    props = world_dir / 'server.properties'
    if not props.exists():
        return cfg
    for line in props.read_text().splitlines():
        if '=' not in line or line.startswith('#'):
            continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip()
        if k == 'enable-rcon':
            cfg['enabled'] = v.lower() == 'true'
        elif k == 'rcon.port':
            try:
                cfg['port'] = int(v)
            except ValueError:
                pass
        elif k == 'rcon.password':
            cfg['password'] = v
    return cfg


# ─── Paintings / resource-pack helpers ───────────────────────────────────────

def _paintings_dir(world_dir: Path) -> Path:
    """Per-world image storage inside the world save (never synced via git)."""
    return world_dir / 'paintings'


def _is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in _IMAGE_EXTS


def _painting_stem(filename: str) -> str:
    """Normalise filename to a valid MC identifier segment."""
    return Path(filename).stem.lower().replace(' ', '_').replace('-', '_').replace('.', '_')


def _image_block_dims(path: Path) -> tuple[int, int]:
    """Return (width_blocks, height_blocks) scaled to fit within 4×4 blocks."""
    try:
        with PILImage.open(path) as img:
            pw, ph = img.size
        w = max(1, pw // 16)
        h = max(1, ph // 16)
        if w > 4 or h > 4:
            scale = 4 / max(w, h)
            w = max(1, round(w * scale))
            h = max(1, round(h * scale))
        return w, h
    except Exception:
        return 1, 1


def _build_resource_pack_zip(paintings_dir: Path) -> bytes:
    """Build the resource-pack zip in memory from a world's paintings directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('pack.mcmeta', _RP_MCMETA)
        if paintings_dir.exists():
            for img_path in sorted(paintings_dir.iterdir()):
                if not _is_image_file(img_path):
                    continue
                stem = _painting_stem(img_path.name)
                dest = f'assets/{PAINTINGS_NS}/textures/painting/{stem}.png'
                if img_path.suffix.lower() == '.png':
                    zf.write(img_path, dest)
                else:
                    # Convert JPEG → PNG
                    with PILImage.open(img_path) as img:
                        png_buf = io.BytesIO()
                        img.convert('RGBA').save(png_buf, 'PNG')
                    zf.writestr(dest, png_buf.getvalue())
    return buf.getvalue()


def _rebuild_datapack(world_dir: Path, paintings_dir: Path) -> None:
    """Recreate the mc-paintings data pack inside the world level's datapacks/ folder."""
    level_name = get_level_name(world_dir)
    dp_root = world_dir / level_name / 'datapacks' / 'mc-paintings'
    if dp_root.exists():
        shutil.rmtree(dp_root)

    variant_dir = dp_root / 'data' / PAINTINGS_NS / 'painting_variant'
    variant_dir.mkdir(parents=True)
    (dp_root / 'pack.mcmeta').write_text(_DP_MCMETA)

    if paintings_dir.exists():
        for img_path in sorted(paintings_dir.iterdir()):
            if not _is_image_file(img_path):
                continue
            stem = _painting_stem(img_path.name)
            w, h = _image_block_dims(img_path)
            (variant_dir / f'{stem}.json').write_text(json.dumps({
                "asset_id": f"{PAINTINGS_NS}:{stem}",
                "width": w,
                "height": h,
            }, indent=2))


def _update_server_properties(world_dir: Path, updates: dict[str, str]) -> None:
    props_file = world_dir / 'server.properties'
    with _props_write_lock:
        if not props_file.exists():
            return
        lines = props_file.read_text().splitlines()
        seen = set()
        new_lines = []
        for line in lines:
            key = line.split('=', 1)[0] if '=' in line else None
            if key in updates:
                new_lines.append(f'{key}={updates[key]}')
                seen.add(key)
            else:
                new_lines.append(line)
        for key, val in updates.items():
            if key not in seen:
                new_lines.append(f'{key}={val}')
        props_file.write_text('\n'.join(new_lines) + '\n')


def _ensure_mc_internal_port(world_dir: Path) -> None:
    """MC binds internally; players connect via the public port multiplexer."""
    _update_server_properties(world_dir, {
        'server-port': str(_mc_internal_port()),
    })


def _active_world_dir() -> Path | None:
    name = load_config().get('active_world')
    if not name:
        return None
    try:
        world_dir = safe_child(WORLDS_DIR, name)
    except ValueError:
        return None
    return world_dir if world_dir.is_dir() else None


def _read_prop(world_dir: Path, key: str) -> str:
    props = world_dir / 'server.properties'
    if not props.exists():
        return ''
    for line in props.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            if k.strip() == key:
                return v.strip()
    return ''


def _offline_player_uuid(name: str) -> str:
    data = hashlib.md5(f'OfflinePlayer:{name}'.encode()).digest()
    b = bytearray(data)
    b[6] = (b[6] & 0x0f) | 0x30
    b[8] = (b[8] & 0x3f) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def _read_whitelist(world_dir: Path) -> list[dict]:
    wl = world_dir / 'whitelist.json'
    if not wl.exists():
        return []
    try:
        with _whitelist_lock:
            return json.loads(wl.read_text())
    except Exception:
        return []


def _write_whitelist(world_dir: Path, entries: list[dict]) -> None:
    with _whitelist_lock:
        (world_dir / 'whitelist.json').write_text(json.dumps(entries, indent=2))


def _whitelist_enabled(world_dir: Path) -> bool:
    return _read_prop(world_dir, 'white-list').lower() == 'true'


def _set_whitelist_enabled(world_dir: Path, enabled: bool) -> None:
    val = 'true' if enabled else 'false'
    _update_server_properties(world_dir, {'white-list': val, 'enforce-whitelist': val})


def _server_address() -> str:
    config = load_config()
    host = config.get('server_host', '').strip() or get_local_ip()
    return f'{host}:{_public_port()}' if host else ''


def _rcon_on_active(command: str) -> str | None:
    world_dir = _active_world_dir()
    if not world_dir:
        return None
    cfg = _rcon_settings(world_dir)
    if not cfg['enabled'] or not cfg['password']:
        return None
    try:
        _mc_status_ping('localhost', _mc_internal_port())
    except Exception:
        return None
    try:
        return _rcon_exec('localhost', cfg['port'], cfg['password'], command)
    except RCONError:
        return None


def _ensure_rcon(world_dir: Path) -> None:
    """Enable RCON in server.properties if not already set, generating a password if needed."""
    props_file = world_dir / 'server.properties'
    if not props_file.exists():
        return
    with _rcon_write_lock:
        lines = props_file.read_text().splitlines()
        current: dict[str, str] = {}
        for line in lines:
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                current[k.strip()] = v.strip()

        already_enabled = current.get('enable-rcon', '').lower() == 'true'
        has_password = bool(current.get('rcon.password', '').strip())

        if already_enabled and has_password:
            return  # nothing to do

        import secrets
        updates: dict[str, str] = {}
        if not already_enabled:
            updates['enable-rcon'] = 'true'
        if not current.get('rcon.port'):
            updates['rcon.port'] = '25575'
        if not has_password:
            updates['rcon.password'] = secrets.token_urlsafe(16)

        seen: set[str] = set()
        new_lines: list[str] = []
        for line in lines:
            k = line.split('=', 1)[0].strip() if '=' in line else None
            if k in updates:
                new_lines.append(f'{k}={updates[k]}')
                seen.add(k)
            else:
                new_lines.append(line)
        for k, v in updates.items():
            if k not in seen:
                new_lines.append(f'{k}={v}')
        props_file.write_text('\n'.join(new_lines) + '\n')


def rebuild_paintings(world_dir: Path) -> dict:
    """Full pipeline: data pack → resource pack zip → server.properties for one world."""
    world_name = world_dir.name
    lock = _world_op_lock(world_name)
    if not lock.acquire(blocking=False):
        raise RuntimeError('Painting rebuild already in progress for this world')
    with _rebuild_busy_lock:
        if world_name in _rebuild_busy:
            lock.release()
            raise RuntimeError('Painting rebuild already in progress for this world')
        _rebuild_busy.add(world_name)
    try:
        return _rebuild_paintings_locked(world_dir)
    finally:
        with _rebuild_busy_lock:
            _rebuild_busy.discard(world_name)
        lock.release()


def _rebuild_paintings_locked(world_dir: Path) -> dict:
    paintings_dir = _paintings_dir(world_dir)

    _rebuild_datapack(world_dir, paintings_dir)

    pack_bytes = _build_resource_pack_zip(paintings_dir)
    sha1 = hashlib.sha1(pack_bytes).hexdigest()

    cached = world_dir / '.resource_pack.zip'
    tmp = world_dir / '.resource_pack.zip.tmp'
    tmp.write_bytes(pack_bytes)
    tmp.replace(cached)

    config = load_config()
    host = config.get('server_host', '').strip()
    if not host:
        host = get_local_ip()
    pack_info: dict = {'url': '', 'sha1': sha1, 'image_count': 0}
    if paintings_dir.exists():
        pack_info['image_count'] = sum(1 for f in paintings_dir.iterdir() if _is_image_file(f))
    if host:
        url = _resource_pack_url(world_dir.name, host)
        _update_server_properties(world_dir, {
            'resource-pack': url,
            'resource-pack-sha1': sha1,
            'server-port': str(_mc_internal_port()),
        })
        loopback = _resource_pack_url(world_dir.name, '127.0.0.1').replace('https://', 'http://')
        pack_info.update({
            'url': url,
            'scheme': _resource_pack_scheme(host),
            'test': _test_resource_pack_url(loopback),
        })
        if not pack_info['test'].get('ok'):
            app.logger.warning(
                'Resource pack self-test failed for %s: %s',
                world_dir.name, pack_info['test'],
            )
    else:
        _ensure_mc_internal_port(world_dir)

    _ensure_rcon(world_dir)
    return pack_info


def rebuild_paintings_all() -> int:
    """Rebuild paintings for every world. Returns count of worlds rebuilt."""
    if not WORLDS_DIR.exists():
        return 0
    count = 0
    for world_dir in WORLDS_DIR.iterdir():
        if world_dir.is_dir():
            try:
                rebuild_paintings(world_dir)
                count += 1
            except Exception as e:
                app.logger.error('rebuild_paintings failed for %s: %s', world_dir.name, e)
    return count


# ─── Server icon ─────────────────────────────────────────────────────────────

def _save_server_icon_file(upload) -> None:
    """Resize upload to 64×64 PNG and save as the global server icon."""
    with PILImage.open(upload) as img:
        img = img.convert('RGBA')
        img = img.resize((ICON_SIZE, ICON_SIZE), PILImage.Resampling.LANCZOS)
        img.save(SERVER_ICON_FILE, 'PNG')


def _apply_server_icon(world_dir: Path) -> None:
    if SERVER_ICON_FILE.is_file():
        shutil.copy2(SERVER_ICON_FILE, world_dir / 'server-icon.png')


def _apply_server_icon_all() -> None:
    if not SERVER_ICON_FILE.is_file() or not WORLDS_DIR.exists():
        return
    for world_dir in WORLDS_DIR.iterdir():
        if world_dir.is_dir():
            _apply_server_icon(world_dir)


def _remove_server_icon_all() -> None:
    if not WORLDS_DIR.exists():
        return
    for world_dir in WORLDS_DIR.iterdir():
        icon = world_dir / 'server-icon.png'
        if icon.is_file():
            icon.unlink()


# ─── Auth ────────────────────────────────────────────────────────────────────

def _client_ip() -> str:
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _login_lockout_message(ip: str) -> str | None:
    with _login_lockouts_lock:
        entry = _login_lockouts.get(ip)
        if not entry:
            return None
        locked_until = entry.get('locked_until')
        if not locked_until:
            return None
        remaining = locked_until - time.time()
        if remaining <= 0:
            _login_lockouts.pop(ip, None)
            return None
        hours = int(remaining // 3600)
        mins = int((remaining % 3600) // 60)
        return f'Too many failed attempts. Try again in {hours}h {mins}m.'


def _login_record_failure(ip: str) -> tuple[int, str | None]:
    """Return (attempts_remaining, lockout_message_if_locked)."""
    with _login_lockouts_lock:
        entry = _login_lockouts.setdefault(ip, {'failures': 0, 'locked_until': None})
        entry['failures'] += 1
        if entry['failures'] >= _LOGIN_MAX_ATTEMPTS:
            entry['locked_until'] = time.time() + _LOGIN_COOLDOWN_SEC
            return 0, 'Too many failed attempts. Try again in 10 hours.'
        return _LOGIN_MAX_ATTEMPTS - entry['failures'], None


def _login_clear_failures(ip: str) -> None:
    with _login_lockouts_lock:
        _login_lockouts.pop(ip, None)


@app.route('/')
def index():
    if not session.get('authenticated'):
        return render_template('login.html', server_name=SERVER_NAME)
    return render_template('index.html', server_name=SERVER_NAME)


@app.route('/login', methods=['POST'])
def login():
    ip = _client_ip()
    lockout = _login_lockout_message(ip)
    if lockout:
        return jsonify({'error': lockout}), 429

    data = request.get_json() or {}
    if data.get('password') == PASSWORD:
        _login_clear_failures(ip)
        session['authenticated'] = True
        return jsonify({'ok': True})

    remaining, lockout = _login_record_failure(ip)
    if lockout:
        return jsonify({'error': lockout}), 429
    return jsonify({
        'error': f'Invalid password ({remaining} attempt{"s" if remaining != 1 else ""} remaining)',
    }), 401


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


# ─── Worlds ──────────────────────────────────────────────────────────────────

@app.route('/api/worlds')
@require_auth
def list_worlds():
    config = load_config()
    worlds = []
    if WORLDS_DIR.exists():
        for d in sorted(WORLDS_DIR.iterdir()):
            if not d.is_dir():
                continue
            worlds.append({
                'name': d.name,
                'size': dir_size(d),
                'modified': datetime.fromtimestamp(d.stat().st_mtime).isoformat(),
                'has_properties': (d / 'server.properties').exists(),
                'active': d.name == config.get('active_world'),
            })
    return jsonify(worlds)


@app.route('/api/worlds/<name>/activate', methods=['POST'])
@require_auth
def activate_world(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    if _world_has_running_job(name):
        return jsonify({'error': 'A background job is running for this world'}), 400
    if _managed_server_running():
        return jsonify({'error': 'Stop the server before changing the active world'}), 400
    with _config_lock:
        config = load_config()
        config['active_world'] = name
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
    _apply_server_icon(world_dir)
    return jsonify({'ok': True})


def _run_download_zip(job_id: str, world_name: str):
    world_dir = safe_child(WORLDS_DIR, world_name)
    tmp_path: Path | None = None

    def log(msg: str):
        with _jobs_lock:
            _jobs[job_id]['log'].append(msg)

    def set_status(status: str, error: str | None = None):
        with _jobs_lock:
            if _jobs.get(job_id, {}).get('status') != 'running':
                return
            _jobs[job_id]['status'] = status
            if error:
                _jobs[job_id]['error'] = error
            _jobs[job_id]['finished_at'] = time.time()
        _prune_finished_jobs()

    try:
        log('Creating zip (large worlds may take several minutes)…')
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        tmp_path = Path(tmp.name)
        tmp.close()
        count = _build_world_zip(world_dir, world_name, tmp_path, log=log)
        size_mb = tmp_path.stat().st_size / (1024 * 1024)
        log(f'Zip ready — {count} files, {size_mb:.1f} MB.')
        with _jobs_lock:
            _jobs[job_id]['zip_path'] = str(tmp_path)
            _jobs[job_id]['world'] = world_name
        set_status('done')
    except Exception as e:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        set_status('error', str(e))


def _cleanup_download_zip(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        zip_path = job.pop('zip_path', None)
    if zip_path:
        Path(zip_path).unlink(missing_ok=True)


@app.route('/api/worlds/<name>/download', methods=['POST'])
@require_auth
def start_download_world(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    if _world_has_running_job(name):
        return jsonify({'error': 'A background job is already running for this world'}), 400
    if _managed_server_running():
        config = load_config()
        if config.get('active_world') == name:
            return jsonify({'error': 'Stop the server before downloading this world'}), 400

    job_id = _try_reserve_job(name, 'download')
    if not job_id:
        return jsonify({'error': 'A background job is already running for this world'}), 400

    t = threading.Thread(target=_run_download_zip, args=(job_id, name), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/worlds/download/<job_id>')
@require_auth
def download_world_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get('type') != 'download':
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@app.route('/api/worlds/download/<job_id>/file')
@require_auth
def download_world_file(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get('type') != 'download':
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') != 'done':
        return jsonify({'error': 'Zip is not ready yet'}), 400
    zip_path = job.get('zip_path')
    world_name = job.get('world', 'world')
    if not zip_path or not Path(zip_path).is_file():
        return jsonify({'error': 'Zip file missing — try downloading again'}), 404

    resp = send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{world_name}.zip',
    )
    resp.call_on_close(lambda: _cleanup_download_zip(job_id))
    return resp


@app.route('/api/worlds/<name>/properties', methods=['GET'])
@require_auth
def get_properties(name):
    props = safe_child(WORLDS_DIR, name) / 'server.properties'
    if not props.exists():
        return jsonify({'error': 'server.properties not found'}), 404
    return jsonify({'content': props.read_text()})


@app.route('/api/worlds/<name>/properties', methods=['POST'])
@require_auth
def save_properties(name):
    if _world_dir_busy(name):
        return jsonify({'error': 'A background job is running for this world'}), 400
    props = safe_child(WORLDS_DIR, name) / 'server.properties'
    if not props.exists():
        return jsonify({'error': 'server.properties not found'}), 404
    data = request.get_json() or {}
    with _props_write_lock:
        props.write_text(data.get('content', ''))
    return jsonify({'ok': True})


@app.route('/api/worlds/<name>/images', methods=['GET'])
@require_auth
def list_images(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    paintings_dir = _paintings_dir(world_dir)
    if not paintings_dir.exists():
        return jsonify([])
    return jsonify([
        {'name': f.name, 'size': f.stat().st_size}
        for f in sorted(paintings_dir.iterdir())
        if _is_image_file(f)
    ])


@app.route('/api/worlds/<name>/images', methods=['POST'])
@require_auth
def upload_image(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    if _world_dir_busy(name):
        return jsonify({'error': 'A background job is running for this world'}), 400
    file = request.files.get('image')
    if not file or not file.filename:
        return jsonify({'error': 'No file provided'}), 400
    original = Path(file.filename).name
    ext = Path(original).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg'):
        return jsonify({'error': 'Only PNG/JPEG images allowed'}), 400
    filename = Path(original).stem + ext  # normalise extension to lowercase
    paintings_dir = _paintings_dir(world_dir)
    paintings_dir.mkdir(parents=True, exist_ok=True)
    file.save(paintings_dir / filename)
    rebuild_paintings(world_dir)
    w, h = _image_block_dims(paintings_dir / filename)
    return jsonify({'ok': True, 'name': filename, 'width_blocks': w, 'height_blocks': h})


@app.route('/api/worlds/<name>/images/<filename>', methods=['DELETE'])
@require_auth
def delete_image(name, filename):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    if _world_dir_busy(name):
        return jsonify({'error': 'A background job is running for this world'}), 400
    img = _paintings_dir(world_dir) / Path(filename).name
    if not img.is_file():
        return jsonify({'error': 'Image not found'}), 404
    img.unlink()
    rebuild_paintings(world_dir)
    return jsonify({'ok': True})


@app.route('/api/worlds/<name>/rebuild-paintings', methods=['POST'])
@require_auth
def rebuild_paintings_endpoint(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    if _world_dir_busy(name):
        return jsonify({'error': 'A background job is running for this world'}), 400
    try:
        pack_info = rebuild_paintings(world_dir)
        return jsonify({'ok': True, 'pack': pack_info})
    except RuntimeError as e:
        if 'already in progress' in str(e):
            return jsonify({'error': str(e)}), 409
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _world_has_running_job(world_name: str, job_type: str | None = None) -> bool:
    with _jobs_lock:
        return any(
            j.get('status') == 'running'
            and j.get('world') == world_name
            and (job_type is None or j.get('type') == job_type)
            for j in _jobs.values()
        )


def _world_dir_busy(world_name: str) -> bool:
    return _world_has_running_job(world_name) or _background_mc_jobs_running()


_MAX_FINISHED_JOBS = 50


def _prune_finished_jobs() -> None:
    with _jobs_lock:
        finished = [
            (jid, j) for jid, j in _jobs.items()
            if j.get('status') != 'running'
        ]
        if len(finished) <= _MAX_FINISHED_JOBS:
            return
        finished.sort(key=lambda item: item[1].get('finished_at', 0))
        for jid, _ in finished[: len(finished) - _MAX_FINISHED_JOBS]:
            _jobs.pop(jid, None)


def _finish_job(job_id: str, status: str = 'done', error: str | None = None) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get('status') != 'running':
            return
        job['status'] = status
        if error:
            job['error'] = error
        job['finished_at'] = time.time()
    _prune_finished_jobs()


def _job_cancel_requested(job_id: str) -> bool:
    with _jobs_lock:
        return bool(_jobs.get(job_id, {}).get('cancel_requested'))


def _request_job_cancel(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job and job.get('status') == 'running':
            job['cancel_requested'] = True
    with _pregen_handles_lock:
        handle = _pregen_handles.get(job_id)
    if handle:
        handle['cancel'].set()


def _run_delete(job_id: str, world_name: str):
    world_dir = safe_child(WORLDS_DIR, world_name)

    def log(msg: str):
        with _jobs_lock:
            _jobs[job_id]['log'].append(msg)

    def set_status(status: str, error: str | None = None):
        with _jobs_lock:
            if _jobs.get(job_id, {}).get('status') != 'running':
                return
            _jobs[job_id]['status'] = status
            if error:
                _jobs[job_id]['error'] = error
            _jobs[job_id]['finished_at'] = time.time()
        _prune_finished_jobs()

    try:
        lock = _world_op_lock(world_name)
        lock.acquire()
        try:
            if not world_dir.is_dir():
                raise RuntimeError(f'World "{world_name}" not found')

            config = load_config()
            if config.get('active_world') == world_name:
                with _config_lock:
                    cfg = load_config()
                    cfg['active_world'] = None
                    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
                log('Cleared active world.')

            log('Deleting world files (large worlds may take several minutes)…')
            shutil.rmtree(world_dir)
        finally:
            lock.release()
        log('World deleted.')
        set_status('done')
    except Exception as e:
        set_status('error', str(e))


@app.route('/api/worlds/<name>', methods=['DELETE'])
@require_auth
def delete_world(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404

    if _managed_server_running():
        return jsonify({'error': 'Stop the server before deleting a world'}), 400

    job_id = _try_reserve_job(name, 'delete')
    if not job_id:
        return jsonify({'error': 'A background job is already running for this world'}), 400

    t = threading.Thread(target=_run_delete, args=(job_id, name), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/worlds/delete/<job_id>')
@require_auth
def delete_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@app.route('/api/worlds/<name>/rename', methods=['POST'])
@require_auth
def rename_world(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    if _managed_server_running():
        return jsonify({'error': 'Stop the server before renaming a world'}), 400
    data = request.get_json() or {}
    new_name = data.get('new_name', '').strip()
    if not valid_world_name(new_name):
        return jsonify({'error': 'Invalid world name'}), 400
    new_dir = WORLDS_DIR / new_name
    if new_dir.exists():
        return jsonify({'error': 'A world with that name already exists'}), 409
    if _world_has_running_job(new_name):
        return jsonify({'error': 'A background job is already running for the target name'}), 400
    job_id = _try_reserve_job(name, 'rename')
    if not job_id:
        return jsonify({'error': 'A background job is already running for this world'}), 400
    lock = _world_op_lock(name)
    try:
        lock.acquire()
        if not world_dir.is_dir():
            _finish_job(job_id, 'error', 'World not found')
            return jsonify({'error': 'World not found'}), 404
        if new_dir.exists():
            _finish_job(job_id, 'error', 'A world with that name already exists')
            return jsonify({'error': 'A world with that name already exists'}), 409
        world_dir.rename(new_dir)
        with _config_lock:
            config = load_config()
            if config.get('active_world') == name:
                config['active_world'] = new_name
                CONFIG_FILE.write_text(json.dumps(config, indent=2))
        _finish_job(job_id)
        return jsonify({'ok': True, 'new_name': new_name})
    except OSError as e:
        _finish_job(job_id, 'error', str(e))
        return jsonify({'error': str(e)}), 500
    finally:
        lock.release()


@app.route('/api/worlds/upload', methods=['POST'])
@require_auth
def upload_world():
    file = request.files.get('world')
    if not file or not file.filename:
        return jsonify({'error': 'No file provided'}), 400
    if not file.filename.lower().endswith('.zip'):
        return jsonify({'error': 'Only .zip files are supported'}), 400
    world_name = Path(file.filename).stem
    if not valid_world_name(world_name):
        return jsonify({'error': 'Invalid world name (derived from filename)'}), 400
    world_dir = WORLDS_DIR / world_name
    if world_dir.exists():
        return jsonify({'error': f'A world named "{world_name}" already exists'}), 409
    job_id = _try_reserve_job(world_name, 'upload')
    if not job_id:
        return jsonify({'error': 'A background job is already running for this world'}), 400
    lock = _world_op_lock(world_name)
    try:
        lock.acquire()
        if world_dir.exists():
            _finish_job(job_id, 'error', f'A world named "{world_name}" already exists')
            return jsonify({'error': f'A world named "{world_name}" already exists'}), 409
        data = file.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Detect whether the zip has a single top-level folder (common for world exports)
            names = zf.namelist()
            top = {n.split('/')[0] for n in names if n.strip('/')}
            if len(top) == 1:
                prefix = next(iter(top)) + '/'
                world_dir.mkdir(parents=True, exist_ok=False)
                for member in zf.infolist():
                    rel = member.filename[len(prefix):]
                    if not rel:
                        continue
                    dest = world_dir / rel
                    if member.filename.endswith('/'):
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(member.filename))
            else:
                world_dir.mkdir(parents=True, exist_ok=False)
                zf.extractall(world_dir)
        (world_dir / 'eula.txt').write_text('eula=true\n')
        try:
            rebuild_paintings(world_dir)
        except Exception as e:
            app.logger.error('rebuild_paintings after upload failed for %s: %s', world_name, e)
        _finish_job(job_id)
        return jsonify({'ok': True, 'name': world_name})
    except Exception as e:
        if world_dir.exists():
            shutil.rmtree(world_dir, ignore_errors=True)
        _finish_job(job_id, 'error', str(e))
        return jsonify({'error': str(e)}), 500
    finally:
        lock.release()


# ─── World Generation ─────────────────────────────────────────────────────────

def _run_generate(job_id: str, new_name: str, inherit_properties: bool, old_active: str | None):
    jar = JARS_DIR / 'server.jar'
    config = load_config()
    java_cmd = config.get('java_cmd', 'java')
    jvm_args = shlex.split(config.get('jvm_args', _AIKAR_FLAGS))

    def log(msg: str):
        with _jobs_lock:
            _jobs[job_id]['log'].append(msg)

    def set_status(status: str, error: str | None = None):
        with _jobs_lock:
            if _jobs.get(job_id, {}).get('status') != 'running':
                return
            _jobs[job_id]['status'] = status
            if error:
                _jobs[job_id]['error'] = error
            _jobs[job_id]['finished_at'] = time.time()
        _prune_finished_jobs()

    new_dir = WORLDS_DIR / new_name
    created_dir = False
    try:
        if new_dir.exists():
            raise RuntimeError(f'A world named "{new_name}" already exists')

        new_dir.mkdir(parents=True)
        created_dir = True
        log(f'Created directory: {new_dir.name}/')

        # Accept EULA automatically
        (new_dir / 'eula.txt').write_text('eula=true\n')
        log('Wrote eula.txt (eula=true)')

        # Optionally copy server.properties from old active world
        if inherit_properties and old_active:
            src_props = WORLDS_DIR / old_active / 'server.properties'
            if src_props.exists():
                shutil.copy(src_props, new_dir / 'server.properties')
                log(f'Copied server.properties from {old_active}/')

        props_file = new_dir / 'server.properties'
        if not props_file.exists():
            props_file.write_text(f'server-port={_mc_internal_port()}\n')
        else:
            _ensure_mc_internal_port(new_dir)

        if not jar.exists():
            raise RuntimeError('server.jar not found in jars/ directory')

        log(f'Starting server: {java_cmd} {" ".join(jvm_args)} -jar {jar.name} --nogui')
        ready_event = threading.Event()
        proc = subprocess.Popen(
            [java_cmd, *jvm_args, '-jar', str(jar), '--nogui'],
            cwd=str(new_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        assert proc.stdout and proc.stdin
        threading.Thread(
            target=_drain_process_output,
            args=(proc, log, ready_event),
            daemon=True,
        ).start()

        world_done = ready_event.wait(timeout=180)
        if world_done:
            log('--- World generated. Sending stop command... ---')
            try:
                proc.stdin.write('stop\n')
                proc.stdin.flush()
            except OSError:
                proc.terminate()

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError('Server took too long to stop after world generation')

        if not world_done:
            raise RuntimeError('Server timed out or exited before world generation completed')

        # Mark new world as active
        with _config_lock:
            config = load_config()
            config['active_world'] = new_name
            CONFIG_FILE.write_text(json.dumps(config, indent=2))
        log(f'Set "{new_name}" as active world.')

        try:
            rebuild_paintings(new_dir)
            log('Configured resource-pack URL and data pack.')
        except Exception as e:
            log(f'Warning: painting setup failed: {e}')

        set_status('done')

    except Exception as e:
        set_status('error', str(e))
        if created_dir and new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)


@app.route('/api/worlds/generate', methods=['POST'])
@require_auth
def start_generate():
    data = request.get_json() or {}
    new_name = data.get('new_name', '').strip()
    inherit_properties = bool(data.get('inherit_properties', True))

    if not valid_world_name(new_name):
        return jsonify({'error': 'Invalid world name'}), 400

    new_dir = WORLDS_DIR / new_name
    if new_dir.exists():
        return jsonify({'error': f'A world named "{new_name}" already exists'}), 409

    job_id = _try_reserve_job(new_name, 'generate')
    if not job_id:
        if _managed_server_running():
            return jsonify({'error': 'Stop the server before generating a world'}), 400
        return jsonify({
            'error': 'World generation or another background server task is already running',
        }), 400

    config = load_config()
    old_active = config.get('active_world')

    t = threading.Thread(
        target=_run_generate,
        args=(job_id, new_name, inherit_properties, old_active),
        daemon=True,
    )
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/worlds/generate/<job_id>')
@require_auth
def generate_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


# ─── Chunk pre-generation (Paper + Chunky) ───────────────────────────────────

# Chunky has no GitHub releases; official Bukkit/Paper builds are on CodeMC CI.
CHUNKY_JAR_URL = (
    'https://ci.codemc.io/job/pop4959/job/Chunky/lastSuccessfulBuild/'
    'artifact/bukkit/build/libs/Chunky-Bukkit-1.5.3.jar'
)


def _ensure_chunky_plugin(world_dir: Path) -> Path:
    plugins_dir = world_dir / 'plugins'
    plugins_dir.mkdir(exist_ok=True)
    dest = plugins_dir / 'Chunky.jar'
    if dest.exists():
        return dest
    with requests.get(CHUNKY_JAR_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def _remove_stale_session_lock(world_dir: Path, level: str, log) -> None:
    lock = world_dir / level / 'session.lock'
    if lock.exists():
        log(f'Removing stale session.lock from {level}/')
        try:
            lock.unlink()
        except OSError as e:
            log(f'Warning: could not remove session.lock: {e}')


def _drain_process_output(proc: subprocess.Popen, log, ready_event: threading.Event | None = None) -> None:
    try:
        assert proc.stdout
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(line)
            if ready_event is not None and 'Done (' in line:
                ready_event.set()
    except Exception:
        pass


def _stop_background_mc(
    proc: subprocess.Popen,
    cfg: dict | None,
    log,
    *,
    cancelled: bool = False,
) -> None:
    if cancelled:
        log('Stopping pre-generation…')
    if cfg and cfg.get('enabled') and cfg.get('password'):
        try:
            _chunky_rcon(cfg, 'chunky cancel')
        except RCONError:
            pass
        try:
            _rcon_exec('localhost', cfg['port'], cfg['password'], 'stop')
        except RCONError:
            if proc.stdin and proc.poll() is None:
                try:
                    proc.stdin.write('stop\n')
                    proc.stdin.flush()
                except OSError:
                    proc.terminate()
    elif proc.poll() is None:
        if proc.stdin:
            try:
                proc.stdin.write('stop\n')
                proc.stdin.flush()
            except OSError:
                proc.terminate()
        else:
            proc.terminate()

    try:
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _pregen_sleep(seconds: int, job_id: str, cancel_event: threading.Event) -> bool:
    """Sleep in 1s slices; return True if cancel was requested."""
    for _ in range(seconds):
        if cancel_event.is_set() or _job_cancel_requested(job_id):
            return True
        time.sleep(1)
    return cancel_event.is_set() or _job_cancel_requested(job_id)


def _chunky_rcon(cfg: dict, cmd: str) -> tuple[str, str]:
    """Run a Chunky RCON command; auto-send 'chunky confirm' when Chunky requires it."""
    resp = _rcon_exec('localhost', cfg['port'], cfg['password'], cmd)
    clean = re.sub(r'§.', '', resp).strip()
    lower = clean.lower()
    if 'confirm' in lower:
        confirm_resp = _rcon_exec('localhost', cfg['port'], cfg['password'], 'chunky confirm')
        confirm_clean = re.sub(r'§.', '', confirm_resp).strip()
        if confirm_clean:
            clean = f'{clean}\n{confirm_clean}'
            lower = clean.lower()
    return lower, clean


def _chunky_reset_task(cfg: dict, log) -> None:
    try:
        lower, clean = _chunky_rcon(cfg, 'chunky cancel')
        log(f'> chunky cancel')
        if clean:
            log(clean)
    except RCONError:
        pass


def _chunky_progress_pct(output: str) -> float | None:
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', output)
    return float(m.group(1)) if m else None


def _run_pregen(job_id: str, world_name: str, center_x: int, center_z: int, radius: int):
    jar = JARS_DIR / 'server.jar'
    config = load_config()
    java_cmd = config.get('java_cmd', 'java')
    jvm_args = shlex.split(config.get('jvm_args', _AIKAR_FLAGS))
    world_dir = WORLDS_DIR / world_name
    cancel_event = threading.Event()
    ready_event = threading.Event()
    proc: subprocess.Popen | None = None
    cfg: dict | None = None
    cancelled = False

    def log(msg: str):
        with _jobs_lock:
            _jobs[job_id]['log'].append(msg)

    def set_status(status: str, error: str | None = None):
        with _jobs_lock:
            if _jobs.get(job_id, {}).get('status') != 'running':
                return
            _jobs[job_id]['status'] = status
            if error:
                _jobs[job_id]['error'] = error
            _jobs[job_id]['finished_at'] = time.time()
        _prune_finished_jobs()

    try:
        if not world_dir.is_dir():
            raise RuntimeError(f'World "{world_name}" not found')
        if not jar.exists():
            raise RuntimeError('server.jar not found in jars/ directory')

        log('Downloading Chunky plugin (if needed)…')
        _ensure_chunky_plugin(world_dir)
        log('Chunky plugin ready.')

        (world_dir / 'eula.txt').write_text('eula=true\n')
        if (world_dir / 'server.properties').exists():
            _ensure_mc_internal_port(world_dir)
            _ensure_rcon(world_dir)
        else:
            raise RuntimeError('server.properties not found — start the server once first')

        level = get_level_name(world_dir)
        _remove_stale_session_lock(world_dir, level, log)

        log(f'Pre-generating world "{level}" — center ({center_x}, {center_z}), radius {radius} blocks')
        log('Works on existing saves — already-explored terrain is skipped.')
        log('Requires a Paper server jar (vanilla will not work).')
        log(f'Starting server: {java_cmd} -jar {jar.name} --nogui')

        proc = subprocess.Popen(
            [java_cmd, *jvm_args, '-jar', str(jar), '--nogui'],
            cwd=str(world_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with _pregen_handles_lock:
            _pregen_handles[job_id] = {'proc': proc, 'cancel': cancel_event, 'cfg': None}

        threading.Thread(
            target=_drain_process_output,
            args=(proc, log, ready_event),
            daemon=True,
        ).start()

        if not ready_event.wait(timeout=180):
            raise RuntimeError('Server timed out during startup')

        if cancel_event.is_set() or _job_cancel_requested(job_id):
            cancelled = True
            log('--- Cancelled before pre-generation started ---')
        else:
            log('Waiting for plugins to load…')
            if _pregen_sleep(4, job_id, cancel_event):
                cancelled = True
                log('--- Cancelled ---')
            else:
                cfg = _rcon_settings(world_dir)
                with _pregen_handles_lock:
                    if job_id in _pregen_handles:
                        _pregen_handles[job_id]['cfg'] = cfg
                if not cfg['enabled'] or not cfg['password']:
                    raise RuntimeError('RCON not configured')

                def rcon(cmd: str, *, chunky: bool = False) -> str:
                    if chunky:
                        lower, clean = _chunky_rcon(cfg, cmd)
                    else:
                        resp = _rcon_exec('localhost', cfg['port'], cfg['password'], cmd)
                        clean = re.sub(r'§.', '', resp).strip()
                        lower = clean.lower()
                    log(f'> {cmd}')
                    if clean:
                        log(clean)
                    return lower

                _chunky_reset_task(cfg, log)

                start_out = ''
                for cmd in (
                    f'chunky world {level}',
                    f'chunky center {center_x} {center_z}',
                    f'chunky radius {radius}',
                ):
                    if cancel_event.is_set() or _job_cancel_requested(job_id):
                        cancelled = True
                        log('--- Cancelled ---')
                        break
                    out = rcon(cmd)
                    if any(x in out for x in ('unknown command', 'not found', 'no such command')):
                        raise RuntimeError(
                            'Chunky command failed — install a Paper server jar from Update Server Version'
                        )

                if not cancelled:
                    start_out = rcon('chunky start', chunky=True)
                    if any(x in start_out for x in ('unknown command', 'not found', 'no such command')):
                        raise RuntimeError(
                            'Chunky command failed — install a Paper server jar from Update Server Version'
                        )
                    if 'already started' in start_out and 'confirm' not in start_out:
                        start_out = rcon('chunky confirm', chunky=True)

                if not cancelled:
                    try:
                        prog_out = rcon('chunky progress')
                    except RCONError as e:
                        raise RuntimeError(f'Could not read Chunky progress: {e}') from e

                    if 'no task' in prog_out:
                        raise RuntimeError(
                            'Chunky did not start — a leftover task from a previous run blocked it. '
                            'Run pre-gen again (the stale task has been cleared).'
                        )

                    pct = _chunky_progress_pct(prog_out)
                    saw_progress = pct is not None
                    if pct is not None and pct >= 100:
                        log('--- Pre-generation complete ---')
                    else:
                        log('--- Pre-generation running (this may take a while) ---')
                        while True:
                            if cancel_event.is_set() or _job_cancel_requested(job_id):
                                cancelled = True
                                log('--- Cancelled by user ---')
                                break
                            if _pregen_sleep(8, job_id, cancel_event):
                                cancelled = True
                                log('--- Cancelled by user ---')
                                break
                            try:
                                prog_out = rcon('chunky progress')
                            except RCONError as e:
                                log(f'RCON error: {e}')
                                break
                            pct = _chunky_progress_pct(prog_out)
                            if pct is not None:
                                saw_progress = True
                                if pct >= 100:
                                    log('--- Pre-generation complete ---')
                                    break
                            if 'no task' in prog_out:
                                if saw_progress:
                                    log('--- Pre-generation complete ---')
                                else:
                                    raise RuntimeError(
                                        'Chunky reported no running task — generation did not start'
                                    )
                                break
                            if any(x in prog_out for x in ('finished', 'complete')):
                                log('--- Pre-generation complete ---')
                                break

    except Exception as e:
        set_status('error', str(e))
    else:
        if cancelled:
            set_status('cancelled')
        else:
            set_status('done')
    finally:
        if proc is not None and proc.poll() is None:
            _stop_background_mc(proc, cfg, log, cancelled=cancelled)
        with _pregen_handles_lock:
            _pregen_handles.pop(job_id, None)


@app.route('/api/worlds/<name>/pregen', methods=['POST'])
@require_auth
def start_pregen(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404

    if _managed_server_running():
        return jsonify({'error': 'Stop the running server before pre-generating'}), 400

    data = request.get_json() or {}
    try:
        center_x = int(data.get('center_x', 0))
        center_z = int(data.get('center_z', 0))
        radius = int(data.get('radius', 1000))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid coordinates or radius'}), 400

    if radius < 100 or radius > 10000:
        return jsonify({'error': 'Radius must be between 100 and 10000 blocks'}), 400

    job_id = _try_reserve_job(name, 'pregen')
    if not job_id:
        if _managed_server_running():
            return jsonify({'error': 'Stop the running server before pre-generating'}), 400
        return jsonify({
            'error': 'Pre-generation or another background server task is already running',
        }), 400

    t = threading.Thread(
        target=_run_pregen,
        args=(job_id, name, center_x, center_z, radius),
        daemon=True,
    )
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/worlds/<name>/pregen/<job_id>/cancel', methods=['POST'])
@require_auth
def cancel_pregen(name, job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get('world') != name or job.get('type') != 'pregen':
            return jsonify({'error': 'Job not found'}), 404
        if job['status'] != 'running':
            return jsonify({'error': 'Job is not running'}), 400
    _request_job_cancel(job_id)
    return jsonify({'ok': True})


@app.route('/api/worlds/<name>/pregen/<job_id>')
@require_auth
def pregen_status(name, job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get('world') != name:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


# ─── Detect host ─────────────────────────────────────────────────────────────

@app.route('/api/detect-host')
@require_auth
def detect_host():
    return jsonify({'host': get_local_ip()})


# ─── RCON endpoints ───────────────────────────────────────────────────────────

@app.route('/api/worlds/<name>/rcon/players')
@require_auth
def rcon_players(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    cfg = _rcon_settings(world_dir)
    if not cfg['enabled']:
        return jsonify({'error': 'RCON is not enabled — add enable-rcon=true to server.properties'}), 400
    if not cfg['password']:
        return jsonify({'error': 'rcon.password is not set in server.properties'}), 400
    try:
        raw = _rcon_exec('localhost', cfg['port'], cfg['password'], 'list')
        clean = re.sub(r'§.', '', raw)
        players: list[str] = []
        m = re.search(r'players online:\s*(.*)', clean)
        if m:
            names = m.group(1).strip()
            if names:
                players = [n.strip() for n in names.split(',') if n.strip()]
        return jsonify({'players': players})
    except RCONError as e:
        return jsonify({'error': str(e)}), 503


@app.route('/api/worlds/<name>/rcon/give', methods=['POST'])
@require_auth
def rcon_give(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    data = request.get_json() or {}
    player = data.get('player', '').strip()
    painting = data.get('painting', '').strip()
    if not player or not painting:
        return jsonify({'error': 'player and painting required'}), 400
    if not re.match(r'^[a-zA-Z0-9_]{1,16}$', player):
        return jsonify({'error': 'Invalid player name'}), 400
    if not re.match(r'^[a-z0-9_]+$', painting):
        return jsonify({'error': 'Invalid painting identifier'}), 400
    cfg = _rcon_settings(world_dir)
    if not cfg['enabled'] or not cfg['password']:
        return jsonify({'error': 'RCON not configured'}), 400
    try:
        cmd = f'give {player} minecraft:painting[minecraft:painting_variant={PAINTINGS_NS}:{painting}]'
        raw = _rcon_exec('localhost', cfg['port'], cfg['password'], cmd)
        if 'Unknown item component' in raw:
            cmd = (
                f'give {player} minecraft:painting[minecraft:entity_data='
                f'{{id:"minecraft:painting",variant:"{PAINTINGS_NS}:{painting}"}}]'
            )
            raw = _rcon_exec('localhost', cfg['port'], cfg['password'], cmd)
        return jsonify({'ok': True, 'response': re.sub(r'§.', '', raw)})
    except RCONError as e:
        return jsonify({'error': str(e)}), 503


@app.route('/api/worlds/<name>/ensure-rcon', methods=['POST'])
@require_auth
def ensure_rcon_endpoint(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    if _world_dir_busy(name):
        return jsonify({'error': 'A background job is running for this world'}), 400
    if not (world_dir / 'server.properties').exists():
        return jsonify({'error': 'server.properties not found — start the server once to generate it'}), 404
    _ensure_rcon(world_dir)
    cfg = _rcon_settings(world_dir)
    return jsonify({'ok': True, 'rcon_port': cfg['port']})


@app.route('/api/worlds/<name>/ops')
@require_auth
def list_ops(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    ops_file = world_dir / 'ops.json'
    if not ops_file.exists():
        return jsonify([])
    try:
        ops = json.loads(ops_file.read_text())
        return jsonify([
            {'uuid': o.get('uuid', ''), 'name': o.get('name', ''), 'level': o.get('level', 4)}
            for o in ops if o.get('name')
        ])
    except Exception:
        return jsonify([])


@app.route('/api/worlds/<name>/rcon/op', methods=['POST'])
@require_auth
def rcon_op(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    data = request.get_json() or {}
    player = data.get('player', '').strip()
    if not re.match(r'^[a-zA-Z0-9_]{1,16}$', player):
        return jsonify({'error': 'Invalid player name'}), 400
    cfg = _rcon_settings(world_dir)
    if not cfg['enabled'] or not cfg['password']:
        return jsonify({'error': 'RCON not configured'}), 400
    try:
        raw = _rcon_exec('localhost', cfg['port'], cfg['password'], f'op {player}')
        return jsonify({'ok': True, 'response': re.sub(r'§.', '', raw)})
    except RCONError as e:
        return jsonify({'error': str(e)}), 503


@app.route('/api/worlds/<name>/rcon/deop', methods=['POST'])
@require_auth
def rcon_deop(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    data = request.get_json() or {}
    player = data.get('player', '').strip()
    if not re.match(r'^[a-zA-Z0-9_]{1,16}$', player):
        return jsonify({'error': 'Invalid player name'}), 400
    cfg = _rcon_settings(world_dir)
    if not cfg['enabled'] or not cfg['password']:
        return jsonify({'error': 'RCON not configured'}), 400
    try:
        raw = _rcon_exec('localhost', cfg['port'], cfg['password'], f'deop {player}')
        return jsonify({'ok': True, 'response': re.sub(r'§.', '', raw)})
    except RCONError as e:
        return jsonify({'error': str(e)}), 503


@app.route('/api/worlds/<name>/rcon/exec', methods=['POST'])
@require_auth
def rcon_exec_endpoint(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    data = request.get_json() or {}
    command = data.get('command', '').strip()
    if not command:
        return jsonify({'error': 'command required'}), 400
    cfg = _rcon_settings(world_dir)
    if not cfg['enabled'] or not cfg['password']:
        return jsonify({'error': 'RCON not configured'}), 400
    try:
        raw = _rcon_exec('localhost', cfg['port'], cfg['password'], command)
        return jsonify({'ok': True, 'response': re.sub(r'§.', '', raw)})
    except RCONError as e:
        return jsonify({'error': str(e)}), 503


# ─── Resource pack ───────────────────────────────────────────────────────────

@app.route('/resourcepack/<path:name>')
def serve_resource_pack(name):
    if name.endswith('.zip'):
        name = name[:-4]
    try:
        world_dir = safe_child(WORLDS_DIR, name)
    except ValueError:
        return 'Not found', 404
    if not world_dir.is_dir():
        return 'Not found', 404
    return _pack_file_response(world_dir)


@app.route('/api/worlds/<name>/resource-pack-info', methods=['GET'])
@require_auth
def resource_pack_info(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    props = world_dir / 'server.properties'
    url = sha1 = ''
    if props.exists():
        for line in props.read_text().splitlines():
            if line.startswith('resource-pack='):
                url = line.split('=', 1)[1].strip()
            elif line.startswith('resource-pack-sha1='):
                sha1 = line.split('=', 1)[1].strip()
    config = load_config()
    host = config.get('server_host', '').strip() or get_local_ip()
    loopback = _resource_pack_url(name, '127.0.0.1').replace('https://', 'http://')
    cached = world_dir / '.resource_pack.zip'
    return jsonify({
        'url': url,
        'sha1': sha1,
        'expected_url': _resource_pack_url(name, host) if host else '',
        'scheme': _resource_pack_scheme(host) if host else '',
        'cached_bytes': cached.stat().st_size if cached.exists() else 0,
        'image_count': sum(1 for f in _paintings_dir(world_dir).iterdir() if _is_image_file(f))
            if _paintings_dir(world_dir).exists() else 0,
        'local_test': _test_resource_pack_url(loopback),
        'curl_test': f'curl -I {loopback}',
    })


# ─── Server icon API ─────────────────────────────────────────────────────────

@app.route('/api/server-icon', methods=['GET'])
@require_auth
def get_server_icon():
    if not SERVER_ICON_FILE.is_file():
        return jsonify({'error': 'No server icon set'}), 404
    return send_file(SERVER_ICON_FILE, mimetype='image/png')


@app.route('/api/server-icon', methods=['POST'])
@require_auth
def upload_server_icon():
    file = request.files.get('icon')
    if not file or not file.filename:
        return jsonify({'error': 'No file provided'}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in _IMAGE_EXTS:
        return jsonify({'error': 'Only PNG/JPEG images allowed'}), 400
    if not _icon_lock.acquire(blocking=False):
        return jsonify({'error': 'Server icon update already in progress'}), 409
    try:
        _save_server_icon_file(file.stream)
        _apply_server_icon_all()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': f'Invalid image: {e}'}), 400
    finally:
        _icon_lock.release()


@app.route('/api/server-icon', methods=['DELETE'])
@require_auth
def delete_server_icon():
    if not _icon_lock.acquire(blocking=False):
        return jsonify({'error': 'Server icon update already in progress'}), 409
    try:
        if SERVER_ICON_FILE.is_file():
            SERVER_ICON_FILE.unlink()
        _remove_server_icon_all()
        return jsonify({'ok': True})
    finally:
        _icon_lock.release()


# ─── Config ───────────────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
@require_auth
def get_config_endpoint():
    config = load_config()
    return jsonify({
        'server_host': config.get('server_host', ''),
        'public_port': _public_port(),
        'jvm_args': config.get('jvm_args', _AIKAR_FLAGS),
        'has_server_icon': SERVER_ICON_FILE.is_file(),
    })


@app.route('/api/config', methods=['POST'])
@require_auth
def save_config_endpoint():
    if not _config_lock.acquire(blocking=False):
        return jsonify({'error': 'Settings save already in progress'}), 409
    try:
        data = request.get_json() or {}
        config = load_config()
        if 'server_host' in data:
            config['server_host'] = data['server_host'].strip()
        if 'public_port' in data:
            try:
                port = int(data['public_port'])
                if port < 1 or port > 65535:
                    raise ValueError
                config['public_port'] = port
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid port'}), 400
        if 'jvm_args' in data:
            config['jvm_args'] = str(data['jvm_args']).strip()
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
        rebuilt = rebuild_paintings_all()
        return jsonify({'ok': True, 'rebuilt_worlds': rebuilt})
    finally:
        _config_lock.release()


# ─── Jars ────────────────────────────────────────────────────────────────────

@app.route('/api/jars')
@require_auth
def list_jars():
    if not JARS_DIR.exists():
        return jsonify([])
    return jsonify([
        {
            'name': j.name,
            'size': j.stat().st_size,
            'modified': datetime.fromtimestamp(j.stat().st_mtime).isoformat(),
        }
        for j in sorted(JARS_DIR.glob('*.jar'))
    ])


@app.route('/api/jars/update', methods=['POST'])
@require_auth
def update_jar():
    if _mc_port_busy():
        return jsonify({'error': 'Stop the server and background tasks before updating the jar'}), 400

    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url.startswith('https://'):
        return jsonify({'error': 'URL must begin with https://'}), 400

    JARS_DIR.mkdir(exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jar.tmp', dir=JARS_DIR) as tmp:
            tmp_path = Path(tmp.name)
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=65536):
                    tmp.write(chunk)

        final = JARS_DIR / 'server.jar'
        tmp_path.replace(final)
        tmp_path = None
        for old in JARS_DIR.glob('*.jar'):
            if old != final:
                old.unlink(missing_ok=True)
        return jsonify({'ok': True, 'name': 'server.jar', 'size': final.stat().st_size})
    except Exception as e:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return jsonify({'error': str(e)}), 500


# ─── Server MOTD / whitelist / logs ──────────────────────────────────────────

@app.route('/api/server/motd', methods=['GET'])
@require_auth
def get_motd():
    world_dir = _active_world_dir()
    if not world_dir:
        return jsonify({'error': 'No active world'}), 404
    if not (world_dir / 'server.properties').exists():
        return jsonify({'motd': ''})
    return jsonify({'motd': _read_prop(world_dir, 'motd')})


@app.route('/api/server/motd', methods=['POST'])
@require_auth
def save_motd():
    world_dir = _active_world_dir()
    if not world_dir:
        return jsonify({'error': 'No active world'}), 404
    if _world_dir_busy(world_dir.name):
        return jsonify({'error': 'A background job is running for the active world'}), 400
    if not (world_dir / 'server.properties').exists():
        return jsonify({'error': 'server.properties not found'}), 404
    motd = (request.get_json() or {}).get('motd', '')
    if not isinstance(motd, str):
        return jsonify({'error': 'Invalid MOTD'}), 400
    _update_server_properties(world_dir, {'motd': motd.strip()})
    return jsonify({'ok': True})


@app.route('/api/server/whitelist', methods=['GET'])
@require_auth
def get_whitelist():
    world_dir = _active_world_dir()
    if not world_dir:
        return jsonify({'error': 'No active world'}), 404
    players = [
        {'name': e.get('name', ''), 'uuid': e.get('uuid', '')}
        for e in _read_whitelist(world_dir) if e.get('name')
    ]
    return jsonify({'enabled': _whitelist_enabled(world_dir), 'players': players})


@app.route('/api/server/whitelist', methods=['POST'])
@require_auth
def set_whitelist_enabled():
    world_dir = _active_world_dir()
    if not world_dir:
        return jsonify({'error': 'No active world'}), 404
    data = request.get_json() or {}
    enabled = bool(data.get('enabled', False))
    _set_whitelist_enabled(world_dir, enabled)
    cmd = 'whitelist on' if enabled else 'whitelist off'
    _rcon_on_active(cmd)
    return jsonify({'ok': True, 'enabled': enabled})


@app.route('/api/server/whitelist/add', methods=['POST'])
@require_auth
def whitelist_add():
    world_dir = _active_world_dir()
    if not world_dir:
        return jsonify({'error': 'No active world'}), 404
    player = (request.get_json() or {}).get('name', '').strip()
    if not re.match(r'^[a-zA-Z0-9_]{1,16}$', player):
        return jsonify({'error': 'Invalid player name'}), 400
    entries = _read_whitelist(world_dir)
    if any(e.get('name', '').lower() == player.lower() for e in entries):
        return jsonify({'error': 'Player already whitelisted'}), 409
    if _rcon_on_active(f'whitelist add {player}') is not None:
        entries = _read_whitelist(world_dir)
    else:
        entries.append({'uuid': _offline_player_uuid(player), 'name': player})
        _write_whitelist(world_dir, entries)
    return jsonify({'ok': True})


@app.route('/api/server/whitelist/<player>', methods=['DELETE'])
@require_auth
def whitelist_remove(player):
    world_dir = _active_world_dir()
    if not world_dir:
        return jsonify({'error': 'No active world'}), 404
    if not re.match(r'^[a-zA-Z0-9_]{1,16}$', player):
        return jsonify({'error': 'Invalid player name'}), 400
    if _rcon_on_active(f'whitelist remove {player}') is not None:
        pass
    else:
        entries = [e for e in _read_whitelist(world_dir) if e.get('name', '').lower() != player.lower()]
        _write_whitelist(world_dir, entries)
    return jsonify({'ok': True})


@app.route('/api/server/logs')
@require_auth
def get_server_logs():
    world_dir = _active_world_dir()
    if not world_dir:
        return jsonify({'error': 'No active world'}), 404
    log_file = world_dir / 'logs' / 'latest.log'
    if not log_file.exists():
        return jsonify({'content': '', 'path': str(log_file.relative_to(BASE_DIR))})
    try:
        lines = int(request.args.get('lines', 200))
        lines = max(50, min(lines, 2000))
    except ValueError:
        lines = 200
    content = log_file.read_text(errors='replace')
    tail = '\n'.join(content.splitlines()[-lines:])
    return jsonify({'content': tail, 'path': str(log_file.relative_to(BASE_DIR))})


# ─── Server process management ───────────────────────────────────────────────

@app.route('/api/server/status')
@require_auth
def get_server_status():
    global _server_proc, _server_start_time
    config = load_config()
    active_world = config.get('active_world')

    with _server_lock:
        proc = _server_proc
        start_time = _server_start_time
        if proc is not None and proc.poll() is not None:
            _server_proc = None
            _server_start_time = None
            proc = None
            start_time = None

    managed_running = proc is not None
    uptime = int(time.time() - start_time) if (managed_running and start_time) else None

    mc_internal = _mc_internal_port()
    public_port = _public_port()

    ping = None
    try:
        ping = _mc_status_ping('localhost', mc_internal)
    except Exception:
        pass

    port_active = ping is not None
    bg_job = _active_background_mc_job()

    if managed_running and not port_active:
        state = 'starting'
    elif managed_running and port_active:
        state = 'running'
    elif bg_job and port_active:
        state = 'pregen' if bg_job['type'] == 'pregen' else 'generating'
    elif port_active:
        state = 'running'
    else:
        state = 'stopped'

    display_world = active_world
    if bg_job and bg_job.get('world'):
        display_world = bg_job['world']

    metrics = _proc_metrics(proc.pid) if managed_running else None
    addr = _server_address()

    return jsonify({
        'state': state,
        'world': display_world,
        'background_job': bg_job,
        'uptime': uptime,
        'server_address': addr or None,
        'players_online': ping['players_online'] if ping else None,
        'players_max': ping['players_max'] if ping else None,
        'version': ping['version'] if ping else None,
        'motd': ping['motd'] if ping else None,
        'metrics': metrics,
    })


@app.route('/api/server/start', methods=['POST'])
@require_auth
def start_server():
    global _server_proc, _server_start_time
    with _server_lock:
        if _server_proc is not None and _server_proc.poll() is None:
            return jsonify({'error': 'Server is already running'}), 400
        if _background_mc_jobs_running():
            return jsonify({'error': 'A background server task is running — wait for it to finish'}), 400

    config = load_config()
    active_world = config.get('active_world')
    if not active_world:
        return jsonify({'error': 'No active world set'}), 400

    world_dir = WORLDS_DIR / active_world
    if not world_dir.is_dir():
        return jsonify({'error': 'Active world directory not found'}), 404

    jar = JARS_DIR / 'server.jar'
    if not jar.exists():
        return jsonify({'error': 'server.jar not found in jars/ directory'}), 400

    (world_dir / 'eula.txt').write_text('eula=true\n')
    _apply_server_icon(world_dir)
    if (world_dir / 'server.properties').exists():
        _ensure_mc_internal_port(world_dir)
        _ensure_rcon(world_dir)
    java_cmd = config.get('java_cmd', 'java')
    jvm_args = shlex.split(config.get('jvm_args', _AIKAR_FLAGS))

    try:
        proc = subprocess.Popen(
            [java_cmd, *jvm_args, '-jar', str(jar), '--nogui'],
            cwd=str(world_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    with _server_lock:
        if _server_proc is not None and _server_proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return jsonify({'error': 'Server is already running'}), 400
        if _background_mc_jobs_running():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return jsonify({'error': 'A background server task started — try again'}), 400
        _server_proc = proc
        _server_start_time = time.time()
    return jsonify({'ok': True})


@app.route('/api/server/stop', methods=['POST'])
@require_auth
def stop_server():
    global _server_proc, _server_start_time
    if _background_mc_jobs_running():
        return jsonify({
            'error': 'A background server task is running — cancel or wait for it to finish first',
        }), 400

    config = load_config()
    active_world = config.get('active_world')

    rcon_sent = False
    if active_world:
        world_dir = WORLDS_DIR / active_world
        cfg = _rcon_settings(world_dir)
        if cfg['enabled'] and cfg['password']:
            try:
                _rcon_exec('localhost', cfg['port'], cfg['password'], 'stop')
                rcon_sent = True
            except Exception:
                pass

    with _server_lock:
        proc = _server_proc
        _server_proc = None
        _server_start_time = None

    if proc is not None and proc.poll() is None:
        if not rcon_sent:
            proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    return jsonify({'ok': True})


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    JARS_DIR.mkdir(exist_ok=True)
    WORLDS_DIR.mkdir(exist_ok=True)
    cfg = load_config()
    public = _public_port()
    internal = _mc_internal_port()
    host = cfg.get('server_host', '').strip()
    tls_ctx = None
    if cfg.get('resource_pack_https') and host and not _is_private_host(host):
        try:
            tls_ctx = ensure_tls_context(BASE_DIR / '.certs', host)
            print(f'TLS enabled for resource packs (host: {host})')
        except Exception as e:
            print(f'Warning: TLS cert generation failed ({e}) — HTTPS packs may not work')
    if public in (FLASK_PORT, PACK_PORT, internal):
        raise SystemExit(
            f'Config error: public_port ({public}) conflicts with internal ports. '
            f'Use {DEFAULT_PUBLIC_PORT} for players and delete config.json to reset.'
        )
    start_port_proxy(
        public_port=public,
        mc_port=internal,
        flask_host=FLASK_HOST,
        flask_port=FLASK_PORT,
        pack_port=PACK_PORT,
        ssl_context=tls_ctx,
        worlds_dir=WORLDS_DIR,
    )
    print(f'Public port {public}: Minecraft + resource packs + web UI')
    print(f'Minecraft binds internally on port {internal}')
    print(f'Flask (internal): {FLASK_HOST}:{FLASK_PORT}')
    if host:
        print(f'Resource packs: {_resource_pack_scheme(host)}://{host}:{public}/resourcepack/<world>.zip')
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, threaded=True)
