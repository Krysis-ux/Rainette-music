/**
 * Rainette Music — detached player window.
 *
 * Runs in its own borderless native pywebview window (movable across monitors).
 * Owns the single <audio> element, the playback queue, transport, and the Web
 * Audio graph (volume gain + 5-band equalizer). The browser window sends it
 * `music_remote_play` / `music_remote_control` over the socket; it broadcasts
 * `music_now_playing_set` back so the browser reflects the current track.
 *
 * EQ + cross-origin audio: Web Audio's MediaElementSource outputs silence for
 * cross-origin media, so plain playback uses the direct googlevideo URL (robust,
 * no bytes through Python). The first time the EQ is enabled we build the audio
 * graph and switch to the same-origin `/audio` proxy for the rest of the session.
 */

import { sendHelper, helperRequest } from './music_shell.js';
import { iconMarkup } from './rainette_icons.js';
import { PlaybackLoadGuard, MediaEventGate } from './playback_load_guard.mjs';

// ── Persistence keys ─────────────────────────────────────────────────────────
const LS = { vol: 'rw.mp.volume', eq: 'rw.mp.eqGains', eqOn: 'rw.mp.eqOn', loop: 'rw.mp.loop' };
const LOCAL_STREAM_TTL_MS = 50 * 60 * 1000;
const PREFETCH_AHEAD = 3;

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* best effort */ } }

// ── EQ configuration ─────────────────────────────────────────────────────────
const EQ_BANDS = [
	{ f: 60,    type: 'lowshelf',  label: 'Bass',   short: '60' },
	{ f: 250,   type: 'peaking',   label: 'Low',    short: '250' },
	{ f: 1000,  type: 'peaking',   label: 'Mid',    short: '1k' },
	{ f: 4000,  type: 'peaking',   label: 'High',   short: '4k' },
	{ f: 12000, type: 'highshelf', label: 'Treble', short: '12k' },
];
const EQ_MIN = -12, EQ_MAX = 12;   // dB
const PRESETS = {
	Flat:        [0, 0, 0, 0, 0],
	'Bass Boost': [8, 4, 0, 0, 1],
	Vocal:       [-2, 0, 4, 3, 1],
	Treble:      [0, 0, 0, 4, 8],
};

// ── State ────────────────────────────────────────────────────────────────────
const state = {
	queue: [],
	index: -1,
	loop: lsGet(LS.loop) === '1',
	playing: false,
	resolvingId: null,
	eqOn: lsGet(LS.eqOn) === '1',
	eqGains: loadEqGains(),
	volume: loadVolume(),
	pendingSeek: null,
	collapsed: true,
};

function loadEqGains() {
	try {
		const arr = JSON.parse(lsGet(LS.eq) || 'null');
		if (Array.isArray(arr) && arr.length === EQ_BANDS.length) return arr.map(n => clamp(Number(n) || 0, EQ_MIN, EQ_MAX));
	} catch { /* fall through */ }
	return EQ_BANDS.map(() => 0);
}
function loadVolume() {
	const raw = lsGet(LS.vol);
	if (raw == null || raw === '') return 1;   // default full volume (Number(null) is 0, not NaN)
	const v = Number(raw);
	return Number.isFinite(v) && v >= 0 ? clamp(v, 0, 1.5) : 1;
}

let audio = null;
let audioCtx = null, srcNode = null, gainNode = null, bands = [];
let graphBuilt = false;       // once true, playback stays on the same-origin proxy
let els = {};
const preResolveInFlight = new Set();
const loadGuard = new PlaybackLoadGuard();
const mediaEventGate = new MediaEventGate(loadGuard);
let activeLoad = null;
let activeMediaToken = null;
let activeMediaSrc = '';
let activeMediaBinding = null;
let mediaLifecycle = [];
let mediaSwitchBarrier = Promise.resolve();
let pauseAfterReload = null;

function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

function publicTrack(track) {
	if (!track || typeof track !== 'object') return null;
	const clean = {};
	for (const [key, value] of Object.entries(track)) {
		if (!key.startsWith('_')) clean[key] = value;
	}
	return clean;
}

function validTracks(tracks) {
	return (tracks || []).filter(t => t && t.source_id);
}

function queueDuration() {
	return state.queue.reduce((sum, track) => {
		const duration = Number(track?.duration_s || 0);
		return sum + (Number.isFinite(duration) && duration > 0 ? duration : 0);
	}, 0);
}

function trackKey(track) {
	return `${track?.source || 'youtube'}:${track?.source_id || ''}`;
}

function streamFresh(track) {
	if (!track?._url) return false;
	const hinted = Number(track._expiresHintS || 0) * 1000;
	const ttl = hinted > 0 ? Math.min(hinted, 5 * 60 * 60 * 1000) : LOCAL_STREAM_TTL_MS;
	return Date.now() - Number(track._urlAt || 0) < ttl;
}

function rememberStream(track, res) {
	track._url = res.url;
	track._urlAt = Date.now();
	track._expiresHintS = Number(res.expires_hint_s || 0);
}

function requestStream(track, opts = {}) {
	return helperRequest('music_stream_url', {
		source_id: track.source_id,
		track_id: opts.prefetch ? '' : (track.id || ''),
		track,
		prefetch: !!opts.prefetch,
		force_refresh: !!opts.forceRefresh,
		load_id: opts.loadToken?.generation,
	}, 20000);
}

function preResolveUpcoming() {
	const start = state.index + 1;
	for (let offset = 0; offset < PREFETCH_AHEAD; offset++) {
		const track = state.queue[start + offset];
		if (!track?.source_id || streamFresh(track)) continue;
		const key = trackKey(track);
		if (preResolveInFlight.has(key)) continue;
		preResolveInFlight.add(key);
		requestStream(track, { prefetch: true })
			.then(res => { if (res && res.ok !== false && res.url) rememberStream(track, res); })
			.catch(() => {})
			.finally(() => preResolveInFlight.delete(key));
	}
}

function _stopEmptyQueue() {
	activeLoad = loadGuard.begin('');
	activeMediaToken = null;
	activeMediaSrc = '';
	activeMediaBinding = null;
	mediaEventGate.invalidate();
	_clearMediaLifecycle();
	state.queue = [];
	state.index = -1;
	state.resolvingId = null;
	state.playing = false;
	if (audio) {
		audio.pause();
		audio.removeAttribute('src');
		audio.load();
	}
	_renderPlay();
	_broadcast('paused', false);
}

// ── Web Audio graph ──────────────────────────────────────────────────────────
function ensureAudioGraph() {
	if (graphBuilt) return graphBuilt === true;
	try {
		const Ctx = window.AudioContext || window.webkitAudioContext;
		audioCtx = new Ctx();
		_connectAudioGraph(audio);
		graphBuilt = true;
		applyEqGains(state.eqGains);
		applyVolume(state.volume);
		return true;
	} catch (err) {
		graphBuilt = 'failed';
		return false;
	}
}

function _connectAudioGraph(element) {
	const nextSrc = audioCtx.createMediaElementSource(element);
	let node = nextSrc;
	const nextBands = EQ_BANDS.map(b => {
			const f = audioCtx.createBiquadFilter();
			f.type = b.type;
			f.frequency.value = b.f;
			if (b.type === 'peaking') f.Q.value = 1.0;
			f.gain.value = 0;
			node.connect(f);
			node = f;
			return f;
		});
	const nextGain = audioCtx.createGain();
	node.connect(nextGain);
	nextGain.connect(audioCtx.destination);
	try { srcNode?.disconnect(); } catch { /* already disconnected */ }
	for (const band of bands) { try { band.disconnect(); } catch { /* already disconnected */ } }
	try { gainNode?.disconnect(); } catch { /* already disconnected */ }
	srcNode = nextSrc;
	bands = nextBands;
	gainNode = nextGain;
}

function _freshAudioElement() {
	const next = new Audio();
	next.preload = 'auto';
	next.volume = Math.min(1, state.volume);
	if (graphBuilt === true) {
		try {
			_connectAudioGraph(next);
		} catch {
			graphBuilt = 'failed';
			state.eqOn = false;
			lsSet(LS.eqOn, '0');
		}
	}
	audio = next;
	applyEqGains(state.eqGains);
	applyVolume(state.volume);
}

function applyEqGains(gains) {
	if (graphBuilt === true) {
		for (let i = 0; i < bands.length; i++) {
			bands[i].gain.value = state.eqOn ? (gains[i] || 0) : 0;
		}
	}
}

function applyVolume(v) {
	state.volume = clamp(v, 0, 1.5);
	if (graphBuilt === true && gainNode) { gainNode.gain.value = state.volume; audio.volume = 1; }
	else if (audio) { audio.volume = Math.min(1, state.volume); }
	lsSet(LS.vol, String(state.volume));
	if (els.volFill) els.volFill.style.width = (state.volume / 1.5 * 100) + '%';
	if (els.volVal) els.volVal.textContent = Math.round(state.volume * 100) + '%';
}

// ── Track loading ────────────────────────────────────────────────────────────
async function _loadCurrent({ preservePaused = false } = {}) {
	const track = state.queue[state.index];
	if (!track) return;
	const loadToken = loadGuard.begin(trackKey(track));
	pauseAfterReload = preservePaused ? loadToken.generation : null;
	activeLoad = loadToken;
	activeMediaToken = null;
	activeMediaSrc = '';
	activeMediaBinding = null;
	await _drainPreviousMedia();
	if (!loadGuard.isCurrent(loadToken, trackKey(state.queue[state.index]))) return;
	_freshAudioElement();
	_renderMeta(track, 'loading');
	_setSeek(0);
	state.resolvingId = track.source_id;
	// Reuse a still-fresh resolved URL (e.g. after an EQ toggle reload) so we
	// don't hit yt-dlp again just to switch the audio path.
	if (streamFresh(track)) {
		_applyAndPlay(track, track._url, loadToken);
		return;
	}
	_broadcast('loading', false);
	try {
		const res = await requestStream(track, { loadToken });
		if (!loadGuard.isCurrent(loadToken, trackKey(state.queue[state.index]))) return;
		if (!res || res.ok === false || !res.url) { await _retryOrFail(loadToken); return; }
		rememberStream(track, res);
		_applyAndPlay(track, res.url, loadToken);
	} catch {
		await _retryOrFail(loadToken);
	}
}

function _applyAndPlay(track, url, loadToken) {
	if (!loadGuard.isCurrent(loadToken, trackKey(state.queue[state.index]))) return;
	const mediaToken = loadGuard.advance(loadToken, trackKey(track));
	if (!mediaToken) return;
	activeLoad = mediaToken;
	_clearMediaLifecycle();
	// Once the EQ graph exists, the element source is bound; only same-origin
	// (proxied) media produces sound, so stay on the proxy for the session.
	let source;
	if (graphBuilt === true) {
		audio.crossOrigin = 'anonymous';
		source = '/audio?u=' + encodeURIComponent(url);
	} else {
		audio.removeAttribute('crossorigin');
		source = url;
	}
	const expectedSrc = new URL(source, document.baseURI).href;
	activeMediaToken = mediaToken;
	activeMediaSrc = expectedSrc;
	activeMediaBinding = mediaEventGate.bind(mediaToken, trackKey(track), expectedSrc, audio);
	_bindMediaLifecycle(activeMediaBinding, audio);
	audio.src = source;
	const resume = () => {
		if (state.pendingSeek != null) { try { audio.currentTime = state.pendingSeek; } catch { /* ignore */ } state.pendingSeek = null; }
	};
	if (pauseAfterReload === mediaToken.generation) {
		pauseAfterReload = null;
		const loaded = () => {
			if (!loadGuard.isCurrent(mediaToken, trackKey(state.queue[state.index]))) return;
			resume();
			state.playing = false;
			_renderPlay();
			_renderMeta(track, 'paused');
			_broadcast('paused', false);
		};
		audio.addEventListener('loadedmetadata', loaded, { once: true });
		mediaLifecycle.push(['loadedmetadata', loaded]);
		audio.load();
		return;
	}
	audio.play().then(() => {
		if (loadGuard.isCurrent(mediaToken, trackKey(state.queue[state.index]))) resume();
	}).catch(() => {
		// autoplay-policy block, decode error, etc. — surface it instead of
		// leaving "now playing" showing with no sound.
		_retryOrFail(mediaToken);
	});
}

function _terminalLoadFailure(loadToken) {
	const track = state.queue[state.index];
	if (!track || !loadGuard.isCurrent(loadToken, trackKey(track))) return false;
	_renderMeta(track, 'error');
	_broadcast('error', false);
	return false;
}

function _currentMediaEvent(binding = activeMediaBinding, owner = audio) {
	const track = state.queue[state.index];
	const currentSource = owner?.currentSrc || owner?.src || '';
	return !!track && owner === audio
		&& mediaEventGate.accepts(binding, trackKey(track), currentSource, owner);
}

function _clearMediaLifecycle() {
	if (!audio) return;
	for (const [type, handler] of mediaLifecycle) audio.removeEventListener(type, handler);
	mediaLifecycle = [];
}

async function _drainPreviousMedia() {
	const drain = async () => {
		mediaEventGate.invalidate();
		_clearMediaLifecycle();
		if (!audio) return;
		const hadSource = !!(audio.getAttribute('src') || audio.currentSrc);
		if (!hadSource) return;
		await new Promise(resolve => {
			let settled = false;
			const finish = () => { if (!settled) { settled = true; resolve(); } };
			audio.addEventListener('emptied', finish, { once: true });
			audio.pause();
			audio.removeAttribute('src');
			audio.load();
			setTimeout(finish, 80);
		});
		await new Promise(resolve => setTimeout(resolve, 0));
	};
	const task = mediaSwitchBarrier.then(drain, drain);
	mediaSwitchBarrier = task.catch(() => {});
	return task;
}

function _bindMediaLifecycle(binding, owner) {
	const loadstart = () => {
		const track = state.queue[state.index];
		if (!track) return;
		mediaEventGate.arm(binding, trackKey(track), owner.currentSrc || owner.src || '', owner);
	};
	owner.addEventListener('loadstart', loadstart);
	mediaLifecycle.push(['loadstart', loadstart]);
	const on = (type, run) => {
		const handler = event => {
			if (!_currentMediaEvent(binding, owner)) return;
			run(event);
		};
		owner.addEventListener(type, handler);
		mediaLifecycle.push([type, handler]);
	};
	on('playing', () => {
		state.playing = true; _renderPlay();
		const track = state.queue[state.index];
		if (track) _renderMeta(track, 'playing');
		_broadcast('playing', true);
		preResolveUpcoming();
	});
	on('pause', () => { state.playing = false; _renderPlay(); _broadcast('paused', false); });
	on('timeupdate', () => {
		if (audio.duration) _setSeek(audio.currentTime / audio.duration);
		if (els.cur) els.cur.textContent = fmt(audio.currentTime);
		_broadcastProgress();
	});
	on('seeked', () => _broadcastProgress(true));
	on('durationchange', () => { if (els.dur) els.dur.textContent = fmt(audio.duration); });
	on('ended', () => _next());
	on('error', () => _retryOrFail(binding.token));
}

async function _retryOrFail(loadToken) {
	const track = state.queue[state.index];
	const key = trackKey(track);
	if (!track || !loadGuard.isCurrent(loadToken, key)) return false;
	if (!loadGuard.claimRetry(loadToken, key)) {
		return _terminalLoadFailure(loadToken);
	}
	const retryToken = loadGuard.advance(loadToken, key);
	return retryToken ? _reResolveAndResume(retryToken) : false;
}

async function _reResolveAndResume(loadToken) {
	const track = state.queue[state.index];
	if (!track || !loadGuard.isCurrent(loadToken, trackKey(track))) return false;
	track._url = '';
	track._urlAt = 0;
	track._expiresHintS = 0;
	try {
		const res = await requestStream(track, { forceRefresh: true, loadToken });
		if (!loadGuard.isCurrent(loadToken, trackKey(state.queue[state.index]))) return false;
		if (res && res.ok !== false && res.url) {
			rememberStream(track, res);
			await _drainPreviousMedia();
			if (!loadGuard.isCurrent(loadToken, trackKey(state.queue[state.index]))) return false;
			_freshAudioElement();
			_applyAndPlay(track, res.url, loadToken);
			return true;
		}
	} catch { /* fall through */ }
	return _terminalLoadFailure(loadToken);
}

// ── Transport ────────────────────────────────────────────────────────────────

// Soft ~180ms volume ramp on pause/resume (Settings → Playback). Uses the EQ
// gain node when the graph exists, else the element volume. Any failure falls
// back to an instant pause/play - the fade is decoration, never a gatekeeper.
const FADE_MS = 180;
let _fadeToken = 0;

function fadeEnabled() {
	return lsGet('rainette.fadePlayPause') !== '0';
}

function _effectiveVolume() {
	return graphBuilt === true ? state.volume : Math.min(1, state.volume);
}

function _restoreVolumeNow() {
	try {
		if (graphBuilt === true && gainNode && audioCtx) gainNode.gain.setValueAtTime(state.volume, audioCtx.currentTime);
		else if (audio) audio.volume = Math.min(1, state.volume);
	} catch { /* best effort */ }
}

function _rampVolume(from, to, ms, done) {
	const token = ++_fadeToken;
	try {
		if (graphBuilt === true && gainNode && audioCtx) {
			const now = audioCtx.currentTime;
			gainNode.gain.cancelScheduledValues(now);
			gainNode.gain.setValueAtTime(Math.max(0.0001, from), now);
			gainNode.gain.linearRampToValueAtTime(Math.max(0.0001, to), now + ms / 1000);
			setTimeout(() => { if (token === _fadeToken && done) done(); }, ms + 15);
			return;
		}
		if (audio) {
			const start = performance.now();
			const step = t => {
				if (token !== _fadeToken || !audio) return;
				const k = Math.min(1, (t - start) / ms);
				audio.volume = Math.max(0, Math.min(1, from + (to - from) * k));
				if (k < 1) requestAnimationFrame(step);
				else if (done) done();
			};
			requestAnimationFrame(step);
			return;
		}
	} catch { /* fall through to instant */ }
	if (done) done();
}

function _togglePlay() {
	if (!audio || !state.queue.length) return;
	if (!_currentMediaEvent()) return;
	if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
	if (audio.paused) {
		if (fadeEnabled()) _rampVolume(0, _effectiveVolume(), FADE_MS);
		audio.play().catch(() => {});
	} else if (fadeEnabled()) {
		_rampVolume(_effectiveVolume(), 0, FADE_MS, () => { audio.pause(); _restoreVolumeNow(); });
	} else {
		audio.pause();
	}
}

// Autoplay-similar ("infinite radio", Settings → Playback): when the queue
// runs out and loop is off, seed a mix from the last track and keep playing.
// Read from localStorage at decision time - the main window writes the key
// and both windows share the same origin/profile.
let _autoplayBusy = false;

function autoplaySimilarEnabled() {
	return lsGet('rainette.autoplaySimilar') === '1';
}

async function _autoplaySimilar() {
	const seed = state.queue[state.index];
	if (!seed || _autoplayBusy) { _broadcast(undefined, state.playing); return; }
	_autoplayBusy = true;
	_broadcast('loading', false);
	try {
		const res = await helperRequest('music_mix_from_seed', { seed: { kind: 'track', track: publicTrack(seed) } }, 20000);
		const existing = new Set(state.queue.map(trackKey));
		const fresh = validTracks(res?.tracks || []).filter(t => !existing.has(trackKey(t)));
		if (res?.ok !== false && fresh.length) {
			state.queue = state.queue.concat(fresh.slice(0, 15));
			state.index++;
			_loadCurrent();
		} else {
			_broadcast('paused', false);
		}
	} catch {
		_broadcast('paused', false);
	} finally {
		_autoplayBusy = false;
	}
}

function _next() {
	if (!state.queue.length) return;
	if (state.index < state.queue.length - 1) { state.index++; _loadCurrent(); }
	else if (state.loop) { state.index = 0; _loadCurrent(); }
	else if (autoplaySimilarEnabled()) _autoplaySimilar();
	else _broadcast(undefined, state.playing);
}
function _prev() {
	if (!state.queue.length) return;
	if (audio && audio.currentTime > 3) { audio.currentTime = 0; _broadcast(undefined, state.playing); return; }
	if (state.index > 0) { state.index--; _loadCurrent(); }
	else if (state.loop) { state.index = state.queue.length - 1; _loadCurrent(); }
	else _broadcast(undefined, state.playing);
}
function _toggleLoop() {
	state.loop = !state.loop;
	lsSet(LS.loop, state.loop ? '1' : '0');
	els.loop?.classList.toggle('on', state.loop);
	_broadcast(undefined, state.playing);
}

// Public queue entry points (also used by the remote-play listener).
function playQueue(tracks, startIndex = 0) {
	const filtered = validTracks(tracks);
	const nextId = filtered[startIndex]?.source_id;
	const curId = state.queue[state.index]?.source_id;
	// If we're already on this exact track (e.g. a duplicate remote_play from the
	// connect-time state handshake), just adopt the queue without restarting it.
	if (nextId && nextId === curId && (state.playing || state.resolvingId)) {
		state.queue = filtered;
		state.index = clamp(startIndex, 0, Math.max(0, filtered.length - 1));
		_broadcast(undefined, state.playing);
		return;
	}
	state.queue = filtered;
	state.index = clamp(startIndex, 0, Math.max(0, state.queue.length - 1));
	if (state.queue.length) _loadCurrent();
	else _stopEmptyQueue();
}

function queueAddNext(track) {
	const item = validTracks([track])[0];
	if (!item) return;
	if (!state.queue.length) { playQueue([item], 0); return; }
	state.queue.splice(clamp(state.index + 1, 0, state.queue.length), 0, item);
	_broadcast(undefined, state.playing);
}

function queueAddEnd(track) {
	const item = validTracks([track])[0];
	if (!item) return;
	if (!state.queue.length) { playQueue([item], 0); return; }
	state.queue.push(item);
	_broadcast(undefined, state.playing);
}

function queueMove(from, to) {
	from = Number(from); to = Number(to);
	if (!Number.isInteger(from) || !Number.isInteger(to) || from === to) return;
	if (from < 0 || from >= state.queue.length) return;
	to = clamp(to, 0, state.queue.length - 1);
	const current = state.queue[state.index] || null;
	const [item] = state.queue.splice(from, 1);
	state.queue.splice(to, 0, item);
	state.index = current ? state.queue.indexOf(current) : -1;
	_broadcast(undefined, state.playing);
}

function queueRemove(index) {
	index = Number(index);
	if (!Number.isInteger(index) || index < 0 || index >= state.queue.length) return;
	const removingCurrent = index === state.index;
	state.queue.splice(index, 1);
	if (!state.queue.length) { _stopEmptyQueue(); return; }
	if (removingCurrent) {
		state.index = clamp(index, 0, state.queue.length - 1);
		_loadCurrent();
	} else {
		if (index < state.index) state.index--;
		_broadcast(undefined, state.playing);
	}
}

function queuePlayIndex(index) {
	index = Number(index);
	if (!Number.isInteger(index) || index < 0 || index >= state.queue.length) return;
	if (index === state.index) { _togglePlay(); return; }
	state.index = index;
	_loadCurrent();
}

function queueShuffle() {
	if (!state.queue.length) return;
	const keepCount = state.index >= 0 ? state.index + 1 : 0;
	const head = state.queue.slice(0, keepCount);
	const tail = state.queue.slice(keepCount);
	for (let i = tail.length - 1; i > 0; i--) {
		const j = Math.floor(Math.random() * (i + 1));
		[tail[i], tail[j]] = [tail[j], tail[i]];
	}
	state.queue = head.concat(tail);
	_broadcast(undefined, state.playing);
}

function queueDedupe() {
	if (!state.queue.length) return;
	const current = state.queue[state.index] || null;
	const currentKey = current ? trackKey(current) : '';
	const seen = new Set();
	const next = [];
	for (let i = 0; i < state.queue.length; i++) {
		const track = state.queue[i];
		const key = trackKey(track);
		if (!key.endsWith(':')) {
			if (track === current) {
				next.push(track);
				seen.add(key);
			} else if (key === currentKey) {
				continue;
			} else if (!seen.has(key)) {
				next.push(track);
				seen.add(key);
			}
		}
	}
	state.queue = next;
	state.index = current ? state.queue.indexOf(current) : -1;
	_broadcast(undefined, state.playing);
}

function queueClearUpNext() {
	const current = state.queue[state.index] || null;
	if (!current) { _stopEmptyQueue(); return; }
	state.queue = [current];
	state.index = 0;
	_broadcast(undefined, state.playing);
}

// ── EQ enable / bands / presets ──────────────────────────────────────────────
function setEqOn(on) {
	const was = state.eqOn;
	state.eqOn = !!on;
	lsSet(LS.eqOn, state.eqOn ? '1' : '0');
	if (state.eqOn && graphBuilt !== true) {
		// First enable: build the graph and re-route the current track through
		// the proxy, preserving playback position.
		const wasPlaying = audio && !audio.paused;
		const t = audio ? audio.currentTime : 0;
		if (ensureAudioGraph() && state.queue[state.index]) {
			state.pendingSeek = t;
			_loadCurrent({ preservePaused: !wasPlaying });
		}
	}
	applyEqGains(state.eqGains);
	if (was !== state.eqOn && audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
	_broadcastEq();
}

function setBand(i, gainDb) {
	state.eqGains[i] = clamp(gainDb, EQ_MIN, EQ_MAX);
	lsSet(LS.eq, JSON.stringify(state.eqGains));
	if (!state.eqOn) setEqOn(true);
	else applyEqGains(state.eqGains);
	_broadcastEq();
}

function applyPreset(name) {
	const preset = PRESETS[name];
	if (!preset) return;
	state.eqGains = preset.slice();
	lsSet(LS.eq, JSON.stringify(state.eqGains));
	if (!state.eqOn) setEqOn(true);
	else applyEqGains(state.eqGains);
	_broadcastEq();
}

// Keeps the Settings page's EQ panel (in the main window) in sync with the
// live state here — Settings never touches the Web Audio graph directly, it
// only sends commands and listens for this broadcast.
function _broadcastEq() {
	sendHelper({ type: 'music_eq_state', on: state.eqOn, gains: state.eqGains.slice() });
}

// ── Now-playing broadcast (keeps the browser window in sync) ─────────────────
function _broadcast(mode, playing) {
	const track = state.queue[state.index] || null;
	const cleanTrack = publicTrack(track);
	const cleanQueue = state.queue.map(publicTrack).filter(Boolean);
	sendHelper({
		type: 'music_now_playing_set',
		track: cleanTrack,
		state: mode || (state.playing ? 'playing' : 'paused'),
		playing: !!playing,
		loop: state.loop,
		current_time: audio && Number.isFinite(audio.currentTime) ? audio.currentTime : 0,
		duration: audio && Number.isFinite(audio.duration) ? audio.duration : (track?.duration_s || 0),
		queue: cleanQueue,
		index: state.index,
		queue_count: cleanQueue.length,
		queue_duration: queueDuration(),
	});
}

// Lightweight position-only tick (throttled) so the main window's docked bar
// and Now Playing view track playback without re-sending the whole queue.
let _lastProgressSent = 0;
function _broadcastProgress(force = false) {
	if (!audio) return;
	const now = Date.now();
	if (!force && now - _lastProgressSent < 500) return;
	_lastProgressSent = now;
	const track = state.queue[state.index] || null;
	sendHelper({
		type: 'music_progress',
		current_time: Number.isFinite(audio.currentTime) ? audio.currentTime : 0,
		duration: Number.isFinite(audio.duration) ? audio.duration : (track?.duration_s || 0),
		playing: state.playing,
		source_id: track?.source_id || '',
	});
}

// ── Rendering ────────────────────────────────────────────────────────────────
function fmt(s) {
	s = Number(s || 0);
	if (!Number.isFinite(s) || s <= 0) return '0:00';
	const m = Math.floor(s / 60), sec = Math.floor(s % 60);
	return m + ':' + String(sec).padStart(2, '0');
}

function _renderMeta(track, mode) {
	if (!els.title) return;
	const artist = track.artist || track.metadata?.artists?.[0]?.name || '';
	els.title.textContent = track.title || '(untitled)';
	els.artist.textContent = mode === 'loading' ? 'Loading…' : (mode === 'error' ? 'Playback failed' : artist);
	els.artist.disabled = !artist;
	if (track.thumbnail_url) {
		els.art.src = track.thumbnail_url;
		els.artShell?.classList.add('has-art');
	} else {
		els.art.removeAttribute('src');
		els.artShell?.classList.remove('has-art');
	}
	els.root.classList.toggle('error', mode === 'error');
}
function _renderPlay() {
	if (els.play) els.play.innerHTML = state.playing ? ICON.pause : ICON.play;
	if (els.playPill) els.playPill.innerHTML = state.playing ? ICON.pause : ICON.play;
}
function _setSeek(ratio) {
	const pct = clamp(Number(ratio) || 0, 0, 1) * 100;
	if (els.seekFill) els.seekFill.style.width = pct + '%';
}
function _seekToClientX(x) {
	if (!audio || !audio.duration || !els.seek) return;
	const r = els.seek.getBoundingClientRect();
	audio.currentTime = clamp((x - r.left) / Math.max(1, r.width), 0, 1) * audio.duration;
}

// ── Icons ────────────────────────────────────────────────────────────────────
const ICON = {
	prev: iconMarkup('prev', 18),
	next: iconMarkup('next', 18),
	play: iconMarkup('play', 18),
	pause: iconMarkup('pause', 18),
	loop: iconMarkup('loop', 18),
	chevronDown: iconMarkup('chevronDown', 18),
	chevronUp: iconMarkup('chevronUp', 18),
	close: iconMarkup('close', 16),
};

// ── Native window controls (pywebview) ───────────────────────────────────────
function nativeApi() { return (window.pywebview && window.pywebview.api) || null; }
async function openCurrentArtist() {
	const track = state.queue[state.index];
	const name = track ? (track.artist || track.metadata?.artists?.[0]?.name || '') : '';
	const artistId = track ? (track.metadata?.artist_id || track.metadata?.artists?.[0]?.id || '') : '';
	if (!name && !artistId) return;
	try { await Promise.resolve(nativeApi()?.main_reveal?.()); } catch { /* focus failure is non-fatal */ }
	sendHelper({
		type: 'music_open_artist',
		artist_id: artistId,
		name,
		thumbnail_url: track?.thumbnail_url || '',
	});
}
function resizePlayerWindow() {
	try { nativeApi()?.player_resize?.(state.collapsed); } catch { /* not in pywebview */ }
}
function setCollapsed(on) {
	state.collapsed = !!on;
	els.root?.classList.toggle('collapsed', state.collapsed);
	if (els.collapseToggle) {
		els.collapseToggle.innerHTML = state.collapsed ? ICON.chevronDown : ICON.chevronUp;
		els.collapseToggle.title = state.collapsed ? 'Show controls' : 'Collapse player';
		els.collapseToggle.setAttribute('aria-label', state.collapsed ? 'Show controls' : 'Collapse player');
	}
	resizePlayerWindow();
}

// ── UI build ─────────────────────────────────────────────────────────────────
function buildUI() {
	const root = document.getElementById('app') || document.body;
	els.root = root;
	root.classList.add('collapsed');
	root.innerHTML = `
		<header class="mp-titlebar">
			<div class="mp-drag pywebview-drag-region">
				<span class="mp-brand">Rainette Music</span>
			</div>
			<div class="mp-winbtns">
				<button id="mpPin" class="mp-winbtn" title="Keep on top" aria-label="Pin on top">⌖</button>
				<button id="mpMin" class="mp-winbtn" title="Minimize" aria-label="Minimize">–</button>
				<button id="mpClose" class="mp-winbtn mp-close" title="Hide player" aria-label="Hide">×</button>
			</div>
		</header>
		<section class="mp-now">
			<div class="mp-art-shell pywebview-drag-region">
				<img class="mp-art" alt="">
				<span class="mp-note" aria-hidden="true">&#9835;</span>
			</div>
			<div class="mp-meta pywebview-drag-region">
				<div class="mp-title">Nothing playing</div>
				<button type="button" class="mp-artist mp-artist-link" disabled>Search in the main window and press play</button>
			</div>
			<button id="mpPlayPill" class="mp-play-pill" data-act="toggle" title="Play/Pause" aria-label="Play or pause">${ICON.play}</button>
			<button id="mpCollapseToggle" class="mp-collapse-toggle" title="Show controls" aria-label="Show controls">${ICON.chevronDown}</button>
			<button id="mpPillClose" class="mp-pill-close" title="Hide player" aria-label="Hide player">${ICON.close}</button>
		</section>
		<div class="mp-seek" role="slider" aria-label="Seek"><div class="mp-seek-fill"></div></div>
		<div class="mp-times"><span class="mp-cur">0:00</span><span class="mp-dur">0:00</span></div>
		<section class="mp-controls">
			<button class="mp-btn" data-act="prev" title="Previous" aria-label="Previous">${ICON.prev}</button>
			<button class="mp-btn mp-play" data-act="toggle" title="Play/Pause" aria-label="Play or pause">${ICON.play}</button>
			<button class="mp-btn" data-act="next" title="Next" aria-label="Next">${ICON.next}</button>
			<button class="mp-btn" data-act="loop" title="Loop" aria-label="Toggle loop">${ICON.loop}</button>
		</section>
		<section class="mp-volrow">
			<span class="mp-vol-ico" title="Volume" aria-hidden="true">${iconMarkup('volume', 14)}</span>
			<div class="mp-vol" role="slider" aria-label="Volume"><div class="mp-vol-fill"></div></div>
			<span class="mp-vol-val">100%</span>
		</section>`;

	els.artShell = root.querySelector('.mp-art-shell');
	els.art = root.querySelector('.mp-art');
	els.title = root.querySelector('.mp-title');
	els.artist = root.querySelector('.mp-artist');
	els.playPill = root.querySelector('#mpPlayPill');
	els.collapseToggle = root.querySelector('#mpCollapseToggle');
	els.seek = root.querySelector('.mp-seek');
	els.seekFill = root.querySelector('.mp-seek-fill');
	els.cur = root.querySelector('.mp-cur');
	els.dur = root.querySelector('.mp-dur');
	els.play = root.querySelector('.mp-play');
	els.loop = root.querySelector('[data-act="loop"]');
	els.vol = root.querySelector('.mp-vol');
	els.volFill = root.querySelector('.mp-vol-fill');
	els.volVal = root.querySelector('.mp-vol-val');

	// Transport
	root.querySelector('.mp-controls').addEventListener('click', e => {
		const b = e.target.closest('[data-act]'); if (!b) return;
		({ toggle: _togglePlay, next: _next, prev: _prev, loop: _toggleLoop }[b.dataset.act] || (() => {}))();
	});
	els.playPill.addEventListener('click', _togglePlay);
	els.artist.addEventListener('click', openCurrentArtist);
	// Seek (pointer drag)
	let seekId = null;
	els.seek.addEventListener('pointerdown', e => { _seekToClientX(e.clientX); seekId = e.pointerId; els.seek.setPointerCapture?.(e.pointerId); });
	els.seek.addEventListener('pointermove', e => { if (seekId === e.pointerId) _seekToClientX(e.clientX); });
	const endSeek = e => { if (seekId === e.pointerId) { els.seek.releasePointerCapture?.(e.pointerId); seekId = null; } };
	els.seek.addEventListener('pointerup', endSeek);
	els.seek.addEventListener('pointercancel', endSeek);
	// Volume (pointer drag)
	let volId = null;
	const setVolFromX = x => { const r = els.vol.getBoundingClientRect(); applyVolume(clamp((x - r.left) / Math.max(1, r.width), 0, 1) * 1.5); };
	els.vol.addEventListener('pointerdown', e => { setVolFromX(e.clientX); volId = e.pointerId; els.vol.setPointerCapture?.(e.pointerId); });
	els.vol.addEventListener('pointermove', e => { if (volId === e.pointerId) setVolFromX(e.clientX); });
	const endVol = e => { if (volId === e.pointerId) { els.vol.releasePointerCapture?.(e.pointerId); volId = null; } };
	els.vol.addEventListener('pointerup', endVol);
	els.vol.addEventListener('pointercancel', endVol);
	els.collapseToggle.addEventListener('click', () => setCollapsed(!state.collapsed));

	// Native window buttons — hidden entirely when not inside pywebview.
	const pin = root.querySelector('#mpPin'), min = root.querySelector('#mpMin'), close = root.querySelector('#mpClose');
	const pillClose = root.querySelector('#mpPillClose');
	const winbtns = root.querySelector('.mp-winbtns');
	// pywebview injects window.pywebview.api asynchronously and fires
	// 'pywebviewready' once it's actually available. If that happens after this
	// script's DOMContentLoaded-time check, nativeApi() would otherwise read as
	// "not native" forever and permanently hide the close buttons even inside a
	// real native window - so re-check once the event confirms readiness.
	function updateNativeChromeVisibility() {
		const isNative = !!nativeApi();
		winbtns.style.display = isNative ? '' : 'none';
		pillClose.style.display = isNative ? '' : 'none';
	}
	updateNativeChromeVisibility();
	window.addEventListener('pywebviewready', updateNativeChromeVisibility, { once: true });
	pin.addEventListener('click', async () => {
		const api = nativeApi(); if (!api) return;
		try {
			const result = await api.player_toggle_pin();
			if (result?.available) pin.classList.toggle('on', !!result.enabled);
			else if (result?.error) pin.title = 'Always on top unavailable: ' + result.error;
		} catch { /* native failure is non-fatal */ }
	});
	min.addEventListener('click', () => nativeApi()?.player_minimize?.());
	close.addEventListener('click', () => nativeApi()?.player_hide?.());
	pillClose.addEventListener('click', () => nativeApi()?.player_hide?.());

	// Restore persisted UI state
	els.loop.classList.toggle('on', state.loop);
	applyVolume(state.volume);
	setCollapsed(true);
}

// ── Audio element lifecycle ──────────────────────────────────────────────────
function initAudio() {
	audio = new Audio();
	audio.preload = 'auto';
	audio.volume = Math.min(1, state.volume);
}

// ── Remote commands from the browser window ──────────────────────────────────
function wireRemote() {
	document.addEventListener('rainette:helper-message', e => {
		const msg = e.detail; if (!msg) return;
		if (msg.type === 'music_remote_play' && Array.isArray(msg.tracks)) {
			playQueue(msg.tracks, msg.index || 0);
			if (window.RW_MINIPLAYER_ENABLED) {
				try { nativeApi()?.reveal_player?.(); } catch { /* not in pywebview */ }
			}
		} else if (msg.type === 'music_theme_set') {
			// Keep in sync with rainette_settings.js's THEME_CLASS map.
			const THEME_CLASS = { light: 'rw-theme-light', dark: 'rw-theme-dark', mono: 'rw-theme-mono', midnight: 'rw-theme-midnight' };
			document.documentElement.classList.remove(...Object.values(THEME_CLASS));
			document.documentElement.classList.add(THEME_CLASS[msg.theme] || THEME_CLASS.light);
		} else if (msg.type === 'music_accent_set') {
			document.documentElement.classList.remove('rw-accent-teal', 'rw-accent-purple');
			if (msg.accent === 'teal' || msg.accent === 'purple') document.documentElement.classList.add('rw-accent-' + msg.accent);
		} else if (msg.type === 'music_remote_control') {
			const a = msg.action;
			if (a === 'queue_add_next') queueAddNext(msg.track);
			else if (a === 'queue_add_end') queueAddEnd(msg.track);
			else if (a === 'queue_move') queueMove(msg.from, msg.to);
			else if (a === 'queue_remove') queueRemove(msg.index);
			else if (a === 'queue_play_index') { queuePlayIndex(msg.index); if (window.RW_MINIPLAYER_ENABLED) { try { nativeApi()?.reveal_player?.(); } catch { /* not in pywebview */ } } }
			else if (a === 'queue_shuffle') queueShuffle();
			else if (a === 'queue_dedupe') queueDedupe();
			else if (a === 'queue_clear_up_next') queueClearUpNext();
			else if (a === 'queue_request_state') _broadcast(undefined, state.playing);
			else if (a === 'eq_set_on') setEqOn(!!msg.on);
			else if (a === 'eq_set_band') setBand(Number(msg.index), Number(msg.gain));
			else if (a === 'eq_apply_preset') applyPreset(msg.preset);
			else if (a === 'eq_request_state') _broadcastEq();
			else if (a === 'set_volume') applyVolume(Number(msg.value));
			else if (!state.queue.length) return;
			else if (a === 'toggle') _togglePlay();
			else if (a === 'next') _next();
			else if (a === 'prev') _prev();
			else if (a === 'loop') _toggleLoop();
			else if (a === 'seek' && audio && audio.duration) { audio.currentTime = clamp(Number(msg.ratio || 0), 0, 1) * audio.duration; _broadcastProgress(true); }
		}
	});
}

// ── Boot ─────────────────────────────────────────────────────────────────────
function boot() {
	initAudio();
	buildUI();
	wireRemote();
	// If the player window was hidden while a play was issued, ask the main
	// window for the current queue once we're connected (sendHelper queues until
	// the socket opens). Harmless when nothing has played yet.
	sendHelper({ type: 'music_request_state' });
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
