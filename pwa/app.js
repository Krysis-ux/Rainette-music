/* Rainette Music — phone client.
 *
 * This app holds no music. It is a remote for one specific computer: the one
 * that approved this phone. Pairing therefore has a real waiting state (the
 * person at that computer has to say yes), and connection state is the most
 * important thing on screen at any moment.
 */

const STORAGE = {
  endpoint: 'rainette.pwa.endpoint',
  token: 'rainette.pwa.token',
  deviceId: 'rainette.pwa.device_id',
  recent: 'rainette.pwa.recent',
};

const state = {
  endpoint: localStorage.getItem(STORAGE.endpoint) || '',
  token: localStorage.getItem(STORAGE.token) || '',
  deviceId: localStorage.getItem(STORAGE.deviceId) || '',
  connected: false,
  computerName: '',
  library: [],
  searchResults: [],
  queue: [],
  queueIndex: -1,
  currentTrack: null,
  eventRevision: 0,
  eventLoopId: 0,
  streamRefreshAttempted: false,
  pairPollId: 0,
};

const $ = selector => document.querySelector(selector);
const setupView = $('#setupView');
const appView = $('#appView');
const tabBar = $('#tabBar');
const player = $('#player');
const audio = $('#audio');
const pairLinkInput = $('#pairLinkInput');
const setupError = $('#setupError');
const statusDot = $('#statusDot');
const statusText = $('#statusText');
const computerLabel = $('#computerLabel');
const searchMessage = $('#searchMessage');
const pairWaiting = $('#pairWaiting');
const pairForm = $('#pairForm');
const pairDeviceName = $('#pairDeviceName');

function defaultDeviceName() {
  const agent = navigator.userAgent;
  if (/iPad/.test(agent)) return 'iPad';
  if (/iPhone/.test(agent)) return 'iPhone';
  if (/Android/.test(agent)) return 'Android phone';
  return 'Phone';
}

function normalizeEndpoint(value) {
  const url = new URL(String(value || '').trim());
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) {
    throw new Error('The companion address must use trusted HTTPS.');
  }
  url.pathname = url.pathname.replace(/\/$/, '');
  url.search = '';
  url.hash = '';
  return url.toString().replace(/\/$/, '');
}

/* The pairing link carries the endpoint and a short-lived invitation in the
 * fragment, so neither is ever sent to the static host that serves this page. */
function readPairingParams(source) {
  const hash = String(source || '').split('#')[1] || '';
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const endpoint = params.get('endpoint');
  const invitation = params.get('invitation');
  if (!endpoint || !invitation) return null;
  return { endpoint, invitation };
}

function setStatus(kind, text) {
  statusDot.className = 'status-dot' + (kind ? ` ${kind}` : '');
  statusText.textContent = text;
}

function showSetup(message = '') {
  state.connected = false;
  state.eventLoopId += 1;
  state.pairPollId += 1;
  setupView.hidden = false;
  appView.hidden = true;
  tabBar.hidden = true;
  player.hidden = true;
  pairWaiting.hidden = true;
  pairForm.hidden = false;
  setupError.textContent = message;
  setStatus('', 'Not connected');
}

function showApp(status) {
  state.connected = true;
  state.computerName = status.name || 'your computer';
  setupView.hidden = true;
  appView.hidden = false;
  tabBar.hidden = false;
  computerLabel.textContent = `Playing from ${state.computerName}`;
  setStatus('online', state.computerName);
  startEventLoop();
}

async function api(path, options = {}) {
  if (!state.endpoint || !state.token) throw new Error('Rainette is not connected to a computer.');
  const headers = new Headers(options.headers || {});
  headers.set('Authorization', `Bearer ${state.token}`);
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(state.endpoint + path, { ...options, headers, cache: 'no-store' });
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { ok: false, msg: `The computer returned HTTP ${response.status}.` };
  }
  if (!response.ok || payload?.ok === false) {
    const error = new Error(payload?.msg || `Request failed with HTTP ${response.status}.`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function command(type, payload = {}, timeoutMs = 35000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await api('/command', {
      method: 'POST',
      signal: controller.signal,
      body: JSON.stringify({
        type,
        id: `${type}_${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`,
        ...payload,
      }),
    });
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('The computer took too long to respond.');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

/* ── Pairing ──────────────────────────────────────────────────────────────
 * request  → the computer shows this phone in its approval list
 * poll     → 202 while nobody has answered, 200 once approved
 * ack      → tells the computer the credential is stored, closing the claim
 */

async function pairPost(endpoint, path, body) {
  const response = await fetch(endpoint + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch { /* an error body is optional */ }
  return { status: response.status, payload };
}

async function startPairing(endpoint, invitation, deviceName) {
  const normalized = normalizeEndpoint(endpoint);
  setupError.textContent = '';
  setStatus('busy', 'Asking to pair…');

  const requested = await pairPost(normalized, '/pair/request', {
    invitation,
    device_name: deviceName || defaultDeviceName(),
  });
  if (requested.status !== 202) {
    throw new Error(requested.payload?.msg || 'That pairing code is no longer valid. Create a new one on your computer.');
  }

  pairForm.hidden = true;
  pairWaiting.hidden = false;
  setStatus('busy', 'Waiting for approval');
  await awaitApproval(normalized, invitation, requested.payload.request_id);
}

async function awaitApproval(endpoint, invitation, requestId) {
  const pollId = ++state.pairPollId;
  const deadline = Date.now() + 5 * 60 * 1000;

  while (pollId === state.pairPollId && Date.now() < deadline) {
    const { status, payload } = await pairPost(endpoint, '/pair/result', {
      request_id: requestId,
      invitation,
    });

    if (status === 200 && payload.device_token) {
      state.endpoint = endpoint;
      state.token = payload.device_token;
      state.deviceId = payload.device_id || '';
      localStorage.setItem(STORAGE.endpoint, state.endpoint);
      localStorage.setItem(STORAGE.token, state.token);
      localStorage.setItem(STORAGE.deviceId, state.deviceId);
      // Acknowledge with the credential so the computer can retire the claim.
      await api('/pair/ack', { method: 'POST', body: JSON.stringify({ request_id: requestId }) })
        .catch(() => { /* the credential already works; ack is housekeeping */ });
      pairWaiting.hidden = true;
      if (await testConnection()) refreshLibrary();
      return;
    }
    if (status === 202) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      continue;
    }
    throw new Error(
      status === 410
        ? 'That pairing code expired. Create a new one on your computer.'
        : payload?.msg || 'The computer declined this phone.',
    );
  }
  if (pollId === state.pairPollId) throw new Error('Nobody approved this phone. Try pairing again.');
}

function pairFromLink(rawLink) {
  const params = readPairingParams(rawLink) || readPairingParams('#' + String(rawLink || '').trim());
  if (!params) throw new Error('That does not look like a Rainette pairing link.');
  return params;
}

/* ── Connection ───────────────────────────────────────────────────────── */

async function testConnection({ reveal = true } = {}) {
  setStatus('busy', 'Connecting…');
  try {
    const status = await api('/status');
    showApp(status);
    if (reveal) switchTab('home');
    return true;
  } catch (error) {
    setStatus('', 'Offline');
    if (reveal) showSetup(connectionError(error));
    return false;
  }
}

function connectionError(error) {
  if (error?.status === 401) return 'This phone is no longer paired. Create a new pairing code on your computer.';
  if (error?.status === 403) return 'This address is not in the computer’s allowed list.';
  return error?.message || 'The computer could not be reached. Check that Rainette is running on it.';
}

/* ── Rendering ────────────────────────────────────────────────────────── */

function trackKey(track) {
  return String(track?.source_id || track?.video_id || track?.url || `${track?.title || ''}|${track?.artist || ''}`);
}

function artwork(track) {
  return track?.thumbnail_url || track?.artwork_url || './icon.svg';
}

function renderTracks(container, tracks, emptyMessage) {
  container.replaceChildren();
  if (!tracks.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = emptyMessage;
    container.append(empty);
    return;
  }
  for (const track of tracks) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'track';
    button.dataset.trackKey = trackKey(track);

    const image = document.createElement('img');
    image.src = artwork(track);
    image.alt = '';
    image.loading = 'lazy';
    image.referrerPolicy = 'no-referrer';

    const copy = document.createElement('span');
    copy.className = 'track-copy';
    const title = document.createElement('b');
    title.textContent = track.title || 'Untitled';
    const artist = document.createElement('span');
    artist.textContent = track.artist || track.uploader || 'Unknown artist';
    copy.append(title, artist);

    button.append(image, copy);
    button.addEventListener('click', () => {
      const index = tracks.findIndex(item => trackKey(item) === trackKey(track));
      playTrack(track, tracks, Math.max(0, index)).catch(showPlaybackError);
    });
    container.append(button);
  }
  markPlayingRow();
}

function markPlayingRow() {
  const key = state.currentTrack ? trackKey(state.currentTrack) : null;
  for (const row of document.querySelectorAll('.track')) {
    row.classList.toggle('is-playing', !!key && row.dataset.trackKey === key);
  }
}

function readRecent() {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE.recent) || '[]');
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

function saveRecent(track) {
  const recent = [track, ...readRecent().filter(item => trackKey(item) !== trackKey(track))].slice(0, 20);
  try { localStorage.setItem(STORAGE.recent, JSON.stringify(recent)); } catch { /* storage quota */ }
  renderTracks($('#recentList'), recent.slice(0, 8), 'Play something from Search to begin your history.');
}

async function refreshLibrary() {
  const target = $('#libraryList');
  target.replaceChildren();
  const loading = document.createElement('p');
  loading.className = 'empty';
  loading.textContent = 'Syncing your library…';
  target.append(loading);
  try {
    const result = await command('music_library_index', { limit: 250 });
    state.library = result.tracks || result.items || [];
    renderTracks(target, state.library, 'Tracks you save on your computer show up here.');
  } catch (error) {
    renderTracks(target, [], connectionError(error));
  }
  renderTracks($('#recentList'), readRecent().slice(0, 8), 'Play something from Search to begin your history.');
}

async function search(query) {
  searchMessage.textContent = 'Searching on your computer…';
  $('#searchResults').replaceChildren();
  try {
    const result = await command('music_search', { query }, 45000);
    state.searchResults = result.items || result.tracks || [];
    searchMessage.textContent = state.searchResults.length
      ? `${state.searchResults.length} result${state.searchResults.length === 1 ? '' : 's'}`
      : 'Nothing matched that.';
    renderTracks($('#searchResults'), state.searchResults, 'Nothing matched that.');
  } catch (error) {
    searchMessage.textContent = connectionError(error);
  }
}

/* ── Playback ─────────────────────────────────────────────────────────── */

function absoluteMediaUrl(value) {
  return new URL(value, state.endpoint + '/').toString();
}

async function playTrack(track, queue = [track], index = 0, options = {}) {
  if (!track?.source_id) throw new Error('This track has no playable source.');
  state.queue = queue.slice();
  state.queueIndex = Math.max(0, Math.min(index, state.queue.length - 1));
  state.currentTrack = track;
  state.streamRefreshAttempted = !!options.forceRefresh;
  updatePlayer(track, true);
  markPlayingRow();
  setStatus('busy', 'Preparing audio…');

  const stream = await command('music_stream_url', {
    source_id: track.source_id,
    track,
    force_refresh: !!options.forceRefresh,
  }, 50000);
  if (!stream.url) throw new Error('The computer did not return an audio stream.');

  const resumeAt = Number(options.resumeAt || 0);
  audio.src = absoluteMediaUrl(stream.url);
  audio.load();
  if (resumeAt > 0) {
    audio.addEventListener('loadedmetadata', () => {
      if (Number.isFinite(audio.duration)) audio.currentTime = Math.min(resumeAt, Math.max(0, audio.duration - 1));
    }, { once: true });
  }
  await audio.play();
  setStatus('online', state.computerName || 'Connected');
  saveRecent(track);
  publishMediaSession(track);
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

function updatePlayer(track, loading = false) {
  player.hidden = false;
  $('#playerArt').src = artwork(track);
  $('#playerTitle').textContent = track?.title || 'Nothing playing';
  $('#playerArtist').textContent = loading ? 'Preparing on your computer…' : (track?.artist || 'Unknown artist');
  const playPause = $('#playPauseButton');
  playPause.dataset.state = loading ? 'loading' : (audio.paused ? 'paused' : 'playing');
  playPause.setAttribute('aria-label', audio.paused ? 'Play' : 'Pause');
}

function updateProgress() {
  const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
  const ratio = duration > 0 ? Math.min(1, audio.currentTime / duration) : 0;
  player.style.setProperty('--progress', String(ratio));
  $('#playerElapsed').textContent = formatTime(audio.currentTime);
  $('#playerDuration').textContent = duration > 0 ? formatTime(duration) : '';
  const scrubber = $('#playerScrubber');
  scrubber.max = String(duration > 0 ? duration : 0);
  if (document.activeElement !== scrubber) scrubber.value = String(audio.currentTime || 0);
}

function publishMediaSession(track) {
  if (!('mediaSession' in navigator)) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track?.title || 'Rainette Music',
      artist: track?.artist || '',
      album: track?.metadata?.album_name || track?.album || '',
      artwork: [{ src: new URL(artwork(track), location.href).toString(), sizes: '512x512' }],
    });
    navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
  } catch { /* metadata artwork may be rejected by older iOS builds */ }
}

function showPlaybackError(error) {
  setStatus('', 'Playback failed');
  searchMessage.textContent = error?.message || 'Playback failed.';
  if (state.currentTrack) updatePlayer(state.currentTrack, false);
}

async function playQueueOffset(offset) {
  if (!state.queue.length) return;
  const index = state.queueIndex + offset;
  if (index < 0 || index >= state.queue.length) return;
  await playTrack(state.queue[index], state.queue, index);
}

async function refreshExpiredStream() {
  if (!state.currentTrack || state.streamRefreshAttempted) return false;
  state.streamRefreshAttempted = true;
  const resumeAt = audio.currentTime || 0;
  try {
    await playTrack(state.currentTrack, state.queue, state.queueIndex, { forceRefresh: true, resumeAt });
    return true;
  } catch {
    return false;
  }
}

function switchTab(name) {
  document.querySelectorAll('.panel').forEach(panel => panel.classList.toggle('active', panel.dataset.panel === name));
  document.querySelectorAll('[data-tab]').forEach(button => {
    const active = button.dataset.tab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  });
  if (name === 'library' && !state.library.length) refreshLibrary();
  if (name === 'search') $('#searchInput').focus();
}

/* ── Live updates from the computer ───────────────────────────────────── */

async function startEventLoop() {
  const loopId = ++state.eventLoopId;
  while (state.connected && loopId === state.eventLoopId) {
    try {
      const result = await api(`/events?after=${state.eventRevision}&wait=25`);
      state.eventRevision = Number(result.revision || state.eventRevision);
      if (result.reset_required) {
        state.eventRevision = Number(result.revision || 0);
        refreshLibrary().catch(() => {});
      }
      for (const event of result.events || []) handleDesktopEvent(event.message || {});
    } catch (error) {
      if (!state.connected || loopId !== state.eventLoopId) return;
      setStatus('', 'Reconnecting…');
      await new Promise(resolve => setTimeout(resolve, 1800));
      if (error?.status === 401 || error?.status === 403) {
        showSetup(connectionError(error));
        return;
      }
    }
  }
}

function handleDesktopEvent(message) {
  if (!message || typeof message !== 'object') return;
  if (message.type === 'music_library_index_result' && Array.isArray(message.tracks || message.items)) {
    state.library = message.tracks || message.items;
    renderTracks($('#libraryList'), state.library, 'Tracks you save on your computer show up here.');
  }
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

$('#pairForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('#pairSubmit');
  button.disabled = true;
  try {
    const { endpoint, invitation } = pairFromLink(pairLinkInput.value);
    await startPairing(endpoint, invitation, pairDeviceName.value.trim());
  } catch (error) {
    pairForm.hidden = false;
    pairWaiting.hidden = true;
    setStatus('', 'Not connected');
    setupError.textContent = error?.message || 'Pairing failed.';
  } finally {
    button.disabled = false;
  }
});

$('#cancelPairing').addEventListener('click', () => {
  state.pairPollId += 1;
  pairWaiting.hidden = true;
  pairForm.hidden = false;
  setStatus('', 'Not connected');
});

$('#searchForm').addEventListener('submit', event => {
  event.preventDefault();
  const query = $('#searchInput').value.trim();
  if (query) search(query);
});

$('#connectionButton').addEventListener('click', () => {
  if (state.connected) switchTab('settings');
  else showSetup();
});

$('#libraryRefreshButton').addEventListener('click', refreshLibrary);
$('#testConnectionButton').addEventListener('click', () => testConnection({ reveal: false }));
$('#installHelpButton').addEventListener('click', () => $('#installDialog').showModal());
$('#disconnectButton').addEventListener('click', () => {
  state.endpoint = '';
  state.token = '';
  state.deviceId = '';
  state.connected = false;
  state.eventLoopId += 1;
  localStorage.removeItem(STORAGE.endpoint);
  localStorage.removeItem(STORAGE.token);
  localStorage.removeItem(STORAGE.deviceId);
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  showSetup('This phone is disconnected. Your computer still has it listed until you revoke it there.');
});

document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => switchTab(button.dataset.tab)));

$('#playPauseButton').addEventListener('click', async () => {
  if (!state.currentTrack) return;
  try {
    if (audio.paused) await audio.play();
    else audio.pause();
  } catch (error) {
    showPlaybackError(error);
  }
});
$('#previousButton').addEventListener('click', () => playQueueOffset(-1).catch(showPlaybackError));
$('#nextButton').addEventListener('click', () => playQueueOffset(1).catch(showPlaybackError));
$('#playerScrubber').addEventListener('input', event => {
  if (Number.isFinite(audio.duration)) audio.currentTime = Number(event.target.value);
});

for (const event of ['play', 'pause', 'loadedmetadata']) {
  audio.addEventListener(event, () => {
    if (state.currentTrack) updatePlayer(state.currentTrack, false);
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
  });
}
audio.addEventListener('timeupdate', updateProgress);
audio.addEventListener('loadedmetadata', updateProgress);
audio.addEventListener('ended', () => playQueueOffset(1).catch(showPlaybackError));
audio.addEventListener('error', async () => {
  if (!(await refreshExpiredStream())) showPlaybackError(new Error('The audio stream expired or became unavailable.'));
});

if ('mediaSession' in navigator) {
  const handlers = {
    play: () => audio.play(),
    pause: () => audio.pause(),
    previoustrack: () => playQueueOffset(-1),
    nexttrack: () => playQueueOffset(1),
    seekto: details => {
      if (Number.isFinite(details.seekTime)) audio.currentTime = details.seekTime;
    },
  };
  for (const [action, handler] of Object.entries(handlers)) {
    try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* unsupported action */ }
  }
}

window.addEventListener('online', () => state.connected && testConnection({ reveal: false }));
window.addEventListener('offline', () => setStatus('', 'Phone offline'));

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js').catch(() => {}));
}

/* ── Boot ─────────────────────────────────────────────────────────────── */

pairDeviceName.placeholder = defaultDeviceName();
renderTracks($('#recentList'), readRecent().slice(0, 8), 'Play something from Search to begin your history.');

const scanned = readPairingParams(location.hash);
if (scanned) {
  // Strip the invitation from the address bar before anything can copy it.
  history.replaceState(null, '', location.pathname + location.search);
  showSetup();
  startPairing(scanned.endpoint, scanned.invitation, '').catch(error => {
    pairForm.hidden = false;
    pairWaiting.hidden = true;
    setupError.textContent = error?.message || 'Pairing failed.';
  });
} else if (state.endpoint && state.token) {
  testConnection().then(ok => { if (ok) refreshLibrary(); });
} else {
  showSetup();
}
