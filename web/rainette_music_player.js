/**
 * Rainette Music mini-player — the persistent liquid-glass "now playing" bubble.
 *
 * Boots once at load (independent of the router, so it survives page nav) and
 * owns the single global <audio> element + playback queue. Exposes a small
 * window.RainetteMusic API the Music page drives:
 *   RainetteMusic.playTrack(track), .playQueue(tracks, i), .toggle(), .next(), .prev()
 *
 * The bubble stays hidden until a track is actually loaded (no empty "playing
 * nothing" chrome). Drag logic is a lightweight local port of the pointer-
 * capture pattern in rainette_router_shell.js — the player only needs drag,
 * not resize, and can't reach index.html's inline makeDraggablePanel from a module.
 */

import { sendHelper, helperRequest, app } from './music_shell.js';

const POS_KEY = 'rainette.musicPlayerPos';
const LOOP_KEY = 'rainette.musicLoop';
const LOCAL_STREAM_TTL_MS = 50 * 60 * 1000;
const PREFETCH_AHEAD = 3;

// Guarded storage access so the module imports cleanly in a DOM-free test env.
function _lsGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function _lsSet(key, val) { try { localStorage.setItem(key, val); } catch { /* best effort */ } }

const state = {
	queue: [],          // [{ source_id, title, artist, thumbnail_url, ... }]
	index: -1,
	loop: _lsGet(LOOP_KEY) === '1',
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
	sendHelper({ type: 'music_now_playing_set', track, state: 'loading', playing: false, loop: state.loop, duration: track.duration_s || 0 });
	_syncDesktopOverlay('loading', true);
	if (_streamFresh(track)) {
		audio.src = track._url;
		audio.play().catch(() => {});
		return;
	}

	try {
		const res = await _requestStream(track);
		// A newer track may have superseded this resolve while it was in flight.
		if (state.resolvingId !== track.source_id) return;
		if (!res || res.ok === false || !res.url) {
			_renderMeta(track, 'error');
			_syncDesktopOverlay('error', true);
			return;
		}
		audio.src = res.url;
		_rememberStream(track, res);
		await audio.play();
	} catch (err) {
		if (state.resolvingId === track.source_id) {
			_renderMeta(track, 'error');
			_syncDesktopOverlay('error', true);
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
	return false;
}

// ── Transport ────────────────────────────────────────────────────────────────

function _togglePlay() {
	if (!audio || !state.queue.length) return;
	if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

function _next() {
	if (!state.queue.length) return;
	if (state.index < state.queue.length - 1) { state.index++; _loadCurrent(); }
	else if (state.loop) { state.index = 0; _loadCurrent(); }
}

function _prev() {
	if (!state.queue.length) return;
	// Restart the track if we're past the first few seconds, else go back.
	if (audio && audio.currentTime > 3) { audio.currentTime = 0; _syncDesktopOverlay(undefined, true); return; }
	if (state.index > 0) { state.index--; _loadCurrent(); }
	else if (state.loop) { state.index = state.queue.length - 1; _loadCurrent(); }
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
		loop: !!state.loop,
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
	const icons = {
		prev: '<path d="M6 5v14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M18 5.5v13a.6.6 0 0 1-.94.49L8 12.5a.6.6 0 0 1 0-.98l9.06-6.5a.6.6 0 0 1 .94.48z" fill="currentColor" stroke="none"/>',
		next: '<path d="M18 5v14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M6 5.5v13a.6.6 0 0 0 .94.49L16 12.5a.6.6 0 0 0 0-.98L6.94 5.02a.6.6 0 0 0-.94.48z" fill="currentColor" stroke="none"/>',
		play: '<path d="M8 5v14l11-7-11-7z" fill="currentColor" stroke="none"/>',
		pause: '<path d="M7 5h4v14H7z" fill="currentColor" stroke="none"/><path d="M13 5h4v14h-4z" fill="currentColor" stroke="none"/>',
		loop: '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
		chevronDown: '<path d="M6 9l6 6 6-6"/>',
		chevronUp: '<path d="M6 15l6-6 6 6"/>',
	};
	return `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icons[name] || ''}</svg>`;
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

function _ensureUI() {
	if (root) return;
	const mount = document.getElementById('rwMusicPlayer') || document.body;
	root = document.createElement('div');
	root.className = 'rw-music-player';
	root.style.display = 'none';
	root.innerHTML = `
		<div class="rw-mp-grip" title="Drag to move">
			<img class="rw-mp-art" alt="">
			<div class="rw-mp-meta">
				<div class="rw-mp-title">—</div>
				<div class="rw-mp-artist"></div>
			</div>
			<button class="rw-mp-expand" type="button" title="Expand" aria-label="Expand player">${_icon('chevronDown')}</button>
		</div>
		<div class="rw-mp-controls">
			<button class="rw-mp-btn" data-act="prev" title="Previous" aria-label="Previous">${_icon('prev')}</button>
			<button class="rw-mp-btn rw-mp-play" data-act="toggle" title="Play/Pause" aria-label="Play or pause">${_icon('play')}</button>
			<button class="rw-mp-btn" data-act="next" title="Next" aria-label="Next">${_icon('next')}</button>
			<button class="rw-mp-btn" data-act="loop" title="Loop" aria-label="Toggle loop">${_icon('loop')}</button>
			<div class="rw-mp-seek"><div class="rw-mp-seek-fill"></div></div>
		</div>`;
	mount.appendChild(root);

	els = {
		grip: root.querySelector('.rw-mp-grip'),
		art: root.querySelector('.rw-mp-art'),
		title: root.querySelector('.rw-mp-title'),
		artist: root.querySelector('.rw-mp-artist'),
		expand: root.querySelector('.rw-mp-expand'),
		play: root.querySelector('.rw-mp-play'),
		loop: root.querySelector('[data-act="loop"]'),
		seekFill: root.querySelector('.rw-mp-seek-fill'),
		seek: root.querySelector('.rw-mp-seek'),
	};

	root.querySelector('.rw-mp-controls').addEventListener('click', e => {
		const b = e.target.closest('[data-act]');
		if (!b) return;
		const act = b.dataset.act;
		if (act === 'toggle') _togglePlay();
		else if (act === 'next') _next();
		else if (act === 'prev') _prev();
		else if (act === 'loop') _toggleLoop();
	});
	els.expand.addEventListener('click', () => _setExpanded(!state.expanded));
	let seekPointerId = null;
	els.seek.addEventListener('pointerdown', e => {
		if (!_seekToClientX(e.clientX)) return;
		seekPointerId = e.pointerId;
		els.seek.setPointerCapture?.(e.pointerId);
		e.preventDefault();
	});
	els.seek.addEventListener('pointermove', e => {
		if (seekPointerId !== e.pointerId) return;
		_seekToClientX(e.clientX);
	});
	const endSeek = e => {
		if (seekPointerId !== e.pointerId) return;
		_seekToClientX(e.clientX);
		els.seek.releasePointerCapture?.(e.pointerId);
		seekPointerId = null;
	};
	els.seek.addEventListener('pointerup', endSeek);
	els.seek.addEventListener('pointercancel', endSeek);

	els.loop.classList.toggle('on', state.loop);
	_setExpanded(false);
	_restorePos();
	_wireDrag();
}

function _renderMeta(track, mode) {
	if (!els.title) return;
	els.title.textContent = track.title || '(untitled)';
	els.artist.textContent = mode === 'loading' ? 'Loading…' : (mode === 'error' ? 'Playback failed' : (track.artist || ''));
	if (track.thumbnail_url) { els.art.src = track.thumbnail_url; els.art.style.display = ''; }
	else els.art.style.display = 'none';
	root.classList.toggle('error', mode === 'error');
}

function _renderPlayState() {
	if (els.play) els.play.innerHTML = state.playing ? _icon('pause') : _icon('play');
}

function _toggleLoop() {
	state.loop = !state.loop;
	_lsSet(LOOP_KEY, state.loop ? '1' : '0');
	els.loop.classList.toggle('on', state.loop);
	_syncDesktopOverlay(undefined, true);
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
	let erroredOnce = false;
	audio.addEventListener('playing', () => {
		state.playing = true; erroredOnce = false; _renderPlayState();
		const track = state.queue[state.index];
		if (track) { _renderMeta(track, 'playing'); if (!track._announced) { track._announced = true; _announce(track); } }
		sendHelper({ type: 'music_now_playing_set', track, state: 'playing', playing: true, loop: state.loop, current_time: audio.currentTime || 0, duration: audio.duration || track?.duration_s || 0 });
		_syncDesktopOverlay('playing', true);
		_preResolveUpcoming();
	});
	audio.addEventListener('pause', () => {
		state.playing = false;
		_renderPlayState();
		const track = state.queue[state.index];
		sendHelper({ type: 'music_now_playing_set', track, state: 'paused', playing: false, loop: state.loop, current_time: audio.currentTime || 0, duration: audio.duration || track?.duration_s || 0 });
		_syncDesktopOverlay('paused', true);
	});
	audio.addEventListener('timeupdate', () => {
		if (audio.duration) _setSeekProgress(audio.currentTime / audio.duration);
		_syncDesktopOverlay();
	});
	audio.addEventListener('durationchange', () => _syncDesktopOverlay(undefined, true));
	audio.addEventListener('ended', () => _next());
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

// In detached-player mode the transport lives in miniplayer.js (its own native
// window), so the docked in-page bubble stays inert here. A plain browser tab
// (no ?remote flag) still boots the bubble as the local engine.
if (typeof document !== 'undefined' && !window.RW_REMOTE) {
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootMusicPlayer);
	else bootMusicPlayer();
	window.RainetteMusic = RainetteMusic;
}
