import { helperRequest } from './music_shell.js';

const native = window.RainetteNativeTransport;
if (native?.isNative) {
	const host = document.createElement('main');
	host.id = 'rwMobileApp';
	host.className = 'rw-mobile-app';
	document.body.append(host);

	const state = { tab: 'home', track: null, playing: false, library: [], results: [], loading: false, outputPicker: false };
	const esc = value => String(value || '').replace(/[&<>\"]/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' })[char]);
	const art = track => track?.thumbnail_url || './assets/rainette-icon-256.png';

	function playerBar() {
		if (!state.track) return '';
		return `<button class="rw-mobile-now" data-action="open-player"><img src="${esc(art(state.track))}" alt=""><span><b>${esc(state.track.title)}</b><small>${esc(state.track.artist)}</small></span><i>${state.playing ? '&#10074;&#10074;' : '&#9654;'}</i></button>`;
	}

	function rows(items, empty) {
		if (state.loading) return '<div class="rw-flower-loader" role="status" aria-label="Loading"><img src="./assets/rainette-icon-256.png" alt=""></div>';
		if (!items.length) return `<p class="rw-mobile-empty-state">${empty}</p>`;
		return `<div class="rw-mobile-song-list">${items.map((track, index) => `<button class="rw-mobile-song" data-track="${index}"><img src="${esc(art(track))}" alt=""><span><b>${esc(track.title)}</b><small>${esc(track.artist)}</small></span><i>&#8942;</i></button>`).join('')}</div>`;
	}

	function outputChooser() {
		if (!state.outputPicker) return '';
		return `<div class="rw-mobile-output-sheet" role="dialog" aria-label="Choose playback device"><button data-action="output-phone"><b>This phone</b><small>Play directly with Rainette mobile</small></button><button data-action="output-desktop"><b>Rainette desktop</b><small>Transfer the current queue to your paired computer</small></button><button data-action="output-close">Cancel</button></div>`;
	}

	function page() {
		const tab = state.tab;
		let content = '';
		if (tab === 'home') content = `<header class="rw-mobile-top"><div class="rw-mobile-brand"><img src="./assets/rainette-icon-256.png" alt="">Rainette</div><button data-action="more" aria-label="Settings">&#9881;</button></header><section class="rw-mobile-hero"><p>Listen without the desktop layout.</p><h1>Your music, in one calm place.</h1></section><section><h2>Recently played</h2>${rows(state.library.slice(0, 6), 'Play something from Search to start your history.')}</section>`;
		else if (tab === 'search') content = `<header class="rw-mobile-top"><h1>Search</h1><button data-action="more" aria-label="Settings">&#9881;</button></header><form class="rw-mobile-search" id="rwMobileSearch"><input id="rwMobileQuery" autocomplete="off" placeholder="Songs, artists, albums"><button>Search</button></form><section><h2>Results</h2>${rows(state.results, 'Find any song from your paired Rainette desktop.')}</section>`;
		else if (tab === 'library') content = `<header class="rw-mobile-top"><h1>Library</h1><button data-action="more" aria-label="Settings">&#9881;</button></header><section><h2>Saved music</h2>${rows(state.library, 'Your saved tracks will appear here and stay synced with desktop.')}</section>`;
		else content = `<header class="rw-mobile-top"><h1>More</h1><button data-action="home" aria-label="Close settings">&times;</button></header><section class="rw-mobile-more"><button data-action="refresh-library"><b>Refresh library</b><small>Pull the latest paired desktop changes</small></button><button data-action="pairing"><b>Paired devices</b><small>Pair from Rainette desktop, then approve it there</small></button><button data-action="output"><b>Play on</b><small>This phone or Rainette desktop</small></button><button data-action="reduce-motion"><b>Reduce motion</b><small>Use still transitions and loaders</small></button></section>`;
		return `<div class="rw-mobile-screen">${content}</div>${playerBar()}${outputChooser()}<nav class="rw-mobile-tabs">${[['home','Home','&#8962;'],['search','Search','&#8981;'],['library','Library','&#9638;'],['more','More','&#8942;']].map(([id,label,icon]) => `<button class="${tab === id ? 'active' : ''}" data-tab="${id}"><i>${icon}</i>${label}</button>`).join('')}</nav>`;
	}

	function render() { host.innerHTML = page(); }
	async function refreshLibrary() {
		state.loading = true; render();
		const result = await helperRequest('music_library_index', { limit: 100 }, 30000);
		state.library = result.tracks || result.items || [];
		state.loading = false;
		render();
	}
	async function play(track) {
		const stream = await helperRequest('music_stream_url', { source_id: track.source_id, track }, 30000);
		if (!stream?.ok || !stream.url) return;
		await native.playback('load', { url: stream.url, title: track.title || 'Rainette Music', artist: track.artist || '' });
		await native.playback('play');
		state.track = track; state.playing = true;
		await native.request({ type: 'music_now_playing_set', track, state: 'playing', playing: true, queue: [track], index: 0 });
		render();
	}

	host.addEventListener('click', async event => {
		const tab = event.target.closest('[data-tab]')?.dataset.tab;
		if (tab) { state.tab = tab; render(); return; }
		const index = event.target.closest('[data-track]')?.dataset.track;
		if (index != null) { await play((state.tab === 'search' ? state.results : state.library)[Number(index)]); return; }
		const action = event.target.closest('[data-action]')?.dataset.action;
		if (action === 'more') { state.tab = 'more'; render(); }
		else if (action === 'home') { state.tab = 'home'; render(); }
		else if (action === 'refresh-library') await refreshLibrary();
		else if (action === 'reduce-motion') document.documentElement.classList.toggle('rw-reduced-motion');
		else if (action === 'output') { state.outputPicker = true; render(); }
		else if (action === 'output-close' || action === 'output-phone') { state.outputPicker = false; render(); }
		else if (action === 'output-desktop' && state.track) {
			const transfer = await native.request({ type: 'music_output_transfer', source_device_id: 'phone', target_device_id: 'desktop', queue: [state.track], index: 0, current_time: 0, playing: state.playing, loop: false });
			if (transfer?.ok) { await native.playback('pause'); state.playing = false; }
			state.outputPicker = false; render();
		}
		else if (action === 'pairing') alert('Use the pairing QR in Rainette desktop, then approve this phone on desktop.');
		else if (action === 'open-player' && state.track) {
			host.classList.add('rw-mobile-full-player');
			host.innerHTML = `<button class="rw-mobile-close-player" data-action="close-player">&#8964;</button><div class="rw-mobile-full-art"><img src="${esc(art(state.track))}" alt=""></div><h1>${esc(state.track.title)}</h1><p>${esc(state.track.artist)}</p><div class="rw-mobile-full-controls"><button data-action="previous">&#9198;</button><button class="rw-mobile-main-play" data-action="toggle-player">${state.playing ? '&#10074;&#10074;' : '&#9654;'}</button><button data-action="next">&#9197;</button></div><button class="rw-mobile-output" data-action="output">Play on&hellip;</button>`;
		}
		else if (action === 'close-player') { host.classList.remove('rw-mobile-full-player'); render(); }
		else if (action === 'toggle-player') { state.playing = !state.playing; await native.playback(state.playing ? 'play' : 'pause'); render(); }
		else if (action === 'next') await native.playback('next');
		else if (action === 'previous') await native.playback('previous');
	});

	host.addEventListener('submit', async event => {
		if (event.target.id !== 'rwMobileSearch') return;
		event.preventDefault();
		const query = host.querySelector('#rwMobileQuery')?.value?.trim();
		if (!query) return;
		state.results = []; state.loading = true; render();
		const result = await helperRequest('music_search', { query }, 30000);
		state.results = result.items || []; state.loading = false; state.tab = 'search'; render();
	});

	document.addEventListener('rainette:helper-message', event => {
		const msg = event.detail || {};
		if (msg.type === 'music_now_playing' && msg.track) { state.track = msg.track; state.playing = !!msg.playing; render(); }
		if (msg.type === 'rainette_companion_refresh') refreshLibrary().catch(() => {});
	});
	render();
	refreshLibrary().catch(() => {});
}
