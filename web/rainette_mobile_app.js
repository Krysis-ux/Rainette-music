import { helperRequest } from './music_shell.js';
import { rainetteLoaderMarkup } from './rainette_loading.js';
import { repeatFromMessage } from './repeat_mode.js';

const native = window.RainetteNativeTransport;
if (native?.isNative) {
	const host = document.createElement('main');
	host.id = 'rwMobileApp';
	host.className = 'rw-mobile-app';
	document.body.append(host);

	const state = {
		tab: 'home', track: null, playing: false, library: [], results: [],
		queue: [], queueIndex: -1, currentTime: 0, duration: 0,
		outputDeviceId: 'desktop',
		fullPlayer: false, outputPicker: false,
		libraryPending: false, searchPending: false, playbackPendingId: '',
		libraryError: '', searchError: '', playbackError: '',
		syncState: 'connecting',
		pairingOpen: false,
		pairing: { phase: 'checking', paired: false, connected: false, deviceId: '', endpointLabel: '', message: '' },
	};
	const esc = value => String(value || '').replace(/[&<>\"']/g, character => ({
		'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;',
	})[character]);
	const art = track => track?.thumbnail_url || './assets/rainette-icon-256.png';
	const trackKey = track => String(track?.source_id || track?.video_id || track?.url || track?.title || '');
	const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
	let phoneStatePublishTimer = null;
	let phoneOwnershipConfirmedAt = 0;

	function canonicalQueue() {
		const queue = state.queue.length ? state.queue.slice() : (state.track ? [state.track] : []);
		const index = queue.length ? clamp(Number(state.queueIndex) || 0, 0, queue.length - 1) : -1;
		return { queue, index };
	}

	function requireSuccess(result, fallback) {
		if (result?.ok === false) throw new Error(result.msg || fallback);
		return result || {};
	}

	function phoneStateMessage() {
		const { queue, index } = canonicalQueue();
		return {
			type: 'music_now_playing_set',
			track: state.track,
			state: state.playing ? 'playing' : 'paused',
			playing: state.playing,
			// No loop/repeat: the phone has no repeat control, so asserting one here
			// only ever reset whatever the desktop was set to.
			current_time: state.currentTime,
			duration: state.duration,
			queue,
			index,
			output_device_id: 'phone',
		};
	}

	async function publishPhoneState() {
		if (state.outputDeviceId !== 'phone' || !state.track) return;
		await native.request(phoneStateMessage());
	}

	function schedulePhoneStatePublish() {
		if (state.outputDeviceId !== 'phone' || state.playbackPendingId) return;
		clearTimeout(phoneStatePublishTimer);
		phoneStatePublishTimer = setTimeout(() => publishPhoneState().catch(() => {}), 120);
	}

	function errorLine(message) {
		return message ? `<p class="rw-mobile-error" role="alert">${esc(message)}</p>` : '';
	}

	function playerBar() {
		if (!state.track) return '';
		return `<button class="rw-mobile-now" data-action="open-player" aria-label="Open now playing">
			<img src="${esc(art(state.track))}" alt=""><span><b>${esc(state.track.title)}</b><small>${esc(state.track.artist)}</small></span>
			<i>${state.playing ? '&#10074;&#10074;' : '&#9654;'}</i>
		</button>`;
	}

	function rows(items, empty, source) {
		const pending = source === 'search' ? state.searchPending : state.libraryPending;
		const error = source === 'search' ? state.searchError : state.libraryError;
		if (pending) return rainetteLoaderMarkup(source === 'search' ? 'Searching Rainette' : 'Syncing your library');
		if (error && !items.length) return errorLine(error);
		if (!items.length) return `<p class="rw-mobile-empty-state">${empty}</p>`;
		return `${errorLine(error)}<div class="rw-mobile-song-list">${items.map((track, index) => {
			const busy = state.playbackPendingId && state.playbackPendingId === trackKey(track);
			return `<button class="rw-mobile-song${busy ? ' is-loading' : ''}" data-track="${index}" data-source="${source}" ${busy ? 'aria-busy="true"' : ''}>
				<img src="${esc(art(track))}" alt=""><span><b>${esc(track.title)}</b><small>${esc(track.artist)}</small></span>
				${busy ? rainetteLoaderMarkup('Preparing audio', { compact: true }) : '<i aria-hidden="true">&#8942;</i>'}
			</button>`;
		}).join('')}</div>${errorLine(state.playbackError)}`;
	}

	function syncBanner() {
		if (state.syncState !== 'reconnecting') return '';
		return `<div class="rw-mobile-sync-banner">${rainetteLoaderMarkup('Reconnecting to desktop', { compact: true })}</div>`;
	}

	function outputChooser() {
		if (!state.outputPicker) return '';
		return `<div class="rw-mobile-sheet-backdrop" data-action="output-close"><section class="rw-mobile-output-sheet" data-action="noop" role="dialog" aria-modal="true" aria-label="Choose playback device">
			<div class="rw-mobile-sheet-handle"></div><h2>Play on</h2>
			<button data-action="output-phone"><b>This phone</b><small>Use Android media controls and this phone's speakers</small></button>
			<button data-action="output-desktop"><b>Rainette desktop</b><small>Move the current track to your paired computer</small></button>
			<button class="rw-mobile-sheet-cancel" data-action="output-close">Cancel</button>
		</section></div>`;
	}

	function pairingCopy() {
		const phase = state.pairing.phase;
		if (phase === 'connecting') return ['Contacting desktop', 'Verifying the private connection and certificate.'];
		if (phase === 'pending_approval') return ['Waiting for approval', 'Approve this phone in Rainette desktop. Keep both devices on the same private Wi-Fi.'];
		if (phase === 'securing') return ['Securing the connection', 'Saving the encrypted device credential and confirming it with desktop.'];
		if (phase === 'approved' || state.pairing.paired) return ['Phone paired', state.pairing.endpointLabel ? `Connected to ${state.pairing.endpointLabel}.` : 'Search, library, and playback can now stay in sync.'];
		if (phase === 'rejected') return ['Pairing rejected', 'Create a new code on desktop when you are ready to try again.'];
		if (phase === 'expired') return ['Code expired', 'Create a fresh pairing code on desktop, then scan it again.'];
		if (phase === 'failed') return ['Could not pair', state.pairing.message || 'Check that both devices use the same private Wi-Fi and Rainette is allowed through Windows Firewall.'];
		if (phase === 'cancelled') return ['Pairing cancelled', 'Nothing changed. Scan a new code whenever you are ready.'];
		return ['Pair this phone', 'On desktop, open Mobile, create a pairing code, and scan it with your phone camera.'];
	}

	function pairingSheet() {
		if (!state.pairingOpen) return '';
		const [title, copy] = pairingCopy();
		const active = ['checking', 'connecting', 'pending_approval', 'securing'].includes(state.pairing.phase);
		const complete = state.pairing.paired || state.pairing.phase === 'approved';
		return `<div class="rw-mobile-sheet-backdrop" data-action="pairing-close"><section class="rw-mobile-pair-sheet" data-action="noop" role="dialog" aria-modal="true" aria-labelledby="rwPairTitle">
			<div class="rw-mobile-sheet-handle"></div>
			${rainetteLoaderMarkup(active ? title : (complete ? 'Pairing complete' : 'Ready to pair'), { state: active ? 'active' : 'complete' })}
			<h2 id="rwPairTitle">${esc(title)}</h2><p>${esc(copy)}</p>
			${state.pairing.deviceId ? `<span class="rw-mobile-device-id">Device ${esc(state.pairing.deviceId.slice(0, 8))}</span>` : ''}
			${!complete && !active ? `<form id="rwMobilePairForm" class="rw-mobile-pair-form"><label for="rwPairUri">Or paste a pairing link</label><input id="rwPairUri" name="uri" inputmode="url" autocomplete="off" placeholder="rainette://pair?..."><button type="submit">Pair securely</button></form>` : ''}
			<div class="rw-mobile-pair-actions"><button data-action="pairing-refresh">Check connection</button><button data-action="pairing-close">Done</button></div>
		</section></div>`;
	}

	function fullPlayer() {
		if (!state.fullPlayer || !state.track) return '';
		return `<section class="rw-mobile-player-view" aria-label="Now playing">
			<button class="rw-mobile-close-player" data-action="close-player" aria-label="Close now playing">&#8964;</button>
			<div class="rw-mobile-full-art"><img src="${esc(art(state.track))}" alt=""></div>
			<div class="rw-mobile-player-copy"><h1>${esc(state.track.title)}</h1><p>${esc(state.track.artist)}</p></div>
			${state.playbackPendingId ? rainetteLoaderMarkup('Preparing audio', { compact: true }) : ''}
			<div class="rw-mobile-full-controls"><button data-action="previous" aria-label="Previous">&#9198;</button><button class="rw-mobile-main-play" data-action="toggle-player" aria-label="Play or pause">${state.playing ? '&#10074;&#10074;' : '&#9654;'}</button><button data-action="next" aria-label="Next">&#9197;</button></div>
			<button class="rw-mobile-output" data-action="output">Play on&hellip;</button>
		</section>`;
	}

	function page() {
		if (state.fullPlayer) return `${fullPlayer()}${outputChooser()}${pairingSheet()}`;
		const tab = state.tab;
		let content = '';
		if (tab === 'home') content = `<header class="rw-mobile-top"><div class="rw-mobile-brand"><img src="./assets/rainette-icon-256.png" alt="">Rainette</div><button data-action="more" aria-label="Settings">&#9881;</button></header><section class="rw-mobile-hero"><p>Quiet listening, everywhere.</p><h1>Your music, in one calm place.</h1></section><section><h2>Recently played</h2>${rows(state.library.slice(0, 6), 'Play something from Search to start your history.', 'home')}</section>`;
		else if (tab === 'search') content = `<header class="rw-mobile-top"><h1>Search</h1><button data-action="more" aria-label="Settings">&#9881;</button></header><form class="rw-mobile-search" id="rwMobileSearch"><input id="rwMobileQuery" autocomplete="off" placeholder="Songs, artists, albums" aria-label="Search music"><button>Search</button></form><section><h2>Results</h2>${rows(state.results, 'Find any song from your paired Rainette desktop.', 'search')}</section>`;
		else if (tab === 'library') content = `<header class="rw-mobile-top"><h1>Library</h1><button data-action="more" aria-label="Settings">&#9881;</button></header><section><h2>Saved music</h2>${rows(state.library, 'Your saved tracks will appear here and stay synced with desktop.', 'library')}</section>`;
		else {
			const pairTitle = state.pairing.paired ? 'Paired desktop' : 'Pair a desktop';
			const pairDetail = state.pairing.paired ? (state.pairing.connected ? 'Connected and syncing now' : 'Saved securely; reconnecting') : 'Connect search, library, and playback';
			content = `<header class="rw-mobile-top"><h1>More</h1><button data-action="home" aria-label="Close settings">&times;</button></header><section class="rw-mobile-more"><button data-action="refresh-library"><b>Refresh library</b><small>Pull the latest paired desktop changes</small></button><button data-action="pairing"><b>${pairTitle}</b><small>${pairDetail}</small></button><button data-action="output"><b>Play on</b><small>This phone or Rainette desktop</small></button><button data-action="reduce-motion"><b>Reduce motion</b><small>Use still transitions and loaders</small></button></section>`;
		}
		return `${syncBanner()}<div class="rw-mobile-screen">${content}</div>${playerBar()}${outputChooser()}${pairingSheet()}<nav class="rw-mobile-tabs">${[['home','Home','&#8962;'],['search','Search','&#8981;'],['library','Library','&#9638;'],['more','More','&#8942;']].map(([id,label,icon]) => `<button class="${tab === id ? 'active' : ''}" data-tab="${id}"><i>${icon}</i>${label}</button>`).join('')}</nav>`;
	}

	function render() { host.innerHTML = page(); }

	async function refreshConnectionStatus({ busy = false } = {}) {
		if (busy) { state.pairing.phase = 'checking'; render(); }
		try {
			const result = await native.connectionStatus();
			state.pairing.paired = !!(result?.paired || (result?.ok && result?.device_id));
			state.pairing.connected = !!(result?.connected ?? result?.ok);
			state.pairing.deviceId = String(result?.device_id || '');
			state.pairing.endpointLabel = String(result?.endpoint_label || result?.endpoint_host || result?.host || '');
			state.pairing.message = String(result?.msg || '');
			state.pairing.phase = state.pairing.paired ? 'approved' : 'idle';
		} catch (error) {
			state.pairing.paired = false;
			state.pairing.connected = false;
			state.pairing.phase = 'idle';
			state.pairing.message = error?.message || '';
		}
		render();
	}

	async function refreshLibrary() {
		if (state.libraryPending) return;
		state.libraryPending = true; state.libraryError = ''; render();
		try {
			const result = await helperRequest('music_library_index', { limit: 100 }, 30000);
			if (!result?.ok) throw new Error(result?.msg || 'Could not sync your library.');
			state.library = result.tracks || result.items || [];
		} catch (error) {
			state.libraryError = error?.message || 'Could not sync your library.';
		} finally {
			state.libraryPending = false;
			render();
		}
	}

	async function play(track) {
		if (!track || state.playbackPendingId) return;
		state.playbackPendingId = trackKey(track); state.playbackError = ''; render();
		try {
			const stream = await helperRequest('music_stream_url', { source_id: track.source_id, track }, 30000);
			if (!stream?.ok || !stream.url) throw new Error(stream?.msg || 'Audio could not be prepared.');
			await native.playback('load', { url: stream.url, title: track.title || 'Rainette Music', artist: track.artist || '' });
			await native.playback('play');
			state.track = track; state.playing = true;
			await native.request({ type: 'music_now_playing_set', track, state: 'playing', playing: true, queue: [track], index: 0, output_device_id: 'phone' });
		} catch (error) {
			state.playbackError = error?.message || 'Audio could not be prepared.';
		} finally {
			state.playbackPendingId = '';
			render();
		}
	}

	async function acceptPhoneTransfer(message) {
		const tracks = Array.isArray(message.queue) ? message.queue : [];
		const track = tracks[Math.max(0, Number(message.index) || 0)];
		if (!track) {
			await native.request({ type: 'music_output_transfer_result', id: message.id, ok: false, target_device_id: 'phone', msg: 'Transfer queue is empty' });
			return;
		}
		state.playbackPendingId = trackKey(track); render();
		try {
			const stream = await helperRequest('music_stream_url', { source_id: track.source_id, track }, 30000);
			if (!stream?.ok || !stream.url) throw new Error(stream?.msg || 'Target audio could not be prepared');
			await native.playback('load', { url: stream.url, title: track.title || 'Rainette Music', artist: track.artist || '' });
			if (Number(message.current_time) > 0) await native.playback('seek', { positionMs: Number(message.current_time) * 1000 });
			// A transfer to the phone carries a single track, so 'all' and 'one' both
			// mean "keep repeating this" to the native player.
			await native.playback('repeat', { enabled: repeatFromMessage(message) !== 'off' });
			if (message.playing) await native.playback('play');
			state.track = track; state.playing = !!message.playing;
			await native.request({ type: 'music_output_transfer_result', id: message.id, ok: true, target_device_id: 'phone' });
		} catch (error) {
			await native.request({ type: 'music_output_transfer_result', id: message.id, ok: false, target_device_id: 'phone', msg: error?.message || 'Phone could not load the transfer' });
		} finally {
			state.playbackPendingId = ''; render();
		}
	}

	host.addEventListener('click', async event => {
		const tab = event.target.closest('[data-tab]')?.dataset.tab;
		if (tab) { state.tab = tab; render(); return; }
		const trackButton = event.target.closest('[data-track]');
		if (trackButton) {
			const source = trackButton.dataset.source;
			const collection = source === 'search' ? state.results : source === 'home' ? state.library.slice(0, 6) : state.library;
			await play(collection[Number(trackButton.dataset.track)]);
			return;
		}
		const actionNode = event.target.closest('[data-action]');
		const action = actionNode?.dataset.action;
		if (!action) return;
		if (action === 'more') { state.tab = 'more'; render(); }
		else if (action === 'home') { state.tab = 'home'; render(); }
		else if (action === 'refresh-library') await refreshLibrary();
		else if (action === 'reduce-motion') {
			const enabled = !document.documentElement.classList.contains('rw-reduced-motion');
			document.documentElement.classList.toggle('rw-reduced-motion', enabled);
			try { localStorage.setItem('rainette.reducedMotion', enabled ? '1' : '0'); } catch { /* best effort */ }
			render();
		}
		else if (action === 'output') { state.outputPicker = true; render(); }
		else if (action === 'output-close' || action === 'output-phone') { state.outputPicker = false; render(); }
		else if (action === 'output-desktop' && state.track) {
			// Deliberately sends no loop/repeat: the phone has no repeat control of
			// its own, and hardcoding loop:false here used to wipe whatever the
			// desktop was set to every time playback was handed back.
			const transfer = await native.request({ type: 'music_output_transfer', source_device_id: 'phone', target_device_id: 'desktop', queue: [state.track], index: 0, current_time: 0, playing: state.playing });
			if (transfer?.ok) { await native.playback('pause'); state.playing = false; }
			else state.playbackError = transfer?.msg || 'Desktop did not accept the transfer.';
			state.outputPicker = false; render();
		}
		else if (action === 'pairing') { state.pairingOpen = true; render(); await refreshConnectionStatus({ busy: true }); }
		else if (action === 'pairing-close') { state.pairingOpen = false; render(); }
		else if (action === 'pairing-refresh') await refreshConnectionStatus({ busy: true });
		else if (action === 'open-player' && state.track) { state.fullPlayer = true; render(); }
		else if (action === 'close-player') { state.fullPlayer = false; render(); }
		else if (action === 'toggle-player') { state.playing = !state.playing; await native.playback(state.playing ? 'play' : 'pause'); render(); }
		else if (action === 'next') await native.playback('next');
		else if (action === 'previous') await native.playback('previous');
	});

	host.addEventListener('submit', async event => {
		if (event.target.id === 'rwMobileSearch') {
			event.preventDefault();
			const query = host.querySelector('#rwMobileQuery')?.value?.trim();
			if (!query || state.searchPending) return;
			state.results = []; state.searchPending = true; state.searchError = ''; render();
			try {
				const result = await helperRequest('music_search', { query }, 30000);
				if (!result?.ok) throw new Error(result?.msg || 'Search failed.');
				state.results = result.items || [];
			} catch (error) {
				state.searchError = error?.message || 'Search failed.';
			} finally {
				state.searchPending = false; state.tab = 'search'; render();
			}
		} else if (event.target.id === 'rwMobilePairForm') {
			event.preventDefault();
			const uri = host.querySelector('#rwPairUri')?.value?.trim();
			if (!uri) return;
			state.pairing.phase = 'connecting'; state.pairing.message = ''; render();
			try { await native.pair(uri); }
			catch (error) { state.pairing.phase = 'failed'; state.pairing.message = error?.message || 'Pairing failed'; render(); }
		}
	});

	document.addEventListener('rainette:helper-message', event => {
		const message = event.detail || {};
		if (message.type === 'music_now_playing' && message.track) { state.track = message.track; state.playing = !!message.playing; render(); }
		else if (message.type === 'music_library_index_result' && message.ok && Array.isArray(message.tracks)) { state.library = message.tracks; render(); }
		else if (message.type === 'music_output_transfer' && message.target_device_id === 'phone') acceptPhoneTransfer(message).catch(() => {});
		else if (message.type === 'rainette_companion_pairing') {
			state.pairing.phase = message.phase || message.status || 'idle';
			state.pairing.message = message.msg || '';
			state.pairingOpen = true;
			if (state.pairing.phase === 'approved') refreshConnectionStatus().catch(() => {});
			else render();
		}
		else if (message.type === 'rainette_companion_sync') {
			state.syncState = message.status === 'reconnecting' ? 'reconnecting' : 'connected';
			state.pairing.connected = state.syncState === 'connected';
			render();
		}
		else if (message.type === 'rainette_companion_refresh') {
			Promise.allSettled([
				refreshLibrary(),
				helperRequest('music_playlist_list', {}, 30000),
				helperRequest('music_followed_artists', {}, 30000),
				helperRequest('music_recent', { limit: 100 }, 30000),
			]);
			native.request({ type: 'music_request_state' }).catch(() => {});
		}
	});

	render();
	refreshConnectionStatus().catch(() => {});
	refreshLibrary().catch(() => {});
}
