/* The phone's own catalog, for the app with no computer behind it.
 *
 * The computer answers search, artist pages, albums, artist pictures and lyrics
 * through ytmusicapi and LRCLIB. The native app can ask the same two places
 * itself, through Capacitor's native HTTP (a WebView's fetch would be refused
 * by CORS), and hands back the computer's own shapes so every screen renders
 * one way whoever answered. Nothing goes through a Rainette server.
 *
 * The parsers are plain functions over YouTube Music's answers and are tested
 * against saved copies of them (tests/test_pwa_ytmusic.py). A shape they do
 * not recognise is a failure, never an empty result: "nothing matched" would
 * be a lie about a page that changed. */

import { STORAGE } from './state.js';
import { nativeAvailable, nativeSearch } from './native-engine.js';

const API = 'https://music.youtube.com/youtubei/v1/';
const CLIENT = { clientName: 'WEB_REMIX', clientVersion: '1.20251020.01.00', hl: 'en', gl: 'US' };
/* YouTube Music's own search filters, as its web client sends them. */
const FILTER = {
	songs: 'EgWKAQIIAWoMEA4QChADEAQQCRAF',
	artists: 'EgWKAQIgAWoMEA4QChADEAQQCRAF',
	albums: 'EgWKAQIYAWoMEA4QChADEAQQCRAF',
};
/* The web client's own User-Agent. Asked with the app's (Android WebView),
 * YouTube Music leaves the artist and album links out of every row, and
 * without them no song can open its album or its artist by id. The same one
 * the iPhone's own search sends (StreamResolver.safari). */
const DESKTOP_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15';
const TIMEOUT_MS = 15000;
const ARTIST_IMAGE_WORKERS = 4;
const LRCLIB = 'https://lrclib.net/api/';

/* ── Transport ────────────────────────────────────────────────────────────*/

/** One request through the app's own HTTP, which CORS does not apply to.
 *  `responseType: 'blob'` answers with base64 in `data`. */
export async function nativeRequest(options) {
	const cap = globalThis.Capacitor;
	if (!cap?.isPluginAvailable?.('CapacitorHttp')) throw catalogFailure('This needs the Rainette app.');
	try {
		return await cap.nativePromise('CapacitorHttp', 'request', {
			connectTimeout: TIMEOUT_MS, readTimeout: TIMEOUT_MS, ...options,
		});
	} catch {
		throw catalogFailure('No internet connection.');
	}
}

async function http(url, { method = 'GET', data, headers = {}, timeoutMs = TIMEOUT_MS } = {}) {
	const response = await nativeRequest({ url, method, headers, data, connectTimeout: timeoutMs, readTimeout: timeoutMs });
	const body = typeof response?.data === 'string' && /^[[{]/.test(response.data.trim())
		? JSON.parse(response.data)
		: response?.data;
	return { status: Number(response?.status) || 0, body };
}

function catalogFailure(message) {
	return new Error(message);
}

async function innertube(endpoint, payload) {
	const { status, body } = await http(`${API}${endpoint}?prettyPrint=false`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Origin: 'https://music.youtube.com', 'User-Agent': DESKTOP_UA },
		data: { context: { client: CLIENT }, ...payload },
	});
	if (status === 429) throw catalogFailure('YouTube Music asked this phone to slow down. Try again in a minute.');
	if (status !== 200 || !body || typeof body !== 'object') throw catalogFailure('YouTube Music did not answer. Try again.');
	return body;
}

/* ── Reading YouTube Music's answers ──────────────────────────────────────*/

function dig(value, ...path) {
	return path.reduce((at, key) => (at == null ? undefined : at[key]), value);
}

const text = runs => (Array.isArray(runs) ? runs.map(run => run?.text || '').join('') : '');
const SEPARATOR = ' • ';
const DURATION = /^\d{1,2}(:\d{2}){1,2}$/;

function seconds(value) {
	if (!DURATION.test(value)) return null;
	return value.split(':').reduce((total, part) => total * 60 + Number(part), 0);
}

function largest(thumbnails) {
	const list = Array.isArray(thumbnails) ? thumbnails : [];
	const best = list.reduce((top, item) => ((item?.width || 0) >= (top?.width || 0) ? item : top), null);
	const url = String(best?.url || '');
	return url.startsWith('https://') ? url : '';
}

const thumbOf = node => largest(dig(node, 'musicThumbnailRenderer', 'thumbnail', 'thumbnails'));
const pageType = run => dig(run, 'navigationEndpoint', 'browseEndpoint', 'browseEndpointContextSupportedConfigs',
	'browseEndpointContextMusicConfig', 'pageType') || '';
const browseId = run => String(dig(run, 'navigationEndpoint', 'browseEndpoint', 'browseId') || '');

function flexRuns(row) {
	return (row?.flexColumns || []).map(column =>
		dig(column, 'musicResponsiveListItemFlexColumnRenderer', 'text', 'runs') || []);
}

/** One song row, anywhere YouTube Music draws one: search, an artist's top
 *  songs, a playlist, an album. `hint` fills what album rows leave out. */
export function songFromRow(row, hint = {}) {
	const columns = flexRuns(row);
	const videoId = row?.playlistItemData?.videoId
		|| dig(columns, 0, 0, 'navigationEndpoint', 'watchEndpoint', 'videoId');
	if (!/^[A-Za-z0-9_-]{11}$/.test(String(videoId || ''))) return null;

	const runs = columns.slice(1).flat();
	const artists = runs.filter(run => pageType(run) === 'MUSIC_PAGE_TYPE_ARTIST')
		.map(run => ({ name: run.text, id: browseId(run) }));
	// Some rows name an artist without linking one: "Artist • Album • 3:45".
	if (!artists.length) {
		const plain = text(columns[1]).split(SEPARATOR)[0].trim();
		if (plain && !DURATION.test(plain) && !/^(song|video)$/i.test(plain)) artists.push({ name: plain, id: '' });
	}
	if (!artists.length && hint.artist) artists.push({ name: hint.artist, id: hint.artistId || '' });
	const albumRun = runs.find(run => pageType(run) === 'MUSIC_PAGE_TYPE_ALBUM');
	const album = albumRun ? { name: albumRun.text, id: browseId(albumRun) } : (hint.album || null);

	const fixed = text(dig(row, 'fixedColumns', 0, 'musicResponsiveListItemFixedColumnRenderer', 'text', 'runs'));
	const duration = seconds(fixed) ?? runs.map(run => seconds(run?.text || '')).find(value => value != null) ?? null;
	const plays = runs.map(run => run?.text || '').find(value => / (plays|views)$/.test(value)) || '';
	const artist = artists[0]?.name.replace(/ - Topic$/, '') || '';

	return {
		source: 'youtube',
		source_id: videoId,
		title: text(columns[0]) || '(untitled)',
		artist,
		duration_s: duration,
		thumbnail_url: thumbOf(row?.thumbnail) || hint.art || '',
		view_count: plays.replace(/ (plays|views)$/, ''),
		metadata: {
			source_detail: 'on-device',
			artists,
			artist_id: artists[0]?.id || '',
			album: album ? { name: album.name, id: album.id } : {},
			album_name: album?.name || '',
			album_id: album?.id || '',
		},
	};
}

/** An artist row from search, shaped like the computer's `_ytm_artist`. */
export function artistFromRow(row) {
	const columns = flexRuns(row);
	const name = text(columns[0]);
	const id = String(dig(row, 'navigationEndpoint', 'browseEndpoint', 'browseId') || '');
	if (!name || !id) return null;
	// "Artist • 70.6M monthly audience": kept as it reads, since it is not a
	// subscriber count, with the number alone for sorting by size.
	const count = text(columns[1]).split(SEPARATOR).pop() || '';
	const counted = /^[\d.,]+[KMB]?\s/.test(count);
	return {
		id, name,
		thumbnail_url: thumbOf(row?.thumbnail),
		subscribers: counted && / subscribers$/.test(count) ? count.split(' ')[0] : '',
		audience: counted && !/ subscribers$/.test(count) ? count.replace(/ audience$/, ' listeners') : '',
		result_type: 'artist',
	};
}

const RELEASE_TYPES = /^(album|single|ep)$/i;

/** An album row from search: "Album • Laufey • 2025". */
export function albumFromRow(row) {
	const columns = flexRuns(row);
	const id = String(dig(row, 'navigationEndpoint', 'browseEndpoint', 'browseId') || '');
	const title = text(columns[0]);
	if (!id || !title) return null;
	const details = text(columns[1]).split(SEPARATOR);
	const artistRun = (columns[1] || []).find(run => browseId(run).startsWith('UC'));
	return releaseShape({
		id, title,
		artist: artistRun?.text || '',
		artistId: browseId(artistRun),
		year: details.find(part => /^\d{4}$/.test(part)) || '',
		type: details.find(part => RELEASE_TYPES.test(part)) || 'Album',
		art: thumbOf(row?.thumbnail),
	});
}

/** One card in an artist page's carousel of releases. */
function albumFromCard(card, artist, fallbackType) {
	const id = String(dig(card, 'navigationEndpoint', 'browseEndpoint', 'browseId') || '');
	const title = text(dig(card, 'title', 'runs'));
	if (!id || !title) return null;
	const details = text(dig(card, 'subtitle', 'runs')).split(SEPARATOR);
	return releaseShape({
		id, title,
		artist: artist.name, artistId: artist.id,
		year: details.find(part => /^\d{4}$/.test(part)) || '',
		type: details.find(part => RELEASE_TYPES.test(part)) || fallbackType,
		art: thumbOf(card?.thumbnailRenderer),
	});
}

function releaseShape({ id, title, artist, artistId, year, type, art }) {
	return {
		id, browse_id: id, title, artist, artist_id: artistId || '', year,
		thumbnail_url: art, release_type: type, result_type: 'album',
	};
}

/** A music video card on an artist page, as a playable song. */
function songFromCard(card, artist) {
	const videoId = dig(card, 'navigationEndpoint', 'watchEndpoint', 'videoId');
	if (!/^[A-Za-z0-9_-]{11}$/.test(String(videoId || ''))) return null;
	const views = text(dig(card, 'subtitle', 'runs')).split(SEPARATOR).find(part => / views$/.test(part)) || '';
	return {
		source: 'youtube',
		source_id: videoId,
		title: text(dig(card, 'title', 'runs')) || '(untitled)',
		artist: artist.name,
		duration_s: null,
		thumbnail_url: thumbOf(card?.thumbnailRenderer),
		view_count: views.replace(/ views$/, ''),
		metadata: {
			source_detail: 'on-device', artists: [{ name: artist.name, id: artist.id }], artist_id: artist.id,
			album: {}, album_name: '', album_id: '',
		},
	};
}

/** The rows of every shelf in a search answer. */
export function searchRows(answer) {
	const sections = dig(answer, 'contents', 'tabbedSearchResultsRenderer', 'tabs', 0, 'tabRenderer', 'content',
		'sectionListRenderer', 'contents');
	if (!Array.isArray(sections)) throw catalogFailure('YouTube Music answered in a way Rainette does not recognise.');
	return sections.flatMap(section => dig(section, 'musicShelfRenderer', 'contents') || [])
		.map(item => item?.musicResponsiveListItemRenderer)
		.filter(Boolean);
}

/** An artist's page: who they are, their top songs, releases and videos. */
export function parseArtistPage(answer, seed = {}) {
	const header = dig(answer, 'header', 'musicImmersiveHeaderRenderer') || dig(answer, 'header', 'musicVisualHeaderRenderer');
	const sections = dig(answer, 'contents', 'singleColumnBrowseResultsRenderer', 'tabs', 0, 'tabRenderer', 'content',
		'sectionListRenderer', 'contents');
	if (!header || !Array.isArray(sections)) throw catalogFailure('That artist page did not load.');

	const artist = {
		id: seed.id || '',
		name: text(dig(header, 'title', 'runs')) || seed.name || 'Unknown artist',
		// The header picture is a wide banner; the square from search is the
		// one that reads as a face in a circle, so it wins when there is one.
		thumbnail_url: seed.art || thumbOf(header.thumbnail) || thumbOf(header.foregroundThumbnail),
		subscribers: text(dig(header, 'subscriptionButton', 'subscribeButtonRenderer', 'subscriberCountText', 'runs')),
		description: text(dig(header, 'description', 'runs')),
	};

	// `more` is where each shelf's full list lives, when it has more than the
	// page shows: YouTube Music's artist page lists ten singles of thirty.
	const page = { artist, songs: [], songsBrowseId: '', albums: [], singles: [], videos: [], more: {} };
	for (const section of sections) {
		const shelf = section.musicShelfRenderer;
		if (shelf) {
			page.songs = (shelf.contents || []).map(item => songFromRow(item.musicResponsiveListItemRenderer, { artist: artist.name, artistId: artist.id })).filter(Boolean);
			page.songsBrowseId = browseId(dig(shelf, 'title', 'runs', 0));
			continue;
		}
		const carousel = section.musicCarouselShelfRenderer;
		if (!carousel) continue;
		const title = text(dig(carousel, 'header', 'musicCarouselShelfBasicHeaderRenderer', 'title', 'runs')).toLowerCase();
		const cards = (carousel.contents || []).map(item => item.musicTwoRowItemRenderer).filter(Boolean);
		const header = dig(carousel, 'header', 'musicCarouselShelfBasicHeaderRenderer');
		const more = dig(header, 'moreContentButton', 'buttonRenderer', 'navigationEndpoint', 'browseEndpoint')
			|| dig(header, 'title', 'runs', 0, 'navigationEndpoint', 'browseEndpoint');
		const kind = title === 'albums' ? 'albums' : title.startsWith('singles') ? 'singles' : title === 'videos' ? 'videos' : '';
		if (kind && more?.browseId) {
			page.more[kind] = more.params ? { browseId: String(more.browseId), params: String(more.params) } : { browseId: String(more.browseId) };
		}
		if (kind === 'albums') page.albums = cards.map(card => albumFromCard(card, artist, 'Album')).filter(Boolean);
		else if (kind === 'singles') page.singles = cards.map(card => albumFromCard(card, artist, 'Single')).filter(Boolean);
		else if (kind === 'videos') page.videos = cards.map(card => songFromCard(card, artist)).filter(Boolean);
	}
	return page;
}

/** A playlist or an album page: its header and its tracks. */
export function parseTrackPage(answer) {
	const columns = dig(answer, 'contents', 'twoColumnBrowseResultsRenderer');
	const shelf = dig(columns, 'secondaryContents', 'sectionListRenderer', 'contents', 0);
	const rows = dig(shelf, 'musicPlaylistShelfRenderer', 'contents') || dig(shelf, 'musicShelfRenderer', 'contents');
	if (!Array.isArray(rows)) throw catalogFailure('That page did not load.');
	const header = dig(columns, 'tabs', 0, 'tabRenderer', 'content', 'sectionListRenderer', 'contents', 0,
		'musicResponsiveHeaderRenderer') || {};
	const artistRun = dig(header, 'straplineTextOne', 'runs', 0);
	const details = text(dig(header, 'subtitle', 'runs')).split(SEPARATOR);
	return {
		title: text(dig(header, 'title', 'runs')),
		artist: artistRun?.text || '',
		artistId: browseId(artistRun),
		year: details.find(part => /^\d{4}$/.test(part)) || '',
		type: details.find(part => RELEASE_TYPES.test(part)) || '',
		art: thumbOf(header.thumbnail),
		rows: rows.map(item => item?.musicResponsiveListItemRenderer).filter(Boolean),
		continuation: continuationOf(rows),
	};
}

const continuationOf = items => String(dig(items?.[items.length - 1], 'continuationItemRenderer', 'continuationEndpoint',
	'continuationCommand', 'token') || '');

/** The release cards of an artist's full "Albums" or "Singles & EPs" page. */
export function parseReleaseGrid(answer) {
	const grid = dig(answer, 'contents', 'singleColumnBrowseResultsRenderer', 'tabs', 0, 'tabRenderer', 'content',
		'sectionListRenderer', 'contents', 0, 'gridRenderer', 'items');
	if (!Array.isArray(grid)) throw catalogFailure('That list did not load.');
	return grid.map(item => item?.musicTwoRowItemRenderer).filter(Boolean);
}

/* A long list arrives a hundred rows at a time. Three more pages is three
 * hundred more songs, which is past what anyone scrolls an artist page for. */
const MORE_PAGES = 3;

async function allRows(page) {
	const rows = [...page.rows];
	let token = page.continuation;
	for (let n = 0; token && n < MORE_PAGES; n += 1) {
		const next = await innertube('browse', { continuation: token });
		const items = (next?.onResponseReceivedActions || [])
			.flatMap(action => dig(action, 'appendContinuationItemsAction', 'continuationItems') || []);
		rows.push(...items.map(item => item?.musicResponsiveListItemRenderer).filter(Boolean));
		token = continuationOf(items);
	}
	return rows;
}

/* ── Asking ───────────────────────────────────────────────────────────────*/

async function searchKind(query, kind) {
	return searchRows(await innertube('search', { query, params: FILTER[kind] }));
}

async function searchSongs(query) {
	try {
		return (await searchKind(query, 'songs')).map(row => songFromRow(row)).filter(Boolean);
	} catch (error) {
		// The app's own song search is the one playback was proven on; it is
		// the fallback rather than nothing.
		if (nativeAvailable()) return nativeSearch(query);
		throw error;
	}
}

/** Songs, artists and albums for one query, as `music_catalog_search` returns them. */
export async function catalogSearch(query) {
	const words = String(query || '').trim();
	if (!words) return { songs: [], artists: [], albums: [] };
	// Artists and albums are a bonus on top of songs: either failing on its
	// own leaves a search that still did its main job.
	const [songs, artists, albums] = await Promise.all([
		searchSongs(words),
		searchKind(words, 'artists').then(rows => rows.map(artistFromRow).filter(Boolean).slice(0, 10)).catch(() => []),
		searchKind(words, 'albums').then(rows => rows.map(albumFromRow).filter(Boolean).slice(0, 12)).catch(() => []),
	]);
	return { songs, artists, albums, source: 'on-device' };
}

async function findArtist(name) {
	const rows = await searchKind(name, 'artists');
	return rows.map(artistFromRow).find(Boolean) || null;
}

/* When YouTube Music cannot be read, the page is the artist's songs found by
 * the app's own search: a smaller page, never an error where one used to work. */
async function songsFor(query, artistName = '') {
	if (!nativeAvailable() || !query) return [];
	const found = await nativeSearch(query);
	const wanted = artistName.toLowerCase();
	const theirs = wanted ? found.filter(song => String(song?.artist || '').toLowerCase().includes(wanted)) : [];
	return theirs.length ? theirs : found;
}

/** An artist's page, as `music_artist_catalog` returns it. */
export async function artistCatalog(request = {}) {
	try {
		return await fullArtistCatalog(request);
	} catch (error) {
		const songs = await songsFor(String(request.name || ''), String(request.name || '')).catch(() => []);
		if (!songs.length) throw error;
		return { artist: { id: '', name: String(request.name || ''), thumbnail_url: String(request.thumbnail_url || '') }, songs, source: 'on-device' };
	}
}

async function fullArtistCatalog({ artist_id: id = '', name = '', thumbnail_url: art = '' } = {}) {
	let seed = { id: String(id || ''), name: String(name || ''), art: String(art || '') };
	if (!seed.id) {
		const found = await findArtist(seed.name);
		if (!found) throw catalogFailure(`Couldn’t find ${seed.name || 'that artist'}.`);
		seed = { id: found.id, name: found.name, art: seed.art || found.thumbnail_url };
	}
	const page = parseArtistPage(await innertube('browse', { browseId: seed.id }), seed);
	const { artist } = page;
	const hint = { artist: artist.name, artistId: artist.id };
	const releases = async (more, fallback, type) => {
		if (!more) return fallback;
		const cards = parseReleaseGrid(await innertube('browse', more));
		return cards.map(card => albumFromCard(card, artist, type)).filter(Boolean);
	};
	const tracks = async (browse, fallback) => {
		if (!browse) return fallback;
		const rows = await allRows(parseTrackPage(await innertube('browse', browse)));
		const songs = rows.map(row => songFromRow(row, hint)).filter(Boolean);
		return songs.length ? songs : fallback;
	};
	// The page shows the first of each; the lists behind it have the rest.
	// Each is a bonus: one failing leaves what the page itself showed.
	const [songs, albums, singles, videos] = await Promise.all([
		tracks(page.songsBrowseId && { browseId: page.songsBrowseId }, page.songs).catch(() => page.songs),
		releases(page.more.albums, page.albums, 'Album').catch(() => page.albums),
		releases(page.more.singles, page.singles, 'Single').catch(() => page.singles),
		tracks(page.more.videos, page.videos).catch(() => page.videos),
	]);
	return { artist, songs, albums, singles, videos, source: 'on-device' };
}

/** An album's tracks, as `music_album_tracks` returns them. */
export async function albumTracks(request = {}) {
	try {
		return await fullAlbumTracks(request);
	} catch (error) {
		const { title = '', artist = '' } = request;
		const tracks = await songsFor(`${artist} ${title}`.trim(), String(artist)).catch(() => []);
		if (!tracks.length) throw error;
		return { album: { id: String(request.album_id || ''), title, artist }, tracks, source: 'on-device' };
	}
}

async function fullAlbumTracks({ album_id: id = '', title = '', artist = '' } = {}) {
	let albumId = String(id || '');
	if (!albumId) {
		const rows = await searchKind(`${artist} ${title}`.trim(), 'albums');
		albumId = rows.map(albumFromRow).find(Boolean)?.id || '';
		if (!albumId) throw catalogFailure(`Couldn’t find ${title || 'that album'}.`);
	}
	const page = parseTrackPage(await innertube('browse', { browseId: albumId }));
	const album = releaseShape({
		id: albumId, title: page.title || title, artist: page.artist || artist, artistId: page.artistId,
		year: page.year, type: page.type || 'Album', art: page.art,
	});
	const hint = { artist: album.artist, artistId: album.artist_id, art: album.thumbnail_url, album: { name: album.title, id: albumId } };
	return { album, tracks: page.rows.map(row => songFromRow(row, hint)).filter(Boolean), source: 'on-device' };
}

/** Pictures for a list of artist names, as `music_artist_images` returns them. */
export async function artistImages({ names = [] } = {}) {
	const wanted = [...new Set((Array.isArray(names) ? names : []).map(name => String(name || '').trim()).filter(Boolean))].slice(0, 40);
	const found = [];
	let next = 0;
	const worker = async () => {
		while (next < wanted.length) {
			const name = wanted[next++];
			try {
				const artist = await findArtist(name);
				if (artist) found.push({ ...artist, query: name });
			} catch { /* one name failing is one placeholder, not a failed list */ }
		}
	};
	await Promise.all(Array.from({ length: ARTIST_IMAGE_WORKERS }, worker));
	return { artists: found };
}

/* ── Lyrics ───────────────────────────────────────────────────────────────
 * Two sources at once: LRCLIB's own database, for lines timed to the song,
 * and YouTube Music's lyrics (Musixmatch, LyricFind), for the words of the
 * many songs LRCLIB lacks. Timed lines win; plain words are next. LRCLIB's
 * outside search is slow (seconds, for a miss), so it is only the last try. */

const LYRICS_TIMEOUT_MS = 8000;

function lyricsShape(entry) {
	return {
		plain: entry?.plainLyrics || '',
		synced: entry?.syncedLyrics || '',
		instrumental: Boolean(entry?.instrumental),
	};
}

const cleanTitle = title => String(title || '')
	.replace(/\s*[([](official|lyric|audio|video|visuali[sz]er|explicit|clean|feat\.?|ft\.?|with)[^)\]]*[)\]]/gi, '')
	.replace(/\s+-\s+(sped up|slowed|remaster(ed)?|live)\b.*$/i, '')
	.trim();

const LRCLIB_HEADERS = { 'User-Agent': 'RainetteMusic (https://github.com/Krysis-ux/Rainette-music)' };
const query = params => Object.entries(params).filter(([, value]) => value)
	.map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join('&');

async function lrclib(title, artist, duration, { outside = false } = {}) {
	const exact = await http(`${LRCLIB}${outside ? 'get' : 'get-cached'}?${query({ track_name: title, artist_name: artist, duration })}`,
		{ headers: LRCLIB_HEADERS, timeoutMs: LYRICS_TIMEOUT_MS });
	if (exact.status === 200 && exact.body && typeof exact.body === 'object') return lyricsShape(exact.body);
	if (outside) return null;
	const found = await http(`${LRCLIB}search?${query({ track_name: cleanTitle(title), artist_name: artist })}`,
		{ headers: LRCLIB_HEADERS, timeoutMs: LYRICS_TIMEOUT_MS });
	const list = Array.isArray(found.body) ? found.body : [];
	const close = entry => !duration || !entry?.duration || Math.abs(entry.duration - duration) <= 8;
	const best = list.find(entry => entry?.syncedLyrics && close(entry))
		|| list.find(entry => entry?.plainLyrics && close(entry))
		|| list.find(entry => entry?.syncedLyrics || entry?.plainLyrics);
	return best ? lyricsShape(best) : null;
}

/** YouTube Music's own lyrics for a song: its Lyrics tab, then that page. */
async function youtubeLyrics(videoId) {
	if (!/^[A-Za-z0-9_-]{11}$/.test(String(videoId || ''))) return null;
	const next = await innertube('next', { videoId });
	const tabs = dig(next, 'contents', 'singleColumnMusicWatchNextResultsRenderer', 'tabbedRenderer',
		'watchNextTabbedResultsRenderer', 'tabs') || [];
	const browseId = tabs.map(tab => dig(tab, 'tabRenderer', 'endpoint', 'browseEndpoint', 'browseId'))
		.find(id => String(id || '').startsWith('MPLY'));
	if (!browseId) return null;
	const page = await innertube('browse', { browseId });
	const shelf = dig(page, 'contents', 'sectionListRenderer', 'contents', 0, 'musicDescriptionShelfRenderer');
	const words = text(dig(shelf, 'description', 'runs')).replace(/\r\n?/g, '\n').trim();
	return words ? { plain: words, synced: '', instrumental: false, source: text(dig(shelf, 'footer', 'runs')) } : null;
}

export async function lyrics({ track } = {}) {
	const title = String(track?.title || '').trim();
	if (!title) throw catalogFailure('Nothing to look up.');
	const artist = String(track?.artist || '').trim();
	const duration = Number(track?.duration_s) > 0 ? Math.round(Number(track.duration_s)) : 0;

	const [timed, words] = await Promise.allSettled([lrclib(title, artist, duration), youtubeLyrics(track?.source_id)]);
	const fromLrclib = timed.status === 'fulfilled' ? timed.value : null;
	const fromYoutube = words.status === 'fulfilled' ? words.value : null;
	if (fromLrclib?.synced || fromLrclib?.instrumental) return fromLrclib;
	if (fromYoutube) return fromYoutube;
	if (fromLrclib?.plain) return fromLrclib;
	const outside = await lrclib(title, artist, duration, { outside: true }).catch(() => null);
	if (outside) return outside;
	if (timed.status === 'rejected' && words.status === 'rejected') throw timed.reason;
	return { ...lyricsShape(null), not_found: true };
}

/* ── Following, on this phone ─────────────────────────────────────────────*/

function readFollowed() {
	try {
		const list = JSON.parse(localStorage.getItem(STORAGE.followed) || '[]');
		return Array.isArray(list) ? list : [];
	} catch {
		return [];
	}
}

function writeFollowed(list) {
	try { localStorage.setItem(STORAGE.followed, JSON.stringify(list)); } catch { /* quota */ }
	return { followed_artists: list };
}

const sameArtist = (entry, { artist_id: id, name }) =>
	(id && entry.id === id) || String(entry.name || '').toLowerCase() === String(name || '').toLowerCase();

function follow(payload = {}) {
	const list = readFollowed().filter(entry => !sameArtist(entry, payload));
	return writeFollowed([...list, {
		id: String(payload.artist_id || ''), name: String(payload.name || ''), thumbnail_url: String(payload.thumbnail_url || ''),
	}]);
}

const unfollow = (payload = {}) => writeFollowed(readFollowed().filter(entry => !sameArtist(entry, payload)));

/* ── Commands ─────────────────────────────────────────────────────────────
 * What the phone can answer itself, by the command name the computer uses.
 * `fallback` ones are also tried when a paired computer cannot answer; the
 * follow list is the computer's to keep while one is paired. */

const PHONE = {
	music_catalog_search: { run: ({ query }) => catalogSearch(query), fallback: true },
	music_search: { run: async ({ query }) => ({ items: String(query || '').trim() ? await searchSongs(String(query).trim()) : [] }), fallback: true },
	music_artist_catalog: { run: artistCatalog, fallback: true },
	music_album_tracks: { run: albumTracks, fallback: true },
	music_artist_images: { run: artistImages, fallback: true },
	music_lyrics: { run: lyrics, fallback: true },
	music_followed_artists: { run: async () => ({ followed_artists: readFollowed() }), fallback: false },
	music_artist_follow: { run: async payload => follow(payload), fallback: false },
	music_artist_unfollow: { run: async payload => unfollow(payload), fallback: false },
};

/** The phone's answer for `type`, or null when only a computer has one. */
export function phoneCommand(type) {
	return nativeAvailable() ? (PHONE[type] || null) : null;
}
