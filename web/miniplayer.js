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
		srcNode = audioCtx.createMediaElementSource(audio);
		let node = srcNode;
		bands = EQ_BANDS.map(b => {
			const f = audioCtx.createBiquadFilter();
			f.type = b.type;
			f.frequency.value = b.f;
			if (b.type === 'peaking') f.Q.value = 1.0;
			f.gain.value = 0;
			node.connect(f);
			node = f;
			return f;
		});
		gainNode = audioCtx.createGain();
		node.connect(gainNode);
		gainNode.connect(audioCtx.destination);
		graphBuilt = true;
		applyEqGains(state.eqGains);
		applyVolume(state.volume);
		return true;
	} catch (err) {
		graphBuilt = 'failed';
		return false;
	}
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
async function _loadCurrent() {
	const track = state.queue[state.index];
	if (!track) return;
	_renderMeta(track, 'loading');
	_setSeek(0);
	state.resolvingId = track.source_id;
	// Reuse a still-fresh resolved URL (e.g. after an EQ toggle reload) so we
	// don't hit yt-dlp again just to switch the audio path.
	if (streamFresh(track)) {
		_applyAndPlay(track, track._url);
		return;
	}
	_broadcast('loading', false);
	try {
		const res = await requestStream(track);
		if (state.resolvingId !== track.source_id) return;
		if (!res || res.ok === false || !res.url) { _renderMeta(track, 'error'); return; }
		rememberStream(track, res);
		_applyAndPlay(track, res.url);
	} catch {
		if (state.resolvingId === track.source_id) _renderMeta(track, 'error');
	}
}

function _applyAndPlay(track, url) {
	// Once the EQ graph exists, the element source is bound; only same-origin
	// (proxied) media produces sound, so stay on the proxy for the session.
	if (graphBuilt === true) {
		audio.crossOrigin = 'anonymous';
		audio.src = '/audio?u=' + encodeURIComponent(url);
	} else {
		audio.removeAttribute('crossorigin');
		audio.src = url;
	}
	const resume = () => {
		if (state.pendingSeek != null) { try { audio.currentTime = state.pendingSeek; } catch { /* ignore */ } state.pendingSeek = null; }
	};
	audio.play().then(resume).catch(() => {});
}

async function _reResolveAndResume() {
	const track = state.queue[state.index];
	if (!track) return false;
	track._url = '';
	track._urlAt = 0;
	track._expiresHintS = 0;
	try {
		const res = await requestStream(track, { forceRefresh: true });
		if (res && res.ok !== false && res.url) {
			rememberStream(track, res);
			_applyAndPlay(track, res.url);
			return true;
		}
	} catch { /* fall through */ }
	_renderMeta(track, 'error');
	return false;
}

// ── Transport ────────────────────────────────────────────────────────────────
function _togglePlay() {
	if (!audio || !state.queue.length) return;
	if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
	if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}
function _next() {
	if (!state.queue.length) return;
	if (state.index < state.queue.length - 1) { state.index++; _loadCurrent(); }
	else if (state.loop) { state.index = 0; _loadCurrent(); }
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
	els.eqPanel?.classList.toggle('on', state.eqOn);
	els.eqToggle?.classList.toggle('on', state.eqOn);
	if (state.eqOn && graphBuilt !== true) {
		// First enable: build the graph and re-route the current track through
		// the proxy, preserving playback position.
		const wasPlaying = audio && !audio.paused;
		const t = audio ? audio.currentTime : 0;
		if (ensureAudioGraph() && state.queue[state.index]) {
			state.pendingSeek = t;
			_loadCurrent();
			if (!wasPlaying) audio.addEventListener('canplay', () => audio.pause(), { once: true });
		}
	}
	// Grow / shrink the native window so the EQ panel isn't clipped.
	resizePlayerWindow();
	applyEqGains(state.eqGains);
	if (was !== state.eqOn && audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
}

function setBand(i, gainDb) {
	state.eqGains[i] = clamp(gainDb, EQ_MIN, EQ_MAX);
	lsSet(LS.eq, JSON.stringify(state.eqGains));
	if (!state.eqOn) setEqOn(true);
	else applyEqGains(state.eqGains);
	_syncEqSliders();
}

function applyPreset(name) {
	const preset = PRESETS[name];
	if (!preset) return;
	state.eqGains = preset.slice();
	lsSet(LS.eq, JSON.stringify(state.eqGains));
	if (!state.eqOn) setEqOn(true);
	else applyEqGains(state.eqGains);
	_syncEqSliders();
	els.presetBtns?.forEach(b => b.classList.toggle('on', b.dataset.preset === name));
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

// ── Rendering ────────────────────────────────────────────────────────────────
function fmt(s) {
	s = Number(s || 0);
	if (!Number.isFinite(s) || s <= 0) return '0:00';
	const m = Math.floor(s / 60), sec = Math.floor(s % 60);
	return m + ':' + String(sec).padStart(2, '0');
}

function _renderMeta(track, mode) {
	if (!els.title) return;
	els.title.textContent = track.title || '(untitled)';
	els.artist.textContent = mode === 'loading' ? 'Loading…' : (mode === 'error' ? 'Playback failed' : (track.artist || ''));
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
	prev: svg('<path d="M6 5v14" /><path d="M18 5.5v13a.6.6 0 0 1-.94.49L8 12.5a.6.6 0 0 1 0-.98l9.06-6.5a.6.6 0 0 1 .94.48z" fill="currentColor" stroke="none"/>'),
	next: svg('<path d="M18 5v14" /><path d="M6 5.5v13a.6.6 0 0 0 .94.49L16 12.5a.6.6 0 0 0 0-.98L6.94 5.02a.6.6 0 0 0-.94.48z" fill="currentColor" stroke="none"/>'),
	play: svg('<path d="M8 5v14l11-7-11-7z" fill="currentColor" stroke="none"/>'),
	pause: svg('<path d="M7 5h4v14H7z" fill="currentColor" stroke="none"/><path d="M13 5h4v14h-4z" fill="currentColor" stroke="none"/>'),
	loop: svg('<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>'),
	chevronDown: svg('<path d="m6 9 6 6 6-6"/>'),
	chevronUp: svg('<path d="m18 15-6-6-6 6"/>'),
};
function svg(inner) {
	return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
}

// ── Native window controls (pywebview) ───────────────────────────────────────
function nativeApi() { return (window.pywebview && window.pywebview.api) || null; }
function resizePlayerWindow() {
	try { nativeApi()?.player_resize?.(state.collapsed, state.eqOn); } catch { /* not in pywebview */ }
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
				<div class="mp-artist">Search in the main window and press play</div>
			</div>
			<button id="mpCollapseToggle" class="mp-collapse-toggle" title="Show controls" aria-label="Show controls">${ICON.chevronDown}</button>
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
			<span class="mp-vol-ico" title="Volume">🔊</span>
			<div class="mp-vol" role="slider" aria-label="Volume"><div class="mp-vol-fill"></div></div>
			<span class="mp-vol-val">100%</span>
			<button id="mpEqToggle" class="mp-eq-toggle" title="Equalizer">EQ</button>
		</section>
		<section class="mp-eq" id="mpEqPanel">
			<div class="mp-eq-presets">
				${Object.keys(PRESETS).map(n => `<button class="mp-preset" data-preset="${n}">${n}</button>`).join('')}
			</div>
			<div class="mp-eq-bands">
				${EQ_BANDS.map((b, i) => `
					<label class="mp-eq-band">
						<input type="range" class="mp-eq-slider" data-band="${i}" min="${EQ_MIN}" max="${EQ_MAX}" step="1" value="0" orient="vertical">
						<span class="mp-eq-label">${b.label}</span>
						<span class="mp-eq-hz">${b.short}</span>
					</label>`).join('')}
			</div>
		</section>`;

	els.artShell = root.querySelector('.mp-art-shell');
	els.art = root.querySelector('.mp-art');
	els.title = root.querySelector('.mp-title');
	els.artist = root.querySelector('.mp-artist');
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
	els.eqToggle = root.querySelector('#mpEqToggle');
	els.eqPanel = root.querySelector('#mpEqPanel');
	els.eqSliders = [...root.querySelectorAll('.mp-eq-slider')];
	els.presetBtns = [...root.querySelectorAll('.mp-preset')];

	// Transport
	root.querySelector('.mp-controls').addEventListener('click', e => {
		const b = e.target.closest('[data-act]'); if (!b) return;
		({ toggle: _togglePlay, next: _next, prev: _prev, loop: _toggleLoop }[b.dataset.act] || (() => {}))();
	});
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
	// EQ
	els.eqToggle.addEventListener('click', () => setEqOn(!state.eqOn));
	els.eqSliders.forEach(s => s.addEventListener('input', () => setBand(Number(s.dataset.band), Number(s.value))));
	els.presetBtns.forEach(b => b.addEventListener('click', () => applyPreset(b.dataset.preset)));
	els.collapseToggle.addEventListener('click', () => setCollapsed(!state.collapsed));

	// Native window buttons — hidden entirely when not inside pywebview.
	const pin = root.querySelector('#mpPin'), min = root.querySelector('#mpMin'), close = root.querySelector('#mpClose');
	if (!nativeApi()) { root.querySelector('.mp-winbtns').style.display = 'none'; }
	pin.addEventListener('click', async () => { const api = nativeApi(); if (!api) return; try { const on = await api.player_toggle_pin(); pin.classList.toggle('on', !!on); } catch { /* ignore */ } });
	min.addEventListener('click', () => nativeApi()?.player_minimize?.());
	close.addEventListener('click', () => nativeApi()?.player_hide?.());

	// Restore persisted UI state
	els.loop.classList.toggle('on', state.loop);
	els.eqPanel.classList.toggle('on', state.eqOn);
	els.eqToggle.classList.toggle('on', state.eqOn);
	_syncEqSliders();
	applyVolume(state.volume);
	setCollapsed(true);
}

function _syncEqSliders() {
	els.eqSliders?.forEach((s, i) => { s.value = String(state.eqGains[i] || 0); });
	const match = Object.keys(PRESETS).find(n => PRESETS[n].every((g, i) => g === state.eqGains[i]));
	els.presetBtns?.forEach(b => b.classList.toggle('on', b.dataset.preset === match));
}

// ── Audio element lifecycle ──────────────────────────────────────────────────
function initAudio() {
	audio = new Audio();
	audio.preload = 'auto';
	audio.volume = Math.min(1, state.volume);
	let erroredOnce = false;
	audio.addEventListener('playing', () => {
		state.playing = true; erroredOnce = false; _renderPlay();
		const track = state.queue[state.index];
		if (track) _renderMeta(track, 'playing');
		_broadcast('playing', true);
		preResolveUpcoming();
	});
	audio.addEventListener('pause', () => { state.playing = false; _renderPlay(); _broadcast('paused', false); });
	audio.addEventListener('timeupdate', () => {
		if (audio.duration) { _setSeek(audio.currentTime / audio.duration); }
		if (els.cur) els.cur.textContent = fmt(audio.currentTime);
	});
	audio.addEventListener('durationchange', () => { if (els.dur) els.dur.textContent = fmt(audio.duration); });
	audio.addEventListener('ended', () => _next());
	audio.addEventListener('error', () => {
		if (!erroredOnce && state.queue[state.index]) { erroredOnce = true; _reResolveAndResume(); }
	});
}

// ── Remote commands from the browser window ──────────────────────────────────
function wireRemote() {
	document.addEventListener('rainette:helper-message', e => {
		const msg = e.detail; if (!msg) return;
		if (msg.type === 'music_remote_play' && Array.isArray(msg.tracks)) {
			playQueue(msg.tracks, msg.index || 0);
			try {
				const api = nativeApi();
				api?.player_allow_show?.();
				api?.show_player?.();
			} catch { /* not in pywebview */ }
		} else if (msg.type === 'music_remote_control') {
			const a = msg.action;
			if (a === 'queue_add_next') queueAddNext(msg.track);
			else if (a === 'queue_add_end') queueAddEnd(msg.track);
			else if (a === 'queue_move') queueMove(msg.from, msg.to);
			else if (a === 'queue_remove') queueRemove(msg.index);
			else if (a === 'queue_play_index') queuePlayIndex(msg.index);
			else if (a === 'queue_shuffle') queueShuffle();
			else if (a === 'queue_dedupe') queueDedupe();
			else if (a === 'queue_clear_up_next') queueClearUpNext();
			else if (a === 'queue_request_state') _broadcast(undefined, state.playing);
			else if (!state.queue.length) return;
			else if (a === 'toggle') _togglePlay();
			else if (a === 'next') _next();
			else if (a === 'prev') _prev();
			else if (a === 'loop') _toggleLoop();
			else if (a === 'seek' && audio && audio.duration) audio.currentTime = clamp(Number(msg.ratio || 0), 0, 1) * audio.duration;
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
