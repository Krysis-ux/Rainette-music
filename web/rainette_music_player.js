/**
 * The in-page "now playing" bubble, used in the browser fallback.
 *
 * Boots once at load so it survives page nav, and owns the global <audio> and
 * queue behind window.RainetteMusic (playTrack, playQueue, toggle, next, prev).
 * Stays hidden until a track loads. Drag is a local port of the pointer-capture
 * pattern, since a module cannot reach index.html's makeDraggablePanel.
 */

import { sendHelper, helperRequest, app } from './music_shell.js';
import { iconMarkup } from './rainette_icons.js';
import { REPEAT_LABEL, normalizeRepeat, nextRepeat, loopFlagFor } from './repeat_mode.js';

const POS_KEY = 'rainette.musicPlayerPos';
// LOOP_KEY is the superseded boolean, read once by _loadRepeat() so an existing
// setting survives the upgrade to three-state repeat.
const LOOP_KEY = 'rainette.musicLoop';
const REPEAT_KEY = 'rainette.musicRepeat';
// Same key miniplayer.js uses (LS.vol) - Settings -> Playback -> "Default
// volume" already wrote to this key but nothing here ever read it back.
const VOLUME_KEY = 'rw.mp.volume';
const LOCAL_STREAM_TTL_MS = 50 * 60 * 1000;
const PREFETCH_AHEAD = 3;

// In remote mode this module runs headless: it's the audio+queue engine (when
// the floating miniplayer popout is off) but never shows its own UI - the
// main-window docked bar in rainette_music.js is the single transport surface,
// driven by the music_now_playing / music_progress broadcasts this engine
// emits. The floating liquid-glass bubble below is only for the plain-browser
// (non-remote) fallback path.
const HEADLESS = typeof window !== 'undefined' && !!window.RW_REMOTE;

// Guarded storage access so the module imports cleanly in a DOM-free test env.
function _lsGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function _lsSet(key, val) { try { localStorage.setItem(key, val); } catch { /* best effort */ } }

function _loadVolume() {
	const raw = _lsGet(VOLUME_KEY);
	if (raw == null || raw === '') return 1;
	const v = Number(raw);
	return Number.isFinite(v) && v >= 0 ? Math.max(0, Math.min(1.5, v)) : 1;
}

// Migrate the old boolean: loop-on only ever meant "loop the queue".
function _loadRepeat() {
	return normalizeRepeat(_lsGet(REPEAT_KEY), _lsGet(LOOP_KEY) === '1' ? 'all' : 'off');
}

const state = {
	queue: [],          // [{ source_id, title, artist, thumbnail_url, ... }]
	index: -1,
	repeat: _loadRepeat(),
	volume: _loadVolume(),
	playing: false,
	resolvingId: null,  // source_id currently being resolved (stream fetch in flight)
	expanded: false,
	desktopOverlay: false,
	lastOverlaySync: 0,
};

let audio = null;
let root = null;      // the bubble element
let els = {};         // cached child refs
let _booted = false;
const preResolveInFlight = new Set();

function _trackKey(track) {
	return `${track?.source || 'youtube'}:${track?.source_id || ''}`;
}

function _publicTrack(track) {
	if (!track || typeof track !== 'object') return null;
	const clean = {};
	for (const [key, value] of Object.entries(track)) {
		if (!key.startsWith('_')) clean[key] = value;
	}
	return clean;
}

function _validTracks(tracks) {
	return (tracks || []).filter(t => t && t.source_id);
}

function _queueDuration() {
	return state.queue.reduce((sum, track) => {
		const duration = Number(track?.duration_s || 0);
		return sum + (Number.isFinite(duration) && duration > 0 ? duration : 0);
	}, 0);
}

// The one place that tells other tabs + the main page's error banner /
// Queue tab what's playing. Ported to mirror miniplayer.js's _broadcast()
// exactly (including the queue/index fields, which this bubble never used to
// send - the Queue tab silently never worked when this engine was active).
function _broadcast(mode, playing) {
	const track = state.queue[state.index] || null;
	const cleanQueue = state.queue.map(_publicTrack).filter(Boolean);
	sendHelper({
		type: 'music_now_playing_set',
		track: _publicTrack(track),
		state: mode || (state.playing ? 'playing' : 'paused'),
		playing: !!playing,
		repeat: state.repeat,
		loop: loopFlagFor(state.repeat),
		current_time: audio && Number.isFinite(audio.currentTime) ? audio.currentTime : 0,
		duration: audio && Number.isFinite(audio.duration) ? audio.duration : (track?.duration_s || 0),
		queue: cleanQueue,
		index: state.index,
		queue_count: cleanQueue.length,
		queue_duration: _queueDuration(),
	});
}

// Lightweight position-only tick (throttled) so the main-window docked bar and
// Now Playing view show a live progress bar without re-sending the whole queue.
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

function _seekRatio(ratio) {
	if (!audio || !audio.duration) return;
	audio.currentTime = Math.max(0, Math.min(1, Number(ratio) || 0)) * audio.duration;
	_broadcastProgress(true);
}

function _stopEmptyQueue() {
	state.queue = [];
	state.index = -1;
	state.resolvingId = null;
	state.playing = false;
	if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
	_renderPlayState();
	_broadcast('paused', false);
}

// ── Queue manipulation (ported from miniplayer.js so the Queue tab and drag-
// reorder work identically whether the native popout or this docked engine
// is the active playback surface) ─────────────────────────────────────────

function queueAddNext(track) {
	const item = _validTracks([track])[0];
	if (!item) return;
	if (!state.queue.length) { RainetteMusic.playQueue([item], 0); return; }
	state.queue.splice(_clamp(state.index + 1, 0, state.queue.length), 0, item);
	_broadcast(undefined, state.playing);
}

function queueAddEnd(track) {
	const item = _validTracks([track])[0];
	if (!item) return;
	if (!state.queue.length) { RainetteMusic.playQueue([item], 0); return; }
	state.queue.push(item);
	_broadcast(undefined, state.playing);
}

function queueMove(from, to) {
	from = Number(from); to = Number(to);
	if (!Number.isInteger(from) || !Number.isInteger(to) || from === to) return;
	if (from < 0 || from >= state.queue.length) return;
	to = _clamp(to, 0, state.queue.length - 1);
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
		state.index = _clamp(index, 0, state.queue.length - 1);
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
	const currentKey = current ? _trackKey(current) : '';
	const seen = new Set();
	const next = [];
	for (let i = 0; i < state.queue.length; i++) {
		const track = state.queue[i];
		const key = _trackKey(track);
		if (!key.endsWith(':')) {
			if (track === current) { next.push(track); seen.add(key); }
			else if (key === currentKey) continue;
			else if (!seen.has(key)) { next.push(track); seen.add(key); }
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

function _streamFresh(track) {
	if (!track?._url) return false;
	const hinted = Number(track._expiresHintS || 0) * 1000;
	const ttl = hinted > 0 ? Math.min(hinted, 5 * 60 * 60 * 1000) : LOCAL_STREAM_TTL_MS;
	return Date.now() - Number(track._resolvedAt || 0) < ttl;
}

function _rememberStream(track, res) {
	track._url = res.url;
	track._resolvedAt = Date.now();
	track._expiresHintS = Number(res.expires_hint_s || 0);
}

function _requestStream(track, opts = {}) {
	return helperRequest('music_stream_url', {
		source_id: track.source_id,
		track_id: opts.prefetch ? '' : (track.id || ''),
		track,
		prefetch: !!opts.prefetch,
		force_refresh: !!opts.forceRefresh,
	}, 20000);
}

function _preResolveUpcoming() {
	const start = state.index + 1;
	for (let offset = 0; offset < PREFETCH_AHEAD; offset++) {
		const track = state.queue[start + offset];
		if (!track?.source_id || _streamFresh(track)) continue;
		const key = _trackKey(track);
		if (preResolveInFlight.has(key)) continue;
		preResolveInFlight.add(key);
		_requestStream(track, { prefetch: true })
			.then(res => { if (res && res.ok !== false && res.url) _rememberStream(track, res); })
			.catch(() => {})
			.finally(() => preResolveInFlight.delete(key));
	}
}

// ── Public API ───────────────────────────────────────────────────────────────

export const RainetteMusic = {
	playTrack(track) { this.playQueue([track], 0); },
	playQueue(tracks, startIndex = 0) {
		state.queue = (tracks || []).filter(t => t && t.source_id);
		state.index = Math.max(0, Math.min(startIndex, state.queue.length - 1));
		if (state.queue.length) _loadCurrent();
	},
	toggle() { _togglePlay(); },
	next() { _next(); },
	prev() { _prev(); },
	current() { return state.queue[state.index] || null; },
	isPlaying() { return state.playing; },
	requestQueueState() { _broadcast(undefined, state.playing); },
	toggleLoop() { _toggleLoop(); },
	isLooping() { return state.repeat !== 'off'; },
	repeatMode() { return state.repeat; },
	setRepeat(mode) { _setRepeat(mode); },
	setVolume(v) { applyVolume(v); },
	getVolume() { return state.volume; },
	seek(ratio) { _seekRatio(ratio); },
	queueAddNext(track) { queueAddNext(track); },
	queueAddEnd(track) { queueAddEnd(track); },
	queueMove(from, to) { queueMove(from, to); },
	queueRemove(index) { queueRemove(index); },
	queuePlayIndex(index) { queuePlayIndex(index); },
	queueShuffle() { queueShuffle(); },
	queueDedupe() { queueDedupe(); },
	queueClearUpNext() { queueClearUpNext(); },
};

// ── Track loading (resolve stream URL, then play) ─────────────────────────────

async function _loadCurrent() {
	const track = state.queue[state.index];
	if (!track) return;
	_ensureUI();
	_show();
	_renderMeta(track, 'loading');
	_setSeekProgress(0);
	state.resolvingId = track.source_id;
	// Broadcast now-playing so other tabs + the page reflect it.
	_broadcast('loading', false);
	_syncDesktopOverlay('loading', true);
	if (_streamFresh(track)) {
		audio.src = track._url;
		audio.play().catch(() => {
			if (state.queue[state.index] === track) { _renderMeta(track, 'error'); _syncDesktopOverlay('error', true); _broadcast('error', false); }
		});
		return;
	}

	try {
		const res = await _requestStream(track);
		// A newer track may have superseded this resolve while it was in flight.
		if (state.resolvingId !== track.source_id) return;
		if (!res || res.ok === false || !res.url) {
			_renderMeta(track, 'error');
			_syncDesktopOverlay('error', true);
			_broadcast('error', false);
			return;
		}
		audio.src = res.url;
		_rememberStream(track, res);
		await audio.play();
	} catch (err) {
		if (state.resolvingId === track.source_id) {
			_renderMeta(track, 'error');
			_syncDesktopOverlay('error', true);
			_broadcast('error', false);
		}
	}
}

async function _reResolveAndResume() {
	// Stream URLs expire (googlevideo signs them for a few hours). On a playback
	// error, re-resolve once before giving up.
	const track = state.queue[state.index];
	if (!track) return;
	track._url = '';
	track._resolvedAt = 0;
	track._expiresHintS = 0;
	try {
		const res = await _requestStream(track, { forceRefresh: true });
		if (res && res.ok !== false && res.url) {
			audio.src = res.url;
			_rememberStream(track, res);
			await audio.play();
			return true;
		}
	} catch { /* fall through */ }
	_renderMeta(track, 'error');
	_syncDesktopOverlay('error', true);
	_broadcast('error', false);
	return false;
}

// ── Transport ────────────────────────────────────────────────────────────────

function _togglePlay() {
	if (!audio || !state.queue.length) return;
	if (audio.paused) {
		audio.play().catch(() => {
			const track = state.queue[state.index];
			if (track) { _renderMeta(track, 'error'); _syncDesktopOverlay('error', true); _broadcast('error', false); }
		});
	} else audio.pause();
}

// `auto` distinguishes a track ending on its own from the user pressing Next, so
// repeat-one never traps the Next button on the same song.
function _next(auto = false) {
	if (!state.queue.length) return;
	if (auto && state.repeat === 'one') { _replayCurrent(); return; }
	if (state.index < state.queue.length - 1) { state.index++; _loadCurrent(); }
	else if (state.repeat === 'all') { state.index = 0; _loadCurrent(); }
}

function _replayCurrent() {
	if (!audio) { _loadCurrent(); return; }
	try {
		audio.currentTime = 0;
		const started = audio.play();
		if (started?.catch) started.catch(() => _loadCurrent());
	} catch {
		_loadCurrent();
		return;
	}
	_syncDesktopOverlay('playing', true);
	_broadcast('playing', true);
}

function _prev() {
	if (!state.queue.length) return;
	// Restart the track if we're past the first few seconds, else go back.
	if (audio && audio.currentTime > 3) { audio.currentTime = 0; _syncDesktopOverlay(undefined, true); return; }
	if (state.index > 0) { state.index--; _loadCurrent(); }
	else if (state.repeat !== 'off') { state.index = state.queue.length - 1; _loadCurrent(); }
}

function _overlayPayload(mode) {
	const track = state.queue[state.index] || null;
	const duration = audio && Number.isFinite(audio.duration) && audio.duration > 0
		? audio.duration
		: Number(track?.duration_s || 0);
	const current = audio && Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
	return {
		track,
		state: mode || (state.playing ? 'playing' : 'paused'),
		playing: !!state.playing,
		repeat: state.repeat,
		loop: loopFlagFor(state.repeat),
		current_time: current,
		duration,
		// NOTE: intentionally does NOT send `expanded`. The native desktop
		// overlay owns its own collapse/expand state (toggled by its chevron
		// + persisted to disk). Sending the in-page bubble's expand flag here
		// used to override the overlay every ~700ms sync, which made the
		// overlay's collapse button "open for a second then close".
	};
}

function _syncDesktopOverlay(mode, force = false) {
	const track = state.queue[state.index] || null;
	if (!track) return;
	const now = Date.now();
	if (!force && now - state.lastOverlaySync < 700) return;
	state.lastOverlaySync = now;
	sendHelper({
		type: state.desktopOverlay ? 'music_overlay_update' : 'music_overlay_show',
		..._overlayPayload(mode),
	});
}

function _setDesktopOverlayVisible(on) {
	state.desktopOverlay = !!on;
	if (root) root.style.display = state.desktopOverlay ? 'none' : (state.queue.length ? '' : 'none');
}

function _icon(name) {
	return iconMarkup(name, 16);
}

function _setSeekProgress(ratio) {
	const pct = Math.max(0, Math.min(1, Number(ratio) || 0)) * 100;
	if (els.seekFill) els.seekFill.style.width = pct + '%';
	if (els.seek) els.seek.style.setProperty('--rw-mp-progress', pct + '%');
}

function _seekToClientX(clientX) {
	if (!audio || !audio.duration || !els.seek) return false;
	const r = els.seek.getBoundingClientRect();
	const ratio = Math.max(0, Math.min(1, (clientX - r.left) / Math.max(1, r.width)));
	audio.currentTime = ratio * audio.duration;
	_setSeekProgress(ratio);
	_syncDesktopOverlay(undefined, true);
	return true;
}

// ── UI ─────────────────────────────────────────────────────────────────────

function _wireSeek(seekEl) {
	let pointerId = null;
	seekEl.addEventListener('pointerdown', e => {
		if (!_seekToClientX(e.clientX)) return;
		pointerId = e.pointerId;
		seekEl.setPointerCapture?.(e.pointerId);
		e.preventDefault();
	});
	seekEl.addEventListener('pointermove', e => { if (pointerId === e.pointerId) _seekToClientX(e.clientX); });
	const end = e => { if (pointerId === e.pointerId) { seekEl.releasePointerCapture?.(e.pointerId); pointerId = null; } };
	seekEl.addEventListener('pointerup', end);
	seekEl.addEventListener('pointercancel', end);
}

function _ensureUI() {
	// Headless (remote) mode: no own UI - the main-window docked bar renders
	// from this engine's broadcasts instead. The render helpers below all guard
	// on els.<x> being present, so they no-op safely with an empty els.
	if (HEADLESS || root) return;
	const mount = document.getElementById('rwMusicPlayer') || document.body;
	root = document.createElement('div');
	root.style.display = 'none';
	_buildLegacyBubbleUI(root);
	mount.appendChild(root);
}

// Floating liquid-glass bubble - only the plain-browser (non-remote) fallback.
function _buildLegacyBubbleUI(bubbleRoot) {
	bubbleRoot.className = 'rw-music-player';
	bubbleRoot.innerHTML = `
		<div class="rw-mp-grip" title="Drag to move">
			<div class="rw-mp-art-shell">
				<img class="rw-mp-art" alt="">
				<span class="rw-mp-note" aria-hidden="true">&#9835;</span>
			</div>
			<div class="rw-mp-meta">
				<div class="rw-mp-title">—</div>
				<div class="rw-mp-artist"></div>
			</div>
			<button class="rw-mp-play-pill" type="button" data-act="toggle" title="Play/Pause" aria-label="Play or pause">${_icon('play')}</button>
			<button class="rw-mp-expand" type="button" title="Expand" aria-label="Expand player">${_icon('chevronDown')}</button>
		</div>
		<div class="rw-mp-controls">
			<button class="rw-mp-btn" data-act="prev" title="Previous" aria-label="Previous">${_icon('prev')}</button>
			<button class="rw-mp-btn rw-mp-play" data-act="toggle" title="Play/Pause" aria-label="Play or pause">${_icon('play')}</button>
			<button class="rw-mp-btn" data-act="next" title="Next" aria-label="Next">${_icon('next')}</button>
			<button class="rw-mp-btn" data-act="loop" title="${REPEAT_LABEL.off}" aria-label="${REPEAT_LABEL.off}">${_icon('loop')}</button>
			<div class="rw-mp-seek"><div class="rw-mp-seek-fill"></div></div>
		</div>`;

	els = {
		grip: bubbleRoot.querySelector('.rw-mp-grip'),
		artShell: bubbleRoot.querySelector('.rw-mp-art-shell'),
		art: bubbleRoot.querySelector('.rw-mp-art'),
		title: bubbleRoot.querySelector('.rw-mp-title'),
		artist: bubbleRoot.querySelector('.rw-mp-artist'),
		playPill: bubbleRoot.querySelector('.rw-mp-play-pill'),
		expand: bubbleRoot.querySelector('.rw-mp-expand'),
		play: bubbleRoot.querySelector('.rw-mp-play'),
		loop: bubbleRoot.querySelector('[data-act="loop"]'),
		seekFill: bubbleRoot.querySelector('.rw-mp-seek-fill'),
		seek: bubbleRoot.querySelector('.rw-mp-seek'),
	};

	bubbleRoot.querySelector('.rw-mp-controls').addEventListener('click', e => {
		const b = e.target.closest('[data-act]');
		if (!b) return;
		const act = b.dataset.act;
		if (act === 'toggle') _togglePlay();
		else if (act === 'next') _next();
		else if (act === 'prev') _prev();
		else if (act === 'loop') _toggleLoop();
	});
	els.playPill.addEventListener('click', _togglePlay);
	els.expand.addEventListener('click', () => _setExpanded(!state.expanded));
	_wireSeek(els.seek);

	_syncRepeatButton();
	_setExpanded(false);
	_restorePos();
	_wireDrag();
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
	root.classList.toggle('error', mode === 'error');
}

function _renderPlayState() {
	const markup = state.playing ? _icon('pause') : _icon('play');
	if (els.play) els.play.innerHTML = markup;
	if (els.playPill) els.playPill.innerHTML = markup;
}

function _toggleLoop() {
	_setRepeat(nextRepeat(state.repeat));
}

function _setRepeat(mode) {
	state.repeat = normalizeRepeat(mode);
	_lsSet(REPEAT_KEY, state.repeat);
	_syncRepeatButton();
	_syncDesktopOverlay(undefined, true);
	_broadcast(undefined, state.playing);
}

function _syncRepeatButton() {
	if (!els?.loop) return;
	els.loop.classList.toggle('on', state.repeat !== 'off');
	els.loop.innerHTML = _icon(state.repeat === 'one' ? 'loopOne' : 'loop');
	els.loop.title = REPEAT_LABEL[state.repeat];
	els.loop.setAttribute('aria-label', REPEAT_LABEL[state.repeat]);
}

function applyVolume(v) {
	state.volume = Math.max(0, Math.min(1.5, Number(v) || 0));
	if (audio) audio.volume = Math.min(1, state.volume);
	_lsSet(VOLUME_KEY, String(state.volume));
}

function _setExpanded(on) {
	state.expanded = on;
	root.classList.toggle('expanded', on);
	els.expand.innerHTML = _icon(on ? 'chevronUp' : 'chevronDown');
}

function _show() { if (root) root.style.display = state.desktopOverlay ? 'none' : ''; }

// Toast: a small transient announcement when a track starts.
function _announce(track) {
	const mount = document.getElementById('rwMusicPlayer') || document.body;
	const toast = document.createElement('div');
	toast.className = 'rw-mp-toast';
	toast.textContent = 'Playing ' + (track.title || 'track') + (track.artist ? ' — ' + track.artist : '');
	mount.appendChild(toast);
	requestAnimationFrame(() => toast.classList.add('in'));
	setTimeout(() => { toast.classList.remove('in'); setTimeout(() => toast.remove(), 260); }, 3000);
}

// ── Drag (lightweight pointer-capture port) ──────────────────────────────────

function _clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

function _restorePos() {
	try {
		const p = JSON.parse(_lsGet(POS_KEY) || 'null');
		if (p && Number.isFinite(p.left) && Number.isFinite(p.top)) {
			root.style.left = _clamp(p.left, 8, window.innerWidth - 120) + 'px';
			root.style.top = _clamp(p.top, 8, window.innerHeight - 60) + 'px';
			root.style.right = 'auto';
			root.style.bottom = 'auto';
		}
	} catch { /* default bottom-right anchor from CSS */ }
}

function _wireDrag() {
	let drag = null;
	els.grip.addEventListener('pointerdown', e => {
		if (e.target.closest('button')) return;
		const r = root.getBoundingClientRect();
		drag = { dx: e.clientX - r.left, dy: e.clientY - r.top, w: r.width, h: r.height };
		root.classList.add('dragging');
		try { els.grip.setPointerCapture(e.pointerId); } catch {}
		e.preventDefault();
	});
	els.grip.addEventListener('pointermove', e => {
		if (!drag) return;
		const left = _clamp(e.clientX - drag.dx, 8, window.innerWidth - drag.w - 8);
		const top = _clamp(e.clientY - drag.dy, 8, window.innerHeight - drag.h - 8);
		Object.assign(root.style, { left: left + 'px', top: top + 'px', right: 'auto', bottom: 'auto' });
	});
	const stop = e => {
		if (!drag) return;
		drag = null;
		root.classList.remove('dragging');
		try { els.grip.releasePointerCapture(e.pointerId); } catch {}
		const r = root.getBoundingClientRect();
		_lsSet(POS_KEY, JSON.stringify({ left: r.left, top: r.top }));
	};
	els.grip.addEventListener('pointerup', stop);
	els.grip.addEventListener('pointercancel', stop);
}

// ── Audio element lifecycle ──────────────────────────────────────────────────

function _ensureAudio() {
	if (audio) return;
	audio = new Audio();
	audio.preload = 'auto';
	audio.volume = Math.min(1, state.volume);
	let erroredOnce = false;
	audio.addEventListener('playing', () => {
		state.playing = true; erroredOnce = false; _renderPlayState();
		const track = state.queue[state.index];
		if (track) { _renderMeta(track, 'playing'); if (!track._announced) { track._announced = true; _announce(track); } }
		_broadcast('playing', true);
		_syncDesktopOverlay('playing', true);
		_preResolveUpcoming();
	});
	audio.addEventListener('pause', () => {
		state.playing = false;
		_renderPlayState();
		_broadcast('paused', false);
		_syncDesktopOverlay('paused', true);
	});
	audio.addEventListener('timeupdate', () => {
		if (audio.duration) _setSeekProgress(audio.currentTime / audio.duration);
		_syncDesktopOverlay();
		_broadcastProgress();
	});
	audio.addEventListener('seeked', () => _broadcastProgress(true));
	audio.addEventListener('durationchange', () => _syncDesktopOverlay(undefined, true));
	audio.addEventListener('ended', () => _next(true));
	audio.addEventListener('error', () => {
		// One re-resolve attempt for an expired stream URL, then surface failure.
		if (!erroredOnce && state.queue[state.index]) { erroredOnce = true; _reResolveAndResume(); }
	});
}

// ── Boot ─────────────────────────────────────────────────────────────────────

export function bootMusicPlayer() {
	if (_booted) return;
	_booted = true;
	_ensureAudio();
	// Cross-tab sync: reflect now-playing broadcasts we didn't originate.
	document.addEventListener('rainette:helper-message', e => {
		const msg = e.detail;
		if (msg?.type === 'music_now_playing' && msg.track) {
			app.musicNowPlaying = msg.track;
		} else if (msg?.type === 'music_overlay_state') {
			if (msg.ok === false) _setDesktopOverlayVisible(false);
			else _setDesktopOverlayVisible(!!msg.visible);
		} else if (msg?.type === 'music_overlay_action') {
			if (!state.queue.length) return;
			const act = msg.action;
			if (act === 'toggle') _togglePlay();
			else if (act === 'next') _next();
			else if (act === 'prev') _prev();
			else if (act === 'loop') _toggleLoop();
			else if (act === 'seek' && audio && audio.duration) {
				const ratio = Math.max(0, Math.min(1, Number(msg.ratio || 0)));
				audio.currentTime = ratio * audio.duration;
				_setSeekProgress(ratio);
				_syncDesktopOverlay(undefined, true);
			}
		}
	});
}

// The desktop native window is always the remote playback engine.  The
// miniplayer preference affects only whether that window is revealed, never
// which engine owns audio.
if (typeof document !== 'undefined' && !window.RW_REMOTE) {
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootMusicPlayer);
	else bootMusicPlayer();
	window.RainetteMusic = RainetteMusic;
}
