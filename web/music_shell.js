/* The page shell the music modules import: the helper WebSocket transport,
 * the DOM utilities, the shared `app` state bag, and the router that mounts
 * the music page. */

import { loopFlagFor, repeatFromMessage } from './repeat_mode.js';

// ── App state (only what the music modules touch) ────────────────────────────

export const app = {
	helperWS: null,
	helperQueue: [],
	helperPending: new Map(),   // reserved for parity; unused by music modules
	memPending: new Map(),      // id → resolve() for helperRequest round-trips
	musicNowPlaying: null,
	musicQueue: { tracks: [], index: -1, playing: false, state: 'idle', loop: false, duration: 0 },
};

const RAINETTE_TOKEN = new URLSearchParams(location.search).get('token') || '';

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
	if (app.helperWS && (app.helperWS.readyState === WebSocket.OPEN || app.helperWS.readyState === WebSocket.CONNECTING)) {
		return app.helperWS;
	}
	const ws = new WebSocket(_wsUrl());
	app.helperWS = ws;
	ws.addEventListener('open', () => {
		const queue = app.helperQueue.splice(0);
		for (const json of queue) ws.send(json);
		// Both WebViews use this as their reconnect handshake.  The server is a
		// live fan-out relay (not a durable command queue), so anything broadcast
		// while one window was offline must be recovered from the other window's
		// authoritative state after every successful socket open.
		document.dispatchEvent(new CustomEvent('rainette:helper-open'));
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
	const ws = ensureHelperWS();
	const json = JSON.stringify(payload);
	if (ws.readyState === WebSocket.OPEN) ws.send(json);
	else {
		app.helperQueue.push(json);
		if (app.helperQueue.length > 60) app.helperQueue.shift();
	}
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
	const _restoreState = (state, playing = false) => {
		if (state === 'playing' || state === 'paused' || state === 'loading') return state;
		return playing ? 'playing' : 'paused';
	};
	// Automatic pop-out is deliberately opt-in. Read the preference at action
	// time so changing Settings takes effect immediately without reloading either
	// native window. The docked player remains the normal playback surface.
	const _miniPlayerAutoOpenEnabled = () => {
		try { return localStorage.getItem('rainette.miniplayerEnabled') === '1'; }
		catch { return false; }
	};
	const _showPlayerIfEnabled = () => {
		if (!_miniPlayerAutoOpenEnabled()) return;
		try { window.pywebview?.api?.reveal_player?.(); } catch { /* not in pywebview */ }
	};
	const _queueControl = payload => sendHelper({ type: 'music_remote_control', ...payload });

	window.RainetteMusic = {
		playQueue(tracks, startIndex = 0) {
			_lastPlay = { tracks: tracks || [], index: startIndex, restoreState: 'loading' };
			sendHelper({ type: 'music_remote_play', tracks: _lastPlay.tracks, index: startIndex });
			_showPlayerIfEnabled();
		},
		playTrack(track) { this.playQueue([track], 0); },
		/* Sends what the user asked for, not "the other one".
		 *
		 * `playing` here is whatever the last broadcast said, and broadcasts can
		 * lag a press or be missed entirely while the player window is parked.
		 * With `toggle` a stale flag means the button does the opposite of its
		 * own icon; with an absolute verb the worst case is a no-op. */
		toggle() {
			// Derived from the same thing the button's icon is derived from, or
			// the verb and the glyph disagree. A loading row shows a pause
			// affordance while `playing` is still false, so asking for "the
			// opposite of playing" there would ask to start what is already
			// starting instead of cancelling it.
			const queue = app.musicQueue || {};
			const showsPause = !!queue.playing || queue.state === 'loading';
			sendHelper({ type: 'music_remote_control', action: showsPause ? 'pause' : 'play' });
		},
		play() { sendHelper({ type: 'music_remote_control', action: 'play' }); },
		pause() { sendHelper({ type: 'music_remote_control', action: 'pause' }); },
		next() { sendHelper({ type: 'music_remote_control', action: 'next' }); },
		prev() { sendHelper({ type: 'music_remote_control', action: 'prev' }); },
		toggleLoop() { sendHelper({ type: 'music_remote_control', action: 'loop' }); },
		isLooping() { return !!app.musicQueue?.loop; },
		repeatMode() { return app.musicQueue?.repeat || 'off'; },
		setRepeat(mode) { sendHelper({ type: 'music_remote_control', action: 'set_repeat', mode }); },
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
		queuePlayIndex(index) { _queueControl({ action: 'queue_play_index', index }); _showPlayerIfEnabled(); },
		queueShuffle() { _queueControl({ action: 'queue_shuffle' }); },
		queueDedupe() { _queueControl({ action: 'queue_dedupe' }); },
		queueClearUpNext() { _queueControl({ action: 'queue_clear_up_next' }); },
		/* Route audio to one sink. The <audio> is in the player window, so this
		 * asks and waits: a false answer means this engine cannot route, and the
		 * caller offers the system sound panel instead. helperRequest is unusable
		 * because the bridge echoes the request back with its own id. */
		setOutputSink(sinkId) {
			if (!sinkId) return Promise.resolve(false);
			const id = 'sink_' + Math.random().toString(36).slice(2);
			return new Promise(resolve => {
				const onMessage = event => {
					const msg = event.detail || {};
					if (msg.type !== 'music_output_sink_result' || msg.id !== id) return;
					clearTimeout(timer);
					document.removeEventListener('rainette:helper-message', onMessage);
					resolve(!!msg.routed);
				};
				const timer = setTimeout(() => {
					document.removeEventListener('rainette:helper-message', onMessage);
					resolve(false);
				}, 4000);
				document.addEventListener('rainette:helper-message', onMessage);
				sendHelper({ type: 'music_remote_control', action: 'set_sink', sink_id: sinkId, id });
			});
		},
	};

	document.addEventListener('rainette:helper-message', e => {
		const msg = e.detail;
		if (!msg) return;
		// Reflect now-playing broadcasts from the player window into app state.
		if (msg.type === 'music_now_playing') {
			if (msg.track) app.musicNowPlaying = msg.track;
			if (Array.isArray(msg.queue)) {
				const incomingState = msg.state || (msg.playing ? 'playing' : 'paused');
				// On a cold player reload, the main window's reconnect probe can
				// arrive before the player's own music_request_state. The empty
				// player then answers `idle`; that is not an authoritative queue
				// clear and must not erase the cached paused/loading intent that the
				// player is about to request. Real queue clears broadcast `paused`.
				if (!msg.queue.length && incomingState === 'idle' && _lastPlay?.tracks?.length) return;
				// Keep the current repeat mode when a producer says nothing about it
				// (the phone has no repeat control); an absent field must not read
				// as "off" and silently reset the button mid-session.
				const repeat = repeatFromMessage(msg, app.musicQueue?.repeat || 'off');
				app.musicQueue = {
					tracks: msg.queue,
					index: Number.isFinite(Number(msg.index)) ? Number(msg.index) : -1,
					playing: !!msg.playing,
					state: incomingState,
					repeat,
					loop: loopFlagFor(repeat),
					duration: Number(msg.queue_duration || 0),
					count: Number(msg.queue_count || msg.queue.length || 0),
				};
				if (app.musicQueue.tracks.length) {
					_lastPlay = {
						tracks: app.musicQueue.tracks,
						index: app.musicQueue.index,
						restoreState: _restoreState(incomingState, msg.playing),
					};
				} else {
					_lastPlay = null;
					app.musicNowPlaying = null;
				}
				document.dispatchEvent(new CustomEvent('rainette:music-queue', { detail: app.musicQueue }));
			}
		}
		// The player window (re)connected and asked for the current queue.
		else if (msg.type === 'music_request_state' && _lastPlay) {
			// Mark this as a state restore so the player can distinguish it from a
			// fresh user play. In particular, `playing:false` alone cannot tell a
			// paused transport from one that was actively resolving a stream.
			const restoreState = _lastPlay.restoreState || 'paused';
			sendHelper({
				type: 'music_remote_play',
				tracks: _lastPlay.tracks,
				index: _lastPlay.index,
				restore_state: restoreState,
				playing: restoreState === 'playing',
			});
		}
	});

	// If the main WebView itself reconnects, ask the player for the transport
	// state it may have continued advancing while this socket was unavailable.
	document.addEventListener('rainette:helper-open', () => {
		_queueControl({ action: 'queue_request_state' });
	});
}

// Open the socket eagerly so the page is responsive on first paint.
if (typeof document !== 'undefined') ensureHelperWS();
