import './styles.css';

const app = document.querySelector('#app');

const state = {
  currentView: 'monitorizare',
  selectedVehicleId: null,
  user: null,
  accessToken: null,
  refreshToken: null,
  cameras: [],
  selectedCameraId: null,
  vehicles: [],
  violations: [],
  history: [],
  cameraAvailable: false,
  cameraLive: false,
  cameraTimer: null,
  cameraRequestInFlight: false,
  cameraObjectUrl: null,
  dataTimer: null,
  apiError: '',
};

const CAMERA_POLL_MS = 2000;
const CAMERA_RETRY_MS = 4000;

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}[character]));

let refreshRequest = null;
let cachedRefresh = null;

const apiRequest = async (path, options = {}) => {
  const send = (token) => {
    const headers = new Headers(options.headers || {});
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    return fetch(`/api/v1${path}`, { ...options, headers, cache: 'no-store' });
  };
  let response = await send(state.accessToken);
  if (response.status === 401 && state.accessToken) {
    if (state.refreshToken) {
      let tokens = cachedRefresh?.previous === state.refreshToken ? cachedRefresh.tokens : null;
      if (!tokens && !refreshRequest) {
        const previous = state.refreshToken;
        refreshRequest = fetch('/api/v1/auth/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: previous }),
        }).then(async (refreshed) => {
          if (!refreshed.ok) return null;
          const value = await refreshed.json();
          cachedRefresh = { previous, tokens: value };
          return value;
        }).finally(() => {
          refreshRequest = null;
        });
      }
      if (!tokens && refreshRequest) tokens = await refreshRequest;
      if (tokens) {
        state.accessToken = tokens.access_token;
        state.refreshToken = tokens.refresh_token;
        response = await send(state.accessToken);
      }
    }
    if (response.status === 401) {
      state.accessToken = null;
      state.refreshToken = null;
      state.user = null;
      cachedRefresh = null;
      state.currentView = 'login';
      renderApp();
    }
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `API error (${response.status})`);
  }
  return response;
};

const refreshMobileData = async () => {
  const camerasResponse = await apiRequest('/cameras');
  state.cameras = await camerasResponse.json();
  if (!state.selectedCameraId || !state.cameras.some((camera) => camera.id === state.selectedCameraId)) {
    state.selectedCameraId = state.cameras.find((camera) => camera.enabled)?.id || null;
  }
  const query = state.selectedCameraId ? `?camera_id=${encodeURIComponent(state.selectedCameraId)}` : '';
  const [dashboardResponse, historyResponse, incidentsResponse] = await Promise.all([
    apiRequest(`/dashboard${query}`),
    apiRequest(`/history${query}`),
    apiRequest('/incidents?status=PENDING'),
  ]);
  const dashboard = await dashboardResponse.json();
  const history = await historyResponse.json();
  const incidents = await incidentsResponse.json();
  const cameraNames = Object.fromEntries(state.cameras.map((camera) => [camera.id, camera.name]));
  state.vehicles = dashboard.vehicles.map((item) => ({
    id: item.vehicle_id,
    plate: item.plate || 'N/A',
    status: item.status === 'OVER_LIMIT' ? 'Depășire limită' : item.status === 'STATIONARY' ? 'Staționar' : 'În mișcare',
    durationMinutes: Math.floor(item.duration_seconds / 60),
    camera: cameraNames[item.camera_id] || item.camera_id,
    cameraId: item.camera_id,
    isActive: true,
    firstSeen: item.first_seen,
    lastSeen: item.last_seen,
    overLimitAt: item.status === 'OVER_LIMIT' ? item.last_seen : null,
  }));
  state.history = history.map((item) => ({
    id: item.vehicle_id,
    plate: item.plate || 'N/A',
    firstSeen: item.first_seen,
    lastSeen: item.last_seen,
    durationMinutes: Math.floor(item.duration_seconds / 60),
    overLimitAt: item.status === 'OVER_LIMIT' ? item.last_seen : null,
    status: item.status,
  }));
  state.violations = incidents.map((item) => ({
    id: item.id,
    vehicleId: item.vehicle_id,
    plate: item.plate || 'N/A',
    durationMinutes: Math.floor(item.duration_seconds / 60),
    camera: cameraNames[item.camera_id] || item.camera_id,
    overLimitAt: item.timestamp,
    status: item.status,
    evidenceId: item.evidence_id,
  }));
  state.apiError = '';
};

const startDataPolling = () => {
  if (state.dataTimer) clearInterval(state.dataTimer);
  state.dataTimer = setInterval(() => {
    if (!state.accessToken) return;
    void refreshMobileData().then(renderApp).catch((error) => {
      state.apiError = error.message || 'Serviciul ParkingAI este indisponibil.';
      renderApp();
    });
  }, 5000);
};

const clearCameraTimer = () => {
  if (state.cameraTimer) {
    clearTimeout(state.cameraTimer);
    state.cameraTimer = null;
  }
};

const requestNextSnapshot = () => {
  if (state.currentView !== 'monitorizare' || state.selectedVehicleId || state.cameraRequestInFlight || !state.selectedCameraId) {
    return;
  }

  const frame = document.querySelector('.camera-frame');
  if (!frame) return;

  const img = frame.querySelector('img[data-live-camera]');
  if (!img) return;

  const fallback = frame.querySelector('[data-camera-fallback]');
  const status = document.querySelector('[data-camera-status]');

  state.cameraRequestInFlight = true;
  apiRequest(`/cameras/${encodeURIComponent(state.selectedCameraId)}/live`)
    .then((response) => response.blob())
    .then((blob) => {
      const objectUrl = URL.createObjectURL(blob);
      if (state.cameraObjectUrl) URL.revokeObjectURL(state.cameraObjectUrl);
      state.cameraObjectUrl = objectUrl;
      img.src = objectUrl;
    state.cameraAvailable = true;
    state.cameraLive = true;
    state.cameraRequestInFlight = false;
    if (status) {
      status.textContent = 'LIVE';
      status.classList.remove('offline');
      status.classList.add('online');
    }
    img.style.display = 'block';
    if (fallback) fallback.style.display = 'none';
    clearCameraTimer();
    state.cameraTimer = setTimeout(() => {
      if (state.currentView === 'monitorizare' && !state.selectedVehicleId) {
        requestNextSnapshot();
      }
    }, CAMERA_POLL_MS);
    })
    .catch(() => {
    state.cameraAvailable = false;
    state.cameraLive = false;
    state.cameraRequestInFlight = false;
    if (status) {
      status.textContent = 'OFFLINE';
      status.classList.remove('online');
      status.classList.add('offline');
    }
    img.style.display = 'none';
    if (fallback) fallback.style.display = 'grid';
    clearCameraTimer();
    state.cameraTimer = setTimeout(() => {
      if (state.currentView === 'monitorizare' && !state.selectedVehicleId) {
        requestNextSnapshot();
      }
    }, CAMERA_RETRY_MS);
    });
};

const startCameraLiveLoop = () => {
  const frame = document.querySelector('.camera-frame');
  if (!frame) return;

  let img = frame.querySelector('img[data-live-camera]');
  if (!img) {
    img = document.createElement('img');
    img.setAttribute('data-live-camera', 'true');
    img.alt = 'Camera live';
    img.style.display = 'block';
    frame.prepend(img);
  }

  clearCameraTimer();
  state.cameraRequestInFlight = false;
  requestNextSnapshot();
};

const formatMinutes = (minutes) => {
  const total = Math.max(0, minutes);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
};

const formatTimestamp = (iso) => new Date(iso).toLocaleString('ro-RO', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

const formatDuration = (seconds) => formatMinutes(Math.floor(Math.max(0, seconds || 0) / 60));

function createLoginScreen() {
  return `
    <div class="app-shell login-shell">
      <div class="login-card">
        <div class="brand-wrap">
          <div class="brand-mark">P</div>
          <div>
            <h1>ParkingAI</h1>
            <p>Poliția Locală</p>
          </div>
        </div>

        <form class="login-form" id="login-form">
          <label>
            <span>Utilizator</span>
            <input type="text" placeholder="Nume utilizator" autocomplete="username" />
          </label>
          <label>
            <span>Parolă</span>
            <input type="password" placeholder="Parolă" autocomplete="current-password" />
          </label>
          <button type="submit" class="primary-btn">Autentificare</button>
        </form>
      </div>
    </div>
  `;
}

function renderVehicleCard(vehicle) {
  const urgent = vehicle.status.startsWith('Depășire');
  return `
    <button class="vehicle-card ${urgent ? 'violation' : ''}" data-id="${vehicle.id}">
      <div class="card-top">
        <div>
          <span class="label">ID</span>
          <strong>#${escapeHtml(vehicle.id)}</strong>
        </div>
        <span class="status-badge ${urgent ? 'danger' : vehicle.status === 'Staționar' ? 'warning' : 'ok'}">${escapeHtml(vehicle.status)}</span>
      </div>
      <div class="plate-row">${escapeHtml(vehicle.plate)}</div>
      <div class="meta-grid">
        <div>
          <span>Timp staționare</span>
          <strong>${formatMinutes(vehicle.durationMinutes)}</strong>
        </div>
        <div>
          <span>Cameră</span>
          <strong>${escapeHtml(vehicle.camera)}</strong>
        </div>
      </div>
    </button>
  `;
}

function renderMonitorizare() {
  const activeVehicles = state.vehicles.filter((v) => v.isActive);
  const violations = activeVehicles.filter((v) => v.status.startsWith('Depășire'));
  const selectedCamera = state.cameras.find((camera) => camera.id === state.selectedCameraId);

  return `
    <div class="screen">
      <header class="topbar">
        <div>
          <p class="eyebrow">Status sistem</p>
          <h2>ONLINE</h2>
        </div>
        <div class="system-pill">
          <span class="dot"></span>
          Camera activă: ${escapeHtml(selectedCamera?.name || 'Neselectată')}
        </div>
      </header>

      <section class="panel live-camera-panel">
        <div class="live-header">
          <h3>CAMERA LIVE</h3>
          <span class="live-pill ${state.cameraAvailable ? 'online' : 'offline'}" data-camera-status>
            <span class="dot"></span>
            ${state.cameraAvailable ? 'LIVE' : 'OFFLINE'}
          </span>
        </div>

        <div class="camera-frame">
          <div class="camera-unavailable" data-camera-fallback style="display: ${state.cameraAvailable ? 'none' : 'grid'};">Camera indisponibilă</div>
        </div>
        <label class="camera-picker">
          Cameră
          <select data-camera-select>
            <option value="">Selectează camera</option>
            ${state.cameras.map((camera) => `<option value="${escapeHtml(camera.id)}" ${camera.id === state.selectedCameraId ? 'selected' : ''}>${escapeHtml(camera.name)} · ${camera.enabled ? 'activă' : 'inactivă'}</option>`).join('')}
          </select>
        </label>
      </section>

      <section class="stats-grid">
        <div class="stat-box">
          <span>Cameră activă</span>
          <strong>${escapeHtml(selectedCamera?.name || 'Nicio cameră')}</strong>
        </div>
        <div class="stat-box">
          <span>Vehicule detectate</span>
          <strong>${activeVehicles.length}</strong>
        </div>
        <div class="stat-box danger">
          <span>Depășiri</span>
          <strong>${violations.length}</strong>
        </div>
      </section>

      <section class="panel">
        <div class="section-head">
          <h3>Vehicule</h3>
          <span>${activeVehicles.length} active</span>
        </div>
        <div class="vehicle-list">
          ${activeVehicles.map(renderVehicleCard).join('')}
        </div>
      </section>
    </div>
  `;
}

function renderDepasiri() {
  const violations = state.violations;
  return `
    <div class="screen">
      <section class="panel">
        <div class="section-head">
          <h3>Depășiri</h3>
          <span>${violations.length} în evidență</span>
        </div>
        <div class="vehicle-list">
          ${violations.map((vehicle) => `
            <div class="violation-row" data-incident="${escapeHtml(vehicle.id)}">
              <div class="row-main">
                <div>
                  <span class="label">ID</span>
                  <strong>#${escapeHtml(vehicle.vehicleId)}</strong>
                </div>
                <div class="plate-wrap">${escapeHtml(vehicle.plate)}</div>
              </div>
              <div class="row-meta">
                <div><span>Durata</span><strong>${formatMinutes(vehicle.durationMinutes)}</strong></div>
                <div><span>Depășire</span><strong>${formatTimestamp(vehicle.overLimitAt)}</strong></div>
                <div><span>Camera</span><strong>${escapeHtml(vehicle.camera)}</strong></div>
              </div>
              ${['ADMIN', 'OPERATOR'].includes(state.user.role) ? `
                <div class="decision-actions">
                  <button class="primary-btn" data-decision="confirmed" data-incident="${escapeHtml(vehicle.id)}">Confirmă</button>
                  <button class="secondary-btn" data-decision="rejected" data-incident="${escapeHtml(vehicle.id)}">Respinge</button>
                </div>
              ` : ''}
            </div>
          `).join('')}
        </div>
      </section>
    </div>
  `;
}

function renderIstoric() {
  return `
    <div class="screen">
      <section class="panel">
        <div class="section-head">
          <h3>Istoric</h3>
          <span>${state.history.length} evenimente</span>
        </div>
        <div class="history-list">
          ${state.history.map((item) => `
            <div class="history-row">
              <div class="history-top">
                <div>
                  <span class="label">ID vehicul</span>
                  <strong>#${escapeHtml(item.id)}</strong>
                </div>
                <span class="status-badge ${item.status === 'În atenție' ? 'warning' : 'ok'}">${item.status}</span>
              </div>
              <div class="plate-row">${escapeHtml(item.plate)}</div>
              <div class="meta-grid compact">
                <div><span>Prima observare</span><strong>${formatTimestamp(item.firstSeen)}</strong></div>
                <div><span>Ultima observare</span><strong>${formatTimestamp(item.lastSeen)}</strong></div>
                <div><span>Durata</span><strong>${formatMinutes(item.durationMinutes)}</strong></div>
                <div><span>Depășire</span><strong>${item.overLimitAt ? formatTimestamp(item.overLimitAt) : '—'}</strong></div>
              </div>
            </div>
          `).join('')}
        </div>
      </section>
    </div>
  `;
}

function renderProfile() {
  return `
    <div class="screen">
      <section class="panel profile-panel">
        <div class="profile-header">
          <div class="avatar">${escapeHtml(state.user.username.charAt(0).toUpperCase())}</div>
          <div>
            <h3>${escapeHtml(state.user.username)}</h3>
            <p>${escapeHtml(state.user.role)}</p>
          </div>
        </div>
        <div class="profile-grid">
          <div><span>Unitate</span><strong>Poliția Locală</strong></div>
          <div><span>Zona</span><strong>Centru Nord</strong></div>
          <div><span>Camere configurate</span><strong>${state.cameras.length}</strong></div>
          <div><span>Versiune</span><strong>ParkingAI Mobile</strong></div>
        </div>
        <button class="secondary-btn" id="logout-btn">Deconectare</button>
      </section>
    </div>
  `;
}

function renderVehicleDetails(vehicle) {
  return `
    <div class="screen detail-screen">
      <section class="panel detail-panel">
        <button class="back-btn" data-action="back-to-monitorizare">← Înapoi</button>
        <div class="detail-header">
          <div>
            <span class="label">ID</span>
            <h3>#${escapeHtml(vehicle.id)}</h3>
          </div>
          <span class="status-badge ${vehicle.status === 'Depășire 15+' ? 'danger' : 'warning'}">${vehicle.status}</span>
        </div>

        <div class="detail-image">
          <div class="placeholder-image">Dovada este disponibilă prin API-ul incidentului.</div>
        </div>

        <div class="detail-grid">
          <div><span>Număr</span><strong>${escapeHtml(vehicle.plate)}</strong></div>
          <div><span>Status</span><strong>${escapeHtml(vehicle.status)}</strong></div>
          <div><span>Durată staționare</span><strong>${formatMinutes(vehicle.durationMinutes)}</strong></div>
          <div><span>Camera</span><strong>${escapeHtml(vehicle.camera)}</strong></div>
          <div><span>Prima observare</span><strong>${formatTimestamp(vehicle.firstSeen)}</strong></div>
          <div><span>Ultima observare</span><strong>${formatTimestamp(vehicle.lastSeen)}</strong></div>
          <div><span>Moment depășire</span><strong>${vehicle.overLimitAt ? formatTimestamp(vehicle.overLimitAt) : '—'}</strong></div>
        </div>
      </section>
    </div>
  `;
}

function renderBottomNav() {
  const items = [
    { key: 'monitorizare', label: 'Monitorizare' },
    { key: 'depasiri', label: 'Depășiri' },
    { key: 'istoric', label: 'Istoric' },
    { key: 'profil', label: 'Profil' },
  ];

  return `
    <nav class="bottom-nav">
      ${items.map((item) => `
        <button class="nav-item ${state.currentView === item.key ? 'active' : ''}" data-view="${item.key}">
          <span>${item.label}</span>
        </button>
      `).join('')}
    </nav>
  `;
}

function renderApp() {
  const shell = document.createElement('div');
  shell.className = 'app-shell';

  const content = document.createElement('main');
  content.className = 'content';

  if (state.currentView === 'login') {
    content.innerHTML = createLoginScreen();
    app.innerHTML = '';
    if (state.apiError) {
      const warning = document.createElement('div');
      warning.className = 'login-error';
      warning.textContent = state.apiError;
      app.appendChild(warning);
    }
    app.appendChild(content);
    return;
  }

  if (state.selectedVehicleId) {
    const selected = [...state.vehicles, ...state.violations, ...state.history].find((v) =>
      String(v.id) === String(state.selectedVehicleId) || String(v.vehicleId) === String(state.selectedVehicleId)
    );
    if (selected) {
      content.innerHTML = renderVehicleDetails(selected);
    }
  } else {
    const views = {
      monitorizare: renderMonitorizare,
      depasiri: renderDepasiri,
      istoric: renderIstoric,
      profil: renderProfile,
    };
    content.innerHTML = views[state.currentView]?.() || renderMonitorizare();
  }

  app.innerHTML = '';
  app.appendChild(content);
  app.insertAdjacentHTML('beforeend', renderBottomNav());

  if (state.currentView === 'monitorizare' && !state.selectedVehicleId) {
    startCameraLiveLoop();
  } else if (state.cameraTimer) {
    clearInterval(state.cameraTimer);
    state.cameraTimer = null;
  }
}

app.addEventListener('click', (event) => {
  const vehicleCard = event.target.closest('.vehicle-card');
  if (vehicleCard) {
    state.selectedVehicleId = vehicleCard.dataset.id;
    renderApp();
    return;
  }

  const violationRow = event.target.closest('.violation-row');
  if (violationRow && !event.target.closest('[data-decision]')) {
    const incidentId = violationRow.dataset.incident;
    const incident = state.violations.find((item) => item.id === incidentId);
    if (incident) {
      state.selectedVehicleId = incident.vehicleId;
      renderApp();
    }
    return;
  }

  const navItem = event.target.closest('.nav-item');
  if (navItem) {
    state.currentView = navItem.dataset.view;
    state.selectedVehicleId = null;
    renderApp();
    return;
  }

  const backButton = event.target.closest('[data-action="back-to-monitorizare"]');
  if (backButton) {
    state.selectedVehicleId = null;
    state.currentView = 'monitorizare';
    renderApp();
    return;
  }

  const logoutBtn = event.target.closest('#logout-btn');
  if (logoutBtn) {
    if (state.refreshToken && state.accessToken) {
      void apiRequest('/auth/logout', {
        method: 'POST',
        body: JSON.stringify({ refresh_token: state.refreshToken }),
      }).catch(() => {});
    }
    state.accessToken = null;
    state.refreshToken = null;
    state.user = null;
    cachedRefresh = null;
    state.cameras = [];
    state.vehicles = [];
    state.violations = [];
    state.history = [];
    if (state.dataTimer) clearInterval(state.dataTimer);
    state.currentView = 'login';
    state.selectedVehicleId = null;
    renderApp();
    return;
  }

  const decisionButton = event.target.closest('[data-decision]');
  if (decisionButton) {
    const incidentId = decisionButton.dataset.incident;
    const decision = decisionButton.dataset.decision;
    if (incidentId && (decision === 'confirmed' || decision === 'rejected')) {
      void apiRequest(`/incidents/${encodeURIComponent(incidentId)}/decision`, {
        method: 'POST',
        body: JSON.stringify({
          decision,
          reason_code: decision === 'confirmed' ? 'AGENT_CONFIRMED' : 'AGENT_REJECTED',
        }),
      }).then(refreshMobileData).then(renderApp).catch((error) => {
        window.alert(`Decizia nu a putut fi salvată: ${error.message}`);
      });
    }
  }
});

app.addEventListener('change', (event) => {
  if (event.target.matches('[data-camera-select]')) {
    state.selectedCameraId = event.target.value || null;
    void refreshMobileData().then(renderApp).catch((error) => window.alert(error.message));
  }
});

app.addEventListener('submit', async (event) => {
  if (event.target.matches('#login-form')) {
    event.preventDefault();

    const form = event.target;
    const username = form.querySelector('input[type="text"]').value.trim();
    const password = form.querySelector('input[type="password"]').value.trim();

    try {
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || 'Autentificare eșuată');
      }
      const result = await response.json();
      state.accessToken = result.access_token;
      state.refreshToken = result.refresh_token;
      state.user = result.user;
      cachedRefresh = null;
      state.currentView = 'monitorizare';
      await refreshMobileData();
      renderApp();
      startDataPolling();
      startCameraLiveLoop();
    } catch (error) {
      const card = form.closest('.login-card');
      const existing = card.querySelector('.login-error');
      if (existing) existing.remove();
      const banner = document.createElement('div');
      banner.className = 'login-error';
      banner.textContent = error.message || 'Autentificare eșuată. Verificați datele de acces.';
      form.appendChild(banner);
    }
  }
});

state.currentView = 'login';
renderApp();

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js').catch(console.error);
  });
}
