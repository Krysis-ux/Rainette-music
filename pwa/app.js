const STORAGE = {
  endpoint: 'rainette.pwa.endpoint',
  token: 'rainette.pwa.token',
  recent: 'rainette.pwa.recent',
};

const state = {
  endpoint: localStorage.getItem(STORAGE.endpoint) || '',
  token: localStorage.getItem(STORAGE.token) || '',
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
};

const $ = selector => document.querySelector(selector);
const setupView = $('#setupView');
const appView = $('#appView');
const tabBar = $('#tabBar');
const player = $('#player');
const audio = $('#audio');
const endpointInput = $('#endpointInput');
const tokenInput = $('#tokenInput');
const setupError = $('#setupError');
const statusDot = $('#statusDot');
const statusText = $('#statusText');
const computerLabel = $('#computerLabel');
const searchMessage = $('#searchMessage');

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

function consumePairingFragment() {
  if (!location.hash || location.hash.length < 2) return false;
  const params = new URLSearchParams(location.hash.slice(1));
  const endpoint = params.get('endpoint');
  const token = params.get('token');
  if (!endpoint || !token) return false;
  try {
    state.endpoint = normalizeEndpoint(endpoint);
    state.token = token;
    localStorage.setItem(STORAGE.endpoint, state.endpoint);
    localStorage.setItem(STORAGE.token, state.token);
    history.replaceState(null, '', location.pathname + location.search);
    return true;
  } catch (error) {
    setupError.textContent = error.message;
    return false;
  }
}

function setStatus(kind, text) {
  statusDot.className = 'status-dot' + (kind ? ` ${kind}` : '');
  statusText.textContent = text;
}

function showSetup(message = '') {
  state.connected = false;
  state.eventLoopId += 1;
  setupView.hidden = false;
  appView.hidden = true;
  tabBar.hidden = true;
  player.hidden = true;
  endpointInput.value = state.endpoint;
  tokenInput.value = state.token;
  setupError.textContent = message;
  setStatus('', 'Not connected');
}

function showApp(status) {
  state.connected = true;
  state.computerName = status.name || 'your computer';
  setupView.hidden = true;
  appView.hidden = false;
  tabBar.hidden = false;
  computerLabel.textContent = `Connected securely to ${state.computerName}.`;
  setStatus('online', state.computerName);
  startEventLoop();
}

async function api(path, options = {}) {
  if (!state.endpoint || !state.token) throw new Error('Rainette is not connected to a computer.');
  const headers = new Headers(options.headers || {});
  headers.set('Authorization', `Bearer ${state.token}`);
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(state.endpoint + path, {
    ...options,
    headers,
    cache: 'no-store',
  });
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
  if (error?.status === 401) return 'The access key was rejected. Generate or scan a fresh pairing link on your computer.';
  if (error?.status === 403) return 'This Vercel address is not in the companion’s allowed-origin list.';
  return error?.message || 'The computer could not be reached. Confirm that Rainette and the HTTPS tunnel are running.';
}

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

    const play = document.createElement('span');
    play.className = 'track-play';
    play.textContent = '▶';
    play.setAttribute('aria-hidden', 'true');

    button.append(image, copy, play);
    button.addEventListener('click', () => {
      const source = tracks;
      const index = source.findIndex(item => trackKey(item) === trackKey(track));
      playTrack(track, source, Math.max(0, index)).catch(showPlaybackError);
    });
    container.append(button);
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
  const targets = [$('#libraryList')];
  targets.forEach(target => {
    target.replaceChildren();
    const loading = document.createElement('p');
    loading.className = 'empty';
    loading.textContent = 'Syncing your library…';
    target.append(loading);
  });
  try {
    const result = await command('music_library_index', { limit: 250 });
    state.library = result.tracks || result.items || [];
    renderTracks($('#libraryList'), state.library, 'Your saved Rainette tracks will appear here.');
  } catch (error) {
    renderTracks($('#libraryList'), [], connectionError(error));
  }
  renderTracks($('#recentList'), readRecent().slice(0, 8), 'Play something from Search to begin your history.');
}

async function search(query) {
  searchMessage.textContent = 'Searching on your computer…';
  $('#searchResults').replaceChildren();
  try {
    const result = await command('music_search', { query }, 45000);
    state.searchResults = result.items || result.tracks || [];
    searchMessage.textContent = state.searchResults.length ? `${state.searchResults.length} results` : 'No results found.';
    renderTracks($('#searchResults'), state.searchResults, 'No matching music was found.');
  } catch (error) {
    searchMessage.textContent = connectionError(error);
  }
}

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

function updatePlayer(track, loading = false) {
  player.hidden = false;
  $('#playerArt').src = artwork(track);
  $('#playerTitle').textContent = track?.title || 'Nothing playing';
  $('#playerArtist').textContent = loading ? 'Preparing on your computer…' : (track?.artist || 'Unknown artist');
  $('#playPauseButton').textContent = loading ? '···' : (audio.paused ? '▶' : 'Ⅱ');
  $('#playPauseButton').setAttribute('aria-label', audio.paused ? 'Play' : 'Pause');
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
  document.querySelectorAll('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === name));
  if (name === 'library' && !state.library.length) refreshLibrary();
}

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
    renderTracks($('#libraryList'), state.library, 'Your saved Rainette tracks will appear here.');
  }
  if (message.type === 'music_now_playing' && message.track && audio.paused) {
    setStatus('online', `${state.computerName || 'Computer'} playing`);
  }
}

$('#connectionForm').addEventListener('submit', async event => {
  event.preventDefault();
  setupError.textContent = '';
  try {
    state.endpoint = normalizeEndpoint(endpointInput.value);
    state.token = tokenInput.value.trim();
    if (state.token.length < 16) throw new Error('The access key is incomplete.');
    localStorage.setItem(STORAGE.endpoint, state.endpoint);
    localStorage.setItem(STORAGE.token, state.token);
    await testConnection();
    if (state.connected) refreshLibrary();
  } catch (error) {
    setupError.textContent = connectionError(error);
  }
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

$('#refreshLibraryButton').addEventListener('click', refreshLibrary);
$('#libraryRefreshButton').addEventListener('click', refreshLibrary);
$('#testConnectionButton').addEventListener('click', () => testConnection({ reveal: false }));
$('#installHelpButton').addEventListener('click', () => $('#installDialog').showModal());
$('#disconnectButton').addEventListener('click', () => {
  state.endpoint = '';
  state.token = '';
  state.connected = false;
  state.eventLoopId += 1;
  localStorage.removeItem(STORAGE.endpoint);
  localStorage.removeItem(STORAGE.token);
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  showSetup('This iPhone has been disconnected. The computer’s access key was not changed.');
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

for (const event of ['play', 'pause', 'ended', 'loadedmetadata']) {
  audio.addEventListener(event, () => {
    if (state.currentTrack) updatePlayer(state.currentTrack, false);
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
  });
}
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
window.addEventListener('offline', () => setStatus('', 'iPhone offline'));

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js').catch(() => {}));
}

const pairedFromLink = consumePairingFragment();
renderTracks($('#recentList'), readRecent().slice(0, 8), 'Play something from Search to begin your history.');
if (state.endpoint && state.token) {
  endpointInput.value = state.endpoint;
  tokenInput.value = state.token;
  testConnection().then(ok => { if (ok) refreshLibrary(); });
} else {
  showSetup(pairedFromLink ? 'Pairing details were saved. Connecting…' : '');
}
