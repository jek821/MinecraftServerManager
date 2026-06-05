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
function fmtBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1024 ** 2) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1024 ** 3) return (b / 1024 ** 2).toFixed(1) + ' MB';
  return (b / 1024 ** 3).toFixed(2) + ' GB';
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
      <span class="world-name">${esc(w.name)}</span>
      ${w.active ? '<span class="badge-active">ACTIVE</span>' : ''}
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
      <button class="btn btn-secondary" data-action="give"     data-world="${esc(w.name)}">Give Painting</button>
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
  if (action === 'give')        await openGive(name);
  if (action === 'delete')      openDeleteConfirm(name);
});

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
    list.innerHTML = `<div class="give-msg err">${esc(err.message)}</div>`;
  }
}

async function fetchPaintingsForGive() {
  const list = document.getElementById('paintingsList');
  list.innerHTML = '<div class="give-msg">Loading…</div>';
  try {
    const images = await apiJson('GET', `/api/worlds/${encodeURIComponent(_giveWorld)}/images`);
    const items = images.map(img => ({
      label: img.name,
      value: img.name.replace(/\.[^.]+$/, '').toLowerCase().replace(/[\s-]/g, '_'),
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
async function loadSettings() {
  try {
    const cfg = await apiJson('GET', '/api/config');
    _serverHost = cfg.server_host || '';
    document.getElementById('serverPort').value = cfg.port || 5000;
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

document.getElementById('autoDetectBtn').addEventListener('click', async () => {
  try {
    const det = await apiJson('GET', '/api/detect-host');
    if (det.host) document.getElementById('serverHost').value = det.host;
  } catch { /* silent */ }
});

document.getElementById('saveSettingsBtn').addEventListener('click', async () => {
  const host = document.getElementById('serverHost').value.trim();
  const port = document.getElementById('serverPort').value.trim() || '5000';
  const statusEl = document.getElementById('settingsStatus');
  try {
    await apiJson('POST', '/api/config', { server_host: host, port: parseInt(port, 10) });
    _serverHost = host;
    statusEl.textContent = '✔ Saved';
    statusEl.className = 'status-text ok';
    statusEl.classList.remove('hidden');
    setTimeout(() => statusEl.classList.add('hidden'), 2500);
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

// ── Init ──────────────────────────────────────────────────────────────────────
loadSettings();
loadWorlds();
loadJars();
