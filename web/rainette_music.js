/**
 * Rainette Music page - search, catalog, playlists, and library.
 *
 * Playback stays delegated to the persistent mini-player (window.RainetteMusic).
 * The helper provides ad-free yt-dlp streams and, when available, YouTube Music
 * metadata for artist/album catalog browsing.
 */

import { sendHelper, helperRequest, app, el, btn } from './music_shell.js';
import { RainetteRouter } from './music_shell.js';

const LAYOUT_KEY = 'rainette.musicLayout';
const QUEUE_SUPPORTED = typeof window !== 'undefined' && !!window.RW_REMOTE;

function _savedLayout() {
	try { return localStorage.getItem(LAYOUT_KEY) === 'grid' ? 'grid' : 'list'; }
	catch { return 'list'; }
}

const pageState = {
	tab: 'search',
	query: '',
	results: [],
	resultArtists: [],
	resultAlbums: [],
	playlists: [],
	openPlaylist: null,
	recent: [],
	library: { tracks: [], artists: [], albums: [] },
	queue: { tracks: [], index: -1, playing: false, loop: false, duration: 0 },
	queueDrawerOpen: false,
	queueSaveBusy: false,
	queueSaveStatus: '',
	view: null,
	layout: _savedLayout(),
	_autoplayOnLoad: false,
};

let _host = null;
let _mounted = false;
let _listenerBound = false;
let _searchDebounce = null;

function fmtDuration(s) {
	s = Number(s || 0);
	if (!s) return '';
	const m = Math.floor(s / 60), sec = Math.floor(s % 60);
	return m + ':' + String(sec).padStart(2, '0');
}

function fmtQueueDuration(s) {
	s = Number(s || 0);
	if (!Number.isFinite(s) || s <= 0) return '';
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	const sec = Math.floor(s % 60);
	return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}

function meta(track) {
	return track && typeof track.metadata === 'object' && track.metadata ? track.metadata : {};
}

function artistName(track) {
	const m = meta(track);
	if (track?.artist) return track.artist;
	if (Array.isArray(m.artists) && m.artists[0]?.name) return m.artists[0].name;
	return '';
}

function artistId(track) {
	const m = meta(track);
	if (m.artist_id) return m.artist_id;
	if (Array.isArray(m.artists) && m.artists[0]?.id) return m.artists[0].id;
	return '';
}

function albumInfo(track) {
	const m = meta(track);
	const album = typeof m.album === 'object' && m.album ? m.album : {};
	const title = m.album_name || album.name || '';
	const id = m.album_id || album.id || '';
	return title || id ? { id, title, artist: artistName(track), artist_id: artistId(track), thumbnail_url: track.thumbnail_url || '' } : null;
}

function trackList(id) {
	const list = el('div', 'rw-track-list' + (pageState.layout === 'grid' ? ' layout-grid' : ''));
	if (id) list.id = id;
	return list;
}

function section(title, sub = '') {
	const wrap = el('div', 'rw-section-title');
	wrap.append(el('h3', '', title));
	if (sub) wrap.append(el('span', '', sub));
	return wrap;
}

function thumbBox(url, fallback = '♪') {
	const thumb = el('div', 'rw-track-thumb');
	if (url) {
		const img = document.createElement('img');
		img.src = url;
		img.alt = '';
		img.loading = 'lazy';
		thumb.appendChild(img);
	} else {
		thumb.textContent = fallback;
	}
	return thumb;
}

function inlineLink(text, onClick) {
	const b = document.createElement('button');
	b.type = 'button';
	b.className = 'rw-inline-link';
	b.textContent = text;
	b.addEventListener('click', e => { e.stopPropagation(); onClick(); });
	return b;
}

function trackCard(track, actions) {
	const card = el('div', 'rw-bubble rw-track-card');
	const metaWrap = el('div', 'rw-track-meta');
	const title = el('div', 'rw-track-title', track.title || '(untitled)');
	const sub = el('div', 'rw-track-sub');
	const artist = artistName(track);
	const album = albumInfo(track);
	if (artist) {
		sub.appendChild(inlineLink(artist, () => openArtist({ id: artistId(track), name: artist, thumbnail_url: track.thumbnail_url || '' })));
	}
	if (album?.title) {
		if (sub.childNodes.length) sub.appendChild(document.createTextNode(' · '));
		sub.appendChild(inlineLink(album.title, () => openAlbum(album)));
	}
	const duration = fmtDuration(track.duration_s);
	if (duration) {
		if (sub.childNodes.length) sub.appendChild(document.createTextNode(' · '));
		sub.appendChild(document.createTextNode(duration));
	}
	metaWrap.append(title, sub);
	const actWrap = el('div', 'rw-track-actions');
	for (const a of actions) actWrap.appendChild(a);
	card.append(thumbBox(track.thumbnail_url), metaWrap, actWrap);
	return card;
}

function artistCard(artist) {
	const card = el('div', 'rw-bubble rw-track-card');
	const metaWrap = el('div', 'rw-track-meta');
	metaWrap.append(
		el('div', 'rw-track-title', artist.name || 'Unknown artist'),
		el('div', 'rw-track-sub', [artist.subscribers || '', artist.track_count ? `${artist.track_count} saved track${artist.track_count === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ')),
	);
	metaWrap.style.cursor = 'pointer';
	metaWrap.addEventListener('click', () => openArtist(artist));
	const open = btn('Open', 'rw-btn rw-btn-ghost', () => openArtist(artist));
	const actWrap = el('div', 'rw-track-actions');
	actWrap.append(open);
	card.append(thumbBox(artist.thumbnail_url, 'A'), metaWrap, actWrap);
	return card;
}

function albumCard(album) {
	const card = el('div', 'rw-bubble rw-track-card');
	const metaWrap = el('div', 'rw-track-meta');
	metaWrap.append(
		el('div', 'rw-track-title', album.title || album.name || 'Unknown album'),
		el('div', 'rw-track-sub', [album.artist || '', album.year || '', album.track_count ? `${album.track_count} track${album.track_count === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ')),
	);
	metaWrap.style.cursor = 'pointer';
	metaWrap.addEventListener('click', () => openAlbum(album));
	const open = btn('Open', 'rw-btn rw-btn-ghost', () => openAlbum(album));
	const actWrap = el('div', 'rw-track-actions');
	actWrap.append(open);
	card.append(thumbBox(album.thumbnail_url, 'ALB'), metaWrap, actWrap);
	return card;
}

function playAction(track, queue) {
	const b = btn('>', 'rw-icon-btn', () => {
		if (queue) window.RainetteMusic?.playQueue(queue, queue.indexOf(track));
		else window.RainetteMusic?.playTrack(track);
	});
	b.title = 'Play';
	return b;
}

function addAction(track) {
	const add = btn('+', 'rw-icon-btn', () => openAddToPlaylist(track));
	add.title = 'Add to playlist';
	return add;
}

function playNextAction(track) {
	const b = btn('+1', 'rw-icon-btn', () => window.RainetteMusic?.queueAddNext?.(track));
	b.title = 'Play next';
	return b;
}

function addToQueueAction(track) {
	const b = btn('Q+', 'rw-icon-btn', () => window.RainetteMusic?.queueAddEnd?.(track));
	b.title = 'Add to end of queue';
	return b;
}

function queueActions(track) {
	return QUEUE_SUPPORTED ? [playNextAction(track), addToQueueAction(track)] : [];
}

function trackActions(track, queue, extras = [], includePlaylist = true) {
	const actions = [playAction(track, queue), ...queueActions(track), ...extras];
	if (includePlaylist) actions.push(addAction(track));
	return actions;
}

function renderCurrent() {
	if (!_host) return;
	if (pageState.view?.kind === 'artist') return renderArtistDetail();
	if (pageState.view?.kind === 'album') return renderAlbumDetail();
	applyTab();
}

function runSearch() {
	const q = pageState.query.trim();
	const status = _host?.querySelector('#rwMusicSearchStatus');
	if (!q) {
		pageState.results = [];
		pageState.resultArtists = [];
		pageState.resultAlbums = [];
		renderResults();
		if (status) status.textContent = '';
		return;
	}
	if (status) status.textContent = 'Searching...';
	sendHelper({ type: 'music_catalog_search', id: 'mcat_' + Math.random().toString(36).slice(2), query: q });
}

function renderSearch() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	const bar = el('div', 'rw-toolbar');
	const input = document.createElement('input');
	input.className = 'rw-input';
	input.type = 'search';
	input.placeholder = 'Search songs, artists, or albums';
	input.value = pageState.query;
	input.style.flex = '1';
	input.addEventListener('input', e => {
		pageState.query = e.target.value;
		clearTimeout(_searchDebounce);
		_searchDebounce = setTimeout(runSearch, 350);
	});
	input.addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(_searchDebounce); runSearch(); } });
	bar.appendChild(input);
	body.appendChild(bar);
	const status = el('div', 'rw-status-line');
	status.id = 'rwMusicSearchStatus';
	body.appendChild(status);
	const list = trackList('rwMusicResults');
	body.appendChild(list);
	renderResults();
}

function renderResults() {
	const list = _host?.querySelector('#rwMusicResults');
	if (!list) return;
	list.innerHTML = '';
	if (!pageState.results.length && !pageState.resultArtists.length && !pageState.resultAlbums.length) return;
	if (pageState.resultArtists.length) {
		list.appendChild(section('Artists'));
		for (const artist of pageState.resultArtists) list.appendChild(artistCard(artist));
	}
	if (pageState.resultAlbums.length) {
		list.appendChild(section('Albums'));
		for (const album of pageState.resultAlbums) list.appendChild(albumCard(album));
	}
	if (pageState.results.length) {
		list.appendChild(section('Songs'));
		for (const track of pageState.results) list.appendChild(trackCard(track, trackActions(track, pageState.results)));
	}
}

function renderArtists() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	const list = trackList('rwMusicArtists');
	if (!pageState.library.artists.length) list.appendChild(_empty('No saved artists yet', 'Play or save tracks to build the local artist index.'));
	for (const artist of pageState.library.artists) list.appendChild(artistCard(artist));
	body.appendChild(list);
}

function renderAlbums() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	const list = trackList('rwMusicAlbums');
	if (!pageState.library.albums.length) list.appendChild(_empty('No saved albums yet', 'Search an album or play tracks with album metadata.'));
	for (const album of pageState.library.albums) list.appendChild(albumCard(album));
	body.appendChild(list);
}

function renderArtistDetail() {
	const body = _host?.querySelector('#rwMusicBody');
	const view = pageState.view;
	if (!body || !view) return;
	body.innerHTML = '';
	const artist = view.artist || {};
	const head = el('div', 'rw-toolbar rw-detail-head');
	head.append(
		btn('Back', 'rw-btn rw-btn-ghost', () => { pageState.view = null; applyTab(); }),
		el('div', 'rw-track-title', artist.name || 'Artist'),
	);
	body.appendChild(head);
	if (view.loading) {
		body.appendChild(el('div', 'rw-status-line', 'Loading artist catalog...'));
		return;
	}
	if (view.msg) body.appendChild(el('div', 'rw-status-line', view.msg));
	const list = trackList('rwArtistCatalog');
	if (view.albums?.length) {
		list.appendChild(section('Albums'));
		for (const album of view.albums) list.appendChild(albumCard(album));
	}
	if (view.singles?.length) {
		list.appendChild(section('Singles'));
		for (const album of view.singles) list.appendChild(albumCard(album));
	}
	if (view.songs?.length) {
		list.appendChild(section('Songs'));
		for (const track of view.songs) list.appendChild(trackCard(track, trackActions(track, view.songs)));
	}
	if (view.videos?.length) {
		list.appendChild(section('Videos'));
		for (const track of view.videos) list.appendChild(trackCard(track, trackActions(track, view.videos)));
	}
	if (!list.childNodes.length) list.appendChild(_empty('No catalog found', 'Try searching the artist name instead.'));
	body.appendChild(list);
}

function renderAlbumDetail() {
	const body = _host?.querySelector('#rwMusicBody');
	const view = pageState.view;
	if (!body || !view) return;
	body.innerHTML = '';
	const album = view.album || {};
	const head = el('div', 'rw-toolbar rw-detail-head');
	head.append(
		btn('Back', 'rw-btn rw-btn-ghost', () => { pageState.view = null; applyTab(); }),
		el('div', 'rw-track-title', album.title || 'Album'),
	);
	body.appendChild(head);
	if (view.loading) {
		body.appendChild(el('div', 'rw-status-line', 'Loading album...'));
		return;
	}
	const tracks = view.tracks || [];
	const list = trackList('rwAlbumTracks');
	if (!tracks.length) list.appendChild(_empty('No tracks found', 'Try searching this album title.'));
	for (const track of tracks) list.appendChild(trackCard(track, trackActions(track, tracks)));
	body.appendChild(list);
}

function openArtist(artist) {
	const name = artist?.name || artist?.artist || '';
	pageState.view = { kind: 'artist', artist: { ...artist, name }, loading: true, songs: [], videos: [], albums: [], singles: [] };
	sendHelper({ type: 'music_artist_catalog', id: 'mart_' + Math.random().toString(36).slice(2), artist_id: artist?.id || artist?.artist_id || '', name });
	renderArtistDetail();
}

function openAlbum(album) {
	const tracks = Array.isArray(album?.tracks) ? album.tracks : [];
	const id = album?.id || album?.browse_id || '';
	pageState.view = { kind: 'album', album, loading: !!id && !tracks.length, tracks };
	if (id) {
		sendHelper({ type: 'music_album_tracks', id: 'malb_' + Math.random().toString(36).slice(2), album_id: id, title: album?.title || album?.name || '', artist: album?.artist || '' });
	}
	renderAlbumDetail();
}

function renderPlaylists() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	if (pageState.openPlaylist) { renderPlaylistDetail(body); return; }

	const bar = el('div', 'rw-toolbar');
	const nameInput = document.createElement('input');
	nameInput.className = 'rw-input';
	nameInput.placeholder = 'New playlist name';
	nameInput.style.maxWidth = '260px';
	const create = btn('Create', 'rw-btn rw-btn-primary', () => {
		const name = nameInput.value.trim();
		if (!name) return;
		sendHelper({ type: 'music_playlist_create', id: 'plc_' + Math.random().toString(36).slice(2), name });
		nameInput.value = '';
	});
	nameInput.addEventListener('keydown', e => { if (e.key === 'Enter') create.click(); });
	bar.append(nameInput, create);
	body.appendChild(bar);

	const list = trackList();
	if (!pageState.playlists.length) list.appendChild(_empty('No playlists yet', 'Create one above, then add tracks from Search.'));
	for (const pl of pageState.playlists) {
		const card = el('div', 'rw-bubble rw-track-card');
		const metaWrap = el('div', 'rw-track-meta');
		metaWrap.append(
			el('div', 'rw-track-title', pl.name),
			el('div', 'rw-track-sub', (pl.track_count || 0) + ' track' + (pl.track_count === 1 ? '' : 's')),
		);
		metaWrap.style.cursor = 'pointer';
		metaWrap.addEventListener('click', () => openPlaylist(pl));
		const del = btn('x', 'rw-icon-btn danger', () => {
			if (confirm('Delete playlist "' + pl.name + '"?')) sendHelper({ type: 'music_playlist_delete', id: 'pld_' + Math.random().toString(36).slice(2), playlist_id: pl.id });
		});
		del.title = 'Delete playlist';
		const open = btn('>', 'rw-icon-btn', () => openPlaylist(pl, true));
		open.title = 'Open and play';
		const actWrap = el('div', 'rw-track-actions');
		actWrap.append(open, del);
		card.append(thumbBox('', 'PL'), metaWrap, actWrap);
		list.appendChild(card);
	}
	body.appendChild(list);
}

function renderPlaylistDetail(body) {
	const pl = pageState.openPlaylist;
	const head = el('div', 'rw-toolbar');
	head.append(btn('Back', 'rw-btn rw-btn-ghost', () => { pageState.openPlaylist = null; renderPlaylists(); }), el('div', 'rw-track-title', pl.name));
	body.appendChild(head);
	const tracks = pl._tracks || [];
	const list = trackList();
	if (!tracks.length) list.appendChild(_empty('Empty playlist', 'Add tracks from the Search tab.'));
	for (const track of tracks) {
		const rm = btn('x', 'rw-icon-btn danger', () => {
			sendHelper({ type: 'music_playlist_remove_track', id: 'plrt_' + Math.random().toString(36).slice(2), playlist_id: pl.id, track_id: track.id });
		});
		rm.title = 'Remove from playlist';
		list.appendChild(trackCard(track, trackActions(track, tracks, [rm], false)));
	}
	body.appendChild(list);
}

function openPlaylist(pl, autoplay = false) {
	pageState.openPlaylist = { ...pl, _tracks: [] };
	pageState._autoplayOnLoad = autoplay;
	sendHelper({ type: 'music_playlist_tracks', id: 'plt_' + Math.random().toString(36).slice(2), playlist_id: pl.id });
	renderPlaylists();
}

function openAddToPlaylist(track) {
	if (!pageState.playlists.length) {
		alert('Create a playlist first (Playlists tab).');
		return;
	}
	const names = pageState.playlists.map((p, i) => `${i + 1}. ${p.name}`).join('\n');
	const pick = prompt('Add "' + (track.title || 'track') + '" to which playlist?\n\n' + names + '\n\nEnter number:');
	const idx = Number(pick) - 1;
	const pl = pageState.playlists[idx];
	if (!pl) return;
	sendHelper({
		type: 'music_playlist_add_track',
		id: 'plat_' + Math.random().toString(36).slice(2),
		playlist_id: pl.id,
		source: track.source || 'youtube',
		source_id: track.source_id,
		title: track.title,
		artist: artistName(track),
		duration_s: track.duration_s,
		thumbnail_url: track.thumbnail_url,
		metadata: meta(track),
	});
}

function queueSummary() {
	const q = pageState.queue || {};
	const tracks = q.tracks || [];
	const count = Number(q.count || tracks.length || 0);
	const duration = fmtQueueDuration(q.duration || tracks.reduce((sum, track) => sum + (Number(track?.duration_s || 0) || 0), 0));
	return count + ' track' + (count === 1 ? '' : 's') + (duration ? ' - ' + duration : '');
}

function defaultQueueName() {
	const d = new Date();
	const pad = n => String(n).padStart(2, '0');
	return `Queue ${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function queueTrackPayload(track) {
	return {
		track_id: track.id || '',
		source: track.source || 'youtube',
		source_id: track.source_id,
		title: track.title,
		artist: artistName(track),
		duration_s: track.duration_s,
		thumbnail_url: track.thumbnail_url,
		metadata: meta(track),
	};
}

async function saveQueueAsPlaylist() {
	const tracks = (pageState.queue.tracks || []).filter(t => t && t.source_id);
	if (!tracks.length || pageState.queueSaveBusy) return;
	const name = (prompt('Save queue as playlist:', defaultQueueName()) || '').trim();
	if (!name) return;
	pageState.queueSaveBusy = true;
	pageState.queueSaveStatus = 'Saving queue...';
	renderQueueSurfaces();
	try {
		const created = await helperRequest('music_playlist_create', { id: 'qpl_' + Math.random().toString(36).slice(2), name }, 7000);
		const playlist = created?.playlist;
		if (!created?.ok || !playlist?.id) throw new Error(created?.msg || 'Could not create playlist');
		for (const track of tracks) {
			const added = await helperRequest('music_playlist_add_track', {
				id: 'qpla_' + Math.random().toString(36).slice(2),
				playlist_id: playlist.id,
				...queueTrackPayload(track),
			}, 7000);
			if (!added?.ok) throw new Error(added?.msg || 'Could not add a track');
		}
		pageState.queueSaveStatus = 'Saved "' + playlist.name + '"';
		sendHelper({ type: 'music_playlist_list', id: 'pll_' + Math.random().toString(36).slice(2) });
	} catch (err) {
		pageState.queueSaveStatus = 'Save failed: ' + (err?.message || err);
	} finally {
		pageState.queueSaveBusy = false;
		renderQueueSurfaces();
	}
}

function queueToolbar(compact = false) {
	const bar = el('div', 'rw-toolbar rw-queue-toolbar');
	bar.appendChild(el('div', 'rw-status-line rw-queue-summary', queueSummary()));
	if (!compact) {
		bar.append(
			btn('Shuffle', 'rw-btn rw-btn-ghost', () => window.RainetteMusic?.queueShuffle?.()),
			btn('Remove duplicates', 'rw-btn rw-btn-ghost', () => window.RainetteMusic?.queueDedupe?.()),
			btn('Clear Up Next', 'rw-btn rw-btn-ghost', () => window.RainetteMusic?.queueClearUpNext?.()),
		);
	}
	const save = btn(pageState.queueSaveBusy ? 'Saving...' : 'Save Queue as Playlist', 'rw-btn rw-btn-primary', saveQueueAsPlaylist);
	save.disabled = pageState.queueSaveBusy || !(pageState.queue.tracks || []).length;
	bar.appendChild(save);
	return bar;
}

function queueRow(track, index, compact = false) {
	const q = pageState.queue || {};
	const current = index === q.index;
	const row = el('div', 'rw-bubble rw-track-card rw-queue-row' + (current ? ' is-current' : '') + (compact ? ' compact' : ''));
	row.draggable = true;
	row.dataset.queueIndex = String(index);
	const grip = el('span', 'rw-queue-grip', '::');
	grip.title = 'Drag to reorder';
	const metaWrap = el('div', 'rw-track-meta');
	const title = el('div', 'rw-track-title', track.title || '(untitled)');
	const parts = [];
	if (current) parts.push('Now playing');
	if (artistName(track)) parts.push(artistName(track));
	if (fmtDuration(track.duration_s)) parts.push(fmtDuration(track.duration_s));
	metaWrap.append(title, el('div', 'rw-track-sub', parts.join(' - ')));
	const play = btn(current ? '||' : '>', 'rw-icon-btn', () => window.RainetteMusic?.queuePlayIndex?.(index));
	play.title = current ? 'Play or pause' : 'Play this track';
	const remove = btn('x', 'rw-icon-btn danger', () => window.RainetteMusic?.queueRemove?.(index));
	remove.title = 'Remove from queue';
	const actions = el('div', 'rw-track-actions');
	actions.append(play, remove);
	row.append(grip, thumbBox(track.thumbnail_url), metaWrap, actions);
	return row;
}

function wireQueueDrag(list) {
	let dragIndex = -1;
	list.addEventListener('dragstart', e => {
		const row = e.target.closest('.rw-queue-row');
		if (!row) return;
		dragIndex = Number(row.dataset.queueIndex);
		row.classList.add('dragging');
		e.dataTransfer.effectAllowed = 'move';
		e.dataTransfer.setData('text/plain', String(dragIndex));
	});
	list.addEventListener('dragover', e => {
		const row = e.target.closest('.rw-queue-row');
		if (!row) return;
		e.preventDefault();
		row.classList.add('drag-over');
	});
	list.addEventListener('dragleave', e => {
		e.target.closest('.rw-queue-row')?.classList.remove('drag-over');
	});
	list.addEventListener('drop', e => {
		const row = e.target.closest('.rw-queue-row');
		if (!row) return;
		e.preventDefault();
		const from = Number(e.dataTransfer.getData('text/plain') || dragIndex);
		const to = Number(row.dataset.queueIndex);
		list.querySelectorAll('.drag-over,.dragging').forEach(n => n.classList.remove('drag-over', 'dragging'));
		if (Number.isInteger(from) && Number.isInteger(to) && from !== to) window.RainetteMusic?.queueMove?.(from, to);
	});
	list.addEventListener('dragend', () => {
		dragIndex = -1;
		list.querySelectorAll('.drag-over,.dragging').forEach(n => n.classList.remove('drag-over', 'dragging'));
	});
}

function queueList(compact = false) {
	const list = el('div', 'rw-track-list rw-queue-list' + (compact ? ' compact' : ''));
	const tracks = pageState.queue.tracks || [];
	if (!tracks.length) list.appendChild(_empty('Queue is empty', 'Play a song to start building Up Next.'));
	for (let i = 0; i < tracks.length; i++) list.appendChild(queueRow(tracks[i], i, compact));
	wireQueueDrag(list);
	return list;
}

function renderQueue() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	body.appendChild(queueToolbar(false));
	if (pageState.queueSaveStatus) body.appendChild(el('div', 'rw-status-line', pageState.queueSaveStatus));
	body.appendChild(queueList(false));
}

function renderQueueDrawer() {
	const drawer = _host?.querySelector('#rwMusicQueueDrawer');
	if (!drawer) return;
	drawer.hidden = !QUEUE_SUPPORTED || !pageState.queueDrawerOpen;
	if (drawer.hidden) return;
	drawer.innerHTML = '';
	const head = el('div', 'rw-queue-drawer-head');
	head.append(el('div', 'rw-track-title', 'Queue'), btn('x', 'rw-icon-btn', () => toggleQueueDrawer(false)));
	drawer.append(head, queueToolbar(true));
	if (pageState.queueSaveStatus) drawer.appendChild(el('div', 'rw-status-line', pageState.queueSaveStatus));
	drawer.appendChild(queueList(true));
}

function renderQueueSurfaces() {
	if (pageState.tab === 'queue' && !pageState.view) renderQueue();
	renderQueueDrawer();
}

function toggleQueueDrawer(open = !pageState.queueDrawerOpen) {
	pageState.queueDrawerOpen = !!open;
	if (QUEUE_SUPPORTED && pageState.queueDrawerOpen) window.RainetteMusic?.requestQueueState?.();
	renderQueueDrawer();
}

function renderRecent() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	body.appendChild(el('div', 'rw-status-line', 'Recently played'));
	const list = trackList('rwMusicRecent');
	if (!pageState.recent.length) list.appendChild(_empty('Nothing played yet', 'Search and play a track to build your history.'));
	for (const track of pageState.recent) list.appendChild(trackCard(track, trackActions(track, pageState.recent, [], false)));
	body.appendChild(list);
}

function _empty(title, sub) {
	const wrap = el('div', 'rw-empty');
	wrap.append(el('div', 'rw-empty-icon', '♪'), el('h3', '', title), el('p', '', sub));
	return wrap;
}

function refreshLibraryIndex() {
	sendHelper({ type: 'music_library_index', id: 'mlib_' + Math.random().toString(36).slice(2), limit: 500 });
}

function applyTab() {
	_host?.querySelectorAll('#rwMusicTabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === pageState.tab));
	if (pageState.view) return renderCurrent();
	if (pageState.tab === 'search') renderSearch();
	else if (pageState.tab === 'artists') { renderArtists(); refreshLibraryIndex(); }
	else if (pageState.tab === 'albums') { renderAlbums(); refreshLibraryIndex(); }
	else if (pageState.tab === 'playlists') { renderPlaylists(); sendHelper({ type: 'music_playlist_list', id: 'pll_' + Math.random().toString(36).slice(2) }); }
	else if (pageState.tab === 'recent') { renderRecent(); sendHelper({ type: 'music_recent', id: 'mrec_' + Math.random().toString(36).slice(2) }); }
	else if (pageState.tab === 'queue') { window.RainetteMusic?.requestQueueState?.(); renderQueue(); }
}

function onHelperMessage(msg) {
	if (!_mounted || !msg) return;
	switch (msg.type) {
		case 'music_catalog_search_result': {
			if (msg.ok) {
				pageState.results = msg.songs || [];
				pageState.resultArtists = msg.artists || [];
				pageState.resultAlbums = msg.albums || [];
			}
			const status = _host?.querySelector('#rwMusicSearchStatus');
			if (status) {
				const total = pageState.results.length + pageState.resultArtists.length + pageState.resultAlbums.length;
				status.textContent = msg.ok ? (total + ' results' + (msg.msg ? ' - ' + msg.msg : '')) : ('Search failed: ' + (msg.msg || ''));
			}
			if (pageState.tab === 'search' && !pageState.view) renderResults();
			break;
		}
		case 'music_playlist_list_result':
		case 'music_playlist_created':
		case 'music_playlist_renamed':
		case 'music_playlist_deleted':
			if (Array.isArray(msg.playlists)) pageState.playlists = msg.playlists;
			if (pageState.tab === 'playlists' && !pageState.openPlaylist && !pageState.view) renderPlaylists();
			break;
		case 'music_playlist_tracks_result':
		case 'music_playlist_track_added':
		case 'music_playlist_track_removed':
			if (pageState.openPlaylist && msg.playlist_id === pageState.openPlaylist.id) {
				pageState.openPlaylist._tracks = msg.tracks || [];
				if (pageState._autoplayOnLoad && (msg.tracks || []).length) {
					pageState._autoplayOnLoad = false;
					window.RainetteMusic?.playQueue(msg.tracks, 0);
				}
				if (pageState.tab === 'playlists' && !pageState.view) renderPlaylists();
			}
			break;
		case 'music_recent_result':
			if (msg.ok) pageState.recent = msg.tracks || [];
			if (pageState.tab === 'recent' && !pageState.view) renderRecent();
			break;
		case 'music_now_playing':
			if (Array.isArray(msg.queue)) {
				pageState.queue = {
					tracks: msg.queue,
					index: Number.isFinite(Number(msg.index)) ? Number(msg.index) : -1,
					playing: !!msg.playing,
					loop: !!msg.loop,
					duration: Number(msg.queue_duration || 0),
					count: Number(msg.queue_count || msg.queue.length || 0),
				};
				renderQueueSurfaces();
			}
			break;
		case 'music_library_index_result':
			if (msg.ok) pageState.library = { tracks: msg.tracks || [], artists: msg.artists || [], albums: msg.albums || [] };
			if (!pageState.view && pageState.tab === 'artists') renderArtists();
			if (!pageState.view && pageState.tab === 'albums') renderAlbums();
			break;
		case 'music_artist_catalog_result':
			if (pageState.view?.kind === 'artist') {
				if (msg.ok) pageState.view = { kind: 'artist', artist: msg.artist || pageState.view.artist, loading: false, songs: msg.songs || [], videos: msg.videos || [], albums: msg.albums || [], singles: msg.singles || [], msg: msg.msg || '' };
				else pageState.view = { ...pageState.view, loading: false, msg: 'Artist catalog failed: ' + (msg.msg || '') };
				renderArtistDetail();
			}
			break;
		case 'music_album_tracks_result':
			if (pageState.view?.kind === 'album') {
				if (msg.ok) pageState.view = { kind: 'album', album: msg.album || pageState.view.album, loading: false, tracks: msg.tracks || [] };
				else pageState.view = { ...pageState.view, loading: false, tracks: [], msg: msg.msg || '' };
				renderAlbumDetail();
			}
			break;
		case 'music_status':
			if (msg.ok) {
				const banner = _host?.querySelector('#rwMusicBanner');
				if (banner && !msg.ytdlp_available) {
					banner.style.display = '';
					banner.textContent = 'Music streaming needs yt-dlp - run: pip install yt-dlp';
				} else if (banner && msg.ytdlp_available && !msg.ytmusic_available) {
					banner.style.display = '';
					banner.textContent = 'Artist and album catalog browsing needs ytmusicapi - run: pip install ytmusicapi';
				}
			}
			break;
	}
}

function updateLayoutButtons() {
	_host?.querySelectorAll('#rwMusicLayoutSeg button').forEach(b => b.classList.toggle('on', b.dataset.layout === pageState.layout));
}

function buildPage(host) {
	const queueButton = QUEUE_SUPPORTED ? '<button id="rwMusicQueueOpen" class="rw-btn rw-btn-ghost" type="button">Queue</button>' : '';
	const queueTab = QUEUE_SUPPORTED ? '<button data-tab="queue" type="button">Queue</button>' : '';
	host.innerHTML = `
		<div class="rw-page-head">
			<div>
				<h1 class="rw-page-title">Music</h1>
				<p class="rw-page-sub">Search, browse artists and albums, build playlists, and play ad-free streams.</p>
			</div>
			<div class="rw-head-spacer"></div>
			${queueButton}
			<div class="rw-segment" id="rwMusicLayoutSeg">
				<button data-layout="list" type="button" title="List view" aria-label="List view">List</button>
				<button data-layout="grid" type="button" title="Grid view" aria-label="Grid view">Grid</button>
			</div>
			<div class="rw-segment" id="rwMusicTabs">
				<button data-tab="search" class="on" type="button">Search</button>
				<button data-tab="artists" type="button">Artists</button>
				<button data-tab="albums" type="button">Albums</button>
				<button data-tab="playlists" type="button">Playlists</button>
				<button data-tab="recent" type="button">Recent</button>
				${queueTab}
			</div>
		</div>
		<div class="rw-page-body">
			<div id="rwMusicBanner" class="rw-status-line" style="display:none;color:var(--rw-warn,#c93)"></div>
			<div id="rwMusicBody"></div>
			<div id="rwMusicQueueDrawer" class="rw-queue-drawer" hidden></div>
		</div>`;

	host.querySelector('#rwMusicTabs').addEventListener('click', e => {
		const b = e.target.closest('button[data-tab]');
		if (b) { pageState.tab = b.dataset.tab; pageState.openPlaylist = null; pageState.view = null; applyTab(); }
	});
	host.querySelector('#rwMusicLayoutSeg').addEventListener('click', e => {
		const b = e.target.closest('button[data-layout]');
		if (!b || b.dataset.layout === pageState.layout) return;
		pageState.layout = b.dataset.layout;
		try { localStorage.setItem(LAYOUT_KEY, pageState.layout); } catch { /* best effort */ }
		updateLayoutButtons();
		applyTab();
	});
	host.querySelector('#rwMusicQueueOpen')?.addEventListener('click', () => toggleQueueDrawer());
	updateLayoutButtons();
}

export const MusicPage = {
	mount(host) {
		_host = host;
		if (!host.dataset.built) { buildPage(host); host.dataset.built = '1'; }
		_mounted = true;
		if (!_listenerBound) {
			_listenerBound = true;
			document.addEventListener('rainette:helper-message', e => onHelperMessage(e.detail));
		}
		sendHelper({ type: 'music_status', id: 'mstat_' + Math.random().toString(36).slice(2) });
		sendHelper({ type: 'music_playlist_list', id: 'pll_' + Math.random().toString(36).slice(2) });
		refreshLibraryIndex();
		if (QUEUE_SUPPORTED) {
			pageState.queue = app.musicQueue || pageState.queue;
			window.RainetteMusic?.requestQueueState?.();
		}
		applyTab();
	},
	unmount() { _mounted = false; },
};

RainetteRouter.register('music', MusicPage);
