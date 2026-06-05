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
import uuid
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_file, session
from PIL import Image as PILImage

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())

BASE_DIR = Path('/home/jemanuel/minecraftServerManager')
WORLDS_DIR = BASE_DIR / 'worldFiles'
JARS_DIR = BASE_DIR / 'jars'
CONFIG_FILE = BASE_DIR / 'config.json'
PASSWORD = os.environ.get('MC_PASSWORD', 'admin')

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
    if not str(resolved).startswith(str(base.resolve()) + os.sep) and resolved != base.resolve():
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
    return Path(filename).stem.lower().replace(' ', '_').replace('-', '_')


def _image_block_dims(path: Path) -> tuple[int, int]:
    """Return (width_blocks, height_blocks) from an image file. 16 px = 1 block."""
    try:
        with PILImage.open(path) as img:
            w, h = img.size
        return max(1, min(16, w // 16)), max(1, min(16, h // 16))
    except Exception:
        return 1, 1


def _build_resource_pack_zip(world_dir: Path) -> bytes:
    """Build the resource-pack zip in memory and return the raw bytes."""
    paintings_dir = world_dir / 'paintings'
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
    """Recreate the mc-paintings data pack inside the world's datapacks/ folder."""
    dp_root = world_dir / 'datapacks' / 'mc-paintings'
    if dp_root.exists():
        shutil.rmtree(dp_root)

    variant_dir = dp_root / 'data' / PAINTINGS_NS / 'painting_variant'
    variant_dir.mkdir(parents=True)
    (dp_root / 'pack.mcmeta').write_text(_DP_MCMETA)

    paintings_dir = world_dir / 'paintings'
    if paintings_dir.exists():
        for img_path in sorted(paintings_dir.iterdir()):
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
    """Full pipeline: data pack → resource pack zip → server.properties."""
    _rebuild_datapack(world_dir)

    pack_bytes = _build_resource_pack_zip(world_dir)
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


# ─── Auth ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if not session.get('authenticated'):
        return render_template('login.html')
    return render_template('index.html')


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
    paintings_dir = safe_child(WORLDS_DIR, name) / 'paintings'
    if not paintings_dir.exists():
        return jsonify([])
    return jsonify([
        {'name': f.name, 'size': f.stat().st_size}
        for f in sorted(paintings_dir.iterdir())
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
    filename = Path(file.filename).name
    if not filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        return jsonify({'error': 'Only PNG/JPEG images allowed'}), 400
    paintings_dir = world_dir / 'paintings'
    paintings_dir.mkdir(exist_ok=True)
    file.save(paintings_dir / filename)
    rebuild_paintings(world_dir)
    w, h = _image_block_dims(paintings_dir / filename)
    return jsonify({'ok': True, 'name': filename, 'width_blocks': w, 'height_blocks': h})


@app.route('/api/worlds/<name>/images/<filename>', methods=['DELETE'])
@require_auth
def delete_image(name, filename):
    world_dir = safe_child(WORLDS_DIR, name)
    img = world_dir / 'paintings' / Path(filename).name
    if not img.is_file():
        return jsonify({'error': 'Image not found'}), 404
    img.unlink()
    rebuild_paintings(world_dir)
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

    try:
        new_dir = WORLDS_DIR / new_name
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

        proc.wait(timeout=30)

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
        cmd = f'give {player} minecraft:painting[painting_variant="{PAINTINGS_NS}:{painting}"] 1'
        raw = _rcon_exec('localhost', cfg['port'], cfg['password'], cmd)
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
    data = _build_resource_pack_zip(world_dir)
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

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jar.tmp', dir=JARS_DIR) as tmp:
            tmp_path = Path(tmp.name)
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=65536):
                    tmp.write(chunk)

        for old in JARS_DIR.glob('*.jar'):
            old.unlink()

        final = JARS_DIR / 'server.jar'
        tmp_path.rename(final)
        tmp_path = None
        return jsonify({'ok': True, 'name': 'server.jar', 'size': final.stat().st_size})
    except Exception as e:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return jsonify({'error': str(e)}), 500


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    JARS_DIR.mkdir(exist_ok=True)
    WORLDS_DIR.mkdir(exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
