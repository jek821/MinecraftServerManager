'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let _propsWorld = null;
let _imagesWorld = null;
let _deleteWorld = null;
let _generateJobId = null;
let _generatePoll = null;
let _serverHost = '';
let _giveWorld = null;
let _selectedPlayer = null;
let _selectedPainting = null;
let _activeWorld = null;

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
    e.target.classList.add('hidden');
  }
  if (e.target.dataset.close) {
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
      <button class="btn btn-danger"    data-action="delete"  data-world="${esc(w.name)}">Delete</button>
    </div>
  `;
  return card;
}

function esc(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Delegate all world card button clicks
document.getElementById('worldsGrid').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const name = btn.dataset.world;

  if (action === 'activate')    await activateWorld(name);
  if (action === 'download')    downloadWorld(name);
  if (action === 'properties')  await openProperties(name);
  if (action === 'images')      await openImages(name);
  if (action === 'delete')      openDeleteConfirm(name);
  if (action === 'rename')      startRename(btn, name);
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
    const newName = input.value.trim();
    if (!newName || newName === name) { cancelRename(); return; }
    confirm.disabled = cancel.disabled = true;
    try {
      await apiJson('POST', `/api/worlds/${encodeURIComponent(name)}/rename`, { new_name: newName });
      await loadWorlds();
    } catch (err) {
      alert('Rename failed: ' + err.message);
      cancelRename();
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
  try {
    await apiJson('POST', `/api/worlds/${encodeURIComponent(name)}/activate`);
    await loadWorlds();
  } catch (err) {
    alert('Error: ' + err.message);
  }
}

function downloadWorld(name) {
  window.location.href = `/api/worlds/${encodeURIComponent(name)}/download`;
}

// ── Properties Modal ──────────────────────────────────────────────────────────
async function openProperties(name) {
  _propsWorld = name;
  document.getElementById('propsModalTitle').textContent = `${name} — server.properties`;
  document.getElementById('propsEditor').value = 'Loading…';
  openModal('propsModal');
  try {
    const data = await apiJson('GET', `/api/worlds/${encodeURIComponent(name)}/properties`);
    document.getElementById('propsEditor').value = data.content;
  } catch (err) {
    document.getElementById('propsEditor').value = 'Error: ' + err.message;
  }
}

document.getElementById('savePropsBtn').addEventListener('click', async () => {
  if (!_propsWorld) return;
  const content = document.getElementById('propsEditor').value;
  try {
    await apiJson('POST', `/api/worlds/${encodeURIComponent(_propsWorld)}/properties`, { content });
    closeModal('propsModal');
  } catch (err) {
    alert('Save failed: ' + err.message);
  }
});

// ── Images Modal ──────────────────────────────────────────────────────────────
async function openImages(name) {
  _imagesWorld = name;
  document.getElementById('imagesModalTitle').textContent = `${name} — Painting Images`;
  document.getElementById('imageUploadStatus').textContent = '';
  const warn = document.getElementById('imagesHostWarning');
  warn.classList.toggle('hidden', !!_serverHost);
  openModal('imagesModal');
  await refreshImages();
  // Rebuild data pack for this world in the background so images take effect
  rebuildPaintingsForWorld(name);
}

async function rebuildPaintingsForWorld(name) {
  const btn = document.getElementById('applyPaintingsBtn');
  const status = document.getElementById('imageUploadStatus');
  if (btn) { btn.disabled = true; btn.textContent = 'Applying…'; }
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(name)}/rebuild-paintings`);
    if (status) {
      const pack = data.pack || {};
      let msg = 'Applied to world — restart the server for changes to take effect.';
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
  } catch (err) {
    if (status) {
      status.textContent = 'Apply failed: ' + err.message;
      status.className = 'status-text err';
    }
  }
  if (btn) { btn.disabled = false; btn.textContent = 'Apply to World'; }
}

document.getElementById('imagesHostLink').addEventListener('click', (e) => {
  e.preventDefault();
  closeModal('imagesModal');
  document.getElementById('serverHost').focus();
});

async function refreshImages() {
  if (!_imagesWorld) return;
  const list = document.getElementById('imagesList');
  try {
    const images = await apiJson('GET', `/api/worlds/${encodeURIComponent(_imagesWorld)}/images`);
    if (images.length === 0) {
      list.innerHTML = '<p class="loading">No images uploaded yet.</p>';
    } else {
      list.innerHTML = images.map(img => `
        <div class="image-item">
          <div>
            <span class="image-name">${esc(img.name)}</span>
            <span class="image-size"> — ${fmtBytes(img.size)}</span>
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
  if (!btn || !_imagesWorld) return;
  const imgName = btn.dataset.delImage;
  try {
    await apiJson('DELETE', `/api/worlds/${encodeURIComponent(_imagesWorld)}/images/${encodeURIComponent(imgName)}`);
    await refreshImages();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
});

document.getElementById('applyPaintingsBtn').addEventListener('click', () => {
  if (_imagesWorld) rebuildPaintingsForWorld(_imagesWorld);
});

document.getElementById('imageFileInput').addEventListener('change', async (e) => {
  if (!_imagesWorld) return;
  const status = document.getElementById('imageUploadStatus');
  const files = Array.from(e.target.files);
  if (!files.length) return;

  status.textContent = `Uploading ${files.length} file(s)…`;
  status.className = 'status-text';

  let ok = 0, fail = 0;
  for (const file of files) {
    const fd = new FormData();
    fd.append('image', file);
    try {
      const res = await fetch(`/api/worlds/${encodeURIComponent(_imagesWorld)}/images`, {
        method: 'POST', body: fd,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      ok++;
    } catch (err) {
      fail++;
    }
  }

  status.textContent = `${ok} uploaded${fail ? `, ${fail} failed` : ''}`;
  status.className = 'status-text ' + (fail ? 'err' : 'ok');
  e.target.value = '';
  await refreshImages();
});

// ── Delete Modal ──────────────────────────────────────────────────────────────
function openDeleteConfirm(name) {
  _deleteWorld = name;
  document.getElementById('deleteModalText').textContent =
    `Are you sure you want to permanently delete "${name}"? This cannot be undone.`;
  openModal('deleteModal');
}

document.getElementById('confirmDeleteBtn').addEventListener('click', async () => {
  if (!_deleteWorld) return;
  try {
    await apiJson('DELETE', `/api/worlds/${encodeURIComponent(_deleteWorld)}`);
    closeModal('deleteModal');
    await loadWorlds();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
});

// ── New World / Generate ──────────────────────────────────────────────────────
document.getElementById('uploadWorldInput').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = '';
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
  }
});

document.getElementById('newWorldBtn').addEventListener('click', () => {
  document.getElementById('newWorldName').value = '';
  document.getElementById('inheritProps').checked = true;
  document.getElementById('generateLog').classList.add('hidden');
  document.getElementById('generateLog').textContent = '';
  const startBtn = document.getElementById('startGenerateBtn');
  startBtn.disabled = false;
  startBtn.textContent = 'Generate';
  startBtn.onclick = null;
  document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
  openModal('generateModal');
});

document.getElementById('startGenerateBtn').addEventListener('click', async () => {
  const name = document.getElementById('newWorldName').value.trim();
  const inherit = document.getElementById('inheritProps').checked;

  if (!name) {
    alert('Please enter a world name.');
    return;
  }

  const logEl = document.getElementById('generateLog');
  logEl.textContent = '';
  logEl.classList.remove('hidden');
  document.getElementById('startGenerateBtn').disabled = true;
  // Prevent closing mid-generation
  document.getElementById('cancelGenerateBtn').removeAttribute('data-close');

  function appendLog(lines) {
    logEl.textContent += lines.join('\n') + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  }

  try {
    const { job_id } = await apiJson('POST', '/api/worlds/generate', {
      new_name: name,
      inherit_properties: inherit,
    });
    _generateJobId = job_id;

    let lastLogLen = 0;
    _generatePoll = setInterval(async () => {
      try {
        const job = await apiJson('GET', `/api/worlds/generate/${job_id}`);
        const newLines = job.log.slice(lastLogLen);
        if (newLines.length) appendLog(newLines);
        lastLogLen = job.log.length;

        if (job.status === 'done') {
          clearInterval(_generatePoll);
          appendLog(['', '✔ Done! World generated successfully.']);
          await loadWorlds();
          setTimeout(() => {
            closeModal('generateModal');
            document.getElementById('startGenerateBtn').disabled = false;
            document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
          }, 1200);
        } else if (job.status === 'error') {
          clearInterval(_generatePoll);
          appendLog(['', '✘ Error: ' + job.error]);
          document.getElementById('startGenerateBtn').disabled = false;
          document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
        }
      } catch {
        clearInterval(_generatePoll);
        appendLog(['Poll error — check server.']);
      }
    }, 1000);

  } catch (err) {
    appendLog(['Error starting generation: ' + err.message]);
    document.getElementById('startGenerateBtn').disabled = false;
    document.getElementById('cancelGenerateBtn').dataset.close = 'generateModal';
  }
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
  const url = document.getElementById('jarUrl').value.trim();
  const statusEl = document.getElementById('jarUpdateStatus');

  if (!url) {
    statusEl.textContent = 'Please enter a URL.';
    statusEl.className = 'status-text err';
    statusEl.classList.remove('hidden');
    return;
  }

  const btn = document.getElementById('updateJarBtn');
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

document.getElementById('refreshPlayersBtn').addEventListener('click', fetchPlayers);

document.getElementById('givePaintingBtn').addEventListener('click', async () => {
  if (!_selectedPlayer || !_selectedPainting || !_giveWorld) return;
  const statusEl = document.getElementById('giveStatus');
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_giveWorld)}/rcon/give`, {
      player: _selectedPlayer,
      painting: _selectedPainting,
    });
    statusEl.textContent = '✔ ' + (data.response || 'Given!');
    statusEl.className = 'give-status ok';
  } catch (err) {
    statusEl.textContent = '✘ ' + err.message;
    statusEl.className = 'give-status err';
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
    document.getElementById('jvmArgs').value = cfg.jvm_args || '';
    refreshServerIconPreview(!!cfg.has_server_icon);
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
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
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
  }
});

document.getElementById('removeServerIconBtn').addEventListener('click', async () => {
  const statusEl = document.getElementById('serverIconStatus');
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
  }
});

document.getElementById('autoDetectBtn').addEventListener('click', async () => {
  try {
    const det = await apiJson('GET', '/api/detect-host');
    if (det.host) document.getElementById('serverHost').value = det.host;
  } catch { /* silent */ }
});

document.getElementById('saveSettingsBtn').addEventListener('click', async () => {
  const host = document.getElementById('serverHost').value.trim();
  const jvmArgs = document.getElementById('jvmArgs').value.trim();
  const statusEl = document.getElementById('settingsStatus');
  try {
    const data = await apiJson('POST', '/api/config', { server_host: host, public_port: 25565, jvm_args: jvmArgs });
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
  }
});

// ── Logout ────────────────────────────────────────────────────────────────────
document.getElementById('logoutBtn').addEventListener('click', async () => {
  await api('POST', '/logout');
  location.reload();
});

// ── Server HUD ────────────────────────────────────────────────────────────────
async function loadServerStatus() {
  try {
    const data = await apiJson('GET', '/api/server/status');
    updateServerHud(data);
  } catch { /* silent */ }
}

function updateServerHud(data) {
  const dot           = document.getElementById('serverStatusDot');
  const label         = document.getElementById('serverStatusText');
  const startBtn      = document.getElementById('startServerBtn');
  const stopBtn       = document.getElementById('stopServerBtn');
  const giveHudBtn    = document.getElementById('givePaintingHudBtn');

  _activeWorld = data.world || null;

  dot.className = 'server-status-dot ' + data.state;

  const stateLabel = { running: 'RUNNING', starting: 'STARTING', stopped: 'STOPPED' }[data.state] || data.state.toUpperCase();
  label.textContent = data.world ? `${stateLabel} — ${data.world}` : stateLabel;

  startBtn.disabled   = data.state !== 'stopped';
  stopBtn.disabled    = data.state === 'stopped';
  giveHudBtn.disabled = data.state !== 'running';
  document.getElementById('manageOpsHudBtn').disabled = data.state !== 'running';

  document.getElementById('statPlayers').textContent =
    data.players_online != null ? `${data.players_online} / ${data.players_max}` : '—';
  document.getElementById('statUptime').textContent  = fmtUptime(data.uptime);
  document.getElementById('statVersion').textContent = data.version || '—';
  document.getElementById('statPort').textContent    = data.mc_port || '—';

  const m = data.metrics;
  document.getElementById('statJvmRam').textContent = m ? fmtBytes(m.rss_kb * 1024) : '—';
  document.getElementById('statSysRam').textContent = m
    ? `${fmtBytes(m.sys_mem_used_kb * 1024)} / ${fmtBytes(m.sys_mem_total_kb * 1024)}`
    : '—';
  document.getElementById('statCpu').textContent = m != null ? m.cpu_pct + '%' : '—';
}

document.getElementById('startServerBtn').addEventListener('click', async () => {
  const btn = document.getElementById('startServerBtn');
  btn.disabled = true;
  btn.textContent = 'Starting…';
  try {
    await apiJson('POST', '/api/server/start');
  } catch (err) {
    alert('Error: ' + err.message);
    btn.textContent = '▶ Start';
    btn.disabled = false;
  }
  await loadServerStatus();
});

document.getElementById('stopServerBtn').addEventListener('click', async () => {
  const btn = document.getElementById('stopServerBtn');
  btn.disabled = true;
  btn.textContent = 'Stopping…';
  try {
    await apiJson('POST', '/api/server/stop');
  } catch (err) {
    alert('Error: ' + err.message);
  }
  btn.textContent = '■ Stop';
  await loadServerStatus();
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
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_opsWorld)}/rcon/op`, { player });
    showOpsStatus('✔ ' + (data.response || `${player} opped`), 'ok');
    await Promise.all([fetchOpsPlayers(), fetchCurrentOps()]);
  } catch (err) {
    showOpsStatus('✘ ' + err.message, 'err');
  }
}

async function deopPlayer(player) {
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_opsWorld)}/rcon/deop`, { player });
    showOpsStatus('✔ ' + (data.response || `${player} deopped`), 'ok');
    await Promise.all([fetchOpsPlayers(), fetchCurrentOps()]);
  } catch (err) {
    showOpsStatus('✘ ' + err.message, 'err');
  }
}

function showOpsStatus(msg, type) {
  const el = document.getElementById('opsStatus');
  el.textContent = msg;
  el.className = 'give-status ' + type;
  el.classList.remove('hidden');
}

document.getElementById('refreshOpsPlayersBtn').addEventListener('click', fetchOpsPlayers);
document.getElementById('refreshOpsListBtn').addEventListener('click', fetchCurrentOps);

document.getElementById('opByNameBtn').addEventListener('click', async () => {
  const input = document.getElementById('opByNameInput');
  const player = input.value.trim();
  if (!player) return;
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

setInterval(loadServerStatus, 5000);

// ── RCON Console ──────────────────────────────────────────────────────────────
document.getElementById('rconSendBtn').addEventListener('click', sendRconCommand);
document.getElementById('rconCmdInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendRconCommand();
});

async function sendRconCommand() {
  if (!_activeWorld) return;
  const input = document.getElementById('rconCmdInput');
  const output = document.getElementById('rconOutput');
  const cmd = input.value.trim();
  if (!cmd) return;
  output.textContent = 'Sending…';
  output.classList.remove('hidden');
  try {
    const data = await apiJson('POST', `/api/worlds/${encodeURIComponent(_activeWorld)}/rcon/exec`, { command: cmd });
    output.textContent = data.response || '(empty response)';
  } catch (err) {
    output.textContent = 'Error: ' + err.message;
  }
}

// ── Collapsible sections ───────────────────────────────────────────────────────
document.querySelectorAll('.collapsible-header').forEach(header => {
  header.addEventListener('click', () => {
    const bodyId = header.dataset.toggle;
    const body = document.getElementById(bodyId);
    const arrow = header.querySelector('.collapse-arrow');
    const collapsed = body.classList.toggle('hidden');
    if (arrow) arrow.textContent = collapsed ? '▸' : '▾';
  });
});

// ── Init ──────────────────────────────────────────────────────────────────────
loadSettings();
loadWorlds();
loadJars();
loadServerStatus();
