/**
 * Standalone shell for the Rainette Music app.
 *
 * Replaces the pieces of jarvis's rainette_home.js + rainette_router_shell.js
 * that the two copied music modules import: the helper WebSocket transport
 * (sendHelper / helperRequest), the DOM utilities (el / btn), the shared `app`
 * state bag, and a minimal RainetteRouter that mounts the music page.
 *
 * The WebSocket contract is identical to the jarvis helper, so the copied
 * modules work unchanged apart from their import paths.
 */

// ── App state (only what the music modules touch) ────────────────────────────

export const app = {
	helperWS: null,
	helperQueue: [],
	helperPending: new Map(),   // reserved for parity; unused by music modules
	memPending: new Map(),      // id → resolve() for helperRequest round-trips
	musicNowPlaying: null,
	musicQueue: { tracks: [], index: -1, playing: false, loop: false, duration: 0 },
};

const RAINETTE_TOKEN = new URLSearchParams(location.search).get('token') || '';

function _nativeTransport() {
	return window.RainetteNativeTransport?.isNative ? window.RainetteNativeTransport : null;
}

export function rainetteAuthHeaders() {
	return RAINETTE_TOKEN ? { 'X-Rainette-Token': RAINETTE_TOKEN } : {};
}

// The server serves both the page and the /ws endpoint, so derive the socket
// URL from the current origin rather than hardcoding a port.
function _wsUrl() {
	const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
	const token = encodeURIComponent(RAINETTE_TOKEN);
	return `${proto}//${location.host}/ws?token=${token}`;
}

// ── WebSocket transport ──────────────────────────────────────────────────────

export function ensureHelperWS() {
	if (_nativeTransport()) return null;
	if (app.helperWS && (app.helperWS.readyState === WebSocket.OPEN || app.helperWS.readyState === WebSocket.CONNECTING)) {
		return app.helperWS;
	}
	const ws = new WebSocket(_wsUrl());
	app.helperWS = ws;
	ws.addEventListener('open', () => {
		const queue = app.helperQueue.splice(0);
		for (const json of queue) ws.send(json);
	});
	ws.addEventListener('close', () => {
		app.helperWS = null;
		setTimeout(ensureHelperWS, 1000);   // auto-reconnect
	});
	ws.addEventListener('message', ev => {
		let msg;
		try { msg = JSON.parse(ev.data); } catch { return; }
		handleHelperMessage(msg);
	});
	return ws;
}

export function sendHelper(payload) {
	const native = _nativeTransport();
	if (native) {
		Promise.resolve(native.request(payload)).then(response => {
			if (response) handleHelperMessage(response);
		}).catch(error => {
			handleHelperMessage({ id: payload.id, ok: false, msg: error?.message || 'native companion request failed' });
		});
		return;
	}
	const ws = ensureHelperWS();
	const json = JSON.stringify(payload);
	if (ws.readyState === WebSocket.OPEN) ws.send(json);
	else {
		app.helperQueue.push(json);
		if (app.helperQueue.length > 60) app.helperQueue.shift();
	}
}

if (typeof window !== 'undefined') {
	window.addEventListener('rainette:native-message', event => handleHelperMessage(event.detail));
}

export function helperRequest(type, payload = {}, timeoutMs = 5000) {
	return new Promise(resolve => {
		const id = payload.id || (type + '_' + Math.random().toString(36).slice(2));
		app.memPending.set(id, resolve);
		setTimeout(() => {
			if (app.memPending.has(id)) {
				app.memPending.delete(id);
				resolve({ ok: false, msg: 'request timed out', items: [] });
			}
		}, timeoutMs);
		sendHelper({ type, id, ...payload });
	});
}

function handleHelperMessage(msg) {
	// Resolve any pending helperRequest keyed by echoed id.
	if (msg && msg.id && app.memPending.has(msg.id)) {
		app.memPending.get(msg.id)(msg);
		app.memPending.delete(msg.id);
	}
	// Every message also fans out to the page/mini-player listeners.
	document.dispatchEvent(new CustomEvent('rainette:helper-message', { detail: msg }));
}

// ── DOM utilities (ported verbatim from rainette_home.js) ─────────────────────

export function el(tag, className, text) {
	const node = document.createElement(tag);
	if (className) node.className = className;
	if (text != null) node.textContent = text;
	return node;
}

export function btn(label, className, onClick) {
	const node = el('button', className || 'rh-button', label);
	node.type = 'button';
	if (onClick) node.addEventListener('click', onClick);
	return node;
}

// ── Minimal router: mount the single music page into its host ─────────────────

export const RainetteRouter = {
	_pages: {},
	register(name, page) {
		this._pages[name] = page;
		if (name === 'music') _mountWhenReady(page);
	},
};

function _mountWhenReady(page) {
	const mount = () => {
		const host = document.getElementById('rwMusicPage');
		if (host) page.mount(host);
	};
	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
	else mount();
}

// ── Remote control shim (detached-player mode) ───────────────────────────────
// When the transport lives in a separate native window, the page's play/transport
// calls become socket messages the player window acts on. rainette_music.js keeps
// calling window.RainetteMusic.* unchanged; this shim stands in for the engine.

if (typeof window !== 'undefined' && window.RW_REMOTE) {
	let _lastPlay = null;
	const _showPlayer = () => { try { window.pywebview && window.pywebview.api && window.pywebview.api.reveal_player(); } catch { /* not in pywebview */ } };
	const _queueControl = payload => sendHelper({ type: 'music_remote_control', ...payload });

	window.RainetteMusic = {
		playQueue(tracks, startIndex = 0) {
			_lastPlay = { tracks: tracks || [], index: startIndex };
			sendHelper({ type: 'music_remote_play', tracks: _lastPlay.tracks, index: startIndex });
			_showPlayer();   // reveal the (hidden-until-play) player window
		},
		playTrack(track) { this.playQueue([track], 0); },
		toggle() { sendHelper({ type: 'music_remote_control', action: 'toggle' }); },
		next() { sendHelper({ type: 'music_remote_control', action: 'next' }); },
		prev() { sendHelper({ type: 'music_remote_control', action: 'prev' }); },
		toggleLoop() { sendHelper({ type: 'music_remote_control', action: 'loop' }); },
		isLooping() { return !!app.musicQueue?.loop; },
		seek(ratio) { _queueControl({ action: 'seek', ratio }); },
		// Volume lives in the floating player window; the shared localStorage
		// key (same origin) keeps the two windows' idea of it in sync, and the
		// remote command applies it live.
		setVolume(v) {
			const vol = Math.max(0, Math.min(1.5, Number(v) || 0));
			try { localStorage.setItem('rw.mp.volume', String(vol)); } catch { /* best effort */ }
			_queueControl({ action: 'set_volume', value: vol });
		},
		getVolume() {
			try { const v = Number(localStorage.getItem('rw.mp.volume')); return Number.isFinite(v) && v >= 0 ? v : 1; }
			catch { return 1; }
		},
		current() { return app.musicNowPlaying || null; },
		isPlaying() { return !!app.musicQueue?.playing; },
		queueState() { return app.musicQueue || { tracks: [], index: -1 }; },
		requestQueueState() { _queueControl({ action: 'queue_request_state' }); },
		queueAddNext(track) { _queueControl({ action: 'queue_add_next', track }); },
		queueAddEnd(track) { _queueControl({ action: 'queue_add_end', track }); },
		queueMove(from, to) { _queueControl({ action: 'queue_move', from, to }); },
		queueRemove(index) { _queueControl({ action: 'queue_remove', index }); },
		queuePlayIndex(index) { _queueControl({ action: 'queue_play_index', index }); },
		queueShuffle() { _queueControl({ action: 'queue_shuffle' }); },
		queueDedupe() { _queueControl({ action: 'queue_dedupe' }); },
		queueClearUpNext() { _queueControl({ action: 'queue_clear_up_next' }); },
	};

	document.addEventListener('rainette:helper-message', e => {
		const msg = e.detail;
		if (!msg) return;
		// Reflect now-playing broadcasts from the player window into app state.
		if (msg.type === 'music_now_playing') {
			if (msg.track) app.musicNowPlaying = msg.track;
			if (Array.isArray(msg.queue)) {
				app.musicQueue = {
					tracks: msg.queue,
					index: Number.isFinite(Number(msg.index)) ? Number(msg.index) : -1,
					playing: !!msg.playing,
					loop: !!msg.loop,
					duration: Number(msg.queue_duration || 0),
					count: Number(msg.queue_count || msg.queue.length || 0),
				};
				if (app.musicQueue.tracks.length) _lastPlay = { tracks: app.musicQueue.tracks, index: app.musicQueue.index };
				document.dispatchEvent(new CustomEvent('rainette:music-queue', { detail: app.musicQueue }));
			}
		}
		// The player window (re)connected and asked for the current queue.
		else if (msg.type === 'music_request_state' && _lastPlay) {
			sendHelper({ type: 'music_remote_play', tracks: _lastPlay.tracks, index: _lastPlay.index });
		}
		else if (msg.type === 'music_output_transfer' && msg.target_device_id === 'desktop') {
			const tracks = Array.isArray(msg.queue) ? msg.queue : [];
			if (!tracks.length) {
				sendHelper({ type: 'music_output_transfer_result', id: msg.id, ok: false, msg: 'Transfer queue is empty' });
				return;
			}
			// playQueue hands the queue to the desktop-owned player. The source
			// phone receives the success response only after this target accepted
			// it, so it can then pause without a playback gap on failed transfers.
			window.RainetteMusic?.playQueue(tracks, Math.max(0, Number(msg.index) || 0));
			sendHelper({
				type: 'music_output_transfer_result', id: msg.id, ok: true,
				target_device_id: 'desktop', current_time: Number(msg.current_time || 0),
			});
		}
	});
}

// Open the socket eagerly so the page is responsive on first paint.
if (typeof document !== 'undefined') ensureHelperWS();
