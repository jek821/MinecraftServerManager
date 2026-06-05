import hashlib
import io
import json
import os
import re
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
from flask import Flask, jsonify, render_template, request, send_file, session
from PIL import Image as PILImage

app = Flask(__name__)

# BASE_DIR must be defined before _load_secret_key so the .secret_key path resolves
BASE_DIR = Path(__file__).resolve().parent.parent
WORLDS_DIR = BASE_DIR / 'worldFiles'
JARS_DIR = BASE_DIR / 'jars'
CONFIG_FILE = BASE_DIR / 'config.json'
PAINTINGS_DIR = BASE_DIR / 'paintings'

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

_server_proc: 'subprocess.Popen | None' = None
_server_start_time: float | None = None
_server_lock = threading.Lock()
_rcon_write_lock = threading.Lock()
_props_write_lock = threading.Lock()


# ─── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {'active_world': None, 'java_cmd': 'java'}


def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


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


def _build_resource_pack_zip() -> bytes:
    """Build the resource-pack zip in memory from the global paintings directory."""
    paintings_dir = PAINTINGS_DIR
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('pack.mcmeta', _RP_MCMETA)
        if paintings_dir.exists():
            for img_path in sorted(paintings_dir.iterdir()):
                if not img_path.is_file():
                    continue
                if img_path.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
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


def _rebuild_datapack(world_dir: Path) -> None:
    """Recreate the mc-paintings data pack inside the world level's datapacks/ folder."""
    level_name = get_level_name(world_dir)
    dp_root = world_dir / level_name / 'datapacks' / 'mc-paintings'
    if dp_root.exists():
        shutil.rmtree(dp_root)

    variant_dir = dp_root / 'data' / PAINTINGS_NS / 'painting_variant'
    variant_dir.mkdir(parents=True)
    (dp_root / 'pack.mcmeta').write_text(_DP_MCMETA)

    if PAINTINGS_DIR.exists():
        for img_path in sorted(PAINTINGS_DIR.iterdir()):
            if not img_path.is_file():
                continue
            if img_path.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
                continue
            stem = _painting_stem(img_path.name)
            w, h = _image_block_dims(img_path)
            (variant_dir / f'{stem}.json').write_text(json.dumps({
                "asset_id": f"{PAINTINGS_NS}:{stem}",
                "width": w,
                "height": h,
            }, indent=2))


def _update_server_properties_pack(world_dir: Path, url: str, sha1: str) -> None:
    props_file = world_dir / 'server.properties'
    with _props_write_lock:
        if not props_file.exists():
            return
        updates = {'resource-pack': url, 'resource-pack-sha1': sha1}
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


def rebuild_paintings(world_dir: Path) -> None:
    """Full pipeline: data pack → resource pack zip → server.properties for one world."""
    _rebuild_datapack(world_dir)

    pack_bytes = _build_resource_pack_zip()
    sha1 = hashlib.sha1(pack_bytes).hexdigest()

    # Cache on disk so /resourcepack/<name> can serve it cheaply
    (world_dir / '.resource_pack.zip').write_bytes(pack_bytes)

    config = load_config()
    host = config.get('server_host', '').strip()
    if not host:
        host = get_local_ip()
    if host:
        port = config.get('port', 5000)
        url = f'http://{host}:{port}/resourcepack/{world_dir.name}'
        _update_server_properties_pack(world_dir, url, sha1)

    _ensure_rcon(world_dir)


def rebuild_paintings_all() -> None:
    """Rebuild paintings for every world so all data packs stay in sync."""
    if not WORLDS_DIR.exists():
        return
    for world_dir in WORLDS_DIR.iterdir():
        if world_dir.is_dir():
            try:
                rebuild_paintings(world_dir)
            except Exception:
                pass


# ─── Auth ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if not session.get('authenticated'):
        return render_template('login.html', server_name=SERVER_NAME)
    return render_template('index.html', server_name=SERVER_NAME)


@app.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    if data.get('password') == PASSWORD:
        session['authenticated'] = True
        return jsonify({'ok': True})
    return jsonify({'error': 'Invalid password'}), 401


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
    config = load_config()
    config['active_world'] = name
    save_config(config)
    return jsonify({'ok': True})


@app.route('/api/worlds/<name>/download')
@require_auth
def download_world(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fp in world_dir.rglob('*'):
            if fp.is_file():
                zf.write(fp, fp.relative_to(WORLDS_DIR))
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'{name}.zip')


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
    props = safe_child(WORLDS_DIR, name) / 'server.properties'
    if not props.exists():
        return jsonify({'error': 'server.properties not found'}), 404
    data = request.get_json() or {}
    props.write_text(data.get('content', ''))
    return jsonify({'ok': True})


@app.route('/api/worlds/<name>/images', methods=['GET'])
@require_auth
def list_images(name):
    if not PAINTINGS_DIR.exists():
        return jsonify([])
    return jsonify([
        {'name': f.name, 'size': f.stat().st_size}
        for f in sorted(PAINTINGS_DIR.iterdir())
        if f.is_file() and f.suffix.lower() in ('.png', '.jpg', '.jpeg')
    ])


@app.route('/api/worlds/<name>/images', methods=['POST'])
@require_auth
def upload_image(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    file = request.files.get('image')
    if not file or not file.filename:
        return jsonify({'error': 'No file provided'}), 400
    original = Path(file.filename).name
    ext = Path(original).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg'):
        return jsonify({'error': 'Only PNG/JPEG images allowed'}), 400
    filename = Path(original).stem + ext  # normalise extension to lowercase
    PAINTINGS_DIR.mkdir(exist_ok=True)
    file.save(PAINTINGS_DIR / filename)
    rebuild_paintings_all()
    w, h = _image_block_dims(PAINTINGS_DIR / filename)
    return jsonify({'ok': True, 'name': filename, 'width_blocks': w, 'height_blocks': h})


@app.route('/api/worlds/<name>/images/<filename>', methods=['DELETE'])
@require_auth
def delete_image(name, filename):
    img = PAINTINGS_DIR / Path(filename).name
    if not img.is_file():
        return jsonify({'error': 'Image not found'}), 404
    img.unlink()
    rebuild_paintings_all()
    return jsonify({'ok': True})


@app.route('/api/worlds/<name>', methods=['DELETE'])
@require_auth
def delete_world(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    shutil.rmtree(world_dir)
    config = load_config()
    if config.get('active_world') == name:
        config['active_world'] = None
        save_config(config)
    return jsonify({'ok': True})


@app.route('/api/worlds/<name>/rename', methods=['POST'])
@require_auth
def rename_world(name):
    world_dir = safe_child(WORLDS_DIR, name)
    if not world_dir.is_dir():
        return jsonify({'error': 'World not found'}), 404
    data = request.get_json() or {}
    new_name = data.get('new_name', '').strip()
    if not valid_world_name(new_name):
        return jsonify({'error': 'Invalid world name'}), 400
    new_dir = WORLDS_DIR / new_name
    if new_dir.exists():
        return jsonify({'error': 'A world with that name already exists'}), 409
    world_dir.rename(new_dir)
    config = load_config()
    if config.get('active_world') == name:
        config['active_world'] = new_name
        save_config(config)
    return jsonify({'ok': True, 'new_name': new_name})


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
    try:
        data = file.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Detect whether the zip has a single top-level folder (common for world exports)
            names = zf.namelist()
            top = {n.split('/')[0] for n in names if n.strip('/')}
            if len(top) == 1:
                prefix = next(iter(top)) + '/'
                world_dir.mkdir(parents=True)
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
                world_dir.mkdir(parents=True)
                zf.extractall(world_dir)
        (world_dir / 'eula.txt').write_text('eula=true\n')
        return jsonify({'ok': True, 'name': world_name})
    except Exception as e:
        if world_dir.exists():
            shutil.rmtree(world_dir, ignore_errors=True)
        return jsonify({'error': str(e)}), 500


# ─── World Generation ─────────────────────────────────────────────────────────

def _run_generate(job_id: str, new_name: str, inherit_properties: bool, old_active: str | None):
    jar = JARS_DIR / 'server.jar'
    config = load_config()
    java_cmd = config.get('java_cmd', 'java')

    def log(msg: str):
        with _jobs_lock:
            _jobs[job_id]['log'].append(msg)

    def set_status(status: str, error: str | None = None):
        with _jobs_lock:
            _jobs[job_id]['status'] = status
            if error:
                _jobs[job_id]['error'] = error

    new_dir = WORLDS_DIR / new_name
    try:
        if new_dir.exists():
            raise RuntimeError(f'A world named "{new_name}" already exists')

        new_dir.mkdir(parents=True)
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

        if not jar.exists():
            raise RuntimeError('server.jar not found in jars/ directory')

        log(f'Starting server: {java_cmd} -jar {jar.name} --nogui')
        proc = subprocess.Popen(
            [java_cmd, '-jar', str(jar), '--nogui'],
            cwd=str(new_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        assert proc.stdout and proc.stdin
        world_done = False
        for line in proc.stdout:
            line = line.rstrip()
            log(line)
            if 'Done (' in line:
                world_done = True
                log('--- World generated. Sending stop command... ---')
                try:
                    proc.stdin.write('stop\n')
                    proc.stdin.flush()
                except OSError:
                    proc.terminate()
                break

        # Drain remaining output after stop
        for line in proc.stdout:
            log(line.rstrip())

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError('Server took too long to stop after world generation')

        if not world_done:
            raise RuntimeError('Server exited before world generation completed')

        # Mark new world as active
        config = load_config()
        config['active_world'] = new_name
        save_config(config)
        log(f'Set "{new_name}" as active world.')

        set_status('done')

    except Exception as e:
        set_status('error', str(e))
        if new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)


@app.route('/api/worlds/generate', methods=['POST'])
@require_auth
def start_generate():
    data = request.get_json() or {}
    new_name = data.get('new_name', '').strip()
    inherit_properties = bool(data.get('inherit_properties', True))

    if not valid_world_name(new_name):
        return jsonify({'error': 'Invalid world name'}), 400

    config = load_config()
    old_active = config.get('active_world')

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {'status': 'running', 'log': [], 'error': None}

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
        # 1.20.5–1.21.4: dedicated painting_variant item component
        cmd = f'give {player} minecraft:painting[minecraft:painting_variant={PAINTINGS_NS}:{painting}]'
        raw = _rcon_exec('localhost', cfg['port'], cfg['password'], cmd)
        if 'Unknown item component' in raw:
            # 1.21.5+: component removed; specify variant via entity NBT
            cmd = f'give {player} minecraft:painting[minecraft:entity_data={{id:"minecraft:painting",variant:"{PAINTINGS_NS}:{painting}"}}]'
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

@app.route('/resourcepack/<name>')
def serve_resource_pack(name):
    try:
        world_dir = safe_child(WORLDS_DIR, name)
    except ValueError:
        return 'Not found', 404
    if not world_dir.is_dir():
        return 'Not found', 404
    cached = world_dir / '.resource_pack.zip'
    if cached.exists():
        return send_file(cached, mimetype='application/zip',
                         as_attachment=True, download_name=f'{name}_paintings.zip')
    # Generate on the fly if cache is missing
    data = _build_resource_pack_zip()
    return send_file(io.BytesIO(data), mimetype='application/zip',
                     as_attachment=True, download_name=f'{name}_paintings.zip')


# ─── Config ───────────────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET'])
@require_auth
def get_config_endpoint():
    config = load_config()
    return jsonify({
        'server_host': config.get('server_host', ''),
        'port': config.get('port', 5000),
    })


@app.route('/api/config', methods=['POST'])
@require_auth
def save_config_endpoint():
    data = request.get_json() or {}
    config = load_config()
    if 'server_host' in data:
        config['server_host'] = data['server_host'].strip()
    if 'port' in data:
        try:
            config['port'] = int(data['port'])
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid port'}), 400
    save_config(config)
    return jsonify({'ok': True})


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
        tmp_path.rename(final)
        tmp_path = None
        for old in JARS_DIR.glob('*.jar'):
            if old != final:
                old.unlink(missing_ok=True)
        return jsonify({'ok': True, 'name': 'server.jar', 'size': final.stat().st_size})
    except Exception as e:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return jsonify({'error': str(e)}), 500


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

    mc_port = 25565
    if active_world:
        try:
            props_file = WORLDS_DIR / active_world / 'server.properties'
            if props_file.exists():
                for line in props_file.read_text().splitlines():
                    if line.startswith('server-port='):
                        mc_port = int(line.split('=', 1)[1].strip())
                        break
        except Exception:
            pass

    ping = None
    try:
        ping = _mc_status_ping('localhost', mc_port)
    except Exception:
        pass

    running = ping is not None
    if managed_running and not running:
        state = 'starting'
    elif running:
        state = 'running'
    else:
        state = 'stopped'

    metrics = _proc_metrics(proc.pid) if managed_running else None

    return jsonify({
        'state': state,
        'world': active_world,
        'uptime': uptime,
        'mc_port': mc_port,
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
    if (world_dir / 'server.properties').exists():
        _ensure_rcon(world_dir)
    java_cmd = config.get('java_cmd', 'java')

    try:
        proc = subprocess.Popen(
            [java_cmd, '-jar', str(jar), '--nogui'],
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
            return jsonify({'error': 'Server is already running'}), 400
        _server_proc = proc
        _server_start_time = time.time()
    return jsonify({'ok': True})


@app.route('/api/server/stop', methods=['POST'])
@require_auth
def stop_server():
    global _server_proc, _server_start_time
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
        if proc is not None and proc.poll() is None and not rcon_sent:
            proc.terminate()

    return jsonify({'ok': True})


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    JARS_DIR.mkdir(exist_ok=True)
    WORLDS_DIR.mkdir(exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
