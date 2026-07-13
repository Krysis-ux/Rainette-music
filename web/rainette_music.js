/**
 * Rainette Music page - search, catalog, playlists, and library.
 *
 * Playback stays delegated to the persistent mini-player (window.RainetteMusic).
 * The helper provides ad-free yt-dlp streams and, when available, YouTube Music
 * metadata for artist/album catalog browsing.
 */

import { sendHelper, helperRequest, rainetteAuthHeaders, app, el, btn } from './music_shell.js';
import { RainetteRouter } from './music_shell.js';
import { iconMarkup } from './rainette_icons.js';
import { confirmDialog, textPrompt, pickerDialog, infoDialog, actionSheet, customDialog } from './rainette_modal.js';
import { createSelect } from './rainette_select.js';
import { renderSettings, defaultLandingTab, shouldAutoOpenQueue } from './rainette_settings.js';
import { renderMobile, unmountMobile } from './rainette_mobile.js';

const LAYOUT_KEY = 'rainette.musicLayout';
const QUEUE_SUPPORTED = typeof window !== 'undefined' && !!window.RW_REMOTE;
const FILTER_PREFIX = 'rainette.musicFilters.';
const ARTIST_MODE_KEY = 'rainette.artistViewMode';
const FOLDER_CLOSED_PREFIX = 'rainette.folderClosed.';

function _savedLayout() {
	try { return localStorage.getItem(LAYOUT_KEY) === 'grid' ? 'grid' : 'list'; }
	catch { return 'list'; }
}

function lsGet(k, fallback = null) { try { const v = localStorage.getItem(k); return v == null ? fallback : v; } catch { return fallback; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* best effort */ } }
function loadFilter(id, fallback) {
	try {
		return { ...fallback, ...(JSON.parse(localStorage.getItem(FILTER_PREFIX + id) || '{}') || {}) };
	} catch { return { ...fallback }; }
}
function saveFilter(id, value) { lsSet(FILTER_PREFIX + id, JSON.stringify(value)); }

const pageState = {
	tab: defaultLandingTab(),
	query: '',
	searchFilter: 'all',
	results: [],
	resultArtists: [],
	resultAlbums: [],
	playlists: [],
	folders: [],
	openPlaylist: null,
	recent: [],
	topArtists: [],
	library: { tracks: [], artists: [], albums: [], followed_artists: [] },
	recentMode: 'songs',
	queue: { tracks: [], index: -1, playing: false, loop: false, duration: 0 },
	queueDrawerOpen: false,
	queueSaveBusy: false,
	queueSaveStatus: '',
	queueSessions: [],
	queueSessionsOpen: false,
	queueSessionStatus: '',
	queueAutosaveTimer: null,
	mixStatus: '',
	view: null,
	layout: _savedLayout(),
	filters: {
		songs: loadFilter('songs', { query: '', sort: 'added' }),
		following: loadFilter('following', { query: '', sort: 'followed' }),
		artists: loadFilter('artists', { query: '', sort: 'count' }),
		albums: loadFilter('albums', { query: '', sort: 'artist' }),
		playlists: loadFilter('playlists', { query: '', sort: 'pinned' }),
		recent: loadFilter('recent', { query: '', sort: 'recent' }),
		queue: loadFilter('queue', { query: '', sort: 'index' }),
		playlistDetail: loadFilter('playlistDetail', { query: '', sort: 'position' }),
	},
	artistMode: lsGet(ARTIST_MODE_KEY, 'list') === 'browse' ? 'browse' : 'list',
	browse: { artistKey: '', albumKey: '' },
	palette: { open: false, query: '', selected: 0, catalog: { songs: [], artists: [], albums: [] }, status: '', lastFocus: null },
	nowPlayingOpen: false,
	insights: { days: 7, data: null, loading: false },
	progress: { current_time: 0, duration: 0, playing: false, source_id: '' },
	playbackStarted: false,
	lyrics: { source_id: '', reqId: '', loading: false, text: '', notFound: false, instrumental: false, error: '', open: false, synced: false, lines: [], activeIndex: -1, userScrollUntil: 0 },
	_autoplayOnLoad: false,
};

const TAB_META = {
	home: { label: 'Home', eyebrow: 'Overview', title: 'Home', sub: 'Pick up where you left off and jump back into what you play most.' },
	search: { label: 'Search', eyebrow: 'Catalog', title: 'Search the catalog', sub: 'Find songs, artists, and albums without leaving the listening flow.' },
	songs: { label: 'Songs', eyebrow: 'Library', title: 'Your songs', sub: 'All locally saved and played songs, with songs first across the library.' },
	following: { label: 'Following', eyebrow: 'Library', title: 'Following', sub: 'Artists you chose to follow, kept separate from listening history.' },
	playlists: { label: 'Playlists', eyebrow: 'Collection', title: 'Playlists', sub: 'Create, open, and manage your saved listening sets.' },
	recent: { label: 'Recents', eyebrow: 'History', title: 'Recently played', sub: 'Return to recently played songs, artists, and albums.' },
	insights: { label: 'Insights', eyebrow: 'History', title: 'Listening insights', sub: 'What you actually played — totals, daily rhythm, and your heavy rotation.' },
	queue: { label: 'Queue', eyebrow: 'Up Next', title: 'Queue', sub: 'Review, reorder, clean up, and save the current queue.' },
	mobile: { label: 'Mobile', eyebrow: 'Companion', title: 'Rainette on Android', sub: 'Download the app, install it, and pair securely with this desktop.' },
	settings: { label: 'Settings', eyebrow: 'App', title: 'Settings', sub: 'Tune appearance, behavior, and desktop playback preferences.' },
};

function navItems() {
	const ids = ['home', 'search', 'songs', 'following', 'recent', 'playlists', 'insights', 'mobile'];
	if (QUEUE_SUPPORTED) ids.push('queue');
	ids.push('settings');
	return ids;
}

let _host = null;
let _mounted = false;
let _listenerBound = false;
let _paletteKeysBound = false;
let _nowPlayingKeysBound = false;
let _searchDebounce = null;
let _lastAutoOpenTrackKey = null;
let _lastOptimisticQueueSignature = null;
let _lastAnimatedTab = null;
let _lyricsLineEls = [];
let _lyricsScrollGuardWired = false;
let _lyricsManualTimer = null;

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

function textMatch(value, query) {
	return !query || String(value || '').toLowerCase().includes(String(query || '').toLowerCase());
}

function dateValue(value) {
	const t = Date.parse(value || '');
	return Number.isFinite(t) ? t : 0;
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

function itemKey(item, fallbackName = '') {
	return String(item?.id || item?.browse_id || item?.source_id || item?.name || item?.title || fallbackName || '').toLowerCase();
}

function artistIdentity(artist) {
	const id = String(artist?.artist_id || artist?.id || artist?.browse_id || '').trim().toLowerCase();
	if (id) return 'id:' + id;
	const name = String(artist?.name || artist?.artist || '').trim().replace(/\s+/g, ' ').toLowerCase();
	return name ? 'name:' + name : '';
}

function isFollowingArtist(artist) {
	const key = artistIdentity(artist);
	return !!key && pageState.library.followed_artists.some(item => (item.artist_key || artistIdentity(item)) === key);
}

function toggleArtistFollow(artist) {
	const payload = {
		id: 'mfollow_' + Math.random().toString(36).slice(2),
		artist_id: artist?.artist_id || artist?.id || artist?.browse_id || '',
		name: artist?.name || artist?.artist || '',
		thumbnail_url: artist?.thumbnail_url || '',
	};
	sendHelper({ type: isFollowingArtist(artist) ? 'music_artist_unfollow' : 'music_artist_follow', ...payload });
}

function filterState(id) {
	return pageState.filters[id] || { query: '', sort: '' };
}

function setFilterValue(id, key, value) {
	pageState.filters[id] = { ...filterState(id), [key]: value };
	saveFilter(id, pageState.filters[id]);
	renderCurrent();
}

function compactFilterToolbar(id, placeholder, sortOptions = [], extra = []) {
	const state = filterState(id);
	const bar = el('div', 'rw-toolbar rw-filter-toolbar');
	const input = document.createElement('input');
	input.className = 'rw-input rw-filter-input';
	input.type = 'search';
	input.placeholder = placeholder;
	input.value = state.query || '';
	input.addEventListener('input', e => setFilterValue(id, 'query', e.target.value));
	bar.appendChild(input);
	if (sortOptions.length) {
		const select = createSelect({
			options: sortOptions,
			value: state.sort,
			ariaLabel: 'Sort',
			onChange: v => setFilterValue(id, 'sort', v),
		});
		bar.appendChild(select);
	}
	for (const node of extra) bar.appendChild(node);
	return bar;
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
		img.addEventListener('error', () => {
			img.remove();
			thumb.textContent = fallback;
		}, { once: true });
		thumb.appendChild(img);
	} else {
		thumb.textContent = fallback;
	}
	return thumb;
}

function iconBtn(iconName, className, onClick, title) {
	const b = document.createElement('button');
	b.type = 'button';
	b.className = className;
	b.innerHTML = iconMarkup(iconName);
	if (title) { b.title = title; b.setAttribute('aria-label', title); }
	if (onClick) b.addEventListener('click', onClick);
	return b;
}

function iconSpan(iconName, className = '') {
	const span = el('span', className);
	span.innerHTML = iconMarkup(iconName, 14);
	span.setAttribute('aria-hidden', 'true');
	return span;
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
	const card = el('div', 'rw-bubble rw-track-card rw-artist-card');
	const metaWrap = el('div', 'rw-track-meta');
	metaWrap.append(
		el('div', 'rw-track-title', artist.name || 'Unknown artist'),
		el('div', 'rw-track-sub', [artist.subscribers || '', artist.track_count ? `${artist.track_count} saved track${artist.track_count === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ')),
	);
	metaWrap.style.cursor = 'pointer';
	metaWrap.addEventListener('click', () => openArtist(artist));
	const open = btn('Open', 'rw-btn rw-btn-ghost', () => openArtist(artist));
	const following = isFollowingArtist(artist);
	const follow = btn(following ? 'Following' : 'Follow', 'rw-btn rw-btn-ghost rw-follow-btn' + (following ? ' on' : ''), () => toggleArtistFollow(artist));
	follow.setAttribute('aria-label', (following ? 'Unfollow ' : 'Follow ') + (artist.name || 'artist'));
	const actWrap = el('div', 'rw-track-actions');
	actWrap.append(open, follow);
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
	return iconBtn('play', 'rw-icon-btn', () => {
		if (queue) window.RainetteMusic?.playQueue(queue, queue.indexOf(track));
		else window.RainetteMusic?.playTrack(track);
	}, 'Play');
}

function trackActions(track, queue, extras = []) {
	return [
		playAction(track, queue),
		iconBtn('more', 'rw-icon-btn', () => openTrackMenu(track, queue, extras), 'More actions'),
	];
}

async function openTrackMenu(track, queue, extras = []) {
	const album = albumInfo(track);
	const artist = artistName(track);
	const items = [];
	if (QUEUE_SUPPORTED) {
		items.push({ label: 'Play next', run: () => window.RainetteMusic?.queueAddNext?.(track) });
		items.push({ label: 'Add to queue', run: () => window.RainetteMusic?.queueAddEnd?.(track) });
	}
	items.push({ label: 'Add to playlist', run: () => openAddToPlaylist(track) });
	items.push({ label: 'Start mix', run: () => startMixFromSeed({ kind: 'track', track }) });
	if (artist) items.push({ label: 'Open artist', run: () => openArtist({ id: artistId(track), name: artist, thumbnail_url: track.thumbnail_url || '' }) });
	if (album?.title) items.push({ label: 'Open album', run: () => openAlbum(album) });
	for (const item of extras) items.push(item);
	await actionSheet({ title: track.title || 'Track actions', items });
}

function renderCurrent() {
	if (!_host) return;
	if (pageState.tab === 'mobile') unmountMobile();
	if (pageState.view?.kind === 'artist') return renderArtistDetail();
	if (pageState.view?.kind === 'album') return renderAlbumDetail();
	applyTab();
}

function updateShellChrome() {
	const current = TAB_META[pageState.tab] || TAB_META.search;
	_host?.querySelectorAll('#rwMusicTabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === pageState.tab));
	const eyebrow = _host?.querySelector('#rwMusicEyebrow');
	const title = _host?.querySelector('#rwMusicTitle');
	const sub = _host?.querySelector('#rwMusicSub');
	if (eyebrow) eyebrow.textContent = current.eyebrow;
	if (title) title.textContent = current.title;
	if (sub) sub.textContent = current.sub;
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
	const filters = el('div', 'rw-segment rw-search-filters');
	for (const [id, label] of [['all', 'All'], ['songs', 'Songs'], ['artists', 'Artists'], ['albums', 'Albums']]) {
		const filter = document.createElement('button');
		filter.type = 'button';
		filter.textContent = label;
		filter.className = pageState.searchFilter === id ? 'on' : '';
		filter.addEventListener('click', () => { pageState.searchFilter = id; renderSearch(); });
		filters.appendChild(filter);
	}
	body.appendChild(filters);
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
	if (!pageState.query.trim()) {
		const artists = recentArtists().slice(0, 8);
		list.appendChild(section('Recent artists', artists.length ? 'From your listening history' : ''));
		if (!artists.length) list.appendChild(_empty('No recent artists yet', 'Play a song and its artist will appear here.'));
		for (const artist of artists) list.appendChild(artistCard(artist));
		return;
	}
	if (!pageState.results.length && !pageState.resultArtists.length && !pageState.resultAlbums.length) return;
	if ((pageState.searchFilter === 'all' || pageState.searchFilter === 'songs') && pageState.results.length) {
		list.appendChild(section('Songs'));
		for (const track of pageState.results) list.appendChild(trackCard(track, trackActions(track, pageState.results)));
	}
	if ((pageState.searchFilter === 'all' || pageState.searchFilter === 'artists') && pageState.resultArtists.length) {
		list.appendChild(section('Artists'));
		for (const artist of pageState.resultArtists) list.appendChild(artistCard(artist));
	}
	if ((pageState.searchFilter === 'all' || pageState.searchFilter === 'albums') && pageState.resultAlbums.length) {
		list.appendChild(section('Albums'));
		for (const album of pageState.resultAlbums) list.appendChild(albumCard(album));
	}
}

function renderSongs() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	body.appendChild(compactFilterToolbar('songs', 'Filter songs or artists', [
		['added', 'Recently added'], ['title', 'Title'], ['artist', 'Artist'], ['played', 'Most played'],
	]));
	const f = filterState('songs');
	const tracks = [...pageState.library.tracks].filter(track => textMatch(track.title, f.query) || textMatch(artistName(track), f.query));
	if (f.sort === 'title') tracks.sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')));
	else if (f.sort === 'artist') tracks.sort((a, b) => String(artistName(a)).localeCompare(String(artistName(b))));
	else if (f.sort === 'played') tracks.sort((a, b) => Number(b.play_count || 0) - Number(a.play_count || 0));
	else tracks.sort((a, b) => dateValue(b.added_at) - dateValue(a.added_at));
	const list = trackList('rwMusicSongs');
	if (!tracks.length) list.appendChild(_empty('No songs yet', pageState.library.tracks.length ? 'Try a different filter.' : 'Search and play songs to build your library.'));
	for (const track of tracks) list.appendChild(trackCard(track, trackActions(track, tracks)));
	body.appendChild(list);
}

function renderFollowing() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	body.appendChild(compactFilterToolbar('following', 'Filter followed artists', [
		['followed', 'Recently followed'], ['az', 'A-Z'],
	]));
	const f = filterState('following');
	const artists = [...pageState.library.followed_artists].filter(artist => textMatch(artist.name, f.query));
	if (f.sort === 'az') artists.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
	else artists.sort((a, b) => dateValue(b.followed_at) - dateValue(a.followed_at));
	const list = trackList('rwMusicFollowing');
	if (!artists.length) list.appendChild(_empty('No followed artists', 'Follow an artist from Search, Recents, or an artist profile.'));
	for (const artist of artists) list.appendChild(artistCard(artist));
	body.appendChild(list);
}

function renderArtists() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	const mode = el('div', 'rw-segment');
	for (const [id, label] of [['list', 'List'], ['browse', 'Browse']]) {
		const b = document.createElement('button');
		b.type = 'button';
		b.textContent = label;
		b.className = pageState.artistMode === id ? 'on' : '';
		b.addEventListener('click', () => {
			pageState.artistMode = id;
			lsSet(ARTIST_MODE_KEY, id);
			renderArtists();
		});
		mode.appendChild(b);
	}
	body.appendChild(compactFilterToolbar('artists', 'Filter artists', [
		['count', 'Saved tracks'],
		['az', 'A-Z'],
		['added', 'Recently added'],
	], [mode]));
	if (pageState.artistMode === 'browse') return renderLibraryBrowser(body);
	const list = trackList('rwMusicArtists');
	const f = filterState('artists');
	const artists = [...pageState.library.artists].filter(a => textMatch(a.name, f.query));
	if (f.sort === 'az') artists.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
	else if (f.sort === 'added') artists.sort((a, b) => dateValue(b.added_at) - dateValue(a.added_at));
	else artists.sort((a, b) => Number(b.track_count || 0) - Number(a.track_count || 0));
	if (!artists.length) list.appendChild(_empty('No artists found', pageState.library.artists.length ? 'Try a different filter.' : 'Play or save tracks to build the local artist index.'));
	for (const artist of artists) list.appendChild(artistCard(artist));
	body.appendChild(list);
}

function renderAlbums() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	body.appendChild(compactFilterToolbar('albums', 'Filter albums or artists', [
		['artist', 'Artist'],
		['az', 'A-Z'],
		['count', 'Track count'],
		['added', 'Recently added'],
	]));
	const list = trackList('rwMusicAlbums');
	const f = filterState('albums');
	const albums = [...pageState.library.albums].filter(a => textMatch(a.title, f.query) || textMatch(a.artist, f.query));
	if (f.sort === 'az') albums.sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')));
	else if (f.sort === 'count') albums.sort((a, b) => Number(b.track_count || 0) - Number(a.track_count || 0));
	else if (f.sort === 'added') albums.sort((a, b) => dateValue(b.added_at) - dateValue(a.added_at));
	else albums.sort((a, b) => (String(a.artist || '') + String(a.title || '')).localeCompare(String(b.artist || '') + String(b.title || '')));
	if (!albums.length) list.appendChild(_empty('No albums found', pageState.library.albums.length ? 'Try a different filter.' : 'Search an album or play tracks with album metadata.'));
	for (const album of albums) list.appendChild(albumCard(album));
	body.appendChild(list);
}

function renderLibraryBrowser(body) {
	const wrap = el('div', 'rw-browser-grid');
	const f = filterState('artists');
	const artists = [...pageState.library.artists].filter(a => textMatch(a.name, f.query));
	if (!pageState.browse.artistKey && artists[0]) pageState.browse.artistKey = itemKey(artists[0]);
	const selectedArtist = artists.find(a => itemKey(a) === pageState.browse.artistKey) || artists[0] || null;
	const artistNameKey = String(selectedArtist?.name || '').toLowerCase();
	const albums = pageState.library.albums.filter(a => !selectedArtist || String(a.artist || '').toLowerCase() === artistNameKey);
	if (selectedArtist && !albums.some(a => itemKey(a) === pageState.browse.albumKey)) pageState.browse.albumKey = albums[0] ? itemKey(albums[0]) : '';
	const selectedAlbum = albums.find(a => itemKey(a) === pageState.browse.albumKey) || null;
	let tracks = pageState.library.tracks.filter(t => !selectedArtist || String(artistName(t) || '').toLowerCase() === artistNameKey);
	if (selectedAlbum) {
		const selectedAlbumKey = itemKey(selectedAlbum);
		tracks = tracks.filter(t => {
			const album = albumInfo(t);
			return album && itemKey(album, album.title) === selectedAlbumKey;
		});
	}
	const artistCol = browserColumn('Artists', artists, selectedArtist, item => {
		pageState.browse.artistKey = itemKey(item);
		pageState.browse.albumKey = '';
		renderArtists();
	}, item => [item.name || 'Unknown artist', `${item.track_count || 0} tracks`]);
	const albumCol = browserColumn('Albums', albums, selectedAlbum, item => {
		pageState.browse.albumKey = itemKey(item);
		renderArtists();
	}, item => [item.title || 'Unknown album', item.artist || '']);
	const trackCol = el('div', 'rw-browser-col rw-browser-tracks');
	trackCol.appendChild(section('Tracks', tracks.length ? `${tracks.length}` : ''));
	const list = el('div', 'rw-track-list');
	if (!tracks.length) list.appendChild(_empty('No tracks in this slice', 'Choose another artist or album.'));
	for (const track of tracks) list.appendChild(trackCard(track, trackActions(track, tracks)));
	trackCol.appendChild(list);
	wrap.append(artistCol, albumCol, trackCol);
	body.appendChild(wrap);
}

function browserColumn(title, items, selected, onSelect, describe) {
	const col = el('div', 'rw-browser-col');
	col.appendChild(section(title, items.length ? `${items.length}` : ''));
	const list = el('div', 'rw-browser-list');
	if (!items.length) list.appendChild(el('div', 'rw-status-line', 'Nothing here yet.'));
	for (const item of items) {
		const [label, sub] = describe(item);
		const b = document.createElement('button');
		b.type = 'button';
		b.className = 'rw-browser-item' + (selected && itemKey(item) === itemKey(selected) ? ' on' : '');
		b.innerHTML = `<span>${label}</span><small>${sub || ''}</small>`;
		b.addEventListener('click', () => onSelect(item));
		list.appendChild(b);
	}
	col.appendChild(list);
	return col;
}

function renderArtistDetail() {
	if (pageState.tab === 'mobile') unmountMobile();
	const body = _host?.querySelector('#rwMusicBody');
	const view = pageState.view;
	if (!body || !view) return;
	body.innerHTML = '';
	const artist = view.artist || {};
	const head = el('div', 'rw-toolbar rw-detail-head');
	const following = isFollowingArtist(artist);
	head.append(
		btn('Back', 'rw-btn rw-btn-ghost', () => { pageState.view = null; applyTab(); }),
		el('div', 'rw-track-title', artist.name || 'Artist'),
		btn(following ? 'Unfollow' : 'Follow', 'rw-btn rw-btn-ghost', () => toggleArtistFollow(artist)),
		btn('Start mix', 'rw-btn rw-btn-primary', () => startMixFromSeed({ kind: 'artist', artist })),
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
	if (pageState.tab === 'mobile') unmountMobile();
	const body = _host?.querySelector('#rwMusicBody');
	const view = pageState.view;
	if (!body || !view) return;
	body.innerHTML = '';
	const album = view.album || {};
	const head = el('div', 'rw-toolbar rw-detail-head');
	head.append(
		btn('Back', 'rw-btn rw-btn-ghost', () => { pageState.view = null; applyTab(); }),
		el('div', 'rw-track-title', album.title || 'Album'),
		btn('Start mix', 'rw-btn rw-btn-primary', () => startMixFromSeed({ kind: 'album', album })),
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
	const folder = btn('New folder', 'rw-btn rw-btn-ghost', createFolderFlow);
	const smart = btn('New smart playlist', 'rw-btn rw-btn-ghost', () => smartPlaylistDialog());
	bar.append(nameInput, create, folder, smart);
	body.appendChild(bar);

	body.appendChild(compactFilterToolbar('playlists', 'Filter playlists', [
		['pinned', 'Pinned first'],
		['updated', 'Recently updated'],
		['az', 'A-Z'],
		['count', 'Track count'],
	]));

	const list = el('div', 'rw-playlist-groups');
	const playlists = filteredPlaylists();
	if (!playlists.length) list.appendChild(_empty('No playlists found', pageState.playlists.length ? 'Try a different filter.' : 'Create one above, then add tracks from Search.'));
	const pinned = playlists.filter(p => p.pinned);
	if (pinned.length) list.appendChild(playlistGroup('Pinned', pinned, 'pin'));
	for (const folder of pageState.folders) {
		const items = playlists.filter(p => !p.pinned && p.folder_id === folder.id);
		if (items.length || !filterState('playlists').query) list.appendChild(playlistGroup(folder.name, items, folder.id, folder));
	}
	const unfiled = playlists.filter(p => !p.pinned && !p.folder_id);
	if (unfiled.length || (!pageState.folders.length && playlists.length)) list.appendChild(playlistGroup('Unfiled', unfiled, 'unfiled'));
	body.appendChild(list);
}

function renderPlaylistDetail(body) {
	const pl = pageState.openPlaylist;
	const head = el('div', 'rw-toolbar');
	head.append(
		btn('Back', 'rw-btn rw-btn-ghost', () => { pageState.openPlaylist = null; renderPlaylists(); }),
		thumbBox(playlistArtworkUrl(pl), pl.kind === 'smart' ? 'SP' : 'PL'),
		el('div', 'rw-track-title', pl.name),
		btn('Play', 'rw-btn rw-btn-primary', () => {
			const tracks = pl._tracks || [];
			if (tracks.length) window.RainetteMusic?.playQueue(tracks, 0);
		}),
	);
	if (pl.kind === 'smart') head.appendChild(btn('Edit rules', 'rw-btn rw-btn-ghost', () => smartPlaylistDialog(pl)));
	body.appendChild(head);
	if (pl.kind === 'smart') body.appendChild(el('div', 'rw-status-line', 'Smart playlist membership is controlled by deterministic rules.'));
	body.appendChild(compactFilterToolbar('playlistDetail', 'Filter tracks', [
		['position', 'Playlist order'],
		['title', 'Title'],
		['artist', 'Artist'],
		['duration', 'Duration'],
	]));
	const tracks = pl._tracks || [];
	const list = trackList();
	const f = filterState('playlistDetail');
	const shown = tracks.filter(t => textMatch(t.title, f.query) || textMatch(artistName(t), f.query));
	if (f.sort === 'title') shown.sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')));
	else if (f.sort === 'artist') shown.sort((a, b) => String(artistName(a)).localeCompare(String(artistName(b))));
	else if (f.sort === 'duration') shown.sort((a, b) => Number(b.duration_s || 0) - Number(a.duration_s || 0));
	if (!shown.length) list.appendChild(_empty('No tracks found', tracks.length ? 'Try a different filter.' : (pl.kind === 'smart' ? 'Adjust the rules to include tracks.' : 'Add tracks from the Search tab.')));
	for (const track of shown) {
		const extras = pl.kind === 'smart' ? [] : [{
			label: 'Remove from playlist',
			danger: true,
			run: () => sendHelper({ type: 'music_playlist_remove_track', id: 'plrt_' + Math.random().toString(36).slice(2), playlist_id: pl.id, track_id: track.id }),
		}];
		list.appendChild(trackCard(track, trackActions(track, shown, extras)));
	}
	body.appendChild(list);
}

function openPlaylist(pl, autoplay = false) {
	pageState.openPlaylist = { ...pl, _tracks: [] };
	pageState._autoplayOnLoad = autoplay;
	sendHelper({ type: pl.kind === 'smart' ? 'music_smart_playlist_tracks' : 'music_playlist_tracks', id: 'plt_' + Math.random().toString(36).slice(2), playlist_id: pl.id });
	renderPlaylists();
}

function filteredPlaylists() {
	const f = filterState('playlists');
	const items = [...pageState.playlists].filter(p => textMatch(p.name, f.query));
	if (f.sort === 'az') items.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
	else if (f.sort === 'count') items.sort((a, b) => Number(b.track_count || 0) - Number(a.track_count || 0));
	else if (f.sort === 'updated') items.sort((a, b) => dateValue(b.updated_at) - dateValue(a.updated_at));
	else items.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || dateValue(b.updated_at) - dateValue(a.updated_at));
	return items;
}

function playlistGroup(title, playlists, key, folder = null) {
	const wrap = el('div', 'rw-playlist-group');
	const closed = folder && lsGet(FOLDER_CLOSED_PREFIX + folder.id) === '1';
	const head = el('div', 'rw-playlist-group-head');
	const toggle = document.createElement('button');
	toggle.type = 'button';
	toggle.className = 'rw-playlist-group-title';
	toggle.textContent = `${title} ${playlists.length ? `(${playlists.length})` : ''}`;
	toggle.addEventListener('click', () => {
		if (!folder) return;
		lsSet(FOLDER_CLOSED_PREFIX + folder.id, closed ? '0' : '1');
		renderPlaylists();
	});
	head.appendChild(toggle);
	if (folder) head.appendChild(iconBtn('more', 'rw-icon-btn', () => folderMenu(folder), 'Folder actions'));
	wrap.appendChild(head);
	if (!closed) {
		const list = el('div', 'rw-track-list rw-playlist-list');
		if (!playlists.length) list.appendChild(el('div', 'rw-status-line', 'No playlists in this folder.'));
		for (const pl of playlists) list.appendChild(playlistCard(pl));
		wrap.appendChild(list);
	}
	wrap.dataset.group = key;
	return wrap;
}

function playlistCard(pl) {
	const card = el('div', 'rw-bubble rw-track-card rw-playlist-card');
	const metaWrap = el('div', 'rw-track-meta');
	metaWrap.append(
		el('div', 'rw-track-title', pl.name),
		el('div', 'rw-track-sub', `${pl.kind === 'smart' ? 'Smart' : 'Playlist'} - ${pl.track_count || 0} track${pl.track_count === 1 ? '' : 's'}${pl.pinned ? ' - Pinned' : ''}`),
	);
	metaWrap.style.cursor = 'pointer';
	metaWrap.addEventListener('click', () => openPlaylist(pl));
	const actions = el('div', 'rw-track-actions');
	actions.append(
		iconBtn('play', 'rw-icon-btn', () => openPlaylist(pl, true), 'Open and play'),
		iconBtn('more', 'rw-icon-btn', () => playlistMenu(pl), 'Playlist actions'),
	);
	card.append(thumbBox(playlistArtworkUrl(pl), pl.kind === 'smart' ? 'SP' : 'PL'), metaWrap, actions);
	return card;
}

function playlistArtworkUrl(pl) {
	return pl?.artwork_key ? '/playlist-artwork/' + encodeURIComponent(pl.artwork_key) : '';
}

async function choosePlaylistArtwork(pl) {
	const input = document.createElement('input');
	input.type = 'file';
	input.accept = 'image/png,image/jpeg,image/webp';
	input.hidden = true;
	document.body.appendChild(input);
	const file = await new Promise(resolve => {
		input.addEventListener('change', () => resolve(input.files?.[0] || null), { once: true });
		input.click();
	});
	input.remove();
	if (!file) return;
	const form = new FormData();
	form.append('file', file, file.name);
	try {
		const response = await fetch('/playlist-artwork/' + encodeURIComponent(pl.id), {
			method: 'POST',
			headers: rainetteAuthHeaders(),
			body: form,
		});
		const result = await response.json().catch(() => ({}));
		if (!response.ok || !result.ok) throw new Error(result.msg || 'Upload failed');
		sendHelper({ type: 'music_playlist_list', id: 'plart_' + Math.random().toString(36).slice(2) });
	} catch (error) {
		await infoDialog({ title: 'Artwork unavailable', message: error?.message || 'Could not save that image.' });
	}
}

async function removePlaylistArtwork(pl) {
	try {
		const response = await fetch('/playlist-artwork/' + encodeURIComponent(pl.id), {
			method: 'DELETE',
			headers: rainetteAuthHeaders(),
		});
		const result = await response.json().catch(() => ({}));
		if (!response.ok || !result.ok) throw new Error(result.msg || 'Remove failed');
		sendHelper({ type: 'music_playlist_list', id: 'plart_' + Math.random().toString(36).slice(2) });
	} catch (error) {
		await infoDialog({ title: 'Artwork unavailable', message: error?.message || 'Could not remove the image.' });
	}
}

async function createFolderFlow() {
	const name = await textPrompt({ title: 'New folder', label: 'Folder name', confirmLabel: 'Create' });
	if (name) sendHelper({ type: 'music_playlist_folder_create', id: 'fldc_' + Math.random().toString(36).slice(2), name });
}

async function folderMenu(folder) {
	const idx = pageState.folders.findIndex(f => f.id === folder.id);
	await actionSheet({
		title: folder.name,
		items: [
			{ label: 'Rename folder', run: async () => {
				const name = await textPrompt({ title: 'Rename folder', label: 'Folder name', defaultValue: folder.name });
				if (name) sendHelper({ type: 'music_playlist_folder_rename', id: 'fldr_' + Math.random().toString(36).slice(2), folder_id: folder.id, name });
			} },
			idx > 0 && { label: 'Move up', run: () => sendHelper({ type: 'music_playlist_folder_move', id: 'fldm_' + Math.random().toString(36).slice(2), folder_id: folder.id, position: idx - 1 }) },
			idx < pageState.folders.length - 1 && { label: 'Move down', run: () => sendHelper({ type: 'music_playlist_folder_move', id: 'fldm_' + Math.random().toString(36).slice(2), folder_id: folder.id, position: idx + 1 }) },
			{ label: 'Delete folder', danger: true, run: async () => {
				const ok = await confirmDialog({ title: 'Delete folder', message: `Move playlists out of "${folder.name}" and delete the folder?`, confirmLabel: 'Delete', danger: true });
				if (ok) sendHelper({ type: 'music_playlist_folder_delete', id: 'fldd_' + Math.random().toString(36).slice(2), folder_id: folder.id });
			} },
		],
	});
}

async function playlistMenu(pl) {
	const folders = [{ id: '', label: 'Unfiled' }, ...pageState.folders.map(f => ({ id: f.id, label: f.name }))];
	await actionSheet({
		title: pl.name,
		items: [
			{ label: 'Choose artwork', run: () => choosePlaylistArtwork(pl) },
			pl.artwork_key && { label: 'Remove artwork', run: () => removePlaylistArtwork(pl) },
			{ label: pl.pinned ? 'Unpin playlist' : 'Pin playlist', run: () => sendHelper({ type: 'music_playlist_update_meta', id: 'plpin_' + Math.random().toString(36).slice(2), playlist_id: pl.id, pinned: !pl.pinned }) },
			{ label: 'Move to folder', run: async () => {
				const folderId = await pickerDialog({ title: 'Move playlist', items: folders });
				if (folderId != null) sendHelper({ type: 'music_playlist_update_meta', id: 'plfld_' + Math.random().toString(36).slice(2), playlist_id: pl.id, folder_id: folderId });
			} },
			{ label: 'Rename', run: async () => {
				const name = await textPrompt({ title: 'Rename playlist', label: 'Playlist name', defaultValue: pl.name });
				if (name) sendHelper({ type: 'music_playlist_rename', id: 'plr_' + Math.random().toString(36).slice(2), playlist_id: pl.id, name });
			} },
			pl.kind === 'smart' && { label: 'Edit rules', run: () => smartPlaylistDialog(pl) },
			{ label: 'Delete', danger: true, run: async () => {
				const ok = await confirmDialog({ title: 'Delete playlist', message: `Delete playlist "${pl.name}"? This can't be undone.`, confirmLabel: 'Delete', danger: true });
				if (ok) sendHelper({ type: pl.kind === 'smart' ? 'music_smart_playlist_delete' : 'music_playlist_delete', id: 'pld_' + Math.random().toString(36).slice(2), playlist_id: pl.id });
			} },
		],
	});
}

function smartPlaylistDialog(existing = null) {
	const rules = existing?.rules || { match: 'all', rules: [{ field: 'played_days', op: 'is', value: 14 }], sort: 'recent', limit: 50 };
	const wrap = el('div', 'rw-smart-form');
	const nameLabel = el('label', 'rw-label', 'Name');
	const name = document.createElement('input');
	name.className = 'rw-input';
	name.value = existing?.name || 'Smart playlist';
	wrap.append(nameLabel, name);
	const row = el('div', 'rw-smart-row');
	const fields = [
		['played_days', 'Played in last days'],
		['not_played_days', 'Not played in days'],
		['added_days', 'Added in last days'],
		['artist', 'Artist contains'],
		['title', 'Title contains'],
		['album', 'Album contains'],
		['has_album', 'Has album metadata'],
		['duration_min', 'Duration at least minutes'],
		['duration_max', 'Duration at most minutes'],
	];
	const firstRule = rules.rules?.[0] || { field: 'played_days', op: 'is', value: 14 };
	const field = createSelect({ options: fields, value: firstRule.field, ariaLabel: 'Rule field' });
	const value = document.createElement('input');
	value.className = 'rw-input';
	value.value = firstRule.field === 'has_album' ? 'true' : String(firstRule.value ?? '');
	value.placeholder = 'Value';
	row.append(field, value);
	wrap.append(el('label', 'rw-label', 'Rule'), row);
	const sort = createSelect({
		options: [['recent', 'Recently played'], ['added', 'Recently added'], ['title', 'Title'], ['artist', 'Artist'], ['duration', 'Duration']],
		value: rules.sort || 'recent',
		ariaLabel: 'Sort',
	});
	const limit = document.createElement('input');
	limit.className = 'rw-input';
	limit.type = 'number';
	limit.min = '1';
	limit.max = '200';
	limit.value = String(rules.limit || 50);
	const foot = el('div', 'rw-smart-row');
	foot.append(sort, limit);
	wrap.append(el('label', 'rw-label', 'Sort and limit'), foot);
	return customDialog({
		title: existing ? 'Edit smart playlist' : 'New smart playlist',
		bodyNode: wrap,
		wire: close => [
			btn('Cancel', 'rw-btn rw-btn-ghost', () => close(null)),
			btn('Save', 'rw-btn rw-btn-primary', () => {
				const fieldValue = field.value;
				const rawValue = fieldValue === 'has_album' ? value.value !== 'false' : value.value;
				const payload = {
					match: 'all',
					rules: [{ field: fieldValue, op: fieldValue === 'has_album' ? 'is' : 'contains', value: rawValue }],
					sort: sort.value,
					limit: Number(limit.value || 50),
				};
				sendHelper({
					type: existing ? 'music_smart_playlist_update' : 'music_smart_playlist_create',
					id: 'spl_' + Math.random().toString(36).slice(2),
					playlist_id: existing?.id,
					name: name.value.trim() || 'Smart playlist',
					rules: payload,
				});
				close(true);
			}),
		],
	});
}

async function openAddToPlaylist(track) {
	const manualPlaylists = pageState.playlists.filter(p => p.kind !== 'smart');
	const createId = '__create_playlist__';
	const plId = await pickerDialog({
		title: 'Add "' + (track.title || 'track') + '" to playlist',
		items: [
			{ id: createId, label: 'Create new playlist' },
			...manualPlaylists.map(p => ({ id: p.id, label: p.name })),
		],
	});
	let pl = manualPlaylists.find(p => p.id === plId);
	if (plId === createId) {
		const name = await textPrompt({ title: 'Create new playlist', label: 'Playlist name', confirmLabel: 'Create' });
		if (!name) return;
		const created = await helperRequest('music_playlist_create', { name }, 8000);
		if (!created?.ok || !created.playlist) {
			await infoDialog({ title: 'Playlist unavailable', message: created?.msg || 'Could not create the playlist.' });
			return;
		}
		pl = created.playlist;
	}
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
	const name = await textPrompt({ title: 'Save queue as playlist', label: 'Playlist name', defaultValue: defaultQueueName(), confirmLabel: 'Save' });
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

function lastQueueSession() {
	return pageState.queueSessions.find(s => s.is_last) || null;
}

function saveLastQueueSessionDebounced() {
	if (!QUEUE_SUPPORTED) return;
	clearTimeout(pageState.queueAutosaveTimer);
	pageState.queueAutosaveTimer = setTimeout(() => {
		const q = pageState.queue || {};
		const tracks = (q.tracks || []).filter(t => t && t.source_id);
		if (!tracks.length) return;
		sendHelper({
			type: 'music_queue_session_save',
			id: 'qsl_' + Math.random().toString(36).slice(2),
			name: 'Last session',
			tracks,
			index: q.index || 0,
			is_last: true,
		});
	}, 900);
}

async function saveManualQueueSession(session = null) {
	const tracks = (pageState.queue.tracks || []).filter(t => t && t.source_id);
	if (!tracks.length && !session) {
		await infoDialog({ title: 'Queue is empty', message: 'Play or queue tracks before saving a session.' });
		return;
	}
	const name = await textPrompt({ title: session ? 'Rename session' : 'Save queue session', label: 'Session name', defaultValue: session?.name || defaultQueueName(), confirmLabel: 'Save' });
	if (!name) return;
	sendHelper({
		type: 'music_queue_session_save',
		id: 'qss_' + Math.random().toString(36).slice(2),
		session_id: session?.id,
		name,
		tracks: session?.tracks || tracks,
		index: session?.index || pageState.queue.index || 0,
		is_last: false,
	});
}

function restoreQueueSession(session) {
	if (!session?.tracks?.length) return;
	window.RainetteMusic?.playQueue(session.tracks, Math.max(0, Number(session.index || 0)));
}

async function queueSessionMenu(session) {
	await actionSheet({
		title: session.name || 'Queue session',
		items: [
			{ label: 'Restore session', run: () => restoreQueueSession(session) },
			{ label: 'Save as playlist', run: () => saveSessionAsPlaylist(session) },
			!session.is_last && { label: 'Rename session', run: () => saveManualQueueSession(session) },
			!session.is_last && { label: 'Delete session', danger: true, run: async () => {
				const ok = await confirmDialog({ title: 'Delete session', message: `Delete "${session.name}"?`, confirmLabel: 'Delete', danger: true });
				if (ok) sendHelper({ type: 'music_queue_session_delete', id: 'qsd_' + Math.random().toString(36).slice(2), session_id: session.id });
			} },
		],
	});
}

async function saveSessionAsPlaylist(session) {
	const name = await textPrompt({ title: 'Save session as playlist', label: 'Playlist name', defaultValue: session.name || defaultQueueName(), confirmLabel: 'Save' });
	if (!name) return;
	try {
		const created = await helperRequest('music_playlist_create', { id: 'splq_' + Math.random().toString(36).slice(2), name }, 7000);
		if (!created?.ok || !created?.playlist?.id) throw new Error(created?.msg || 'Could not create playlist');
		for (const track of session.tracks || []) {
			const added = await helperRequest('music_playlist_add_track', { id: 'splqt_' + Math.random().toString(36).slice(2), playlist_id: created.playlist.id, ...queueTrackPayload(track) }, 7000);
			if (!added?.ok) throw new Error(added?.msg || 'Could not add a track');
		}
		sendHelper({ type: 'music_playlist_list', id: 'pll_' + Math.random().toString(36).slice(2) });
	} catch (err) {
		await infoDialog({ title: 'Save failed', message: String(err?.message || err) });
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
			btn('Restore last', 'rw-btn rw-btn-ghost', () => restoreQueueSession(lastQueueSession())),
			btn('Sessions', 'rw-btn rw-btn-ghost', () => { pageState.queueSessionsOpen = !pageState.queueSessionsOpen; renderQueue(); }),
		);
	}
	const save = btn(pageState.queueSaveBusy ? 'Saving...' : 'Save Queue as Playlist', 'rw-btn rw-btn-primary', saveQueueAsPlaylist);
	save.disabled = pageState.queueSaveBusy || !(pageState.queue.tracks || []).length;
	bar.appendChild(save);
	if (!compact) bar.appendChild(btn('Save session', 'rw-btn rw-btn-ghost', () => saveManualQueueSession()));
	return bar;
}

function queueRow(track, index, compact = false) {
	const q = pageState.queue || {};
	const current = index === q.index;
	const row = el('div', 'rw-bubble rw-track-card rw-queue-row' + (current ? ' is-current' : '') + (compact ? ' compact' : ''));
	row.dataset.queueIndex = String(index);
	const grip = el('span', 'rw-queue-grip', '::');
	grip.title = 'Drag to reorder';
	const metaWrap = el('div', 'rw-track-meta');
	const title = el('div', 'rw-track-title', track.title || '(untitled)');
	const parts = [];
	if (current) parts.push('Now playing');
	if (artistName(track)) parts.push(artistName(track));
	if (fmtDuration(track.duration_s)) parts.push(fmtDuration(track.duration_s));
	if (current) {
		// Animated equalizer bars mark the live row; the "Now playing" text above
		// stays as the accessible/textual signal, so the bars are aria-hidden.
		const titleRow = el('div', 'rw-queue-title-row');
		const eq = el('span', 'rw-playing-eq' + (q.playing ? '' : ' paused'));
		eq.setAttribute('aria-hidden', 'true');
		eq.append(el('span'), el('span'), el('span'));
		titleRow.append(eq, title);
		metaWrap.append(titleRow, el('div', 'rw-track-sub', parts.join(' - ')));
	} else {
		metaWrap.append(title, el('div', 'rw-track-sub', parts.join(' - ')));
	}
	const play = iconBtn(current && q.playing ? 'pause' : 'play', 'rw-icon-btn', () => window.RainetteMusic?.queuePlayIndex?.(index), current ? 'Play or pause' : 'Play this track');
	const more = iconBtn('more', 'rw-icon-btn', () => openTrackMenu(track, q.tracks || [], [{
		label: 'Remove from queue',
		danger: true,
		run: () => window.RainetteMusic?.queueRemove?.(index),
	}]), 'More actions');
	const actions = el('div', 'rw-track-actions');
	actions.append(play, more);
	row.append(grip, thumbBox(track.thumbnail_url), metaWrap, actions);
	return row;
}

// Custom pointer-based drag reorder (replaces native HTML5 drag-and-drop,
// which always rendered its own default ghost-image snapshot of the row -
// there is no setDragImage() call anywhere to suppress it). The dragged row
// stays in-flow and tracks the pointer directly via --rw-drag-y (see
// .rw-queue-row.dragging in rainette_pages.css); siblings it crosses shift
// live using the same .flip-move transition the post-drop settle already
// uses in reorderQueueOptimistic(), so there's a single animation system
// instead of two. Commit happens through reorderQueueOptimistic() unchanged.
function wireQueuePointerDrag(list) {
	list.addEventListener('pointerdown', e => {
		const grip = e.target.closest('.rw-queue-grip');
		if (!grip) return;
		const row = grip.closest('.rw-queue-row');
		if (!row) return;
		const rows = [...list.querySelectorAll('.rw-queue-row')];
		const startSlot = rows.indexOf(row);
		if (startSlot < 0) return;
		e.preventDefault();
		const drag = {
			row, rows, startSlot, currentSlot: startSlot,
			rowHeight: row.getBoundingClientRect().height || 1,
			startY: e.clientY, pointerId: e.pointerId, pendingDeltaY: 0, raf: null,
		};
		row.setPointerCapture(e.pointerId);
		row.classList.add('dragging');

		const onMove = ev => {
			if (ev.pointerId !== drag.pointerId) return;
			drag.pendingDeltaY = ev.clientY - drag.startY;
			if (drag.raf) return;
			drag.raf = requestAnimationFrame(() => { drag.raf = null; _applyQueueDragFrame(drag); });
		};
		const onEnd = ev => {
			if (ev.pointerId !== drag.pointerId) return;
			if (drag.raf) cancelAnimationFrame(drag.raf);
			row.removeEventListener('pointermove', onMove);
			row.removeEventListener('pointerup', onEnd);
			row.removeEventListener('pointercancel', onEnd);
			row.releasePointerCapture(drag.pointerId);
			row.classList.remove('dragging');
			row.style.removeProperty('--rw-drag-y');
			drag.rows.forEach(r => { if (r !== row) { r.classList.remove('flip-move'); r.style.transform = ''; } });
			const fromAbs = Number(row.dataset.queueIndex);
			const toRow = drag.rows[drag.currentSlot];
			const toAbs = toRow ? Number(toRow.dataset.queueIndex) : fromAbs;
			if (Number.isInteger(fromAbs) && Number.isInteger(toAbs) && fromAbs !== toAbs) reorderQueueOptimistic(fromAbs, toAbs);
		};
		row.addEventListener('pointermove', onMove);
		row.addEventListener('pointerup', onEnd);
		row.addEventListener('pointercancel', onEnd);
	});
}

// Applies one animation-frame's worth of drag state: moves the dragged row
// with the pointer and, once it crosses a sibling's slot, shifts that
// sibling out of the way (skipped entirely under reduced motion, matching
// reorderQueueOptimistic's own guard).
function _applyQueueDragFrame(drag) {
	drag.row.style.setProperty('--rw-drag-y', drag.pendingDeltaY + 'px');
	const rawSlot = drag.startSlot + Math.round(drag.pendingDeltaY / drag.rowHeight);
	const targetSlot = Math.max(0, Math.min(drag.rows.length - 1, rawSlot));
	if (targetSlot === drag.currentSlot) return;
	drag.currentSlot = targetSlot;
	if (motionDisabled()) return;
	drag.rows.forEach((r, slot) => {
		if (r === drag.row) return;
		let shift = 0;
		if (drag.startSlot < targetSlot && slot > drag.startSlot && slot <= targetSlot) shift = -1;
		else if (drag.startSlot > targetSlot && slot >= targetSlot && slot < drag.startSlot) shift = 1;
		r.classList.add('flip-move');
		r.style.transform = shift ? `translateY(${shift * drag.rowHeight}px)` : '';
	});
}

function _queueSignature(queue) {
	return (queue?.tracks || []).map(t => (t.source || 'youtube') + ':' + (t.source_id || '')).join('|') + '@' + (queue?.index ?? -1);
}

// Inverse of a single splice-move(from -> to): given a row's index *after*
// the move, returns the index it had *before* the move, so FLIP can pair a
// freshly-rendered row with the rect it occupied a moment ago.
function _inverseRemapIndex(newIndex, from, to) {
	if (newIndex === to) return from;
	if (from < to) {
		if (newIndex >= from && newIndex < to) return newIndex + 1;
	} else if (from > to) {
		if (newIndex > to && newIndex <= from) return newIndex - 1;
	}
	return newIndex;
}

// Reorders the queue locally and re-renders immediately (so the move reads
// as an instant snap, not a freeze-then-jump waiting on the relay round trip
// through the detached player window), then plays a FLIP animation from each
// row's old screen position to its new one, and finally sends the real
// queueMove so the player window's authoritative queue stays in sync.
function reorderQueueOptimistic(from, to) {
	const q = pageState.queue;
	if (!q || !Array.isArray(q.tracks) || from < 0 || from >= q.tracks.length || to < 0 || to >= q.tracks.length) {
		window.RainetteMusic?.queueMove?.(from, to);
		return;
	}
	// Queue rows can appear in up to two places at once (the Queue tab body and
	// the queue drawer) - key snapshots by their stable parent container, not
	// by the .rw-queue-list element itself, since renderQueueSurfaces() below
	// destroys and recreates that element (innerHTML='' + rebuild), which would
	// leave a captured .rw-queue-list reference pointing at a detached node.
	const containers = ['#rwMusicBody', '#rwMusicQueueDrawer'].map(sel => _host?.querySelector(sel)).filter(Boolean);
	const beforeRectsByContainer = containers.map(container => {
		const m = new Map();
		container.querySelectorAll('.rw-queue-row').forEach(row => m.set(Number(row.dataset.queueIndex), row.getBoundingClientRect()));
		return [container, m];
	});

	// Mirror miniplayer.js's queueMove(): splice the array, then re-find the
	// current track by identity rather than recomputing its index by hand.
	const tracks = q.tracks.slice();
	const [moved] = tracks.splice(from, 1);
	tracks.splice(to, 0, moved);
	const current = q.tracks[q.index] || null;
	const newIndex = current ? tracks.indexOf(current) : -1;
	pageState.queue = { ...q, tracks, index: newIndex };
	_lastOptimisticQueueSignature = _queueSignature(pageState.queue);

	renderQueueSurfaces();

	if (!motionDisabled()) {
		for (const [container, beforeRects] of beforeRectsByContainer) {
			container.querySelectorAll('.rw-queue-row').forEach(row => {
				const newAbsIndex = Number(row.dataset.queueIndex);
				const oldAbsIndex = _inverseRemapIndex(newAbsIndex, from, to);
				const oldRect = beforeRects.get(oldAbsIndex);
				if (!oldRect) return;
				const newRect = row.getBoundingClientRect();
				const dy = oldRect.top - newRect.top;
				if (Math.abs(dy) < 1) return;
				row.style.transition = 'none';
				row.style.transform = `translateY(${dy}px)`;
				row.classList.add('flip-move');
				requestAnimationFrame(() => {
					requestAnimationFrame(() => {
						row.style.transition = '';
						row.style.transform = '';
						row.addEventListener('transitionend', () => row.classList.remove('flip-move'), { once: true });
					});
				});
			});
		}
	}

	window.RainetteMusic?.queueMove?.(from, to);
}

function queueList(compact = false) {
	const list = el('div', 'rw-track-list rw-queue-list' + (compact ? ' compact' : ''));
	const sourceTracks = pageState.queue.tracks || [];
	const f = filterState('queue');
	const tracks = compact ? sourceTracks : sourceTracks
		.map((track, index) => ({ track, index }))
		.filter(item => textMatch(item.track.title, f.query) || textMatch(artistName(item.track), f.query))
		.map(item => ({ ...item.track, _queueIndex: item.index }));
	if (!tracks.length) list.appendChild(_empty('Queue is empty', 'Play a song to start building Up Next.'));
	for (let i = 0; i < tracks.length; i++) list.appendChild(queueRow(tracks[i], compact ? i : tracks[i]._queueIndex, compact));
	wireQueuePointerDrag(list);
	return list;
}

function renderQueue() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	body.appendChild(queueToolbar(false));
	if (pageState.queueSaveStatus) body.appendChild(el('div', 'rw-status-line', pageState.queueSaveStatus));
	if (pageState.queueSessionStatus) body.appendChild(el('div', 'rw-status-line', pageState.queueSessionStatus));
	body.appendChild(compactFilterToolbar('queue', 'Filter queue', []));
	if (pageState.queueSessionsOpen) body.appendChild(renderQueueSessions());
	body.appendChild(queueList(false));
}

function renderQueueSessions() {
	const wrap = el('div', 'rw-bubble rw-bubble-pad rw-queue-sessions');
	const head = el('div', 'rw-section-title');
	head.append(el('h3', '', 'Sessions'), el('span', '', `${pageState.queueSessions.length}`));
	wrap.appendChild(head);
	const list = el('div', 'rw-session-list');
	if (!pageState.queueSessions.length) list.appendChild(el('div', 'rw-status-line', 'No saved sessions yet.'));
	for (const session of pageState.queueSessions) {
		const row = el('div', 'rw-session-row');
		const meta = el('div', 'rw-track-meta');
		meta.append(el('div', 'rw-track-title', session.name || 'Queue session'), el('div', 'rw-track-sub', `${session.track_count || 0} tracks${session.is_last ? ' - Last session' : ''}`));
		row.append(meta, iconBtn('play', 'rw-icon-btn', () => restoreQueueSession(session), 'Restore session'), iconBtn('more', 'rw-icon-btn', () => queueSessionMenu(session), 'Session actions'));
		list.appendChild(row);
	}
	wrap.appendChild(list);
	return wrap;
}

function renderQueueDrawer() {
	const drawer = _host?.querySelector('#rwMusicQueueDrawer');
	if (!drawer) return;
	drawer.hidden = !QUEUE_SUPPORTED || !pageState.queueDrawerOpen;
	if (drawer.hidden) return;
	drawer.innerHTML = '';
	const head = el('div', 'rw-queue-drawer-head');
	head.append(el('div', 'rw-track-title', 'Queue'), iconBtn('close', 'rw-icon-btn', () => toggleQueueDrawer(false), 'Close queue'));
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
	const mode = el('div', 'rw-segment');
	for (const [id, label] of [['songs', 'Songs'], ['artists', 'Artists'], ['albums', 'Albums']]) {
		const button = document.createElement('button');
		button.type = 'button';
		button.textContent = label;
		button.className = pageState.recentMode === id ? 'on' : '';
		button.addEventListener('click', () => { pageState.recentMode = id; renderRecent(); });
		mode.appendChild(button);
	}
	body.appendChild(compactFilterToolbar('recent', 'Filter recent tracks', [
		['recent', 'Recently played'],
		['title', 'Title'],
		['artist', 'Artist'],
	], [mode]));
	const list = trackList('rwMusicRecent');
	const f = filterState('recent');
	if (pageState.recentMode === 'artists') {
		const artists = recentArtists().filter(artist => textMatch(artist.name, f.query));
		if (f.sort === 'title' || f.sort === 'artist') artists.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
		if (!artists.length) list.appendChild(_empty('No recent artists', pageState.recent.length ? 'Try a different filter.' : 'Play a song to build your history.'));
		for (const artist of artists) list.appendChild(artistCard(artist));
		body.appendChild(list);
		return;
	}
	if (pageState.recentMode === 'albums') {
		const albums = recentAlbums().filter(album => textMatch(album.title, f.query) || textMatch(album.artist, f.query));
		if (f.sort === 'title') albums.sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')));
		else if (f.sort === 'artist') albums.sort((a, b) => String(a.artist || '').localeCompare(String(b.artist || '')));
		if (!albums.length) list.appendChild(_empty('No recent albums', pageState.recent.length ? 'These plays do not include album metadata yet.' : 'Play a song to build your history.'));
		for (const album of albums) list.appendChild(albumCard(album));
		body.appendChild(list);
		return;
	}
	const tracks = [...pageState.recent].filter(t => textMatch(t.title, f.query) || textMatch(artistName(t), f.query));
	if (f.sort === 'title') tracks.sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')));
	else if (f.sort === 'artist') tracks.sort((a, b) => String(artistName(a)).localeCompare(String(artistName(b))));
	if (!tracks.length) list.appendChild(_empty('Nothing found', pageState.recent.length ? 'Try a different filter.' : 'Search and play a track to build your history.'));
	let lastGroup = '';
	for (const track of tracks) {
		const group = recentGroup(track.played_at || track.last_played_at);
		if (group !== lastGroup) {
			lastGroup = group;
			list.appendChild(section(group));
		}
		list.appendChild(trackCard(track, trackActions(track, tracks)));
	}
	body.appendChild(list);
}

function recentArtists() {
	const seen = new Set();
	const artists = [];
	for (const track of pageState.recent) {
		const name = artistName(track);
		if (!name) continue;
		const artist = {
			id: artistId(track),
			artist_id: artistId(track),
			name,
			thumbnail_url: track.thumbnail_url || '',
			last_played_at: track.played_at || track.last_played_at || '',
		};
		const key = artistIdentity(artist);
		if (!key || seen.has(key)) continue;
		seen.add(key);
		artists.push(artist);
	}
	return artists;
}

function recentAlbums() {
	const seen = new Set();
	const albums = [];
	for (const track of pageState.recent) {
		const album = albumInfo(track);
		if (!album?.title) continue;
		const key = String(album.id || `${album.artist}:${album.title}`).toLowerCase();
		if (!key || seen.has(key)) continue;
		seen.add(key);
		albums.push({ ...album, last_played_at: track.played_at || track.last_played_at || '' });
	}
	return albums;
}

function recentGroup(value) {
	const t = Date.parse(value || '');
	if (!Number.isFinite(t)) return 'Earlier';
	const now = new Date();
	const d = new Date(t);
	const day = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
	const itemDay = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
	if (itemDay === day) return 'Today';
	if (itemDay === day - 86400000) return 'Yesterday';
	return 'Earlier';
}

// ── Insights tab ──────────────────────────────────────────────────────────
// Everything here is aggregated locally from music_play_history (see
// state.listening_insights) - no network. Deliberately NOT the big-number
// stat-card wall: one quiet summary strip, the daily-rhythm bar chart, then
// the ranked heavy-rotation lists.

function fetchInsights() {
	pageState.insights.loading = true;
	sendHelper({ type: 'music_insights', id: 'mins_' + Math.random().toString(36).slice(2), days: pageState.insights.days });
}

function insightsWindowLabel(days) {
	return days === 7 ? 'the last 7 days' : days === 30 ? 'the last 30 days' : 'all time';
}

function fmtMinutes(mins) {
	mins = Math.max(0, Math.round(Number(mins) || 0));
	const h = Math.floor(mins / 60);
	return h ? `${h}h ${mins % 60}m` : `${mins}m`;
}

function insightCount(value) {
	const count = Number(value);
	return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
}

function renderInsights() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';

	const seg = el('div', 'rw-segment');
	for (const [days, label] of [[7, '7 days'], [30, '30 days'], [0, 'All time']]) {
		const b = document.createElement('button');
		b.type = 'button';
		b.textContent = label;
		b.className = pageState.insights.days === days ? 'on' : '';
		b.addEventListener('click', () => {
			if (pageState.insights.days === days) return;
			pageState.insights.days = days;
			fetchInsights();
			renderInsights();
		});
		seg.appendChild(b);
	}
	const bar = el('div', 'rw-toolbar');
	bar.appendChild(seg);
	body.appendChild(bar);

	const data = pageState.insights.data;
	if (!data) {
		body.appendChild(el('div', 'rw-status-line', pageState.insights.loading ? 'Crunching your listening history…' : ''));
		return;
	}
	const totalPlays = insightCount(data.total_plays);
	const uniqueTracks = insightCount(data.unique_tracks);
	const uniqueArtists = insightCount(data.unique_artists);
	if (!totalPlays) {
		body.appendChild(_empty('No plays in ' + insightsWindowLabel(pageState.insights.days), 'Play a few tracks and this page starts telling your listening story.'));
		return;
	}

	const scroll = el('div', 'rw-insights-scroll');

	// Quiet one-line summary - reads as a sentence, not a metrics wall.
	const strip = el('div', 'rw-insights-strip');
	const parts = [
		[String(totalPlays), totalPlays === 1 ? 'play' : 'plays'],
		[fmtMinutes(data.total_minutes), 'listened'],
		[String(uniqueTracks), uniqueTracks === 1 ? 'track' : 'tracks'],
		[String(uniqueArtists), uniqueArtists === 1 ? 'artist' : 'artists'],
	];
	parts.forEach(([value, label], i) => {
		if (i) strip.appendChild(el('span', 'rw-insights-strip-sep', '·'));
		const item = el('span', 'rw-insights-strip-item');
		item.append(el('strong', '', value), document.createTextNode(' ' + label));
		strip.appendChild(item);
	});
	scroll.appendChild(section('Your listening', insightsWindowLabel(pageState.insights.days)));
	scroll.appendChild(strip);

	scroll.appendChild(section('Daily rhythm'));
	scroll.appendChild(renderInsightsChart(data.daily || []));

	if (data.top_tracks?.length) {
		scroll.appendChild(section('Heavy rotation', 'by play count'));
		const list = el('div', 'rw-insights-ranked');
		data.top_tracks.forEach((track, i) => {
			const plays = insightCount(track.play_count);
			const row = el('div', 'rw-bubble rw-track-card rw-insights-rank-row');
			const rank = el('span', 'rw-insights-rank', String(i + 1));
			const metaWrap = el('div', 'rw-track-meta');
			metaWrap.append(
				el('div', 'rw-track-title', track.title || '(untitled)'),
				el('div', 'rw-track-sub', [artistName(track), `${plays} play${plays === 1 ? '' : 's'}`].filter(Boolean).join(' · ')),
			);
			const actions = el('div', 'rw-track-actions');
			actions.append(playAction(track, data.top_tracks), iconBtn('more', 'rw-icon-btn', () => openTrackMenu(track, data.top_tracks), 'More actions'));
			row.append(rank, thumbBox(track.thumbnail_url), metaWrap, actions);
			list.appendChild(row);
		});
		scroll.appendChild(list);
	}

	if (data.top_artists?.length) {
		scroll.appendChild(section('Top artists'));
		const list = el('div', 'rw-track-list rw-insights-artists');
		for (const artist of data.top_artists) {
			const plays = insightCount(artist.play_count);
			list.appendChild(artistCard({ ...artist, subscribers: `${plays} play${plays === 1 ? '' : 's'}` }));
		}
		scroll.appendChild(list);
	}

	body.appendChild(scroll);
}

// Single-series magnitude chart: one accent bar per local day, baseline-
// anchored with rounded data-ends, value revealed per-bar on hover (native
// tooltip + aria for screen readers). Peak day gets the one direct label.
function renderInsightsChart(daily) {
	const wrap = el('div', 'rw-bubble rw-bubble-pad rw-insights-chart-card');
	const chart = el('div', 'rw-insights-chart');
	chart.setAttribute('role', 'img');
	const series = (Array.isArray(daily) ? daily : []).map(d => ({ date: String(d?.date || ''), count: insightCount(d?.count) }));
	const max = Math.max(1, ...series.map(d => d.count));
	const total = series.reduce((sum, d) => sum + d.count, 0);
	chart.setAttribute('aria-label', `Plays per day, ${total} total over ${series.length} days`);
	const peakCount = Math.max(0, ...series.map(d => d.count));
	const peakIndex = peakCount > 0 ? series.findIndex(d => d.count === peakCount) : -1;
	series.forEach((d, i) => {
		const col = el('div', 'rw-insights-col');
		const count = d.count;
		const bar = el('div', 'rw-insights-bar' + (count ? '' : ' zero'));
		bar.style.height = count ? Math.max(6, Math.round(count / max * 100)) + '%' : '0%';
		bar.style.setProperty('--rw-bar-i', String(i));
		const date = new Date(d.date + 'T00:00:00');
		const validDate = Number.isFinite(date.getTime());
		const nice = validDate ? date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) : (d.date || 'Unknown day');
		col.title = `${nice} — ${count} play${count === 1 ? '' : 's'}`;
		col.setAttribute('aria-label', col.title);
		col.appendChild(el('span', 'rw-insights-bar-label' + (i === peakIndex ? ' peak' : '') + (!count ? ' zero' : ''), String(count)));
		col.appendChild(bar);
		col.appendChild(el('span', 'rw-insights-day', validDate ? (series.length <= 7
			? date.toLocaleDateString(undefined, { weekday: 'narrow' })
			: (date.getDate() === 1 || date.getDay() === 1 ? String(date.getDate()) : '')) : ''));
		chart.appendChild(col);
	});
	if (!series.length) chart.appendChild(el('div', 'rw-status-line', 'No daily data for this range.'));
	wrap.appendChild(chart);
	return wrap;
}

// A curated overview, not a browsable list - deliberately no filter/sort
// toolbar, unlike every other tab. Stacks existing card/list components
// (trackCard/playlistCard/artistCard) over data already fetched elsewhere
// (recent, playlists) plus one new local aggregate (top artists).
function renderHome() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';

	const current = currentQueueTrack();
	if (current) {
		body.appendChild(section('Continue listening'));
		const list = trackList();
		list.appendChild(trackCard(current, trackActions(current, pageState.queue.tracks?.length ? pageState.queue.tracks : [current])));
		body.appendChild(list);
	}

	body.appendChild(section('Recently played'));
	const recentTracks = pageState.recent.slice(0, 10);
	if (recentTracks.length) {
		const list = trackList();
		for (const track of recentTracks) list.appendChild(trackCard(track, trackActions(track, recentTracks)));
		body.appendChild(list);
	} else {
		body.appendChild(_empty('Nothing played yet', 'Search and play a track to start building your history.'));
	}

	body.appendChild(section('Your playlists'));
	const playlists = [...pageState.playlists]
		.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || dateValue(b.updated_at) - dateValue(a.updated_at))
		.slice(0, 8);
	if (playlists.length) {
		const list = trackList();
		for (const pl of playlists) list.appendChild(playlistCard(pl));
		body.appendChild(list);
	} else {
		body.appendChild(_empty('No playlists yet', 'Create a playlist to see it here.'));
	}

	body.appendChild(section('Top artists'));
	if (pageState.topArtists.length) {
		const list = trackList();
		for (const artist of pageState.topArtists) {
			const plays = insightCount(artist.play_count);
			list.appendChild(artistCard({ ...artist, subscribers: `${plays} play${plays === 1 ? '' : 's'}` }));
		}
		body.appendChild(list);
	} else {
		body.appendChild(_empty('No listening history yet', 'Play a few tracks to see your top artists.'));
	}
}

function openCommandPalette() {
	pageState.palette.open = true;
	pageState.palette.query = '';
	pageState.palette.selected = 0;
	pageState.palette.catalog = { songs: [], artists: [], albums: [] };
	pageState.palette.status = '';
	pageState.palette.lastFocus = document.activeElement;
	renderCommandPalette();
	setTimeout(() => _host?.querySelector('#rwCommandInput')?.focus(), 0);
}

function closeCommandPalette() {
	pageState.palette.open = false;
	const palette = _host?.querySelector('#rwCommandPalette');
	if (palette) { palette.hidden = true; palette.innerHTML = ''; }
	const focus = pageState.palette.lastFocus;
	if (focus && typeof focus.focus === 'function') setTimeout(() => focus.focus(), 0);
}

function paletteCommands() {
	const q = pageState.palette.query.trim().toLowerCase();
	const commands = [];
	for (const id of navItems()) {
		commands.push({ label: TAB_META[id].label, hint: 'Go to tab', run: () => { pageState.tab = id; pageState.view = null; pageState.openPlaylist = null; applyTab(); } });
	}
	if (QUEUE_SUPPORTED) {
		commands.push({ label: 'Open queue drawer', hint: queueSummary(), run: () => toggleQueueDrawer(true) });
		commands.push({ label: 'Save queue session', hint: 'Preserve Up Next', run: () => saveManualQueueSession() });
		const last = lastQueueSession();
		if (last) commands.push({ label: 'Restore last session', hint: `${last.track_count || 0} tracks`, run: () => restoreQueueSession(last) });
	}
	commands.push({ label: 'Keyboard shortcuts', hint: 'Press ?', run: () => toggleShortcutsView(true) });
	for (const pl of pageState.playlists.slice(0, 12)) commands.push({ label: pl.name, hint: pl.kind === 'smart' ? 'Smart playlist' : 'Playlist', run: () => { pageState.tab = 'playlists'; openPlaylist(pl); } });
	for (const track of pageState.recent.slice(0, 8)) commands.push({ label: track.title || '(untitled)', hint: artistName(track) || 'Recent track', run: () => window.RainetteMusic?.playTrack(track) });
	for (const artist of pageState.palette.catalog.artists) commands.push({ label: artist.name, hint: 'Catalog artist', run: () => openArtist(artist) });
	for (const album of pageState.palette.catalog.albums) commands.push({ label: album.title || album.name, hint: album.artist || 'Catalog album', run: () => openAlbum(album) });
	for (const track of pageState.palette.catalog.songs) commands.push({ label: track.title || '(untitled)', hint: artistName(track) || 'Catalog song', run: () => window.RainetteMusic?.playTrack(track) });
	return commands.filter(c => !q || textMatch(c.label, q) || textMatch(c.hint, q));
}

let _paletteDebounce = null;
function updatePaletteQuery(value) {
	pageState.palette.query = value;
	pageState.palette.selected = 0;
	clearTimeout(_paletteDebounce);
	if (value.trim().length >= 2) {
		pageState.palette.status = 'Searching catalog...';
		_paletteDebounce = setTimeout(async () => {
			const id = 'pcat_' + Math.random().toString(36).slice(2);
			const result = await helperRequest('music_catalog_search', { id, query: pageState.palette.query.trim() }, 9000);
			if (!pageState.palette.open) return;
			if (result?.ok) pageState.palette.catalog = { songs: result.songs || [], artists: result.artists || [], albums: result.albums || [] };
			pageState.palette.status = result?.ok ? '' : ('Search failed: ' + (result?.msg || ''));
			renderPaletteList();
		}, 250);
	} else {
		pageState.palette.catalog = { songs: [], artists: [], albums: [] };
		pageState.palette.status = '';
	}
	// Only the results list is rebuilt here — the input keeps its own DOM node
	// (and caret position), unlike the old renderCommandPalette()-on-every-
	// keystroke approach.
	renderPaletteList();
}

function runPaletteSelection() {
	const commands = paletteCommands();
	const cmd = commands[Math.max(0, Math.min(pageState.palette.selected, commands.length - 1))];
	if (!cmd) return;
	closeCommandPalette();
	cmd.run();
}

// Rebuilds the whole panel (input + list container). Called only on genuine
// open/close, never on keystroke/hover — see renderPaletteList()/
// setPaletteSelection() for the parts that change during interaction.
function renderCommandPalette() {
	const palette = _host?.querySelector('#rwCommandPalette');
	if (!palette) return;
	palette.hidden = !pageState.palette.open;
	if (!pageState.palette.open) { palette.innerHTML = ''; return; }
	palette.innerHTML = '';
	const panel = el('div', 'rw-command-panel');
	const input = document.createElement('input');
	input.id = 'rwCommandInput';
	input.className = 'rw-input';
	input.type = 'search';
	input.placeholder = 'Search commands, playlists, songs...';
	input.value = pageState.palette.query;
	input.addEventListener('input', e => updatePaletteQuery(e.target.value));
	input.addEventListener('keydown', e => {
		if (e.key === 'Escape') { e.preventDefault(); closeCommandPalette(); }
		else if (e.key === 'ArrowDown') { e.preventDefault(); movePaletteSelection(1); }
		else if (e.key === 'ArrowUp') { e.preventDefault(); movePaletteSelection(-1); }
		else if (e.key === 'Enter') { e.preventDefault(); runPaletteSelection(); }
	});
	panel.appendChild(input);
	panel.appendChild(el('div', 'rw-status-line rw-command-status'));
	panel.appendChild(el('div', 'rw-command-list'));
	palette.appendChild(panel);
	renderPaletteList();
}

// Rebuilds only the status line + result rows, leaving the <input> (and its
// focus/caret position) untouched. Safe to call on every keystroke.
function renderPaletteList() {
	const status = _host?.querySelector('#rwCommandPalette .rw-command-status');
	const list = _host?.querySelector('#rwCommandPalette .rw-command-list');
	if (!list) return;
	const commands = paletteCommands();
	if (status) {
		status.hidden = !pageState.palette.status;
		status.textContent = pageState.palette.status || '';
	}
	list.innerHTML = '';
	if (!commands.length) list.appendChild(el('div', 'rw-status-line', 'No matches.'));
	commands.forEach((cmd, i) => {
		const b = document.createElement('button');
		b.type = 'button';
		b.className = 'rw-command-item' + (i === pageState.palette.selected ? ' on' : '');
		b.innerHTML = `<span>${cmd.label}</span><small>${cmd.hint || ''}</small>`;
		b.addEventListener('mouseenter', () => setPaletteSelection(i));
		b.addEventListener('click', () => { pageState.palette.selected = i; runPaletteSelection(); });
		list.appendChild(b);
	});
}

// Pure class-toggle among the already-rendered rows — this is what used to be
// a full renderCommandPalette() call on every mouseenter, which destroyed and
// recreated the node under the cursor and made the list unclickable.
function setPaletteSelection(i) {
	pageState.palette.selected = i;
	_host?.querySelectorAll('#rwCommandPalette .rw-command-item').forEach((b, idx) => b.classList.toggle('on', idx === i));
}

function movePaletteSelection(delta) {
	const commands = paletteCommands();
	if (!commands.length) return;
	const next = Math.max(0, Math.min(commands.length - 1, pageState.palette.selected + delta));
	setPaletteSelection(next);
	_host?.querySelectorAll('#rwCommandPalette .rw-command-item')[next]?.scrollIntoView({ block: 'nearest' });
}

function bindCommandPaletteKeys() {
	if (_paletteKeysBound) return;
	_paletteKeysBound = true;
	document.addEventListener('keydown', e => {
		const isPalette = (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k';
		if (isPalette) {
			e.preventDefault();
			if (pageState.palette.open) closeCommandPalette();
			else openCommandPalette();
		} else if (pageState.palette.open && e.key === 'Escape') {
			e.preventDefault();
			closeCommandPalette();
		}
	});
}

// ── Now Playing view (docked-bar mode only) ──────────────────────────────
// Opened by clicking the docked bar's art/title area, which lives in
// rainette_music_player.js - a separately-booted module that can't import
// this one, so it dispatches a DOM event instead. Structurally modeled on
// the command palette: a fixed full-viewport overlay, built fresh on open,
// re-rendered on every 'music_now_playing' broadcast so it stays live while
// open (see onHelperMessage's music_now_playing case).

function currentQueueTrack() {
	const q = pageState.queue || {};
	return (q.tracks || [])[q.index] ?? null;
}

// ── Ambient art color ─────────────────────────────────────────────────────
// Samples the current cover art on a tiny canvas and tints the Now Playing
// panel with the dominant color (Apple-Music-style atmosphere). Thumbnails
// come from ytimg, which serves CORS headers; if a host doesn't, the canvas
// taints, we catch the SecurityError, and the panel just keeps its neutral
// surface. Results are cached per track.
const _ambientCache = new Map();

function _applyAmbientToPanel() {
	const panel = _host?.querySelector('#rwNowPlayingView .rw-now-view-panel');
	if (!panel) return;
	const sid = currentQueueTrack()?.source_id || '';
	const color = sid ? _ambientCache.get(sid) : '';
	if (color) panel.style.setProperty('--rw-np-ambient', color);
	else panel.style.removeProperty('--rw-np-ambient');
}

function sampleAmbientColor(track) {
	const sid = track?.source_id || '';
	const url = track?.thumbnail_url || '';
	if (!sid || !url) return;
	if (_ambientCache.has(sid)) { _applyAmbientToPanel(); return; }
	const img = new Image();
	img.crossOrigin = 'anonymous';
	img.onload = () => {
		try {
			const size = 24;
			const canvas = document.createElement('canvas');
			canvas.width = size;
			canvas.height = size;
			const ctx = canvas.getContext('2d', { willReadFrequently: true });
			ctx.drawImage(img, 0, 0, size, size);
			const data = ctx.getImageData(0, 0, size, size).data;
			// Weighted average biased toward saturated mid-lightness pixels, so
			// the wash picks up the artwork's character, not its black bars.
			let r = 0, g = 0, b = 0, w = 0;
			for (let i = 0; i < data.length; i += 4) {
				const pr = data[i], pg = data[i + 1], pb = data[i + 2];
				const mx = Math.max(pr, pg, pb), mn = Math.min(pr, pg, pb);
				const midLight = mx + mn > 70 && mx + mn < 460;
				const weight = 1 + (mx - mn) * (midLight ? 3 : 0.4) / 255 * 8;
				r += pr * weight; g += pg * weight; b += pb * weight; w += weight;
			}
			_ambientCache.set(sid, w ? `rgb(${Math.round(r / w)} ${Math.round(g / w)} ${Math.round(b / w)})` : '');
		} catch {
			_ambientCache.set(sid, '');   // tainted canvas (no CORS) - no tint
		}
		_applyAmbientToPanel();
	};
	img.onerror = () => _ambientCache.set(sid, '');
	img.src = url;
}

let _nowViewEntered = false;

function openNowPlayingView() {
	pageState.nowPlayingOpen = true;
	_nowViewEntered = false;   // play the entrance once per open, not per re-render
	renderNowPlayingView();
	if (pageState.lyrics.open) fetchLyricsForCurrent();
}

function closeNowPlayingView() {
	pageState.nowPlayingOpen = false;
	const view = _host?.querySelector('#rwNowPlayingView');
	if (view) { view.hidden = true; view.innerHTML = ''; }
}

function renderNowPlayingView() {
	const view = _host?.querySelector('#rwNowPlayingView');
	if (!view) return;
	view.hidden = !pageState.nowPlayingOpen;
	if (!pageState.nowPlayingOpen) { view.innerHTML = ''; return; }

	const track = currentQueueTrack();
	const q = pageState.queue || {};
	view.innerHTML = '';
	const panel = el('div', 'rw-now-view-panel' + (pageState.lyrics.open ? ' lyrics-open' : ''));
	// Entrance plays only on the hidden→open transition - this render also runs
	// on every now-playing broadcast while open, which must not re-animate.
	if (!_nowViewEntered) {
		_nowViewEntered = true;
		if (!motionDisabled()) panel.classList.add('rw-now-enter');
	}

	panel.appendChild(iconBtn('close', 'rw-icon-btn rw-now-view-close', closeNowPlayingView, 'Close'));

	// Left column: art + metadata + transport. Right column (when lyrics
	// open): scrollable lyrics. On narrow widths they stack (CSS).
	const main = el('div', 'rw-now-view-main');

	const artShell = el('div', 'rw-now-view-art-shell' + (track?.thumbnail_url ? ' has-art' : ''));
	if (track?.thumbnail_url) {
		const img = document.createElement('img');
		img.className = 'rw-now-view-art';
		img.src = track.thumbnail_url;
		img.alt = '';
		artShell.appendChild(img);
	} else {
		artShell.appendChild(el('span', 'rw-now-view-note', '♪'));
	}
	main.appendChild(artShell);
	main.appendChild(el('div', 'rw-now-view-title', track?.title || 'Nothing playing'));
	main.appendChild(el('div', 'rw-now-view-artist', track ? (artistName(track) || '') : 'Search and press play'));

	// Seek bar + time labels (synced via pageState.progress / updateProgressUi).
	const seekRow = el('div', 'rw-now-view-seek-row');
	seekRow.appendChild(el('span', 'rw-now-view-time rw-now-view-time-cur', fmtClock(pageState.progress.current_time)));
	const seek = el('div', 'rw-now-view-seek');
	seek.setAttribute('role', 'slider');
	seek.setAttribute('aria-label', 'Seek');
	seek.appendChild(el('div', 'rw-now-view-seek-fill'));
	wireSeekBar(seek);
	seekRow.appendChild(seek);
	seekRow.appendChild(el('span', 'rw-now-view-time rw-now-view-time-dur', fmtClock(pageState.progress.duration)));
	main.appendChild(seekRow);

	const transport = el('div', 'rw-now-view-transport');
	const shuffleBtn = iconBtn('shuffle', 'rw-now-view-btn', () => {
		window.RainetteMusic?.queueShuffle?.();
		shuffleBtn.classList.remove('pulse');
		void shuffleBtn.offsetWidth; // restart the animation on repeated clicks
		shuffleBtn.classList.add('pulse');
	}, 'Shuffle queue');
	const prevBtn = iconBtn('prev', 'rw-now-view-btn', () => window.RainetteMusic?.prev?.(), 'Previous');
	const playBtn = iconBtn(q.playing ? 'pause' : 'play', 'rw-now-view-btn rw-now-view-play', () => window.RainetteMusic?.toggle?.(), 'Play or pause');
	const nextBtn = iconBtn('next', 'rw-now-view-btn', () => window.RainetteMusic?.next?.(), 'Next');
	const loopBtn = iconBtn('loop', 'rw-now-view-btn' + (q.loop ? ' on' : ''), () => window.RainetteMusic?.toggleLoop?.(), 'Toggle loop');
	transport.append(shuffleBtn, prevBtn, playBtn, nextBtn, loopBtn);
	main.appendChild(transport);

	const volRow = el('div', 'rw-now-view-volume');
	const volIcon = el('span', 'rw-now-view-vol-icon');
	volIcon.innerHTML = iconMarkup('volume', 16);
	const volSlider = document.createElement('input');
	volSlider.type = 'range';
	volSlider.className = 'rw-now-view-vol-slider';
	volSlider.min = '0';
	volSlider.max = '150';
	volSlider.step = '1';
	volSlider.setAttribute('aria-label', 'Volume');
	// The slider value IS the percentage (100 = unity, 150 = the engine's max
	// boost) - same scale as the Settings default-volume slider, so there's no
	// dead zone at the top and no surprise 45%-looking "full" volume.
	volSlider.value = String(Math.round((window.RainetteMusic?.getVolume?.() ?? 1) * 100));
	volSlider.addEventListener('input', () => window.RainetteMusic?.setVolume?.(Number(volSlider.value) / 100));
	volRow.append(volIcon, volSlider);
	main.appendChild(volRow);

	const actionsRow = el('div', 'rw-now-view-actions');
	actionsRow.appendChild(labeledAction('listPlay', 'Queue', () => { closeNowPlayingView(); toggleQueueDrawer(true); }));
	actionsRow.appendChild(labeledAction('listAdd', 'Add to playlist', () => { if (track) openAddToPlaylist(track); }));
	actionsRow.appendChild(labeledAction('mic', pageState.lyrics.open ? 'Hide lyrics' : 'Lyrics', () => toggleLyrics(), pageState.lyrics.open));
	const sleepBtn = labeledAction('moon', sleepTimerLabel(), openSleepTimerMenu, sleepTimerActive());
	sleepBtn.id = 'rwSleepAction';
	actionsRow.appendChild(sleepBtn);
	actionsRow.appendChild(labeledAction('more', 'More', () => { if (track) openTrackMenu(track, q.tracks || []); }));
	main.appendChild(actionsRow);

	panel.appendChild(main);

	if (pageState.lyrics.open) {
		const lyricsCol = el('div', 'rw-now-view-lyrics');
		lyricsCol.id = 'rwLyricsPanel';
		panel.appendChild(lyricsCol);
	}

	view.appendChild(panel);
	if (pageState.lyrics.open) renderLyricsPanel();
	if (track) { _applyAmbientToPanel(); sampleAmbientColor(track); }
	updateProgressUi();
}

// An icon button with a small label underneath — for the Now Playing view's
// primary actions (queue / add-to-playlist / lyrics / more).
function labeledAction(icon, label, onClick, active = false) {
	const b = document.createElement('button');
	b.type = 'button';
	b.className = 'rw-now-view-action' + (active ? ' on' : '');
	b.title = label;
	b.setAttribute('aria-label', label);
	b.innerHTML = `<span class="rw-now-view-action-ico">${iconMarkup(icon, 18)}</span><span class="rw-now-view-action-label">${label}</span>`;
	b.addEventListener('click', onClick);
	return b;
}

// ── Sleep timer ───────────────────────────────────────────────────────────
// Engine-agnostic: pauses through window.RainetteMusic when it fires, so it
// works with both the detached player window and the docked fallback engine.
const sleepState = { until: 0, endOfTrack: false, timerId: null };

function sleepTimerActive() {
	return sleepState.endOfTrack || sleepState.until > Date.now();
}

function sleepTimerLabel() {
	if (sleepState.endOfTrack) return 'After track';
	if (sleepState.until > Date.now()) {
		const mins = Math.max(1, Math.ceil((sleepState.until - Date.now()) / 60000));
		return `Sleep ${mins}m`;
	}
	return 'Sleep';
}

function cancelSleepTimer() {
	clearTimeout(sleepState.timerId);
	sleepState.timerId = null;
	sleepState.until = 0;
	sleepState.endOfTrack = false;
	updateSleepUi();
}

function startSleepTimer(minutes) {
	cancelSleepTimer();
	sleepState.until = Date.now() + minutes * 60000;
	sleepState.timerId = setTimeout(fireSleepTimer, minutes * 60000);
	updateSleepUi();
}

function fireSleepTimer() {
	cancelSleepTimer();
	if (window.RainetteMusic?.isPlaying?.()) window.RainetteMusic.toggle?.();
}

async function openSleepTimerMenu() {
	const items = [15, 30, 45, 60].map(mins => ({
		label: `Stop in ${mins} minutes`,
		run: () => startSleepTimer(mins),
	}));
	items.push({ label: 'Stop after this track', run: () => { cancelSleepTimer(); sleepState.endOfTrack = true; updateSleepUi(); } });
	if (sleepTimerActive()) items.push({ label: 'Cancel sleep timer', danger: true, run: cancelSleepTimer });
	await actionSheet({ title: 'Sleep timer', items });
}

// Refreshes just the action button (label countdown + active state) without a
// full Now Playing re-render - called on every progress tick.
function updateSleepUi() {
	const b = _host?.querySelector('#rwSleepAction');
	if (!b) return;
	b.classList.toggle('on', sleepTimerActive());
	const label = b.querySelector('.rw-now-view-action-label');
	if (label) label.textContent = sleepTimerLabel();
	b.title = sleepState.endOfTrack ? 'Stops when this track ends' : 'Sleep timer';
}

function toggleLyrics() {
	pageState.lyrics.open = !pageState.lyrics.open;
	if (pageState.lyrics.open) fetchLyricsForCurrent();
	renderNowPlayingView();
}

function fetchLyricsForCurrent() {
	const track = currentQueueTrack();
	if (!track) { pageState.lyrics = { ...pageState.lyrics, source_id: '', text: '', notFound: true, loading: false, synced: false, lines: [], activeIndex: -1 }; return; }
	if (pageState.lyrics.source_id === track.source_id && (pageState.lyrics.text || pageState.lyrics.notFound)) return; // cached
	const reqId = 'lyr_' + Math.random().toString(36).slice(2);
	pageState.lyrics = { ...pageState.lyrics, source_id: track.source_id, reqId, loading: true, text: '', notFound: false, instrumental: false, error: '', synced: false, lines: [], activeIndex: -1, userScrollUntil: 0 };
	sendHelper({ type: 'music_lyrics', id: reqId, track });
}

// Parses LRC-format synced lyrics ("[mm:ss.xx]line text", possibly several
// timestamp tags per line for a repeated chorus) into a time-sorted list.
// Returns [] for malformed/missing input - the caller falls back to the
// plain-text rendering in that case, since LRCLIB doesn't have synced
// lyrics for every track.
function parseSyncedLyrics(lrc) {
	if (!lrc) return [];
	const tagRe = /\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]/g;
	const lines = [];
	for (const rawLine of lrc.split('\n')) {
		const tags = [...rawLine.matchAll(tagRe)];
		if (!tags.length) continue;
		const text = rawLine.replace(tagRe, '').trim();
		for (const m of tags) {
			const time = Number(m[1]) * 60 + Number(m[2]) + (m[3] ? Number('0.' + m[3]) : 0);
			lines.push({ time, text });
		}
	}
	lines.sort((a, b) => a.time - b.time);
	return lines;
}

function renderLyricsPanel() {
	const panel = _host?.querySelector('#rwLyricsPanel');
	if (!panel) return;
	panel.innerHTML = '';
	panel.classList.toggle('is-manual-scroll', Date.now() < pageState.lyrics.userScrollUntil);
	_lyricsLineEls = [];
	const l = pageState.lyrics;
	if (l.loading) { panel.appendChild(el('div', 'rw-now-view-lyrics-status', 'Loading lyrics…')); return; }
	if (l.error) { panel.appendChild(el('div', 'rw-now-view-lyrics-status', l.error)); return; }
	if (l.instrumental) { panel.appendChild(el('div', 'rw-now-view-lyrics-status', 'Instrumental — no lyrics.')); return; }
	if (l.notFound || (!l.text && !l.lines.length)) { panel.appendChild(el('div', 'rw-now-view-lyrics-status', 'No lyrics found for this track.')); return; }
	const body = el('div', 'rw-now-view-lyrics-body');
	if (l.synced && l.lines.length) {
		l.lines.forEach((line, i) => {
			const p = el('p', 'rw-now-view-lyrics-line', line.text || ' ');
			p.dataset.lyricsIndex = String(i);
			p.addEventListener('click', () => {
				const dur = pageState.progress.duration;
				if (dur > 0) window.RainetteMusic?.seek?.(Math.min(1, Math.max(0, line.time / dur)));
			});
			body.appendChild(p);
			_lyricsLineEls.push(p);
		});
	} else {
		for (const line of l.text.split('\n')) {
		body.appendChild(el('p', 'rw-now-view-lyrics-line', line || ' '));
	}
	}
	panel.appendChild(body);
	wireLyricsAutoScrollGuard(panel);
	if (l.synced && l.lines.length) updateLyricsHighlight(true);
}

// Manual scrolling always wins: any wheel/pointer interaction on the lyrics
// panel suspends auto-scroll-following for a few seconds, extended on each
// further interaction, so it only resumes once the user actually stops.
function wireLyricsAutoScrollGuard(panel) {
	if (panel.dataset.scrollGuardWired) return;
	panel.dataset.scrollGuardWired = '1';
	const suspend = () => {
		pageState.lyrics.userScrollUntil = Date.now() + 3000;
		panel.classList.add('is-manual-scroll');
		clearTimeout(_lyricsManualTimer);
		_lyricsManualTimer = setTimeout(() => panel.classList.remove('is-manual-scroll'), 3050);
	};
	panel.addEventListener('wheel', suspend, { passive: true });
	panel.addEventListener('pointerdown', suspend);
}

// Finds the current line from pageState.progress.current_time (already kept
// live by updateProgressUi's callers) and highlights + auto-scrolls it,
// unless the user is actively/recently scrolling the panel themselves.
function updateLyricsHighlight(forceScroll = false) {
	const l = pageState.lyrics;
	if (!l.open || !l.synced || !l.lines.length || !_lyricsLineEls.length) return;
	const t = pageState.progress.current_time || 0;
	let idx = -1;
	for (let i = 0; i < l.lines.length; i++) {
		if (l.lines[i].time <= t) idx = i; else break;
	}
	if (idx === l.activeIndex && !forceScroll) return;
	const prevEl = _lyricsLineEls[l.activeIndex];
	if (prevEl) prevEl.classList.remove('is-current');
	l.activeIndex = idx;
	const activeEl = _lyricsLineEls[idx];
	if (!activeEl) return;
	activeEl.classList.add('is-current');
	if (forceScroll || Date.now() >= l.userScrollUntil) {
		const panel = _host?.querySelector('#rwLyricsPanel');
		panel?.classList.remove('is-manual-scroll');
		if (panel) {
			const top = activeEl.offsetTop - panel.clientHeight / 2 + activeEl.offsetHeight / 2;
			panel.scrollTo({ top: Math.max(0, top), behavior: motionDisabled() ? 'auto' : 'smooth' });
		}
	}
}

// ── Global keyboard shortcuts + cheatsheet ────────────────────────────────
// Transport hotkeys never fire while the user is typing, while a modal or the
// palette is open, or while an interactive control has focus (Space there
// means "click the focused button", not "toggle playback").

let _shortcutsOpen = false;
let _globalKeysBound = false;

function shortcutRows() {
	const rows = [
		[['Space'], 'Play or pause'],
		[['←', '→'], 'Seek back / forward 5 seconds'],
		[['Ctrl', '←/→'], 'Previous / next track'],
		[['L'], 'Toggle loop'],
	];
	if (QUEUE_SUPPORTED) rows.push([['Q'], 'Open or close the queue drawer']);
	rows.push([['Ctrl', 'K'], 'Command palette']);
	rows.push([['?'], 'This cheatsheet']);
	rows.push([['Esc'], 'Close overlays']);
	return rows;
}

function toggleShortcutsView(open = !_shortcutsOpen) {
	_shortcutsOpen = !!open;
	const view = _host?.querySelector('#rwShortcutsView');
	if (!view) return;
	view.hidden = !_shortcutsOpen;
	view.innerHTML = '';
	if (!_shortcutsOpen) return;
	const panel = el('div', 'rw-shortcuts-panel');
	const head = el('div', 'rw-shortcuts-head');
	head.append(el('h3', '', 'Keyboard shortcuts'), iconBtn('close', 'rw-icon-btn', () => toggleShortcutsView(false), 'Close'));
	panel.appendChild(head);
	const list = el('div', 'rw-shortcuts-list');
	for (const [keys, desc] of shortcutRows()) {
		const row = el('div', 'rw-shortcut-row');
		const keyWrap = el('span', 'rw-shortcut-keys');
		keys.forEach((token, i) => {
			if (i) keyWrap.appendChild(el('span', 'rw-shortcut-plus', '+'));
			keyWrap.appendChild(el('kbd', '', token));
		});
		row.append(keyWrap, el('span', 'rw-shortcut-desc', desc));
		list.appendChild(row);
	}
	panel.appendChild(list);
	view.appendChild(panel);
}

function bindGlobalShortcuts() {
	if (_globalKeysBound) return;
	_globalKeysBound = true;
	document.addEventListener('keydown', e => {
		if (e.defaultPrevented || pageState.palette.open) return;
		const t = e.target;
		if (t?.closest?.('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) return;
		if (document.querySelector('.rw-modal-backdrop')) return;
		if (e.key === '?' && !e.ctrlKey && !e.metaKey) { e.preventDefault(); toggleShortcutsView(); return; }
		if (e.key === 'Escape' && _shortcutsOpen) { e.preventDefault(); toggleShortcutsView(false); return; }
		if (t?.closest?.('button, a, [role="slider"], .rh-selectx')) return;
		const rm = window.RainetteMusic;
		if (!rm) return;
		if (e.code === 'Space') {
			if (pageState.playbackStarted || (pageState.queue.tracks || []).length) { e.preventDefault(); rm.toggle?.(); }
			return;
		}
		if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
			const dir = e.key === 'ArrowRight' ? 1 : -1;
			if (e.ctrlKey || e.metaKey) { e.preventDefault(); (dir > 0 ? rm.next : rm.prev)?.call(rm); return; }
			const dur = pageState.progress.duration;
			if (dur > 0) { e.preventDefault(); rm.seek?.(Math.max(0, Math.min(1, (pageState.progress.current_time + dir * 5) / dur))); }
			return;
		}
		if ((e.key === 'l' || e.key === 'L') && !e.ctrlKey && !e.metaKey) { e.preventDefault(); rm.toggleLoop?.(); return; }
		if ((e.key === 'q' || e.key === 'Q') && !e.ctrlKey && !e.metaKey && QUEUE_SUPPORTED) { e.preventDefault(); toggleQueueDrawer(); return; }
	});
}

function bindNowPlayingKeys() {
	if (_nowPlayingKeysBound) return;
	_nowPlayingKeysBound = true;
	document.addEventListener('rainette:open-now-playing', openNowPlayingView);
	document.addEventListener('keydown', e => {
		if (pageState.nowPlayingOpen && e.key === 'Escape') { e.preventDefault(); closeNowPlayingView(); }
	});
}

// ── Persistent docked bar (main window) ──────────────────────────────────
// Always-present bottom transport in the main window whenever something is
// playing - engine-agnostic (commands via window.RainetteMusic, state from
// music_now_playing / music_progress broadcasts), so it works identically
// whether the engine is the floating miniplayer (popout on) or the headless
// local engine (popout off). The floating miniplayer, when on, is an
// additional synced surface on top of this - not a replacement.

function fmtClock(s) {
	s = Math.max(0, Math.floor(Number(s) || 0));
	const m = Math.floor(s / 60);
	return m + ':' + String(s % 60).padStart(2, '0');
}

// Pointer-drag seek on a bar element; reports a 0..1 ratio to RainetteMusic.seek.
// Also keyboard-operable (role="slider" contract): focusable, arrows nudge ±5s,
// Home/End jump to the track edges.
function wireSeekBar(seekEl) {
	let pointerId = null;
	const ratioAt = clientX => {
		const r = seekEl.getBoundingClientRect();
		return Math.max(0, Math.min(1, (clientX - r.left) / Math.max(1, r.width)));
	};
	seekEl.tabIndex = 0;
	seekEl.setAttribute('aria-valuemin', '0');
	seekEl.addEventListener('keydown', e => {
		const dur = pageState.progress.duration;
		if (!(dur > 0)) return;
		let target = null;
		if (e.key === 'ArrowRight' || e.key === 'ArrowUp') target = pageState.progress.current_time + 5;
		else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') target = pageState.progress.current_time - 5;
		else if (e.key === 'Home') target = 0;
		else if (e.key === 'End') target = dur - 1;
		if (target == null) return;
		e.preventDefault();
		window.RainetteMusic?.seek?.(Math.max(0, Math.min(1, target / dur)));
	});
	seekEl.addEventListener('pointerdown', e => {
		pointerId = e.pointerId;
		// Seek first, then try to capture - a failed capture (e.g. a synthetic
		// event with no valid pointerId) must not swallow the seek.
		window.RainetteMusic?.seek?.(ratioAt(e.clientX));
		try { seekEl.setPointerCapture?.(e.pointerId); } catch { /* ignore */ }
		e.preventDefault();
	});
	seekEl.addEventListener('pointermove', e => { if (pointerId === e.pointerId) window.RainetteMusic?.seek?.(ratioAt(e.clientX)); });
	const end = e => { if (pointerId === e.pointerId) { seekEl.releasePointerCapture?.(e.pointerId); pointerId = null; } };
	seekEl.addEventListener('pointerup', end);
	seekEl.addEventListener('pointercancel', end);
}

let _dockedBarBuilt = false;
let _dockedBarShown = false;
function ensureDockedBar() {
	const bar = _host?.querySelector('#rwDockedBar');
	if (!bar || _dockedBarBuilt) return bar;
	_dockedBarBuilt = true;
	// A pop-out button appears only when the floating miniplayer is enabled -
	// it re-reveals that window if it was hidden, without disturbing this bar.
	const popoutBtn = window.RW_MINIPLAYER_ENABLED
		? `<button class="rw-now-btn rw-now-popout" data-act="popout" title="Pop out player" aria-label="Pop out floating player">${iconMarkup('chevronUp', 16)}</button>`
		: '';
	bar.innerHTML = `
		<div class="rw-now-seek" role="slider" aria-label="Seek"><div class="rw-now-seek-fill"></div></div>
		<div class="rw-now-bar-row">
			<button class="rw-now-bar-meta" type="button" title="Open Now Playing">
				<div class="rw-now-art-shell">
					<img class="rw-now-art" alt="">
					<span class="rw-now-note" aria-hidden="true">&#9835;</span>
				</div>
				<div class="rw-now-text">
					<div class="rw-now-title">—</div>
					<div class="rw-now-artist"></div>
				</div>
			</button>
			<div class="rw-now-controls">
				<button class="rw-now-btn" data-act="prev" title="Previous" aria-label="Previous">${iconMarkup('prev', 16)}</button>
				<button class="rw-now-btn rw-now-play" data-act="toggle" title="Play/Pause" aria-label="Play or pause">${iconMarkup('play', 16)}</button>
				<button class="rw-now-btn" data-act="next" title="Next" aria-label="Next">${iconMarkup('next', 16)}</button>
				<button class="rw-now-btn rw-now-loop" data-act="loop" title="Loop" aria-label="Toggle loop">${iconMarkup('loop', 16)}</button>
			</div>
			<div class="rw-now-bar-extra">${popoutBtn}</div>
		</div>`;
	// Delegated on the whole row so it catches both the transport controls and
	// the (separately-grouped) pop-out button. The meta button has no data-act,
	// so it falls through to its own openNowPlayingView listener below.
	bar.querySelector('.rw-now-bar-row').addEventListener('click', e => {
		const b = e.target.closest('[data-act]');
		if (!b) return;
		const act = b.dataset.act;
		if (act === 'toggle') window.RainetteMusic?.toggle?.();
		else if (act === 'next') window.RainetteMusic?.next?.();
		else if (act === 'prev') window.RainetteMusic?.prev?.();
		else if (act === 'loop') window.RainetteMusic?.toggleLoop?.();
		else if (act === 'popout') { try { window.pywebview?.api?.reveal_player?.(); } catch { /* not in pywebview */ } }
	});
	const volume = document.createElement('input');
	volume.type = 'range';
	volume.className = 'rw-now-volume';
	volume.min = '0';
	volume.max = '150';
	volume.step = '1';
	volume.value = String(Math.round((window.RainetteMusic?.getVolume?.() ?? 1) * 100));
	volume.setAttribute('aria-label', 'Volume');
	volume.setAttribute('aria-valuetext', volume.value + '%');
	volume.addEventListener('input', () => {
		volume.setAttribute('aria-valuetext', volume.value + '%');
		window.RainetteMusic?.setVolume?.(Number(volume.value) / 100);
	});
	bar.querySelector('.rw-now-bar-extra')?.prepend(iconSpan('volume', 'rw-now-volume-icon'), volume);
	bar.querySelector('.rw-now-bar-meta').addEventListener('click', openNowPlayingView);
	wireSeekBar(bar.querySelector('.rw-now-seek'));
	return bar;
}

function renderDockedBar() {
	if (!QUEUE_SUPPORTED) return;   // non-remote uses the floating bubble instead
	const bar = ensureDockedBar();
	if (!bar) return;
	const track = currentQueueTrack();
	const q = pageState.queue || {};
	// Only visible once playback has started this session (see the
	// music_now_playing handler) - never merely because a queue was restored.
	const show = !!track && pageState.playbackStarted;
	bar.hidden = !show;
	_host?.classList.toggle('rw-has-docked-bar', show);
	// Slide-up entrance only on the transition from hidden→shown, so it "pops
	// up" when playback begins rather than re-animating on every render.
	if (show && !_dockedBarShown && !motionDisabled()) {
		bar.classList.remove('rw-docked-enter');
		void bar.offsetWidth;
		bar.classList.add('rw-docked-enter');
		bar.addEventListener('animationend', () => bar.classList.remove('rw-docked-enter'), { once: true });
	}
	_dockedBarShown = show;
	if (!show) return;

	const art = bar.querySelector('.rw-now-art');
	const artShell = bar.querySelector('.rw-now-art-shell');
	const nextArt = track.thumbnail_url || '';
	// Only touch the <img> when the art actually changes - this render runs on
	// every now-playing broadcast - and fade the new art in when it does.
	if (art.dataset.src !== nextArt) {
		art.dataset.src = nextArt;
		if (nextArt) {
			if (!motionDisabled()) {
				art.classList.remove('rw-art-swap');
				void art.offsetWidth;
				art.classList.add('rw-art-swap');
				art.addEventListener('animationend', () => art.classList.remove('rw-art-swap'), { once: true });
			}
			art.src = nextArt;
			artShell.classList.add('has-art');
		} else {
			art.removeAttribute('src');
			artShell.classList.remove('has-art');
		}
	}
	bar.querySelector('.rw-now-title').textContent = track.title || '(untitled)';
	bar.querySelector('.rw-now-artist').textContent = artistName(track) || '';
	bar.querySelector('.rw-now-play').innerHTML = iconMarkup(q.playing ? 'pause' : 'play', 16);
	bar.querySelector('.rw-now-loop')?.classList.toggle('on', !!q.loop);
	const volume = bar.querySelector('.rw-now-volume');
	if (volume && document.activeElement !== volume) {
		volume.value = String(Math.round((window.RainetteMusic?.getVolume?.() ?? 1) * 100));
		volume.setAttribute('aria-valuetext', volume.value + '%');
	}
	updateProgressUi();
}

// Applies pageState.progress to the docked bar's seek fill and the Now Playing
// view's seek fill + time labels (whichever are on screen).
function updateProgressUi() {
	const p = pageState.progress || {};
	const ratio = p.duration > 0 ? Math.max(0, Math.min(1, p.current_time / p.duration)) : 0;
	const pct = (ratio * 100) + '%';
	for (const s of _host?.querySelectorAll('#rwDockedBar .rw-now-seek, #rwNowPlayingView .rw-now-view-seek') || []) {
		s.setAttribute('aria-valuemax', String(Math.round(p.duration || 0)));
		s.setAttribute('aria-valuenow', String(Math.round(p.current_time || 0)));
		s.setAttribute('aria-valuetext', fmtClock(p.current_time) + ' of ' + fmtClock(p.duration));
	}
	const barFill = _host?.querySelector('#rwDockedBar .rw-now-seek-fill');
	if (barFill) barFill.style.width = pct;
	const barSeek = _host?.querySelector('#rwDockedBar .rw-now-seek');
	if (barSeek) barSeek.style.setProperty('--rw-mp-progress', pct);
	const npFill = _host?.querySelector('#rwNowPlayingView .rw-now-view-seek-fill');
	if (npFill) npFill.style.width = pct;
	const npSeek = _host?.querySelector('#rwNowPlayingView .rw-now-view-seek');
	if (npSeek) npSeek.style.setProperty('--rw-mp-progress', pct);
	const cur = _host?.querySelector('#rwNowPlayingView .rw-now-view-time-cur');
	const dur = _host?.querySelector('#rwNowPlayingView .rw-now-view-time-dur');
	if (cur) cur.textContent = fmtClock(p.current_time);
	if (dur) dur.textContent = fmtClock(p.duration);
	updateSleepUi();
	updateLyricsHighlight();
}

async function startMixFromSeed(seed) {
	pageState.mixStatus = 'Building mix...';
	const result = await helperRequest('music_mix_from_seed', { id: 'mix_' + Math.random().toString(36).slice(2), seed }, 20000);
	pageState.mixStatus = '';
	if (!result?.ok || !Array.isArray(result.tracks) || !result.tracks.length) {
		await infoDialog({ title: 'Mix unavailable', message: result?.msg || 'No matching tracks were found.' });
		return;
	}
	await actionSheet({
		title: 'Start mix',
		items: [
			{ label: 'Play mix now', hint: `${result.tracks.length} tracks`, run: () => window.RainetteMusic?.playQueue(result.tracks, 0) },
			{ label: 'Add mix to queue', hint: result.status || '', run: () => {
				for (const track of result.tracks) window.RainetteMusic?.queueAddEnd?.(track);
			} },
		],
	});
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
	if (pageState.tab !== 'mobile') unmountMobile();
	updateShellChrome();
	if (pageState.view) return renderCurrent();
	if (pageState.tab === 'home') {
		renderHome();
		sendHelper({ type: 'music_recent', id: 'mrec_' + Math.random().toString(36).slice(2) });
		sendHelper({ type: 'music_top_artists', id: 'mta_' + Math.random().toString(36).slice(2) });
	}
	else if (pageState.tab === 'search') {
		renderSearch();
		sendHelper({ type: 'music_recent', id: 'mrec_' + Math.random().toString(36).slice(2) });
	}
	else if (pageState.tab === 'songs') { renderSongs(); refreshLibraryIndex(); }
	else if (pageState.tab === 'following') { renderFollowing(); refreshLibraryIndex(); }
	else if (pageState.tab === 'playlists') { renderPlaylists(); sendHelper({ type: 'music_playlist_list', id: 'pll_' + Math.random().toString(36).slice(2) }); }
	else if (pageState.tab === 'recent') { renderRecent(); sendHelper({ type: 'music_recent', id: 'mrec_' + Math.random().toString(36).slice(2) }); }
	else if (pageState.tab === 'insights') { renderInsights(); fetchInsights(); }
	else if (pageState.tab === 'queue') { window.RainetteMusic?.requestQueueState?.(); renderQueue(); }
	else if (pageState.tab === 'mobile') renderMobile(_host?.querySelector('#rwMusicBody'));
	else if (pageState.tab === 'settings') renderSettings(_host);
	animatePageEnter();
}

// A short enter transition on the content pane whenever the destination
// actually changes (tab or detail view) - deliberately NOT on same-view
// re-renders, which fire constantly from live now-playing/queue broadcasts
// and would otherwise make the page flicker on every playback update.
function animatePageEnter() {
	const body = _host?.querySelector('#rwMusicBody');
	if (!body) return;
	const viewKey = pageState.view ? ('view:' + pageState.view.kind) : ('tab:' + pageState.tab);
	if (viewKey === _lastAnimatedTab) return;
	_lastAnimatedTab = viewKey;
	if (motionDisabled()) return;
	body.classList.remove('rw-page-enter');
	void body.offsetWidth;                 // force reflow so the animation restarts
	body.classList.add('rw-page-enter');
	body.addEventListener('animationend', () => body.classList.remove('rw-page-enter'), { once: true });
}

function motionDisabled() {
	return document.documentElement.classList.contains('rw-reduced-motion')
		|| !!window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
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
		case 'music_playlist_meta_updated':
		case 'music_playlist_folder_created':
		case 'music_playlist_folder_renamed':
		case 'music_playlist_folder_deleted':
		case 'music_playlist_folder_moved':
		case 'music_smart_playlist_created':
		case 'music_smart_playlist_updated':
		case 'music_smart_playlist_deleted':
			if (Array.isArray(msg.playlists)) pageState.playlists = msg.playlists;
			if (Array.isArray(msg.folders)) pageState.folders = msg.folders;
			if (pageState.openPlaylist && Array.isArray(msg.playlists)) {
				const updated = msg.playlists.find(pl => pl.id === pageState.openPlaylist.id);
				if (updated) pageState.openPlaylist = { ...pageState.openPlaylist, ...updated };
			}
			if (msg.playlist && pageState.openPlaylist?.id === msg.playlist.id) pageState.openPlaylist = { ...pageState.openPlaylist, ...msg.playlist };
			renderPinnedPlaylists();
			if (pageState.tab === 'playlists' && !pageState.view) renderPlaylists();
			if (pageState.tab === 'home' && !pageState.view) renderHome();
			break;
		case 'music_playlist_tracks_result':
		case 'music_playlist_track_added':
		case 'music_playlist_track_removed':
		case 'music_smart_playlist_tracks_result':
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
			if (pageState.tab === 'home' && !pageState.view) renderHome();
			if (pageState.tab === 'search' && !pageState.view && !pageState.query.trim()) renderResults();
			break;
		case 'music_top_artists_result':
			if (msg.ok) pageState.topArtists = msg.artists || [];
			if (pageState.tab === 'home' && !pageState.view) renderHome();
			break;
		case 'music_insights_result':
			pageState.insights.loading = false;
			if (msg.ok) pageState.insights.data = msg;
			if (pageState.tab === 'insights' && !pageState.view) renderInsights();
			break;
		case 'music_queue_session_list_result':
		case 'music_queue_session_saved':
		case 'music_queue_session_deleted':
			if (Array.isArray(msg.sessions)) pageState.queueSessions = msg.sessions;
			pageState.queueSessionStatus = msg.ok ? '' : ('Queue sessions failed: ' + (msg.msg || ''));
			if (pageState.tab === 'queue' && !pageState.view) renderQueue();
			break;
		case 'music_now_playing': {
			const banner = _host?.querySelector('#rwMusicBanner');
			if (banner) {
				if (msg.state === 'error' && msg.track) {
					banner.dataset.kind = 'playback-error';
					banner.style.display = '';
					banner.textContent = `Couldn't play "${msg.track.title || 'this track'}" - playback failed.`;
				} else if (banner.dataset.kind === 'playback-error') {
					banner.style.display = 'none';
					banner.textContent = '';
					delete banner.dataset.kind;
				}
			}
			if (Array.isArray(msg.queue)) {
				pageState.queue = {
					tracks: msg.queue,
					index: Number.isFinite(Number(msg.index)) ? Number(msg.index) : -1,
					playing: !!msg.playing,
					loop: !!msg.loop,
					duration: Number(msg.queue_duration || 0),
					count: Number(msg.queue_count || msg.queue.length || 0),
				};
				// If this confirms a drag-reorder we already applied optimistically,
				// skip the render - otherwise the FLIP animation is immediately
				// followed by a second, pointless full-rebuild "jump".
				const confirmsOptimisticReorder = _lastOptimisticQueueSignature && _lastOptimisticQueueSignature === _queueSignature(pageState.queue);
				_lastOptimisticQueueSignature = null;
				if (!confirmsOptimisticReorder) renderQueueSurfaces();
				saveLastQueueSessionDebounced();
				const current = pageState.queue.tracks[pageState.queue.index];
				const key = current ? (current.source || 'youtube') + ':' + (current.source_id || '') : null;
				if (key !== _lastAutoOpenTrackKey) {
					const isNewTrack = key != null;
					const hadTrack = _lastAutoOpenTrackKey != null;
					_lastAutoOpenTrackKey = key;
					// "Stop after this track": the watched track just ended (or was
					// skipped) - pause whatever started next and clear the timer.
					if (sleepState.endOfTrack && hadTrack) {
						cancelSleepTimer();
						if (isNewTrack) setTimeout(() => { if (window.RainetteMusic?.isPlaying?.()) window.RainetteMusic.toggle?.(); }, 150);
					}
					if (isNewTrack && pageState.queue.playing && shouldAutoOpenQueue()) toggleQueueDrawer(true);
				}
			}
			// Keep pageState.progress's current track in sync so a stale
			// position from the previous track doesn't linger on the bar.
			{
				const cur = currentQueueTrack();
				pageState.progress = {
					current_time: Number(msg.current_time || 0),
					duration: Number(msg.duration || cur?.duration_s || 0),
					playing: !!msg.playing,
					source_id: cur?.source_id || '',
				};
				// The docked bar should only appear once playback has actually
				// begun - not merely because a queue was restored/persisted on
				// launch (which broadcasts a paused, current_time:0 queue). Once
				// started it stays (even when paused) until the queue empties.
				if (!cur) pageState.playbackStarted = false;
				else if (msg.playing || msg.state === 'playing' || msg.state === 'loading' || Number(msg.current_time) > 0) pageState.playbackStarted = true;
			}
			renderDockedBar();
			if (pageState.nowPlayingOpen) {
				// Track changed while the view is open + lyrics showing → refetch.
				const cur = currentQueueTrack();
				if (pageState.lyrics.open && cur && cur.source_id !== pageState.lyrics.source_id) fetchLyricsForCurrent();
				renderNowPlayingView();
			}
			if (pageState.tab === 'home' && !pageState.view) renderHome();
			break;
		}
		case 'music_progress': {
			// Ignore ticks for a track other than the one we currently show
			// (a stale in-flight broadcast after a fast track change).
			const cur = currentQueueTrack();
			if (msg.source_id && cur && msg.source_id !== cur.source_id) break;
			pageState.progress = {
				current_time: Number(msg.current_time || 0),
				duration: Number(msg.duration || pageState.progress.duration || 0),
				playing: !!msg.playing,
				source_id: msg.source_id || pageState.progress.source_id,
			};
			// Safety net: if a progress tick shows real playback but the bar
			// isn't up yet (e.g. a now_playing broadcast was missed), reveal it.
			if ((msg.playing || Number(msg.current_time) > 0) && cur && !pageState.playbackStarted) {
				pageState.playbackStarted = true;
				renderDockedBar();
			}
			updateProgressUi();
			break;
		}
		case 'music_lyrics_result':
			if (msg.id && msg.id === pageState.lyrics.reqId) {
				const parsedLines = msg.ok ? parseSyncedLyrics(msg.synced) : [];
				pageState.lyrics = {
					...pageState.lyrics,
					loading: false,
					text: msg.ok ? (msg.plain || '') : '',
					notFound: !!msg.not_found || (msg.ok && !msg.plain && !msg.instrumental),
					instrumental: !!msg.instrumental,
					error: msg.ok ? '' : (msg.msg || 'Lyrics unavailable'),
					synced: parsedLines.length > 0,
					lines: parsedLines,
					activeIndex: -1,
				};
				if (pageState.nowPlayingOpen) renderLyricsPanel();
			}
			break;
		case 'music_library_index_result':
			if (msg.ok) pageState.library = {
				tracks: msg.tracks || [],
				artists: msg.artists || [],
				albums: msg.albums || [],
				followed_artists: msg.followed_artists || [],
			};
			if (!pageState.view && pageState.tab === 'songs') renderSongs();
			if (!pageState.view && pageState.tab === 'following') renderFollowing();
			break;
		case 'music_artist_followed':
		case 'music_artist_unfollowed':
		case 'music_followed_artists_result':
			if (msg.ok && Array.isArray(msg.followed_artists)) pageState.library.followed_artists = msg.followed_artists;
			if (pageState.view?.kind === 'artist') renderArtistDetail();
			else if (pageState.tab === 'following') renderFollowing();
			else if (pageState.tab === 'search') renderSearch();
			else if (pageState.tab === 'recent') renderRecent();
			break;
		case 'music_open_artist':
			if (msg.name || msg.artist_id) {
				pageState.openPlaylist = null;
				openArtist({ id: msg.artist_id || '', name: msg.name || '', thumbnail_url: msg.thumbnail_url || '' });
			}
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

function renderPinnedPlaylists() {
	const wrap = _host?.querySelector('#rwPinnedPlaylists');
	if (!wrap) return;
	wrap.innerHTML = '';
	const pinned = pageState.playlists.filter(p => p.pinned).slice(0, 6);
	if (!pinned.length) return;
	wrap.appendChild(el('div', 'rw-sidebar-label', 'Pinned'));
	for (const pl of pinned) {
		const b = document.createElement('button');
		b.type = 'button';
		b.className = 'rw-pinned-playlist';
		b.textContent = pl.name;
		b.title = pl.name;
		b.addEventListener('click', () => { pageState.tab = 'playlists'; pageState.view = null; openPlaylist(pl); updateShellChrome(); });
		wrap.appendChild(b);
	}
}

function buildPage(host) {
	const queueButton = QUEUE_SUPPORTED ? '<button id="rwMusicQueueOpen" class="rw-btn rw-btn-ghost rw-sidebar-action" type="button">Open queue drawer</button>' : '';
	const tabs = navItems().map(id => `<button data-tab="${id}" type="button"><span>${TAB_META[id].label}</span></button>`).join('');
	host.innerHTML = `
		<div class="rw-music-shell">
			<aside class="rw-music-sidebar" aria-label="Music navigation">
				<div class="rw-music-brand">
					<div class="rw-music-mark" aria-hidden="true"><img src="/assets/rainette-icon-256.png" alt=""></div>
					<div class="rw-music-brand-text">
						<div class="rw-music-brand-name">Rainette</div>
						<div class="rw-music-brand-sub">Music desk</div>
					</div>
				</div>
				<nav class="rw-music-nav" id="rwMusicTabs">${tabs}</nav>
				<div class="rw-pinned-playlists" id="rwPinnedPlaylists"></div>
				<div class="rw-music-sidebar-foot">
					<div class="rw-sidebar-label">View</div>
					<div class="rw-segment rw-layout-segment" id="rwMusicLayoutSeg">
						<button data-layout="list" type="button" title="List view" aria-label="List view">List</button>
						<button data-layout="grid" type="button" title="Grid view" aria-label="Grid view">Grid</button>
					</div>
					<button id="rwCommandOpen" class="rw-btn rw-btn-ghost rw-sidebar-action" type="button">Command palette</button>
					${queueButton}
				</div>
			</aside>
			<div class="rw-music-main">
				<div class="rw-page-head">
					<div>
						<div class="rw-page-eyebrow" id="rwMusicEyebrow">Catalog</div>
						<h1 class="rw-page-title" id="rwMusicTitle">Search the catalog</h1>
						<p class="rw-page-sub" id="rwMusicSub">Find songs, artists, and albums without leaving the listening flow.</p>
					</div>
				</div>
				<div class="rw-page-body">
					<div id="rwMusicBanner" class="rw-status-line" style="display:none;color:var(--rw-warn,#c93)"></div>
					<div id="rwMusicBody"></div>
					<div id="rwMusicQueueDrawer" class="rw-queue-drawer" hidden></div>
					<div id="rwCommandPalette" class="rw-command-palette" hidden></div>
					<div id="rwNowPlayingView" class="rw-now-view" hidden></div>
					<div id="rwShortcutsView" class="rw-shortcuts-view" hidden></div>
				</div>
			</div>
			<div id="rwDockedBar" class="rw-now-bar rw-docked-bar" hidden></div>
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
	host.querySelector('#rwCommandOpen')?.addEventListener('click', openCommandPalette);
	// Backdrop-click-to-close: only when the mousedown lands on the palette's
	// own backdrop element, not when it bubbles up from the panel/input/list.
	host.querySelector('#rwCommandPalette')?.addEventListener('mousedown', e => {
		if (e.target === e.currentTarget) closeCommandPalette();
	});
	host.querySelector('#rwNowPlayingView')?.addEventListener('mousedown', e => {
		if (e.target === e.currentTarget) closeNowPlayingView();
	});
	host.querySelector('#rwShortcutsView')?.addEventListener('mousedown', e => {
		if (e.target === e.currentTarget) toggleShortcutsView(false);
	});
	updateLayoutButtons();
	renderPinnedPlaylists();
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
		sendHelper({ type: 'music_queue_session_list', id: 'qsl_' + Math.random().toString(36).slice(2) });
		refreshLibraryIndex();
		bindCommandPaletteKeys();
		bindNowPlayingKeys();
		bindGlobalShortcuts();
		if (QUEUE_SUPPORTED) {
			pageState.queue = app.musicQueue || pageState.queue;
			renderDockedBar();   // reflect any already-playing track immediately
			window.RainetteMusic?.requestQueueState?.();
		}
		applyTab();
	},
	unmount() { _mounted = false; unmountMobile(); },
};

RainetteRouter.register('music', MusicPage);
