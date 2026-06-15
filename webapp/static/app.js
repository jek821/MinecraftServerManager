'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let _propsWorld = null;
let _imagesWorld = null;
let _deleteWorld = null;
let _serverHost = '';
let _publicPort = 25565;
let _giveWorld = null;
let _selectedPlayer = null;
let _selectedPainting = null;
let _activeWorld = null;

const _locks = new Set();
let _statusFetchInFlight = false;
let _logsFetchInFlight = false;
let _rebuildInFlight = false;
let _propsOpenGen = 0;
let _imagesOpenGen = 0;

function withLock(key, fn) {
  if (_locks.has(key)) return undefined;
  _locks.add(key);
  return Promise.resolve(fn()).finally(() => _locks.delete(key));
}

function setModalLocked(modalId, locked) {
  const modal = document.getElementById(modalId);
  if (!modal) return;
  const closeBtn = modal.querySelector('.modal-close');
  if (locked) {
    modal.dataset.locked = '1';
    if (closeBtn) closeBtn.style.display = 'none';
  } else {
    delete modal.dataset.locked;
    if (closeBtn) closeBtn.style.display = '';
  }
}

function startJobPoll(getPath, handlers) {
  let lastLogLen = 0;
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    try {
      const job = await apiJson('GET', getPath);
      if (stopped) return;
      if (job.log && handlers.onLog) {
        const newLines = job.log.slice(lastLogLen);
        if (newLines.length) handlers.onLog(newLines);
        lastLogLen = job.log.length;
      }
      if (job.status !== 'running') {
        handlers.onDone(job);
        return;
      }
    } catch (err) {
      if (!stopped) handlers.onDone({ status: 'error', error: err.message });
      return;
    }
    timer = setTimeout(tick, handlers.intervalMs || 1500);
  };
  tick();
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

// ── API helper ────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  return res;
}

async function apiJson(method, path, body) {
  const res = await api(method, path, body);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Unknown error');
  return data;
}

// ── Formatting ────────────────────────────────────────────────────────────────
function fmtUptime(seconds) {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1024 ** 2) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1024 ** 3) return (b / 1024 ** 2).toFixed(1) + ' MB';
  return (b / 1024 ** 3).toFixed(2) + ' GB';
}

function paintingStem(filename) {
  return filename.replace(/\.[^.]+$/, '').toLowerCase().replace(/[\s.-]/g, '_');
}

function fmtDate(iso) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openModal(id) {
  document.getElementById(id).classList.remove('hidden');
}

function closeModal(id) {
  document.getElementById(id).classList.add('hidden');
}

// Close any modal when clicking the overlay background
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) {
    if (e.target.dataset.locked) return;
    e.target.classList.add('hidden');
    return;
  }
  if (e.target.dataset.close) {
    const modal = document.getElementById(e.target.dataset.close);
    if (modal?.dataset.locked) return;
    closeModal(e.target.dataset.close);
  }
});

// ── Worlds ────────────────────────────────────────────────────────────────────
async function loadWorlds() {
  const grid = document.getElementById('worldsGrid');
  try {
    const worlds = await apiJson('GET', '/api/worlds');
    if (worlds.length === 0) {
      grid.innerHTML = '<p class="loading">No world saves found.</p>';
      return;
    }
    grid.innerHTML = '';
    worlds.forEach(w => grid.appendChild(buildWorldCard(w)));
    applyServerGatedButtons();
  } catch (err) {
    grid.innerHTML = `<p class="loading" style="color:var(--danger)">${err.message}</p>`;
  }
}

function buildWorldCard(w) {
  const card = document.createElement('div');
  card.className = 'world-card' + (w.active ? ' is-active' : '');
  card.dataset.name = w.name;

  card.innerHTML = `
    <div class="world-card-header">
      <span class="world-name" id="world-name-${esc(w.name)}">${esc(w.name)}</span>
      ${w.active ? '<span class="badge-active">ACTIVE</span>' : ''}
      <button class="btn btn-ghost btn-sm rename-btn" data-action="rename" data-world="${esc(w.name)}" title="Rename">✎</button>
    </div>
    <div class="world-meta">
      <span>${fmtBytes(w.size)}</span>
      <span>Modified ${fmtDate(w.modified)}</span>
    </div>
    <div class="world-actions">
      ${!w.active ? `<button class="btn btn-active" data-action="activate" data-world="${esc(w.name)}">Set Active</button>` : ''}
      <button class="btn btn-secondary" data-action="download" data-world="${esc(w.name)}">Download</button>
      ${w.has_properties ? `<button class="btn btn-secondary" data-action="properties" data-world="${esc(w.name)}">Properties</button>` : ''}
      <button class="btn btn-secondary" data-action="images" data-world="${esc(w.name)}">Images</button>
      <button class="btn btn-secondary" data-action="pregen"  data-world="${esc(w.name)}">Pre-gen</button>
      <button class="btn btn-danger"    data-action="delete"  data-world="${esc(w.name)}">Delete</button>
    </div>
  `;
  return card;
}

function esc(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _serverState() {
  return _lastServerStatus.state || 'stopped';
}

function _isManagedServerRunning() {
  return _serverState() === 'running';
}

function _isServerBusy() {
  if (_serverPending) return true;
  return ['running', 'starting', 'stopping', 'pregen', 'generating'].includes(_serverState());
}

function _worldActionBlocked(action, worldName) {
  if (!_isServerBusy()) return false;
  if (['activate', 'delete', 'rename', 'pregen'].includes(action)) return true;
  if (_isManagedServerRunning() && worldName === _lastServerStatus.world) {
    return ['download', 'properties', 'images'].includes(action);
  }
  return false;
}

function applyServerGatedButtons() {
  const busy = _isServerBusy();
  const running = _isManagedServerRunning();
  const active = _lastServerStatus.world;

  document.getElementById('newWorldBtn').disabled = busy;
  const uploadInput = document.getElementById('uploadWorldInput');
  uploadInput.disabled = busy;
  document.querySelector('label[for="uploadWorldInput"]')?.classList.toggle('is-disabled', busy);
  document.getElementById('updateJarBtn').disabled = busy;

  document.querySelectorAll('#worldsGrid [data-action]').forEach(btn => {
    const blocked = _worldActionBlocked(btn.dataset.action, btn.dataset.world);
    btn.disabled = blocked;
    btn.title = blocked ? 'Stop the server first' : '';
  });

  const saveProps = document.getElementById('savePropsBtn');
  if (saveProps && !_locks.has('saveProps')) {
    saveProps.disabled = !!(running && _propsWorld && _propsWorld === active);
  }

  const applyPaintings = document.getElementById('applyPaintingsBtn');
  const imageInput = document.getElementById('imageFileInput');
  const imageLabel = document.querySelector('label[for="imageFileInput"]');
  const imagesBlocked = !!(running && _imagesWorld && _imagesWorld === active);
  if (applyPaintings && !_locks.has('imageUpload') && !_rebuildInFlight) {
    applyPaintings.disabled = imagesBlocked;
  }
  if (imageInput) imageInput.disabled = imagesBlocked || _locks.has('imageUpload');
  if (imageLabel) imageLabel.classList.toggle('is-disabled', imagesBlocked);
}

// Delegate all world card button clicks
document.getElementById('worldsGrid').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action;
  const name = btn.dataset.world;

  if (action === 'activate') {
    if (btn.disabled || _locks.has('activate')) return;
    btn.disabled = true;
    try {
      await activateWorld(name);
    } finally {
      applyServerGatedButtons();
    }
  }
  if (action === 'download')    await downloadWorld(btn, name);
  if (action === 'properties')  await openProperties(name);
  if (action === 'images')      await openImages(name);
  if (action === 'pregen')      openPregen(name);
  if (action === 'delete')      openDeleteConfirm(name);
  if (action === 'rename') {
    if (_worldActionBlocked('rename', name)) return;
    startRename(btn, name);
  }
});

function startRename(btn, name) {
  const header = btn.closest('.world-card-header');
  const nameSpan = header.querySelector('.world-name');
  if (header.querySelector('.rename-input')) return; // already open

  btn.style.display = 'none';
  const input = document.createElement('input');
  input.className = 'rename-input';
  input.value = name;
  input.maxLength = 64;
  const confirm = document.createElement('button');
  confirm.className = 'btn btn-primary btn-sm';
  confirm.textContent = '✓';
  const cancel = document.createElement('button');
  cancel.className = 'btn btn-ghost btn-sm';
  cancel.textContent = '✕';

  nameSpan.replaceWith(input);
  btn.after(confirm, cancel);
  input.select();

  async function doRename() {
    if (_locks.has('rename')) return;
    const newName = input.value.trim();
    if (!newName || newName === name) { cancelRename(); return; }
    confirm.disabled = cancel.disabled = true;
    _locks.add('rename');
    try {
      await apiJson('POST', `/api/worlds/${encodeURIComponent(name)}/rename`, { new_name: newName });
      await loadWorlds();
    } catch (err) {
      alert('Rename failed: ' + err.message);
      cancelRename();
    } finally {
      _locks.delete('rename');
    }
  }
  function cancelRename() {
    input.replaceWith(nameSpan);
    confirm.remove(); cancel.remove();
    btn.style.display = '';
  }

  confirm.addEventListener('click', doRename);
  cancel.addEventListener('click', cancelRename);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') doRename();
    if (e.key === 'Escape') cancelRename();
  });
}

async function activateWorld(name) {
  return withLock('activate', async () => {
    try {
      await apiJson('POST', `/api/worlds/${encodeURIComponent(name)}/activate`);
      await Promise.all([loadWorlds(), loadServerStatus()]);
      if (!document.getElementById('whitelistBody').classList.contains('hidden')) loadWhitelist();
    } catch (err) {
      alert('Error: ' + err.message);
    }
  });
}

let _stopDownloadPoll = null;

async function downloadWorld(btn, name) {
  if (_locks.has('download')) return;
  _locks.add('download');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Zipping…';
  if (_stopDownloadPoll) _stopDownloadPoll();

  try {
    const { job_id } = await apiJson('POST', `/api/worlds/${encodeURIComponent(name)}/download`);

    await new Promise((resolve, reject) => {
      _stopDownloadPoll = startJobPoll(`/api/worlds/download/${job_id}`, {
        intervalMs: 1500,
        onLog(lines) {
          const last = lines[lines.length - 1];
          if (last && last.startsWith('Zipping')) btn.textContent = last;
        },
        onDone(job) {
          _stopDownloadPoll = null;
          if (job.status === 'done') {
            btn.textContent = 'Downloading…';
            window.location.href = `/api/worlds/download/${job_id}/file`;
            resolve();
          } else {
            reject(new Error(job.error || 'Zip failed'));
          }
        },
      });
    });
  } catch (err) {
    alert('Download failed: ' + err.message);
  } finally {
    _locks.delete('download');
    btn.disabled = false;
    btn.textContent = orig;
  }
}

// ── Properties Modal ──────────────────────────────────────────────────────────
async function openProperties(name) {
  if (_worldActionBlocked('properties', name)) return;
  const gen = ++_propsOpenGen;
  _propsWorld = name;
  document.getElementById('propsModalTitle').textContent = `${name} — server.properties`;
  document.getElementById('propsEditor').value = 'Loading…';
  openModal('propsModal');
  applyServerGatedButtons();
  try {
    const data = await apiJson('GET', `/api/worlds/${encodeURIComponent(name)}/properties`);
    if (gen !== _propsOpenGen) return;
    document.getElementById('propsEditor').value = data.content;
  } catch (err) {
    if (gen !== _propsOpenGen) return;
    document.getElementById('propsEditor').value = 'Error: ' + err.message;
  }
}

document.getElementById('savePropsBtn').addEventListener('click', async () => {
  if (!_propsWorld || _locks.has('saveProps')) return;
  _locks.add('saveProps');
  const btn = document.getElementById('savePropsBtn');
  const content = document.getElementById('propsEditor').value;
  btn.disabled = true;
  setModalLocked('propsModal', true);
  try {
    await apiJson('POST', `/api/worlds/${encodeURIComponent(_propsWorld)}/properties`, { content });
    closeModal('propsModal');
  } catch (err) {
    alert('Save failed: ' + err.message);
  } finally {
    _locks.delete('saveProps');
    btn.disabled = false;
    setModalLocked('propsModal', false);
  }
});

// ── Images Modal ──────────────────────────────────────────────────────────────
async function openImages(name) {
  if (_worldActionBlocked('images', name)) return;
  const gen = ++_imagesOpenGen;
  _imagesWorld = name;
  document.getElementById('imagesModalTitle').textContent = `${name} — Painting Images`;
  document.getElementById('imageUploadStatus').textContent = '';
  const warn = document.getElementById('imagesHostWarning');
  warn.classList.toggle('hidden', !!_serverHost);
  openModal('imagesModal');
  applyServerGatedButtons();
  await Promise.all([refreshImages(gen), refreshPaintingsDebug(gen)]);
  if (gen === _imagesOpenGen) rebuildPaintingsForWorld(name, gen);
}

function _debugFlag(ok, okLabel, badLabel) {
  const cls = ok ? 'ok' : 'warn';
  const text = ok ? okLabel : badLabel;
  return `<span class="images-debug-flag ${cls}">${esc(text)}</span>`;
}

async function refreshPaintingsDebug(expectedGen) {
  if (!_imagesWorld) return;
  const panel = document.getElementById('imagesDebugContent');
  const details = document.getElementById('imagesDebugPanel');
  if (!panel) return;
  try {
    const d = await apiJson('GET', `/api/worlds/${encodeURIComponent(_imagesWorld)}/paintings-debug`);
    if (expectedGen != null && expectedGen !== _imagesOpenGen) return;

    const hasIssue = !d.sha1_in_sync || !d.pack_download_ok || d.paintings.some(p =>
      !p.in_datapack || !p.in_pack_zip || !p.pack_matches_source
    );
    if (details) details.open = hasIssue;

    let html = `<div class="images-debug-meta">
      Level: <code>${esc(d.level_name)}</code> ·
      SHA1 ${d.sha1_in_sync ? 'in sync' : '<strong style="color:var(--danger)">OUT OF SYNC</strong>'}
      · Pack download ${d.pack_download_ok ? 'OK' : '<strong style="color:var(--danger)">FAILED</strong>'}
      ${d.props_sha1 ? `<br>props: <code>${esc(d.props_sha1.slice(0, 12))}…</code>` : ''}
      ${d.zip_sha1 ? ` zip: <code>${esc(d.zip_sha1.slice(0, 12))}…</code>` : ''}
      ${d.pack_url ? `<br>URL: <code>${esc(d.pack_url)}</code>` : ''}
    </div>`;

    if (!d.paintings.length) {
      html += '<p class="loading">No images uploaded.</p>';
    } else {
      html += d.paintings.map(p => {
        const issue = !p.in_datapack || !p.in_pack_zip || !p.pack_matches_source;
        const [pw, ph] = p.pixels;
        const [bw, bh] = p.blocks;
        const [ew, eh] = p.minecraft_expects_pixels;
        const packPx = p.pack_texture_pixels;
        const packPxLabel = packPx ? `${packPx[0]}×${packPx[1]}` : 'missing';
        return `<div class="images-debug-card${issue ? ' has-issue' : ''}">
          <h4>${esc(p.file)} <code>${esc(p.stem)}</code></h4>
          <div class="images-debug-flags">
            ${_debugFlag(p.in_datapack, 'In datapack', 'Missing datapack')}
            ${_debugFlag(p.in_pack_zip, 'In pack zip', 'Missing from zip')}
            ${_debugFlag(p.pack_matches_source, `Pack texture ${packPxLabel} (full res)`, `Pack downscaled to ${packPxLabel} — click Apply to World`)}
          </div>
          <div>Source: ${pw}×${ph} · Wall: ${bw}×${bh} blocks · MC nominal: ${ew}×${eh}px</div>
          <div class="images-debug-cmd">
            <code>${esc(p.summon_test)}</code>
            <button type="button" class="btn btn-ghost btn-sm" data-copy-cmd="${esc(p.summon_test)}">Copy</button>
          </div>
        </div>`;
      }).join('');
    }

    if (d.ingame_checks?.length) {
      html += `<ul class="images-debug-checks">${d.ingame_checks.map(c => `<li>${esc(c)}</li>`).join('')}</ul>`;
    }

    panel.innerHTML = html;
  } catch (err) {
    panel.innerHTML = `<p class="loading" style="color:var(--danger)">${esc(err.message)}</p>`;
  }
}

async function refreshPackStatus(expectedGen) {
  return refreshPaintingsDebug(expectedGen);
}

document.getElementById('imagesDebugContent')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-copy-cmd]');
  if (!btn) return;
  try {
    await navigator.clipboard.writeText(btn.dataset.copyCmd);
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = orig; }, 1200);
  } catch {
    alert('Could not copy — select the command text manually.');
  }
});

async function rebuildPaintingsForWorld(name, expectedGen) {
  if (_rebuildInFlight) return;
  _rebuildInFlight = true;
  const btn = document.getElementById('applyPaintingsBtn');
  const status = document.getElementById('imageUploadStatus');
  if (btn) { btn.disabled = true; btn.textContent = 'Applying…'; }
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(name)}/rebuild-paintings`);
    if (expectedGen == null || expectedGen === _imagesOpenGen) {
      if (status) {
        const pack = data.pack || {};
        let msg = pack.hint || 'Applied to world.';
        if (pack.url) {
          msg += `\nPack URL: ${pack.url}`;
          if (pack.image_count === 0) {
            msg += '\n⚠ No images uploaded yet — pack is empty.';
          }
          if (pack.test && !pack.test.ok) {
            msg += `\n⚠ Server self-test failed: ${pack.test.error || 'HTTP ' + (pack.test.status || '?')}`;
            msg += '\nRun from another network: curl -I ' + pack.url;
          }
        }
        status.textContent = msg;
        status.className = 'status-text ' + (pack.test && !pack.test.ok ? 'err' : 'ok');
        status.style.whiteSpace = 'pre-wrap';
      }
      await refreshPaintingsDebug(expectedGen);
    }
  } catch (err) {
    if (expectedGen == null || expectedGen === _imagesOpenGen) {
      if (status) {
        status.textContent = 'Apply failed: ' + err.message;
        status.className = 'status-text err';
      }
    }
  }
  if (expectedGen == null || expectedGen === _imagesOpenGen) {
    if (btn) { btn.disabled = false; btn.textContent = 'Apply to World'; }
    applyServerGatedButtons();
  }
  _rebuildInFlight = false;
}

document.getElementById('imagesHostLink').addEventListener('click', (e) => {
  e.preventDefault();
  closeModal('imagesModal');
  document.getElementById('serverHost').focus();
});

async function refreshImages(expectedGen) {
  if (!_imagesWorld) return;
  const list = document.getElementById('imagesList');
  try {
    const images = await apiJson('GET', `/api/worlds/${encodeURIComponent(_imagesWorld)}/images`);
    if (expectedGen != null && expectedGen !== _imagesOpenGen) return;
    if (images.length === 0) {
      list.innerHTML = '<p class="loading">No images uploaded yet.</p>';
    } else {
      list.innerHTML = images.map(img => `
        <div class="image-item">
          <div>
            <span class="image-name">${esc(img.name)}</span>
            <span class="image-size"> — id: <code>${esc(img.stem || paintingStem(img.name))}</code> · ${fmtBytes(img.size)}</span>
          </div>
          <button class="btn btn-danger" style="padding:0.25rem 0.5rem;font-size:0.7rem"
            data-del-image="${esc(img.name)}">✕</button>
        </div>
      `).join('');
    }
  } catch (err) {
    list.innerHTML = `<p class="loading" style="color:var(--danger)">${err.message}</p>`;
  }
}

document.getElementById('imagesList').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-del-image]');
  if (!btn || !_imagesWorld || btn.disabled || _locks.has('imageDelete')) return;
  _locks.add('imageDelete');
  btn.disabled = true;
  const imgName = btn.dataset.delImage;
  try {
    await apiJson('DELETE', `/api/worlds/${encodeURIComponent(_imagesWorld)}/images/${encodeURIComponent(imgName)}`);
    await Promise.all([refreshImages(), refreshPaintingsDebug()]);
  } catch (err) {
    alert('Delete failed: ' + err.message);
    btn.disabled = false;
  } finally {
    _locks.delete('imageDelete');
  }
});

document.getElementById('applyPaintingsBtn').addEventListener('click', () => {
  if (_imagesWorld && !_rebuildInFlight && !_locks.has('rebuild')) {
    withLock('rebuild', () => rebuildPaintingsForWorld(_imagesWorld));
  }
});

document.getElementById('imageFileInput').addEventListener('change', async (e) => {
  if (!_imagesWorld || _locks.has('imageUpload')) return;
  const status = document.getElementById('imageUploadStatus');
  const files = Array.from(e.target.files);
  if (!files.length) return;
  _locks.add('imageUpload');
  const input = e.target;

  try {
    status.textContent = `Uploading ${files.length} file(s)…`;
    status.className = 'status-text';

    let ok = 0, fail = 0;
    let lastHint = '';
    for (const file of files) {
      const fd = new FormData();
      fd.append('image', file);
      try {
        const res = await fetch(`/api/worlds/${encodeURIComponent(_imagesWorld)}/images`, {
          method: 'POST', body: fd,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error);
        if (data.hint) lastHint = data.hint;
        ok++;
      } catch (err) {
        fail++;
        if (files.length === 1) {
          status.textContent = '✘ ' + (err.message || 'Upload failed');
          status.className = 'status-text err';
          input.value = '';
          await Promise.all([refreshImages(), refreshPaintingsDebug()]);
          return;
        }
      }
    }

    let msg = `${ok} uploaded${fail ? `, ${fail} failed` : ''}`;
    if (ok && lastHint) msg += `\n${lastHint}`;
    status.textContent = msg;
    status.className = 'status-text ' + (fail ? 'err' : 'ok');
    if (lastHint) status.style.whiteSpace = 'pre-wrap';
    input.value = '';
    await Promise.all([refreshImages(), refreshPaintingsDebug()]);
  } finally {
    _locks.delete('imageUpload');
  }
});

// ── Delete Modal ──────────────────────────────────────────────────────────────
function _resetDeleteModal() {
  const input = document.getElementById('deleteConfirmInput');
  const btn = document.getElementById('confirmDeleteBtn');
  const field = document.getElementById('deleteConfirmField');
  input.value = '';
  input.disabled = false;
  btn.disabled = true;
  btn.textContent = 'Delete';
  field.classList.remove('hidden');
}

function _updateDeleteConfirmBtn() {
  const input = document.getElementById('deleteConfirmInput');
  const btn = document.getElementById('confirmDeleteBtn');
  if (_locks.has('delete')) return;
  btn.disabled = input.value !== _deleteWorld;
}

function openDeleteConfirm(name) {
  if (_worldActionBlocked('delete', name)) return;
  if (_locks.has('delete')) {
    alert('A delete is already in progress.');
    return;
  }
  _deleteWorld = name;
  document.getElementById('deleteModalText').textContent =
    `This permanently deletes all files for "${name}". This cannot be undone.`;
  document.getElementById('deleteConfirmName').textContent = name;
  _resetDeleteModal();
  openModal('deleteModal');
  document.getElementById('deleteConfirmInput').focus();
}

let _stopDeletePoll = null;

document.getElementById('deleteConfirmInput').addEventListener('input', _updateDeleteConfirmBtn);

document.getElementById('confirmDeleteBtn').addEventListener('click', async () => {
  if (!_deleteWorld || _locks.has('delete')) return;
  const input = document.getElementById('deleteConfirmInput');
  if (input.value !== _deleteWorld) {
    alert('World name does not match.');
    return;
  }

  _locks.add('delete');
  const btn = document.getElementById('confirmDeleteBtn');
  const status = document.getElementById('deleteModalText');
  const field = document.getElementById('deleteConfirmField');
  btn.disabled = true;
  btn.textContent = 'Deleting…';
  input.disabled = true;
  field.classList.add('hidden');
  setModalLocked('deleteModal', true);
  if (_stopDeletePoll) _stopDeletePoll();

  try {
    const { job_id } = await apiJson('DELETE', `/api/worlds/${encodeURIComponent(_deleteWorld)}`);
    status.textContent = 'Deleting world files… large saves can take several minutes.';

    _stopDeletePoll = startJobPoll(`/api/worlds/delete/${job_id}`, {
      intervalMs: 2000,
      onLog(lines) { status.textContent = lines[lines.length - 1] || status.textContent; },
      onDone(job) {
        _stopDeletePoll = null;
        _locks.delete('delete');
        setModalLocked('deleteModal', false);
        if (job.status === 'done') {
          _deleteWorld = null;
          closeModal('deleteModal');
          loadWorlds();
        } else {
          alert('Delete failed: ' + (job.error || 'Unknown error'));
          _resetDeleteModal();
          status.textContent =
            `This permanently deletes all files for "${document.getElementById('deleteConfirmName').textContent}". This cannot be undone.`;
        }
      },
    });
  } catch (err) {
    _locks.delete('delete');
    setModalLocked('deleteModal', false);
    _resetDeleteModal();
    alert('Delete failed: ' + err.message);
  }
});

// ── New World / Generate ──────────────────────────────────────────────────────
document.getElementById('uploadWorldInput').addEventListener('change', async (e) => {
  if (_isServerBusy() || _locks.has('worldUpload')) return;
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = '';
  _locks.add('worldUpload');
  const fd = new FormData();
  fd.append('world', file);
  const label = document.querySelector('label[for="uploadWorldInput"]');
  const orig = label.textContent;
  label.textContent = 'Uploading…';
  try {
    const res = await fetch('/api/worlds/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Upload failed');
    await loadWorlds();
  } catch (err) {
    alert('Upload failed: ' + err.message);
  } finally {
    label.textContent = orig;
    _locks.delete('worldUpload');
  }
});

let _stopGeneratePoll = null;

document.getElementById('newWorldBtn').addEventListener('click', () => {
  if (_isServerBusy() || _locks.has('generate')) return;
  if (_stopGeneratePoll) _stopGeneratePoll();
  document.getElementById('newWorldName').value = '';
  document.getElementById('inheritProps').checked = true;
  document.getElementById('generateLog').classList.add('hidden');
  document.getElementById('generateLog').textContent = '';
  const startBtn = document.getElementById('startGenerateBtn');
  startBtn.disabled = false;
  startBtn.textContent = 'Generate';
  setModalLocked('generateModal', false);
  document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
  openModal('generateModal');
});

document.getElementById('startGenerateBtn').addEventListener('click', async () => {
  if (_locks.has('generate')) return;
  _locks.add('generate');
  const name = document.getElementById('newWorldName').value.trim();
  const inherit = document.getElementById('inheritProps').checked;

  if (!name) {
    _locks.delete('generate');
    alert('Please enter a world name.');
    return;
  }

  const logEl = document.getElementById('generateLog');
  logEl.textContent = '';
  logEl.classList.remove('hidden');
  const startBtn = document.getElementById('startGenerateBtn');
  startBtn.disabled = true;
  startBtn.textContent = 'Generating…';
  setModalLocked('generateModal', true);
  document.getElementById('cancelGenerateBtn').removeAttribute('data-close');
  if (_stopGeneratePoll) _stopGeneratePoll();

  function appendLog(lines) {
    logEl.textContent += lines.join('\n') + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  }

  try {
    const { job_id } = await apiJson('POST', '/api/worlds/generate', {
      new_name: name,
      inherit_properties: inherit,
    });
    _stopGeneratePoll = startJobPoll(`/api/worlds/generate/${job_id}`, {
      intervalMs: 1000,
      onLog: appendLog,
      onDone(job) {
        _stopGeneratePoll = null;
        _locks.delete('generate');
        if (job.status === 'done') {
          appendLog(['', '✔ Done! World generated successfully.']);
          loadWorlds();
          setTimeout(() => {
            setModalLocked('generateModal', false);
            closeModal('generateModal');
            startBtn.disabled = false;
            startBtn.textContent = 'Generate';
            document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
          }, 1200);
        } else {
          appendLog(['', '✘ Error: ' + (job.error || 'Unknown error')]);
          setModalLocked('generateModal', false);
          startBtn.disabled = false;
          startBtn.textContent = 'Generate';
          document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
        }
      },
    });

  } catch (err) {
    _locks.delete('generate');
    appendLog(['Error starting generation: ' + err.message]);
    setModalLocked('generateModal', false);
    startBtn.disabled = false;
    startBtn.textContent = 'Generate';
    document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
  }
});

// ── Pre-generate ──────────────────────────────────────────────────────────────
let _pregenWorld = null;
let _pregenJobId = null;
let _pregenPollWorld = null;
let _pregenRunning = false;

function _setPregenModalLocked(locked) {
  const modal = document.getElementById('pregenModal');
  const cancelBtn = document.getElementById('cancelPregenBtn');
  const closeBtn = modal.querySelector('.modal-close');
  _pregenRunning = locked;
  if (locked) {
    modal.dataset.locked = '1';
    cancelBtn.textContent = 'Cancel Pre-gen';
    cancelBtn.removeAttribute('data-close');
    closeBtn.style.display = 'none';
  } else {
    delete modal.dataset.locked;
    cancelBtn.textContent = 'Close';
    cancelBtn.dataset.close = 'pregenModal';
    closeBtn.style.display = '';
    _pregenJobId = null;
    _pregenPollWorld = null;
  }
}

function _resetPregenModal() {
  _setPregenModalLocked(false);
  _locks.delete('pregen');
  const cancelBtn = document.getElementById('cancelPregenBtn');
  cancelBtn.disabled = false;
  document.getElementById('startPregenBtn').disabled = false;
  document.getElementById('startPregenBtn').textContent = 'Start Pre-gen';
}

function openPregen(name) {
  if (_worldActionBlocked('pregen', name)) return;
  if (_pregenRunning && _pregenPollWorld && _pregenPollWorld !== name) {
    alert(`Pre-generation is already running for ${_pregenPollWorld}. Cancel it first.`);
    return;
  }
  _pregenWorld = name;
  document.getElementById('pregenModalTitle').textContent = `Pre-generate — ${name}`;
  document.getElementById('pregenCenterX').value = '0';
  document.getElementById('pregenCenterZ').value = '0';
  document.getElementById('pregenRadius').value = '1000';
  const logEl = document.getElementById('pregenLog');
  if (!_pregenRunning) {
    logEl.textContent = '';
    logEl.classList.add('hidden');
    _resetPregenModal();
  }
  openModal('pregenModal');
}

let _stopPregenPoll = null;

function _pollPregenJob(jobId) {
  const logEl = document.getElementById('pregenLog');

  function appendLog(lines) {
    logEl.textContent += lines.join('\n') + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  }

  if (_stopPregenPoll) _stopPregenPoll();
  _pregenPollWorld = _pregenWorld;
  _stopPregenPoll = startJobPoll(
    `/api/worlds/${encodeURIComponent(_pregenPollWorld)}/pregen/${jobId}`,
    {
      intervalMs: 1500,
      onLog: appendLog,
      onDone(job) {
        _stopPregenPoll = null;
        if (job.status === 'done') {
          appendLog(['', '✔ Pre-generation complete.']);
        } else if (job.status === 'cancelled') {
          appendLog(['', 'Pre-generation cancelled. Server stopped.']);
        } else {
          appendLog(['', '✘ Error: ' + (job.error || 'Unknown error')]);
        }
        _resetPregenModal();
      },
    },
  );
}

document.getElementById('startPregenBtn').addEventListener('click', async () => {
  if (!_pregenWorld || _pregenRunning || _locks.has('pregen')) return;
  _locks.add('pregen');
  const logEl = document.getElementById('pregenLog');
  logEl.textContent = '';
  logEl.classList.remove('hidden');
  document.getElementById('startPregenBtn').disabled = true;
  document.getElementById('startPregenBtn').textContent = 'Running…';
  _setPregenModalLocked(true);

  const center_x = parseInt(document.getElementById('pregenCenterX').value, 10) || 0;
  const center_z = parseInt(document.getElementById('pregenCenterZ').value, 10) || 0;
  const radius = parseInt(document.getElementById('pregenRadius').value, 10) || 1000;

  function appendLog(lines) {
    logEl.textContent += lines.join('\n') + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  }

  try {
    const { job_id } = await apiJson('POST', `/api/worlds/${encodeURIComponent(_pregenWorld)}/pregen`, {
      center_x, center_z, radius,
    });
    _pregenJobId = job_id;
    _pregenPollWorld = _pregenWorld;
    _pollPregenJob(job_id);
  } catch (err) {
    appendLog(['Error: ' + err.message]);
    _locks.delete('pregen');
    _resetPregenModal();
  }
});

document.getElementById('cancelPregenBtn').addEventListener('click', async (e) => {
  if (_pregenRunning && _pregenJobId) {
    e.stopPropagation();
    e.preventDefault();
    const btn = document.getElementById('cancelPregenBtn');
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = 'Cancelling…';
    try {
      await apiJson('POST', `/api/worlds/${encodeURIComponent(_pregenPollWorld || _pregenWorld)}/pregen/${_pregenJobId}/cancel`);
    } catch (err) {
      alert('Cancel failed: ' + err.message);
      btn.disabled = false;
      btn.textContent = 'Cancel Pre-gen';
    }
    return;
  }
  closeModal('pregenModal');
});

// ── Jars ──────────────────────────────────────────────────────────────────────
async function loadJars() {
  const el = document.getElementById('jarsContent');
  try {
    const jars = await apiJson('GET', '/api/jars');
    if (jars.length === 0) {
      el.innerHTML = '<p class="loading">No jars found in jars/ directory.</p>';
      return;
    }
    el.innerHTML = jars.map(j => `
      <div class="jar-item">
        <div>
          <div class="jar-name">${esc(j.name)}</div>
          <div class="jar-meta">${fmtBytes(j.size)} · Updated ${fmtDate(j.modified)}</div>
        </div>
      </div>
    `).join('');
  } catch (err) {
    el.innerHTML = `<p class="loading" style="color:var(--danger)">${err.message}</p>`;
  }
}

document.getElementById('updateJarBtn').addEventListener('click', async () => {
  if (_isServerBusy() || _locks.has('jarUpdate')) return;
  const url = document.getElementById('jarUrl').value.trim();
  const statusEl = document.getElementById('jarUpdateStatus');

  if (!url) {
    statusEl.textContent = 'Please enter a URL.';
    statusEl.className = 'status-text err';
    statusEl.classList.remove('hidden');
    return;
  }

  const btn = document.getElementById('updateJarBtn');
  _locks.add('jarUpdate');
  btn.disabled = true;
  btn.textContent = 'Downloading…';
  statusEl.textContent = 'Downloading jar… this may take a moment.';
  statusEl.className = 'status-text';
  statusEl.classList.remove('hidden');

  try {
    const data = await apiJson('POST', '/api/jars/update', { url });
    statusEl.textContent = `✔ Updated to ${data.name} (${fmtBytes(data.size)})`;
    statusEl.className = 'status-text ok';
    document.getElementById('jarUrl').value = '';
    await loadJars();
  } catch (err) {
    statusEl.textContent = '✘ ' + err.message;
    statusEl.className = 'status-text err';
  } finally {
    _locks.delete('jarUpdate');
    btn.disabled = false;
    btn.textContent = 'Download & Replace';
  }
});

// ── Give Painting Modal ───────────────────────────────────────────────────────
async function openGive(name) {
  _giveWorld = name;
  _selectedPlayer = null;
  _selectedPainting = null;
  document.getElementById('giveModalTitle').textContent = `Give Painting — ${name}`;
  document.getElementById('givePaintingBtn').disabled = true;
  document.getElementById('giveStatus').classList.add('hidden');
  openModal('giveModal');
  await Promise.all([fetchPlayers(), fetchPaintingsForGive()]);
}

async function fetchPlayers() {
  const list = document.getElementById('playersList');
  list.innerHTML = '<div class="give-msg">Connecting…</div>';
  try {
    const data = await apiJson('GET', `/api/worlds/${encodeURIComponent(_giveWorld)}/rcon/players`);
    renderSelectList(list, data.players, 'player', (val) => {
      _selectedPlayer = val;
      updateGiveBtn();
    }, 'No players online.');
  } catch (err) {
    const isRconErr = /rcon|enable-rcon/i.test(err.message);
    if (isRconErr) {
      list.innerHTML = '<div class="give-msg">RCON not configured — enabling automatically…</div>';
      try {
        await apiJson('POST', `/api/worlds/${encodeURIComponent(_giveWorld)}/ensure-rcon`);
        list.innerHTML = '<div class="give-msg">RCON enabled in server.properties. Restart the server to apply, then re-open Give Painting.</div>';
      } catch (fixErr) {
        list.innerHTML = `<div class="give-msg err">Auto-fix failed: ${esc(fixErr.message)}</div>`;
      }
    } else {
      list.innerHTML = `<div class="give-msg err">${esc(err.message)}</div>`;
    }
  }
}

async function fetchPaintingsForGive() {
  const list = document.getElementById('paintingsList');
  list.innerHTML = '<div class="give-msg">Loading…</div>';
  try {
    const images = await apiJson('GET', `/api/worlds/${encodeURIComponent(_giveWorld)}/images`);
    const items = images.map(img => ({
      label: img.name,
      value: paintingStem(img.name),
    }));
    renderSelectList(list, items, 'painting', (val) => {
      _selectedPainting = val;
      updateGiveBtn();
    }, 'No paintings uploaded yet.');
  } catch (err) {
    list.innerHTML = `<div class="give-msg err">${esc(err.message)}</div>`;
  }
}

function renderSelectList(container, items, _stateKey, onSelect, emptyMsg) {
  if (!items.length) {
    container.innerHTML = `<div class="give-msg">${emptyMsg}</div>`;
    return;
  }
  // items can be strings or {label, value}
  const normalised = items.map(i => typeof i === 'string' ? { label: i, value: i } : i);
  container.innerHTML = normalised.map(i =>
    `<div class="give-item" data-value="${esc(i.value)}">${esc(i.label)}</div>`
  ).join('');
  container.querySelectorAll('.give-item').forEach(el => {
    el.addEventListener('click', () => {
      container.querySelectorAll('.give-item').forEach(e => e.classList.remove('selected'));
      el.classList.add('selected');
      onSelect(el.dataset.value);
    });
  });
}

function updateGiveBtn() {
  document.getElementById('givePaintingBtn').disabled = !(_selectedPlayer && _selectedPainting);
}

document.getElementById('refreshPlayersBtn').addEventListener('click', () => withLock('fetchPlayers', fetchPlayers));

document.getElementById('givePaintingBtn').addEventListener('click', async () => {
  if (!_selectedPlayer || !_selectedPainting || !_giveWorld || _locks.has('give')) return;
  const btn = document.getElementById('givePaintingBtn');
  const statusEl = document.getElementById('giveStatus');
  btn.disabled = true;
  _locks.add('give');
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_giveWorld)}/rcon/give`, {
      player: _selectedPlayer,
      painting: _selectedPainting,
    });
    statusEl.textContent = '✔ ' + (data.response || 'Given!')
    if (data.warning) statusEl.textContent += '\n' + data.warning;
    statusEl.style.whiteSpace = 'pre-wrap';
    statusEl.className = 'give-status ok';
  } catch (err) {
    statusEl.textContent = '✘ ' + err.message;
    statusEl.className = 'give-status err';
  } finally {
    _locks.delete('give');
    btn.disabled = !(_selectedPlayer && _selectedPainting);
  }
  statusEl.classList.remove('hidden');
});

// ── Settings ──────────────────────────────────────────────────────────────────
function refreshServerIconPreview(hasIcon) {
  const preview = document.getElementById('serverIconPreview');
  const removeBtn = document.getElementById('removeServerIconBtn');
  if (hasIcon) {
    preview.src = `/api/server-icon?t=${Date.now()}`;
    preview.classList.remove('hidden');
    removeBtn.classList.remove('hidden');
  } else {
    preview.classList.add('hidden');
    preview.removeAttribute('src');
    removeBtn.classList.add('hidden');
  }
}

async function loadSettings() {
  try {
    const cfg = await apiJson('GET', '/api/config');
    _serverHost = cfg.server_host || '';
    _publicPort = cfg.public_port || 25565;
    document.getElementById('jvmArgs').value = cfg.jvm_args || '';
    refreshServerIconPreview(!!cfg.has_server_icon);
    try {
      const motd = await apiJson('GET', '/api/server/motd');
      document.getElementById('motdInput').value = motd.motd || '';
    } catch { /* no active world */ }
    if (_serverHost) {
      document.getElementById('serverHost').value = _serverHost;
    } else {
      // Auto-populate with detected IP if nothing saved yet
      try {
        const det = await apiJson('GET', '/api/detect-host');
        if (det.host) document.getElementById('serverHost').value = det.host;
      } catch { /* silent */ }
    }
  } catch { /* non-fatal */ }
}

document.getElementById('serverIconInput').addEventListener('change', async (e) => {
  if (_locks.has('serverIcon')) return;
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  _locks.add('serverIcon');
  const statusEl = document.getElementById('serverIconStatus');
  const form = new FormData();
  form.append('icon', file);
  try {
    const res = await fetch('/api/server-icon', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Upload failed');
    refreshServerIconPreview(true);
    statusEl.textContent = '✔ Icon saved — restart server to show in server list';
    statusEl.className = 'status-text ok';
    statusEl.classList.remove('hidden');
    setTimeout(() => statusEl.classList.add('hidden'), 3000);
  } catch (err) {
    statusEl.textContent = '✘ ' + err.message;
    statusEl.className = 'status-text err';
    statusEl.classList.remove('hidden');
  } finally {
    _locks.delete('serverIcon');
  }
});

document.getElementById('removeServerIconBtn').addEventListener('click', async () => {
  if (_locks.has('serverIcon')) return;
  _locks.add('serverIcon');
  const statusEl = document.getElementById('serverIconStatus');
  const btn = document.getElementById('removeServerIconBtn');
  btn.disabled = true;
  try {
    await apiJson('DELETE', '/api/server-icon');
    refreshServerIconPreview(false);
    statusEl.textContent = '✔ Icon removed — restart server to update server list';
    statusEl.className = 'status-text ok';
    statusEl.classList.remove('hidden');
    setTimeout(() => statusEl.classList.add('hidden'), 3000);
  } catch (err) {
    statusEl.textContent = '✘ ' + err.message;
    statusEl.className = 'status-text err';
    statusEl.classList.remove('hidden');
  } finally {
    _locks.delete('serverIcon');
    btn.disabled = false;
  }
});

document.getElementById('autoDetectBtn').addEventListener('click', async () => {
  if (_locks.has('detectHost')) return;
  _locks.add('detectHost');
  const btn = document.getElementById('autoDetectBtn');
  btn.disabled = true;
  try {
    const det = await apiJson('GET', '/api/detect-host');
    if (det.host) document.getElementById('serverHost').value = det.host;
  } catch { /* silent */ }
  finally {
    _locks.delete('detectHost');
    btn.disabled = false;
  }
});

document.getElementById('saveSettingsBtn').addEventListener('click', async () => {
  if (_locks.has('saveSettings')) return;
  const host = document.getElementById('serverHost').value.trim();
  const jvmArgs = document.getElementById('jvmArgs').value.trim();
  const statusEl = document.getElementById('settingsStatus');
  const btn = document.getElementById('saveSettingsBtn');
  btn.disabled = true;
  _locks.add('saveSettings');
  try {
    const motd = document.getElementById('motdInput').value;
    const data = await apiJson('POST', '/api/config', { server_host: host, public_port: _publicPort, jvm_args: jvmArgs });
    try { await apiJson('POST', '/api/server/motd', { motd }); } catch { /* no active world */ }
    _serverHost = host;
    const rebuilt = data.rebuilt_worlds || 0;
    statusEl.textContent = rebuilt
      ? `✔ Saved — resource-pack URLs updated for ${rebuilt} world(s)`
      : '✔ Saved';
    statusEl.className = 'status-text ok';
    statusEl.classList.remove('hidden');
    setTimeout(() => statusEl.classList.add('hidden'), 4000);
  } catch (err) {
    statusEl.textContent = '✘ ' + err.message;
    statusEl.className = 'status-text err';
    statusEl.classList.remove('hidden');
  } finally {
    _locks.delete('saveSettings');
    btn.disabled = false;
  }
});

// ── Logout ────────────────────────────────────────────────────────────────────
document.getElementById('logoutBtn').addEventListener('click', async () => {
  if (_locks.has('logout')) return;
  _locks.add('logout');
  document.getElementById('logoutBtn').disabled = true;
  await api('POST', '/logout');
  location.reload();
});

// ── Server HUD ────────────────────────────────────────────────────────────────
const START_LABEL = '▶ Start';
const STOP_LABEL = '■ Stop';
const STATUS_POLL_SLOW = 5000;
const STATUS_POLL_FAST = 1000;

let _lastServerStatus = { state: 'stopped', world: null };
let _serverPending = null; // null | 'starting' | 'stopping'
let _statusPollTimer = null;

function scheduleStatusPoll(interval) {
  if (_statusPollTimer) clearInterval(_statusPollTimer);
  _statusPollTimer = setInterval(loadServerStatus, interval);
}

async function loadServerStatus() {
  if (_statusFetchInFlight) return;
  _statusFetchInFlight = true;
  try {
    const data = await apiJson('GET', '/api/server/status');
    _lastServerStatus = data;
    updateServerHud(data);
  } catch { /* silent */ }
  finally { _statusFetchInFlight = false; }
}

function updateServerHud(data) {
  const dot        = document.getElementById('serverStatusDot');
  const label      = document.getElementById('serverStatusText');
  const startBtn   = document.getElementById('startServerBtn');
  const stopBtn    = document.getElementById('stopServerBtn');
  const giveHudBtn = document.getElementById('givePaintingHudBtn');

  _activeWorld = data.world || null;

  if (_serverPending === 'starting' && data.state === 'running') _serverPending = null;
  if (_serverPending === 'starting' && data.state === 'stopped') _serverPending = null;
  if (_serverPending === 'stopping' && data.state === 'stopped') _serverPending = null;

  let displayState = data.state;
  if (_serverPending === 'stopping' && data.state === 'running') displayState = 'stopping';
  if (_serverPending === 'starting' && data.state === 'stopped') displayState = 'starting';

  dot.className = 'server-status-dot ' + displayState;

  const stateLabel = {
    running: 'RUNNING',
    pregen: 'PRE-GEN',
    generating: 'GENERATING',
    starting: 'STARTING',
    stopping: 'STOPPING',
    stopped: 'STOPPED',
  }[displayState] || displayState.toUpperCase();
  label.textContent = data.world ? `${stateLabel} — ${data.world}` : stateLabel;

  if (_serverPending === 'starting') {
    startBtn.textContent = 'Starting…';
    startBtn.disabled = true;
    stopBtn.textContent = STOP_LABEL;
    stopBtn.disabled = true;
  } else if (_serverPending === 'stopping') {
    stopBtn.textContent = 'Stopping…';
    stopBtn.disabled = true;
    startBtn.textContent = START_LABEL;
    startBtn.disabled = true;
  } else {
    startBtn.textContent = START_LABEL;
    stopBtn.textContent = STOP_LABEL;
    const bgBusy = data.state === 'pregen' || data.state === 'generating';
    startBtn.disabled = data.state !== 'stopped';
    stopBtn.disabled = data.state === 'stopped' || bgBusy;
  }

  const playable = data.state === 'running' && !_serverPending;
  giveHudBtn.disabled = !playable;
  document.getElementById('manageOpsHudBtn').disabled = !playable;

  document.getElementById('statPlayers').textContent =
    data.players_online != null ? `${data.players_online} / ${data.players_max}` : '—';
  document.getElementById('statUptime').textContent  = fmtUptime(data.uptime);
  document.getElementById('statVersion').textContent = data.version || '—';

  const m = data.metrics;
  document.getElementById('statJvmRam').textContent = m ? fmtBytes(m.rss_kb * 1024) : '—';
  document.getElementById('statSysRam').textContent = m
    ? `${fmtBytes(m.sys_mem_used_kb * 1024)} / ${fmtBytes(m.sys_mem_total_kb * 1024)}`
    : '—';
  document.getElementById('statCpu').textContent = m != null ? m.cpu_pct + '%' : '—';

  const addrEl = document.getElementById('serverAddress');
  const copyBtn = document.getElementById('copyAddressBtn');
  if (data.server_address) {
    addrEl.textContent = data.server_address;
    copyBtn.disabled = false;
  } else {
    addrEl.textContent = 'Set Server Host in Settings';
    copyBtn.disabled = true;
  }

  scheduleStatusPoll(_serverPending ? STATUS_POLL_FAST : STATUS_POLL_SLOW);
  if (playable && !document.getElementById('serverLogsBody').classList.contains('hidden')) {
    loadServerLogs();
  }

  applyServerGatedButtons();
}

document.getElementById('copyAddressBtn').addEventListener('click', async () => {
  const addr = document.getElementById('serverAddress').textContent;
  if (!addr || addr.startsWith('Set ')) return;
  try {
    await navigator.clipboard.writeText(addr);
    const btn = document.getElementById('copyAddressBtn');
    const prev = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = prev; }, 1500);
  } catch {
    alert('Copy failed — select and copy manually: ' + addr);
  }
});

document.getElementById('startServerBtn').addEventListener('click', async () => {
  if (_serverPending || _locks.has('serverAction')) return;
  _locks.add('serverAction');
  _serverPending = 'starting';
  updateServerHud(_lastServerStatus);
  try {
    await apiJson('POST', '/api/server/start');
  } catch (err) {
    _serverPending = null;
    updateServerHud(_lastServerStatus);
    alert('Error: ' + err.message);
    _locks.delete('serverAction');
    return;
  }
  _locks.delete('serverAction');
  await loadServerStatus();
});

document.getElementById('stopServerBtn').addEventListener('click', async () => {
  if (_serverPending || _locks.has('serverAction')) return;
  _locks.add('serverAction');
  _serverPending = 'stopping';
  updateServerHud(_lastServerStatus);
  try {
    await apiJson('POST', '/api/server/stop');
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      await loadServerStatus();
      if (_lastServerStatus.state === 'stopped') break;
      await new Promise(r => setTimeout(r, 1000));
    }
    if (_lastServerStatus.state !== 'stopped') {
      _serverPending = null;
      updateServerHud(_lastServerStatus);
      alert('Server did not stop in time. Refresh the page and try again.');
      return;
    }
  } catch (err) {
    _serverPending = null;
    updateServerHud(_lastServerStatus);
    alert('Error: ' + err.message);
    return;
  } finally {
    _locks.delete('serverAction');
  }
});

document.getElementById('givePaintingHudBtn').addEventListener('click', async () => {
  if (!_activeWorld) return;
  await openGive(_activeWorld);
});

// ── Ops Modal ─────────────────────────────────────────────────────────────────
let _opsWorld = null;

async function openOps(name) {
  _opsWorld = name;
  document.getElementById('opsModalTitle').textContent = `Manage Ops — ${name}`;
  document.getElementById('opsStatus').classList.add('hidden');
  document.getElementById('opByNameInput').value = '';
  openModal('opsModal');
  await Promise.all([fetchOpsPlayers(), fetchCurrentOps()]);
}

async function fetchOpsPlayers() {
  const list = document.getElementById('opsPlayersList');
  list.innerHTML = '<div class="give-msg">Connecting…</div>';
  try {
    const [pd, od] = await Promise.all([
      apiJson('GET', `/api/worlds/${encodeURIComponent(_opsWorld)}/rcon/players`),
      apiJson('GET', `/api/worlds/${encodeURIComponent(_opsWorld)}/ops`),
    ]);
    const opNames = new Set((od || []).map(o => o.name.toLowerCase()));
    const players = pd.players || [];
    if (!players.length) {
      list.innerHTML = '<div class="give-msg">No players online.</div>';
      return;
    }
    list.innerHTML = players.map(p => `
      <div class="ops-player-row">
        <span class="ops-player-name">${esc(p)}</span>
        ${opNames.has(p.toLowerCase())
          ? '<span class="ops-badge">OP</span>'
          : `<button class="btn btn-active btn-sm" data-op="${esc(p)}">Op</button>`}
      </div>
    `).join('');
    list.querySelectorAll('[data-op]').forEach(btn =>
      btn.addEventListener('click', () => opPlayer(btn.dataset.op))
    );
  } catch (err) {
    list.innerHTML = `<div class="give-msg err">${esc(err.message)}</div>`;
  }
}

async function fetchCurrentOps() {
  const list = document.getElementById('currentOpsList');
  list.innerHTML = '<div class="give-msg">Loading…</div>';
  try {
    const ops = await apiJson('GET', `/api/worlds/${encodeURIComponent(_opsWorld)}/ops`);
    if (!ops.length) {
      list.innerHTML = '<div class="give-msg">No ops configured.</div>';
      return;
    }
    list.innerHTML = ops.map(op => `
      <div class="ops-player-row">
        <span class="ops-player-name">${esc(op.name)}</span>
        <button class="btn btn-danger btn-sm" data-deop="${esc(op.name)}">Deop</button>
      </div>
    `).join('');
    list.querySelectorAll('[data-deop]').forEach(btn =>
      btn.addEventListener('click', () => deopPlayer(btn.dataset.deop))
    );
  } catch (err) {
    list.innerHTML = `<div class="give-msg err">${esc(err.message)}</div>`;
  }
}

async function opPlayer(player) {
  if (_locks.has('opAction')) return;
  _locks.add('opAction');
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_opsWorld)}/rcon/op`, { player });
    showOpsStatus('✔ ' + (data.response || `${player} opped`), 'ok');
    await Promise.all([fetchOpsPlayers(), fetchCurrentOps()]);
  } catch (err) {
    showOpsStatus('✘ ' + err.message, 'err');
  } finally {
    _locks.delete('opAction');
  }
}

async function deopPlayer(player) {
  if (_locks.has('opAction')) return;
  _locks.add('opAction');
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_opsWorld)}/rcon/deop`, { player });
    showOpsStatus('✔ ' + (data.response || `${player} deopped`), 'ok');
    await Promise.all([fetchOpsPlayers(), fetchCurrentOps()]);
  } catch (err) {
    showOpsStatus('✘ ' + err.message, 'err');
  } finally {
    _locks.delete('opAction');
  }
}

function showOpsStatus(msg, type) {
  const el = document.getElementById('opsStatus');
  el.textContent = msg;
  el.className = 'give-status ' + type;
  el.classList.remove('hidden');
}

document.getElementById('refreshOpsPlayersBtn').addEventListener('click', () => withLock('opsRefresh', fetchOpsPlayers));
document.getElementById('refreshOpsListBtn').addEventListener('click', () => withLock('opsRefresh', fetchCurrentOps));

document.getElementById('opByNameBtn').addEventListener('click', async () => {
  const input = document.getElementById('opByNameInput');
  const player = input.value.trim();
  if (!player || _locks.has('opAction')) return;
  input.value = '';
  await opPlayer(player);
});

document.getElementById('opByNameInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('opByNameBtn').click();
});

document.getElementById('manageOpsHudBtn').addEventListener('click', async () => {
  if (!_activeWorld) return;
  await openOps(_activeWorld);
});

// ── RCON Console ──────────────────────────────────────────────────────────────
document.getElementById('rconSendBtn').addEventListener('click', sendRconCommand);
document.getElementById('rconCmdInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendRconCommand();
});

async function sendRconCommand() {
  if (!_activeWorld || _locks.has('rcon')) return;
  const input = document.getElementById('rconCmdInput');
  const output = document.getElementById('rconOutput');
  const sendBtn = document.getElementById('rconSendBtn');
  const cmd = input.value.trim();
  if (!cmd) return;
  _locks.add('rcon');
  sendBtn.disabled = true;
  input.disabled = true;
  output.textContent = 'Sending…';
  output.classList.remove('hidden');
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_activeWorld)}/rcon/exec`, { command: cmd });
    output.textContent = data.response || '(empty response)';
    input.value = '';
  } catch (err) {
    output.textContent = 'Error: ' + err.message;
  } finally {
    _locks.delete('rcon');
    sendBtn.disabled = false;
    input.disabled = false;
  }
}

// ── Whitelist ─────────────────────────────────────────────────────────────────
async function loadWhitelist() {
  return withLock('loadWhitelist', async () => {
  const list = document.getElementById('whitelistPlayers');
  const checkbox = document.getElementById('whitelistEnabled');
  try {
    const data = await apiJson('GET', '/api/server/whitelist');
    checkbox.checked = data.enabled;
    if (!data.players.length) {
      list.innerHTML = '<li class="hud-list-empty">No players whitelisted.</li>';
    } else {
      list.innerHTML = data.players.map(p => `
        <li>
          <span>${esc(p.name)}</span>
          <button class="btn btn-danger btn-sm" data-whitelist-remove="${esc(p.name)}">✕</button>
        </li>
      `).join('');
    }
  } catch {
    checkbox.checked = false;
    list.innerHTML = '<li class="hud-list-empty">No active world.</li>';
  }
  });
}

function showWhitelistStatus(msg, ok) {
  const el = document.getElementById('whitelistStatus');
  el.textContent = msg;
  el.className = 'status-text ' + (ok ? 'ok' : 'err');
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 3000);
}

document.getElementById('whitelistEnabled').addEventListener('change', async (e) => {
  if (_locks.has('whitelist')) { e.target.checked = !e.target.checked; return; }
  _locks.add('whitelist');
  e.target.disabled = true;
  try {
    await apiJson('POST', '/api/server/whitelist', { enabled: e.target.checked });
    showWhitelistStatus(e.target.checked ? 'Whitelist enabled' : 'Whitelist disabled', true);
  } catch (err) {
    e.target.checked = !e.target.checked;
    showWhitelistStatus(err.message, false);
  } finally {
    _locks.delete('whitelist');
    e.target.disabled = false;
  }
});

document.getElementById('whitelistAddBtn').addEventListener('click', async () => {
  const input = document.getElementById('whitelistAddInput');
  const name = input.value.trim();
  if (!name || _locks.has('whitelist')) return;
  const btn = document.getElementById('whitelistAddBtn');
  btn.disabled = true;
  _locks.add('whitelist');
  try {
    await apiJson('POST', '/api/server/whitelist/add', { name });
    input.value = '';
    await loadWhitelist();
    showWhitelistStatus(`Added ${name}`, true);
  } catch (err) {
    showWhitelistStatus(err.message, false);
  } finally {
    _locks.delete('whitelist');
    btn.disabled = false;
  }
});

document.getElementById('whitelistAddInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('whitelistAddBtn').click();
});

document.getElementById('whitelistPlayers').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-whitelist-remove]');
  if (!btn || btn.disabled || _locks.has('whitelist')) return;
  btn.disabled = true;
  _locks.add('whitelist');
  const name = btn.dataset.whitelistRemove;
  try {
    await apiJson('DELETE', `/api/server/whitelist/${encodeURIComponent(name)}`);
    await loadWhitelist();
    showWhitelistStatus(`Removed ${name}`, true);
  } catch (err) {
    showWhitelistStatus(err.message, false);
    btn.disabled = false;
  } finally {
    _locks.delete('whitelist');
  }
});

// ── Server Logs ───────────────────────────────────────────────────────────────
async function loadServerLogs() {
  if (_logsFetchInFlight) return;
  _logsFetchInFlight = true;
  const output = document.getElementById('serverLogsOutput');
  const pathEl = document.getElementById('serverLogsPath');
  try {
    const data = await apiJson('GET', '/api/server/logs?lines=300');
    output.textContent = data.content || '(empty log file)';
    pathEl.textContent = data.path || '';
    output.scrollTop = output.scrollHeight;
  } catch (err) {
    output.textContent = err.message;
    pathEl.textContent = '';
  } finally {
    _logsFetchInFlight = false;
  }
}

document.getElementById('refreshLogsBtn').addEventListener('click', () => withLock('logs', loadServerLogs));

// ── Collapsible sections ───────────────────────────────────────────────────────
document.querySelectorAll('.collapsible-header').forEach(header => {
  header.addEventListener('click', () => {
    const bodyId = header.dataset.toggle;
    const body = document.getElementById(bodyId);
    const arrow = header.querySelector('.collapse-arrow');
    const collapsed = body.classList.toggle('hidden');
    if (arrow) arrow.textContent = collapsed ? '▸' : '▾';
    if (bodyId === 'whitelistBody' && !collapsed) loadWhitelist();
    if (bodyId === 'serverLogsBody' && !collapsed) loadServerLogs();
  });
});

// ── Init ──────────────────────────────────────────────────────────────────────
loadSettings();
loadWorlds();
loadJars();
loadServerStatus();
