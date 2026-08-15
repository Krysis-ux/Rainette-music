/* Rainette Music — phone client. A remote for the one computer that approved
 * it, so pairing has a real waiting state. This file owns transport, pairing
 * and connection; everything visual lives in ./src. */

import { state, STORAGE, artworkUrl, artistName, formatTime, readRecent, rememberRecent, trackKey } from './src/state.js';
import { fetchDesktopRecent, mergeRecent, fetchPlaylists, fetchPlaylistTracks, playlistSubtitle } from './src/collections.js';
import { $, el, icon, toast, stagger } from './src/dom.js';
import { configureBridge, isUnsupportedCommand } from './src/bridge.js';
import { configurePlayer, subscribe, playTrack, toggle, skip, currentTrack, isPlaying, isLoading, currentTime, duration, resetPlayback, isLinked } from './src/player.js';
import { renderTracks, markPlayingRows, configureTracks } from './src/tracks.js';
import { configureSync, startEventLoop, stopEventLoop } from './src/sync.js';
import { wireMiniBar } from './src/nowplaying.js';
import { openQueueSheet } from './src/queue.js';
import { configureExtras, setLinked, openOutputPicker, sleepShouldStopAfterTrack, openTrackMenu, openSleepTimer, sleepLabel } from './src/extras.js';
import { closeAllSheets } from './src/sheets.js';
import { openScanner, scanningIsPossible } from './src/scanner.js';
import { artistRow, albumRow, openArtist, openFollowedArtists, loadFollowed, artistRef } from './src/catalog.js';
import { sortControl, sortTracks } from './src/sorting.js';
import { openEqualizer, eqSummary, eqOnTrackLoaded } from './src/eq.js';

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
function isStandalone() {
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

/* Re-rendering a list into the same rows it already shows costs a full rebuild
 * and replays the entrance animation, which reads as a flash. */
function sameTracks(a, b) {
	return a.length === b.length && a.every((track, index) => trackKey(track) === trackKey(b[index]));
}

let shownRecent = [];

function renderRecent(recent = readRecent()) {
	const list = recent.slice(0, 12);
	if (sameTracks(list, shownRecent)) return;
	shownRecent = list;
	renderTracks($('#recentList'), list, {
		emptyMessage: 'Play something here or on your computer and it shows up.',
		onPlay: playFromList(list),
	});
}

/* The computer's history and this phone's are one list. Rendering the local
 * copy first keeps the tab populated while the computer answers. */
let recentInFlight = null;

async function refreshRecent() {
	renderRecent();
	// One request at a time: tab switches and playback both ask for this, and
	// two answers landing together re-render the list twice for no gain.
	if (recentInFlight) return recentInFlight;
	recentInFlight = (async () => {
		try {
			const desktop = await fetchDesktopRecent();
			if (desktop.length) renderRecent(mergeRecent(desktop));
		} finally {
			recentInFlight = null;
		}
	})();
	return recentInFlight;
}

/* ── Library and playlists ────────────────────────────────────────────────
 * One panel, two modes. Every async render claims a token and drops its result
 * if something else claimed the panel since, so a late answer cannot paint over
 * an open playlist. */

const library = { mode: 'songs', playlistId: null, token: 0 };

/* One control serves whichever list the panel is showing, because "sort" means
 * the same thing in all of them and a second chip per mode would be three
 * controls doing one job. */
const librarySort = sortControl({
	scope: 'library',
	onChange: () => {
		if (library.mode === 'songs') showLibraryTracks(state.library);
		else if (library.playlistId) renderPlaylistTracks(library.playlistId, library.playlistName);
	},
});
$('#librarySort').append(librarySort.node);

function claimLibrary() {
	return ++library.token;
}

function libraryOwns(token) {
	return token === library.token;
}

/* Sorting is only offered where there is a list of tracks to sort — the
 * playlist index and the artist list are neither. */
function paintLibrarySort() {
	const sortable = (library.mode === 'songs') || (library.mode === 'playlists' && !!library.playlistId);
	$('#librarySort').hidden = !sortable;
}

/** Render the song list from a payload the computer pushed on its own. */
function showLibraryTracks(tracks) {
	state.library = tracks;
	if (library.mode !== 'songs' || library.playlistId) return;
	claimLibrary();
	const sorted = sortTracks(tracks, librarySort.current());
	renderTracks($('#libraryList'), sorted, {
		emptyMessage: 'Tracks you save on your computer show up here.',
		onPlay: playFromList(sorted),
	});
}

async function refreshLibrary() {
	const token = claimLibrary();
	const target = $('#libraryList');
	// Only blank the panel when there is nothing to keep. Re-syncing over a list
	// the user is already reading should not take it away from them.
	if (!state.library.length) target.replaceChildren(el('p', 'empty', 'Syncing your library…'));
	try {
		const result = await command('music_library_index', { limit: 250 });
		if (!libraryOwns(token)) return;
		state.library = result.tracks || result.items || [];
		const sorted = sortTracks(state.library, librarySort.current());
		renderTracks(target, sorted, {
			emptyMessage: 'Tracks you save on your computer show up here.',
			onPlay: playFromList(sorted),
		});
	} catch (error) {
		if (!libraryOwns(token)) return;
		target.replaceChildren(el('p', 'empty', connectionError(error)));
	}
}

/* The artists behind the library, built from the tracks themselves rather than
 * fetched: the computer has no "artists in your library" command, and every
 * track already names one. Followed artists are folded in so the list is not
 * missing somebody just because none of their tracks are saved. */
async function renderLibraryArtists() {
	const token = claimLibrary();
	const target = $('#libraryList');
	target.replaceChildren(el('p', 'empty', 'Gathering artists…'));

	if (!state.library.length) {
		try {
			const result = await command('music_library_index', { limit: 250 });
			if (!libraryOwns(token)) return;
			state.library = result.tracks || result.items || [];
		} catch { /* followed artists alone are still a useful list */ }
	}
	const followedArtists = await loadFollowed();
	if (!libraryOwns(token)) return;

	const byName = new Map();
	for (const entry of followedArtists.map(artistRef)) {
		if (entry.name) byName.set(entry.name.toLowerCase(), { ...entry, count: 0 });
	}
	for (const track of state.library) {
		const name = artistName(track);
		if (!name) continue;
		const key = name.toLowerCase();
		const existing = byName.get(key);
		if (existing) {
			existing.count += 1;
			existing.id = existing.id || track?.metadata?.artist_id || '';
			existing.art = existing.art || track.thumbnail_url || '';
		} else {
			byName.set(key, {
				id: track?.metadata?.artist_id || '',
				name,
				art: track.thumbnail_url || '',
				subscribers: '',
				count: 1,
			});
		}
	}

	const artists = [...byName.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
	if (!artists.length) {
		target.replaceChildren(el('p', 'empty', 'Save some music on your computer, or follow an artist, and they show up here.'));
		return;
	}
	target.replaceChildren(...artists.map(artist => artistRow(artist)));
	stagger(target, ':scope > .collection-row');
}

async function renderLibraryPanel() {
	$('#libraryModeSongs')?.classList.toggle('active', library.mode === 'songs');
	$('#libraryModePlaylists')?.classList.toggle('active', library.mode === 'playlists');
	$('#libraryModeArtists')?.classList.toggle('active', library.mode === 'artists');
	paintLibrarySort();

	if (library.mode === 'songs') { library.playlistId = null; return refreshLibrary(); }
	if (library.mode === 'artists') { library.playlistId = null; return renderLibraryArtists(); }
	if (library.playlistId) return renderPlaylistTracks(library.playlistId, library.playlistName);

	const token = claimLibrary();
	const target = $('#libraryList');
	target.replaceChildren(el('p', 'empty', 'Loading your playlists…'));
	try {
		const playlists = await fetchPlaylists();
		if (!libraryOwns(token)) return;
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
				library.playlistId = playlist.id;
				library.playlistName = playlist.name;
				paintLibrarySort();
				renderPlaylistTracks(playlist.id, playlist.name);
			});
			return row;
		}));
		stagger(target, ':scope > .collection-row');
	} catch (error) {
		if (!libraryOwns(token)) return;
		target.replaceChildren(el('p', 'empty', connectionError(error)));
	}
}

async function renderPlaylistTracks(playlistId, name = '') {
	const token = claimLibrary();
	const target = $('#libraryList');
	target.replaceChildren(el('p', 'empty', 'Loading…'));
	const back = el('button', 'collection-back', '‹ All playlists');
	back.type = 'button';
	back.addEventListener('click', () => {
		library.playlistId = null;
		library.playlistName = '';
		paintLibrarySort();
		renderLibraryPanel();
	});
	try {
		const tracks = await fetchPlaylistTracks(playlistId);
		if (!libraryOwns(token)) return;
		target.replaceChildren(back);
		if (name) target.append(el('h2', 'collection-title', name));
		const list = el('div', 'track-list');
		target.append(list);
		const sorted = sortTracks(tracks, librarySort.current());
		renderTracks(list, sorted, {
			emptyMessage: 'This playlist is empty.',
			onPlay: playFromList(sorted),
		});
	} catch (error) {
		if (!libraryOwns(token)) return;
		target.replaceChildren(back, el('p', 'empty', connectionError(error)));
	}
}

/* ── Search ───────────────────────────────────────────────────────────────
 * Search asks for the catalog, not just a track list: an artist is not a track
 * with no source, and an album is not one either. Rendering all three as track
 * rows is what made tapping "2Slimey" try to play a person and fail.
 *
 * `music_search` remains the fallback, because a computer running an older
 * Rainette refuses the catalog command outright, and a phone that then shows
 * nothing at all is worse than one that shows songs. */

const searchState = { songs: [], artists: [], albums: [], filter: 'all', sorted: [] };
const searchSortHost = $('#searchSort');
const searchFilter = $('#searchFilter');

const searchSort = sortControl({
	scope: 'search',
	onChange: () => renderSearchResults(),
});
searchSortHost.append(searchSort.node);

async function search(query) {
	searchMessage.textContent = 'Searching on your computer…';
	$('#searchResults').replaceChildren();
	searchFilter.hidden = true;
	searchSortHost.hidden = true;

	let result;
	try {
		result = await command('music_catalog_search', { query }, 45000);
	} catch (error) {
		// A computer whose allow-list predates catalog search still answers the
		// flat one, and songs are most of what a search is for. Anything else —
		// a timeout, a dead tunnel — would only fail again more slowly.
		if (!isUnsupportedCommand(error)) { searchMessage.textContent = connectionError(error); return; }
		try {
			const flat = await command('music_search', { query }, 45000);
			result = { songs: flat.items || flat.tracks || [], artists: [], albums: [] };
		} catch (fallbackError) {
			searchMessage.textContent = connectionError(fallbackError);
			return;
		}
	}

	searchState.songs = Array.isArray(result.songs) ? result.songs : (result.items || result.tracks || []);
	searchState.artists = Array.isArray(result.artists) ? result.artists : [];
	searchState.albums = Array.isArray(result.albums) ? result.albums : [];
	state.searchResults = searchState.songs;

	const total = searchState.songs.length + searchState.artists.length + searchState.albums.length;
	if (!total) {
		searchMessage.textContent = result.msg || 'Nothing matched that.';
		$('#searchResults').replaceChildren(el('p', 'empty', 'Nothing matched that.'));
		return;
	}

	// Tabs for kinds that came back empty are dead ends, so they are hidden
	// rather than shown as something to tap and be disappointed by.
	const counts = { all: total, songs: searchState.songs.length, artists: searchState.artists.length, albums: searchState.albums.length };
	for (const button of searchFilter.querySelectorAll('button')) {
		button.hidden = counts[button.dataset.filter] === 0;
	}
	if (counts[searchState.filter] === 0) searchState.filter = 'all';
	searchFilter.hidden = false;

	searchMessage.textContent = result.msg
		|| `${total} result${total === 1 ? '' : 's'} · hold a row for more, swipe it to queue`;
	paintSearchFilter();
	renderSearchResults();
}

function paintSearchFilter() {
	for (const button of searchFilter.querySelectorAll('button')) {
		const on = button.dataset.filter === searchState.filter;
		button.classList.toggle('active', on);
		button.setAttribute('aria-selected', String(on));
	}
}

/** A labelled block inside the results. The heading is what lets the "Top" view
 *  put all three kinds in one scroller without them reading as one list. */
function resultSection(title, nodes, listClass = '') {
	const section = el('section', 'result-section');
	section.append(el('h2', 'result-heading', title));
	const list = el('div', listClass);
	list.append(...nodes);
	section.append(list);
	return { section, list };
}

function renderSearchResults() {
	const target = $('#searchResults');
	target.replaceChildren();
	const { filter, songs, artists, albums } = searchState;

	// Sorting is about a list of songs; artists and albums have their own order
	// from the computer and no field worth reordering them on here.
	const showSort = (filter === 'all' || filter === 'songs') && songs.length > 1;
	searchSortHost.hidden = !showSort;
	const orderedSongs = sortTracks(songs, searchSort.current());
	searchState.sorted = orderedSongs;

	if (filter === 'artists' || (filter === 'all' && artists.length)) {
		const { section } = resultSection('Artists', artists.slice(0, filter === 'all' ? 4 : artists.length).map(artistRow));
		target.append(section);
	}
	if (filter === 'albums' || (filter === 'all' && albums.length)) {
		const { section } = resultSection('Albums & singles', albums.slice(0, filter === 'all' ? 4 : albums.length).map(albumRow));
		target.append(section);
	}
	if (filter === 'songs' || filter === 'all') {
		const { section, list } = resultSection('Songs', [], 'track-list');
		target.append(section);
		renderTracks(list, orderedSongs, {
			emptyMessage: 'No songs matched that.',
			onPlay: (track, index) => {
				playTrack(track, orderedSongs, index).catch(showPlaybackError);
				renderRecent(rememberRecent(track));
			},
		});
	}
	if (!target.childElementCount) target.append(el('p', 'empty', 'Nothing of that kind matched.'));
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

/* Playback emits several times a second. Every write here is guarded and the
 * whole pass is coalesced onto one frame, because the main thread this loop
 * occupies is the same one that has to answer the next tap. */
const shownMini = { art: '', title: '', artist: '', transport: '', elapsed: '', total: '' };
let miniFrame = 0;

function scheduleMiniBar() {
	if (miniFrame) return;
	miniFrame = requestAnimationFrame(() => { miniFrame = 0; renderMiniBar(); });
}

function renderMiniBar() {
	const track = currentTrack();
	if (!track) { player.hidden = true; return; }
	player.hidden = false;

	const art = artworkUrl(track);
	if (art !== shownMini.art) { shownMini.art = art; $('#playerArt').src = art; }

	const title = track.title || 'Nothing playing';
	if (title !== shownMini.title) { shownMini.title = title; $('#playerTitle').textContent = title; }

	const loading = isLoading();
	const playing = isPlaying();
	const linked = isLinked();
	const artist = loading
		? 'Preparing on your computer…'
		: (artistName(track) || (linked ? state.computerName : 'Unknown artist'));
	if (artist !== shownMini.artist) { shownMini.artist = artist; $('#playerArtist').textContent = artist; }

	const transport = loading ? 'loading' : (playing ? 'playing' : 'paused');
	if (transport !== shownMini.transport) {
		shownMini.transport = transport;
		const button = $('#playPauseButton');
		button.dataset.state = transport;
		button.setAttribute('aria-label', playing ? 'Pause' : 'Play');
		player.classList.toggle('is-linked', linked);
	}

	const length = duration();
	const at = currentTime();
	player.style.setProperty('--progress', String(length > 0 ? Math.min(1, at / length) : 0));
	const elapsed = formatTime(at);
	if (elapsed !== shownMini.elapsed) { shownMini.elapsed = elapsed; $('#playerElapsed').textContent = elapsed; }
	const total = length > 0 ? formatTime(length) : '';
	if (total !== shownMini.total) { shownMini.total = total; $('#playerDuration').textContent = total; }

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

	// Each row says what it is currently set to, so the panel answers "is the
	// EQ on?" without having to be opened first.
	$('#equalizerState').textContent = eqSummary();
	$('#sleepState').textContent = sleepLabel() === 'Sleep' ? 'Off' : sleepLabel();
	$('#outputState').textContent = isLinked() ? (state.computerName || 'Your computer') : 'This phone';
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

configureBridge({
	command,
	mediaUrl: absoluteMediaUrl,
	events: ({ after, wait, follow }) => api(`/events?after=${after}&wait=${wait}&follow=${follow ? 1 : 0}`),
});
configurePlayer({
	onError: showPlaybackError,
	// An equaliser switched on with nothing playing has no element to route yet,
	// so it takes effect on the first track that loads.
	onSourceLoaded: () => { eqOnTrackLoaded().catch(() => {}); },
});
// Press and hold any row for its menu, which is how an artist is reachable from
// the library, the queue and the recent list rather than only from search.
configureTracks({ onMenu: (track, list) => openTrackMenu(track, list) });
configureSync({
	onStatus: setStatus,
	onLibrary: showLibraryTracks,
	// The desktop restarted, so nothing this phone holds is known to be current.
	onDesktopReset: () => {
		state.library = [];
		if (activeTab === 'library') renderLibraryPanel().catch(() => {});
		if (activeTab === 'home') refreshRecent().catch(() => {});
	},
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

subscribe(scheduleMiniBar);

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

/* Scanning from inside the app is the only route that pairs *this* copy. The
 * phone's own Camera app opens the browser, which on iOS is a different app
 * with different storage from the icon on the Home Screen. */
if (scanningIsPossible()) {
	$('#scanButton').hidden = false;
	$('#scanDivider').hidden = false;
	$('#scanButton').addEventListener('click', async () => {
		const scanned = await openScanner();
		if (!scanned) return;
		pairLinkInput.value = scanned;
		setupError.textContent = '';
		try {
			const { endpoint, invitation } = pairFromLink(scanned);
			await startPairing(endpoint, invitation, pairDeviceName.value.trim());
		} catch (error) {
			pairForm.hidden = false;
			pairWaiting.hidden = true;
			setStatus('', 'Not connected');
			setupError.textContent = error?.message || 'That code is not a Rainette pairing link.';
		}
	});
}

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

for (const button of searchFilter.querySelectorAll('button')) {
	button.addEventListener('click', () => {
		if (searchState.filter === button.dataset.filter) return;
		searchState.filter = button.dataset.filter;
		paintSearchFilter();
		renderSearchResults();
	});
}

/* Every sheet closes by popping its own history entry, so this is the one
 * signal that covers the equaliser, the sleep timer and the output picker
 * alike — each of which changes a value the settings rows display. */
window.addEventListener('popstate', () => {
	if (activeTab === 'settings') renderSettings();
});

$('#connectionButton').addEventListener('click', () => {
	if (state.connected) switchTab('settings');
	else showSetup();
});

$('#libraryRefreshButton').addEventListener('click', () => { state.library = []; renderLibraryPanel(); });

const LIBRARY_MODES = [['#libraryModeSongs', 'songs'], ['#libraryModePlaylists', 'playlists'], ['#libraryModeArtists', 'artists']];

for (const [id, mode] of LIBRARY_MODES) {
	$(id).addEventListener('click', () => {
		if (library.mode === mode) return;
		library.mode = mode;
		library.playlistId = null;
		library.playlistName = '';
		for (const [otherId] of LIBRARY_MODES) {
			$(otherId).setAttribute('aria-selected', String(otherId === id));
		}
		renderLibraryPanel();
	});
}
$('#testConnectionButton').addEventListener('click', () => testConnection({ reveal: false }));
$('#installHelpButton').addEventListener('click', () => $('#installDialog').showModal());
$('#outputButton').addEventListener('click', () => openOutputPicker().then(renderSettings));
$('#equalizerButton').addEventListener('click', () => openEqualizer());
$('#sleepButton').addEventListener('click', () => openSleepTimer().then(renderSettings));
$('#followedButton').addEventListener('click', () => openFollowedArtists());
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
