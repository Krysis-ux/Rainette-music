/* Rainette Music — phone client.
 *
 * This app holds no music. It is a remote for one specific computer: the one
 * that approved this phone. Pairing therefore has a real waiting state (the
 * person at that computer has to say yes), and connection state is the most
 * important thing on screen at any moment.
 *
 * This file owns the parts that must not be guessed at: the transport and its
 * diagnosis, pairing, and connection lifecycle. Everything visual lives in
 * ./src — see src/player.js for playback and src/sync.js for how this phone and
 * the computer stay in step.
 */

import { state, STORAGE, artworkUrl, artistName, formatTime, readRecent, rememberRecent } from './src/state.js';
import { fetchDesktopRecent, mergeRecent, fetchPlaylists, fetchPlaylistTracks, playlistSubtitle } from './src/collections.js';
import { $, el, icon, toast } from './src/dom.js';
import { configureBridge } from './src/bridge.js';
import { configurePlayer, subscribe, playTrack, toggle, skip, currentTrack, isPlaying, isLoading, currentTime, duration, resetPlayback, isLinked } from './src/player.js';
import { renderTracks, markPlayingRows } from './src/tracks.js';
import { configureSync, startEventLoop, stopEventLoop } from './src/sync.js';
import { wireMiniBar } from './src/nowplaying.js';
import { openQueueSheet } from './src/queue.js';
import { configureExtras, setLinked, openOutputPicker, sleepShouldStopAfterTrack } from './src/extras.js';
import { closeAllSheets } from './src/sheets.js';

const setupView = $('#setupView');
const appView = $('#appView');
const tabBar = $('#tabBar');
const player = $('#player');
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

const LOCAL_HOSTNAMES = ['localhost', '127.0.0.1', '[::1]', '::1'];

function isLoopbackHost(hostname) {
	return LOCAL_HOSTNAMES.includes(String(hostname || '').toLowerCase());
}

/* True only when this page is itself being served from the computer, which is
 * the one situation where a loopback companion address is the right answer. */
function pageIsLoopback() {
	return isLoopbackHost(location.hostname);
}

function endpointHost(value) {
	try {
		return new URL(String(value || '')).hostname;
	} catch {
		return '';
	}
}

function normalizeEndpoint(value) {
	const url = new URL(String(value || '').trim());
	if (url.protocol !== 'https:' && !(url.protocol === 'http:' && isLoopbackHost(url.hostname))) {
		throw new Error('The companion address must use trusted HTTPS.');
	}
	url.pathname = url.pathname.replace(/\/$/, '');
	url.search = '';
	url.hash = '';
	return url.toString().replace(/\/$/, '');
}

/* A pairing code minted before the computer had a public address carries
 * `http://127.0.0.1:<port>`. On a phone that is the phone itself, and an HTTPS
 * page is not permitted to call it at all, so the request never leaves the
 * device. Catching it here turns a dead end into an instruction. */
function unusableEndpointReason(endpoint) {
	if (pageIsLoopback()) return '';
	if (!isLoopbackHost(endpointHost(endpoint))) return '';
	return 'This pairing code points at 127.0.0.1, which on a phone means the phone itself. On the computer open Rainette → Settings → Mobile, choose “Generate & use HTTPS tunnel”, then create a new pairing code.';
}

/* Browsers collapse DNS failure, refused connections, TLS problems, blocked
 * mixed content and rejected CORS into one TypeError whose whole message is
 * "Failed to fetch" (Chromium) or "Load failed" (WebKit). Anything useful about
 * the cause has to be reconstructed from what we already know. */
function describeTransportFailure(endpoint) {
	const unusable = unusableEndpointReason(endpoint);
	if (unusable) return unusable;
	const host = endpointHost(endpoint);
	return `Could not reach ${host || 'your computer'}. Check that Rainette is open there and its HTTPS tunnel is running, then create a new pairing code.`;
}

async function transport(url, options = {}) {
	try {
		return await fetch(url, options);
	} catch (error) {
		if (error?.name === 'AbortError') throw error;
		const failure = new Error(describeTransportFailure(url));
		failure.transport = true;
		throw failure;
	}
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

/* iOS gives a Home Screen app its own storage, separate from Safari's. Pairing
 * in the browser and then opening the icon therefore lands on an app that has
 * genuinely never been paired -- not a bug to fix, but the reason to say so
 * plainly instead of showing the same blank form twice. */
export function isStandalone() {
	return window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone === true;
}

function showSetup(message = '') {
	state.connected = false;
	stopEventLoop();
	state.pairPollId += 1;
	closeAllSheets();
	setupView.hidden = false;
	appView.hidden = true;
	tabBar.hidden = true;
	player.hidden = true;
	pairWaiting.hidden = true;
	pairForm.hidden = false;
	setupError.textContent = message;
	$('#setupStandaloneNote').hidden = !(isStandalone() && !state.token);
	setStatus('', 'Not connected');
}

function showApp(status) {
	state.connected = true;
	state.computerName = status.name || 'your computer';
	state.deviceId = status.device_id || state.deviceId;
	setupView.hidden = true;
	appView.hidden = false;
	tabBar.hidden = false;
	computerLabel.textContent = isLinked() ? `Linked to ${state.computerName}` : `Playing from ${state.computerName}`;
	setStatus('online', state.computerName);
	startEventLoop();
}

async function api(path, options = {}) {
	if (!state.endpoint || !state.token) throw new Error('Rainette is not connected to a computer.');
	const headers = new Headers(options.headers || {});
	headers.set('Authorization', `Bearer ${state.token}`);
	if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
	const response = await transport(state.endpoint + path, { ...options, headers, cache: 'no-store' });
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

function absoluteMediaUrl(value) {
	return new URL(value, state.endpoint + '/').toString();
}

/* ── Pairing ──────────────────────────────────────────────────────────────
 * request  → the computer shows this phone in its approval list
 * poll     → 202 while nobody has answered, 200 once approved
 * ack      → tells the computer the credential is stored, closing the claim
 */

async function pairPost(endpoint, path, body) {
	const response = await transport(endpoint + path, {
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

/* A computer that runs a Quick Tunnel gets a new hostname every time it starts,
 * so an already-paired phone mostly needs a new *address*, not a new identity.
 * The device credential it already holds is what proves the pairing, so if that
 * credential still authenticates at the new address there is nothing for anyone
 * to approve a second time. */
async function adoptEndpoint(endpoint) {
	if (!state.token || endpoint === state.endpoint) return false;
	const previous = state.endpoint;
	state.endpoint = endpoint;
	try {
		await api('/status');
		localStorage.setItem(STORAGE.endpoint, endpoint);
		return true;
	} catch {
		state.endpoint = previous;
		return false;
	}
}

async function startPairing(endpoint, invitation, deviceName) {
	const normalized = normalizeEndpoint(endpoint);
	setupError.textContent = '';

	// Refuse a code that cannot work before spending a doomed request on it.
	const unusable = unusableEndpointReason(normalized);
	if (unusable) throw new Error(unusable);

	setStatus('busy', 'Connecting…');
	if (await adoptEndpoint(normalized)) {
		pairWaiting.hidden = true;
		if (await testConnection()) refreshLibrary();
		return;
	}

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

/* ── Library, search, recent ──────────────────────────────────────────── */

function playFromList(list) {
	return (track, index) => {
		playTrack(track, list, Math.max(0, index)).catch(showPlaybackError);
		renderRecent(rememberRecent(track));
	};
}

function renderRecent(recent = readRecent()) {
	const list = recent.slice(0, 12);
	renderTracks($('#recentList'), list, {
		emptyMessage: 'Play something here or on your computer and it shows up.',
		onPlay: playFromList(list),
	});
}

/* The computer's history and this phone's are one list. Rendering the local
 * copy first keeps the tab populated while the computer answers. */
async function refreshRecent() {
	renderRecent();
	const desktop = await fetchDesktopRecent();
	if (desktop.length) renderRecent(mergeRecent(desktop));
}

async function refreshLibrary() {
	const target = $('#libraryList');
	target.replaceChildren(el('p', 'empty', 'Syncing your library…'));
	try {
		const result = await command('music_library_index', { limit: 250 });
		state.library = result.tracks || result.items || [];
		renderTracks(target, state.library, {
			emptyMessage: 'Tracks you save on your computer show up here.',
			onPlay: playFromList(state.library),
		});
	} catch (error) {
		target.replaceChildren(el('p', 'empty', connectionError(error)));
	}
	refreshRecent();
}

/* ── Playlists ────────────────────────────────────────────────────────────
 * The computer's playlists, browsable rather than only writable. Library is a
 * two-mode panel instead of a fifth tab: a phone tab bar stops being tappable
 * somewhere around four. */

let libraryMode = 'songs';
let openPlaylistId = null;

async function renderLibraryPanel() {
	const target = $('#libraryList');
	$('#libraryModeSongs')?.classList.toggle('active', libraryMode === 'songs');
	$('#libraryModePlaylists')?.classList.toggle('active', libraryMode === 'playlists');

	if (libraryMode === 'songs') { openPlaylistId = null; return refreshLibrary(); }
	if (openPlaylistId) return renderPlaylistTracks(openPlaylistId);

	target.replaceChildren(el('p', 'empty', 'Loading your playlists…'));
	try {
		const playlists = await fetchPlaylists();
		if (!playlists.length) {
			target.replaceChildren(el('p', 'empty', 'Playlists you make on your computer show up here.'));
			return;
		}
		target.replaceChildren(...playlists.map(playlist => {
			const row = el('button', 'collection-row');
			row.type = 'button';
			row.append(
				el('span', 'collection-mark', (playlist.name || '?').slice(0, 1).toUpperCase()),
				el('span', 'collection-copy',
					el('b', '', playlist.name || 'Untitled playlist'),
					el('span', '', playlistSubtitle(playlist))),
			);
			row.addEventListener('click', () => {
				openPlaylistId = playlist.id;
				renderPlaylistTracks(playlist.id, playlist.name);
			});
			return row;
		}));
	} catch (error) {
		target.replaceChildren(el('p', 'empty', connectionError(error)));
	}
}

async function renderPlaylistTracks(playlistId, name = '') {
	const target = $('#libraryList');
	target.replaceChildren(el('p', 'empty', 'Loading…'));
	const back = el('button', 'collection-back', '‹ All playlists');
	back.type = 'button';
	back.addEventListener('click', () => { openPlaylistId = null; renderLibraryPanel(); });
	try {
		const tracks = await fetchPlaylistTracks(playlistId);
		target.replaceChildren(back);
		if (name) target.append(el('h2', 'collection-title', name));
		const list = el('div', 'track-list');
		target.append(list);
		renderTracks(list, tracks, {
			emptyMessage: 'This playlist is empty.',
			onPlay: playFromList(tracks),
		});
	} catch (error) {
		target.replaceChildren(back, el('p', 'empty', connectionError(error)));
	}
}

async function search(query) {
	searchMessage.textContent = 'Searching on your computer…';
	$('#searchResults').replaceChildren();
	try {
		const result = await command('music_search', { query }, 45000);
		state.searchResults = result.items || result.tracks || [];
		searchMessage.textContent = state.searchResults.length
			? `${state.searchResults.length} result${state.searchResults.length === 1 ? '' : 's'} · swipe a row to queue it`
			: 'Nothing matched that.';
		renderTracks($('#searchResults'), state.searchResults, {
			emptyMessage: 'Nothing matched that.',
			onPlay: playFromList(state.searchResults),
		});
	} catch (error) {
		searchMessage.textContent = connectionError(error);
	}
}

function showPlaybackError(error) {
	setStatus('', 'Playback failed');
	toast(error?.message || 'Playback failed.', { icon: 'close' });
	renderMiniBar();
}

/* ── Mini bar ─────────────────────────────────────────────────────────────
 * The one persistent piece of playback UI. It is a button, not a strip of
 * text: tapping it opens the full card, which is what every phone player does
 * and what this client conspicuously did not. */

function renderMiniBar() {
	const track = currentTrack();
	if (!track) { player.hidden = true; return; }
	player.hidden = false;

	$('#playerArt').src = artworkUrl(track);
	$('#playerTitle').textContent = track.title || 'Nothing playing';
	$('#playerArtist').textContent = isLoading()
		? 'Preparing on your computer…'
		: (artistName(track) || (isLinked() ? state.computerName : 'Unknown artist'));

	const playing = isPlaying();
	const loading = isLoading();
	const button = $('#playPauseButton');
	button.dataset.state = loading ? 'loading' : (playing ? 'playing' : 'paused');
	button.setAttribute('aria-label', playing ? 'Pause' : 'Play');

	const length = duration();
	player.style.setProperty('--progress', String(length > 0 ? Math.min(1, currentTime() / length) : 0));
	$('#playerElapsed').textContent = formatTime(currentTime());
	$('#playerDuration').textContent = length > 0 ? formatTime(length) : '';
	player.classList.toggle('is-linked', isLinked());
	markPlayingRows();
}

/* ── Tabs ─────────────────────────────────────────────────────────────── */

let activeTab = 'home';

function switchTab(name) {
	const panels = [...document.querySelectorAll('.panel')];
	const order = panels.map(panel => panel.dataset.panel);
	// Panels slide in from the side the tab sits on, so moving between them
	// reads as lateral travel rather than a cut.
	const direction = order.indexOf(name) > order.indexOf(activeTab) ? 'from-right' : 'from-left';
	activeTab = name;

	for (const panel of panels) {
		const active = panel.dataset.panel === name;
		panel.classList.toggle('active', active);
		panel.classList.remove('from-right', 'from-left');
		if (active) panel.classList.add(direction);
	}
	for (const button of document.querySelectorAll('[data-tab]')) {
		const active = button.dataset.tab === name;
		button.classList.toggle('active', active);
		button.setAttribute('aria-current', active ? 'page' : 'false');
	}
	if (name === 'library' && !state.library.length) renderLibraryPanel();
	if (name === 'home') refreshRecent();
	if (name === 'search') $('#searchInput').focus();
	if (name === 'settings') renderSettings();
}

/* ── Settings ─────────────────────────────────────────────────────────── */

function renderSettings() {
	const link = $('#linkToggle');
	if (!link) return;
	link.setAttribute('aria-pressed', String(state.linked));
	link.querySelector('small').textContent = state.linked
		? `Mirroring ${state.computerName || 'your computer'} — this phone shows and controls what it plays`
		: 'This phone runs its own session, separate from the computer';
	$('#linkState').innerHTML = icon(state.linked ? 'check' : 'link', 18);
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

configureBridge({
	command,
	mediaUrl: absoluteMediaUrl,
	events: ({ after, wait, follow }) => api(`/events?after=${after}&wait=${wait}&follow=${follow ? 1 : 0}`),
});
configurePlayer({ onError: showPlaybackError });
configureSync({
	onStatus: setStatus,
	onLibrary: () => refreshLibrary().catch(() => {}),
	onAuthLost: error => showSetup(connectionError(error)),
});
configureExtras({
	onLinkChange: linked => {
		computerLabel.textContent = linked ? `Linked to ${state.computerName}` : `Playing from ${state.computerName}`;
		renderSettings();
		renderMiniBar();
		toast(linked ? `Following ${state.computerName}` : 'Playing on this phone', { icon: linked ? 'link' : 'phone' });
		// Re-assert the mode immediately instead of waiting out the poll.
		stopEventLoop();
		startEventLoop();
	},
});

subscribe(renderMiniBar);

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

$('#libraryRefreshButton').addEventListener('click', () => { state.library = []; renderLibraryPanel(); });

for (const [id, mode] of [['#libraryModeSongs', 'songs'], ['#libraryModePlaylists', 'playlists']]) {
	$(id).addEventListener('click', () => {
		if (libraryMode === mode) return;
		libraryMode = mode;
		openPlaylistId = null;
		for (const button of [$('#libraryModeSongs'), $('#libraryModePlaylists')]) {
			button.setAttribute('aria-selected', String(button.id === id.slice(1)));
		}
		renderLibraryPanel();
	});
}
$('#testConnectionButton').addEventListener('click', () => testConnection({ reveal: false }));
$('#installHelpButton').addEventListener('click', () => $('#installDialog').showModal());
$('#outputButton').addEventListener('click', () => openOutputPicker());
$('#linkToggle').addEventListener('click', () => setLinked(!state.linked));
$('#disconnectButton').addEventListener('click', () => {
	state.endpoint = '';
	state.token = '';
	state.deviceId = '';
	state.connected = false;
	stopEventLoop();
	localStorage.removeItem(STORAGE.endpoint);
	localStorage.removeItem(STORAGE.token);
	localStorage.removeItem(STORAGE.deviceId);
	resetPlayback();
	showSetup('This phone is disconnected. Your computer still has it listed until you revoke it there.');
});

for (const button of document.querySelectorAll('[data-tab]')) {
	button.addEventListener('click', () => switchTab(button.dataset.tab));
}

$('#playPauseButton').addEventListener('click', event => { event.stopPropagation(); toggle(); });
$('#prevButton').addEventListener('click', event => { event.stopPropagation(); skip(-1).catch(showPlaybackError); });
$('#nextButton').addEventListener('click', event => { event.stopPropagation(); skip(1).catch(showPlaybackError); });
$('#queueButton').addEventListener('click', event => { event.stopPropagation(); openQueueSheet(); });
wireMiniBar();

/* The sleep timer's "stop after this track" has to be checked as a track ends,
 * which only this layer sees — the player treats an ending track as a cue to
 * advance. */
subscribe(() => {
	if (!isPlaying() && sleepShouldStopAfterTrack()) toast('Stopped for the night', { icon: 'moon' });
});

window.addEventListener('online', () => state.connected && testConnection({ reveal: false }));
window.addEventListener('offline', () => setStatus('', 'Phone offline'));

if ('serviceWorker' in navigator) {
	window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js').catch(() => {}));
}

/* ── Boot ─────────────────────────────────────────────────────────────── */

pairDeviceName.placeholder = defaultDeviceName();
renderRecent();

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
