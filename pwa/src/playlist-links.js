/* A playlist from a pasted link, read on the phone: Spotify, Apple Music, YouTube, SoundCloud, Tidal,
 * Deezer, JioSaavn, NetEase, or any page that publishes its track list (Qobuz, Bandcamp...).
 * The computer's playlist_links.py answers the same `music_playlist_link`. */

import { nativeRequest, albumTracks, DESKTOP_UA } from './ytmusic.js';

const PAGE = 100;
const MAX_TRACKS = 2000;
// The Spotify web player's own playlist query. Spotify can rotate it; then only the first 100 load.
const SPOTIFY_PAGE_QUERY = '19ff1327c29e99c208c86d7a9d8f1929cfdf3d3202a0ff4253c821f1901aa94d';
const SPOTIFY_PATHFINDER = 'https://api-partner.spotify.com/pathfinder/v2/query';

const PATTERNS = [
	['spotify', /(?:open\.spotify\.com\/(?:intl-[a-z-]+\/)?(?:embed\/)?|spotify:)(playlist|album)[/:]([A-Za-z0-9]{10,40})/i],
	['apple', /music\.apple\.com\/([a-z]{2})\/(playlist|album)\/(?:[^/?#\s]+\/)?(pl\.[A-Za-z0-9-]+|\d+)/i],
	['deezer', /deezer\.com\/(?:[a-z]{2}\/)?(playlist|album)\/(\d+)/i],
	['youtube', /(?:youtube\.com|youtu\.be)\/\S*?[?&]list=([A-Za-z0-9_-]{10,80})/i],
	['soundcloud', /soundcloud\.com\/([\w-]+)\/sets\/([\w-]+)(?:\/(s-\w+))?/i],
	['tidal', /tidal\.com\/(?:browse\/)?(playlist|album)\/([0-9a-fA-F-]{36}|\d+)/i],
	['jiosaavn', /jiosaavn\.com\/(?:s\/)?(featured|playlist|album)\/(?:[^/?#\s]+\/)*([\w-]+)/i],
	['netease', /music\.163\.com\/\S*?playlist\?(?:\S*?&)?id=(\d+)/i],
];
// Share buttons hand out these; they are opened as given and must lead to one of the above.
const SHORT = /https:\/\/(?:spotify\.link|link\.deezer\.com|deezer\.page\.link|on\.soundcloud\.com|apple\.co)\/[A-Za-z0-9/_-]+/i;
const LINK = /https:\/\/\S+/i;
const BROWSER_ACCEPT = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8';

function known(text) {
	for (const [source, pattern] of PATTERNS) {
		const match = pattern.exec(text);
		if (match) return { source, match };
	}
	return null;
}

/** A link (any link: an unknown one should hear why), not a list of songs. */
export function looksLikeLink(text) {
	const value = String(text || '');
	return /^\s*(https?:\/\/|spotify:)/i.test(value) || Boolean(known(value) || SHORT.test(value));
}

/** `{ url, text }` for a GET, or a POST of `data` as JSON. */
async function get(url, { data, headers = {} } = {}) {
	const response = await nativeRequest({
		url, method: data ? 'POST' : 'GET', data,
		// A browser's Accept: Bandcamp answers anything else with a bot check.
		headers: { 'User-Agent': DESKTOP_UA, Accept: BROWSER_ACCEPT, 'Accept-Language': 'en-US,en;q=0.9',
			...(data ? { 'Content-Type': 'application/json' } : {}), ...headers },
	});
	if (Number(response?.status) >= 400) throw new Error(`HTTP ${response.status}`);
	const body = response?.data;
	return { url: String(response?.url || url), text: typeof body === 'string' ? body : JSON.stringify(body ?? '') };
}

const ENTITIES = { amp: '&', quot: '"', apos: "'", lt: '<', gt: '>', nbsp: ' ' };
/* JioSaavn and page titles arrive with HTML entities (&quot;, &#39;). */
const decode = value => String(value || '').replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, code) => {
	if (code[0] !== '#') return ENTITIES[code.toLowerCase()] ?? whole;
	const hex = code[1].toLowerCase() === 'x';
	return String.fromCodePoint(parseInt(code.slice(hex ? 2 : 1), hex ? 16 : 10));
});

const entry = (title, artist = '', album = '') =>
	({ title: decode(title).trim(), artist: decode(artist).trim(), album: decode(album).trim() });

/* Partial is decided here, after the ceiling: a reader cannot see what the cut drops. */
function result(source, name, entries, total = 0, extra = {}) {
	const titled = entries.filter(item => item.title);
	const kept = titled.slice(0, MAX_TRACKS);
	const ready = extra.tracks?.length || 0; // YouTube's songs come ready, not as entries
	const whole = Math.max(total, titled.length + ready);
	return { name, source, tracks: [], ...extra, total: whole, partial: Boolean(extra.partial) || whole > kept.length + ready, entries: kept };
}

function scriptJson(page, opening) {
	const start = page.indexOf(opening);
	if (start < 0) return null;
	const from = start + opening.length;
	try { return JSON.parse(page.slice(from, page.indexOf('</script>', from))); } catch { return null; }
}

/* ── Spotify ──────────────────────────────────────────────────────────────*/

async function spotify([, rawKind, id]) {
	const kind = rawKind.toLowerCase();
	const { text: page } = await get(`https://open.spotify.com/embed/${kind}/${id}`).catch(() => ({ text: '' }));
	const state = scriptJson(page, '<script id="__NEXT_DATA__" type="application/json">')?.props?.pageProps?.state;
	const entity = state?.data?.entity;
	if (!entity?.trackList?.length) throw new Error('Spotify would not share that playlist. Make it public, or export it with exportify.net.');
	const entries = entity.trackList.map(track =>
		entry(track?.title, String(track?.subtitle || '').split(', ')[0], kind === 'album' ? entity.name : ''));
	let total = entries.length;
	let partial = false;
	// The embed stops at 100; the web player's query, with the embed's own guest token, has the rest.
	if (kind === 'playlist' && entries.length >= PAGE) {
		({ total, partial } = await spotifyRest(id, state?.settings?.session?.accessToken, entries));
	}
	return result('spotify', String(entity.name || ''), entries, total, { partial });
}

async function spotifyRest(id, token, entries) {
	let total = 0;
	try {
		while (entries.length < MAX_TRACKS) {
			const { text } = await get(SPOTIFY_PATHFINDER, {
				data: {
					variables: { uri: `spotify:playlist:${id}`, offset: entries.length, limit: PAGE },
					operationName: 'fetchPlaylist',
					extensions: { persistedQuery: { version: 1, sha256Hash: SPOTIFY_PAGE_QUERY } },
				},
				headers: { Authorization: `Bearer ${token}` },
			});
			const content = JSON.parse(text).data.playlistV2.content;
			total = Number(content.totalCount);
			const items = content.items || [];
			for (const item of items) {
				const track = item?.itemV2?.data || {};
				entries.push(entry(track.name, track.artists?.items?.[0]?.profile?.name, track.albumOfTrack?.name));
			}
			if (!items.length || entries.length >= total) return { total, partial: false };
		}
	} catch { /* the first 100 are still worth importing */ }
	return { total: Math.max(total, entries.length), partial: true };
}

/* ── Apple Music ──────────────────────────────────────────────────────────*/

async function apple([, country, rawKind, id]) {
	const kind = rawKind.toLowerCase();
	const { text: page } = await get(`https://music.apple.com/${country.toLowerCase()}/${kind}/${id}`).catch(() => ({ text: '' }));
	const data = scriptJson(page, '<script type="application/json" id="serialized-server-data">');
	let name = '';
	let total = 0;
	const entries = [];
	for (const section of data?.data?.[0]?.data?.sections || []) {
		for (const item of section?.items || []) {
			if (section.itemKind === 'containerDetailHeaderLockup') {
				name = String(item.title || '');
				total = Number(item.trackCount) || 0;
			} else if (section.itemKind === 'trackLockup') {
				entries.push(entry(item.title, item.artistName, item.tertiaryLinks?.[0]?.title || (kind === 'album' ? name : '')));
			}
		}
	}
	if (!entries.length) throw new Error('Apple Music would not share that playlist. Make it public first.');
	return result('apple', name, entries, total, { partial: total > entries.length });
}

/* ── Deezer ───────────────────────────────────────────────────────────────*/

async function deezer([, rawKind, id]) {
	const kind = rawKind.toLowerCase();
	const info = JSON.parse((await get(`https://api.deezer.com/${kind}/${id}`)).text);
	if (!info || info.error) throw new Error('Deezer would not share that playlist. Make it public first.');
	const name = String(info.title || '');
	const rows = [...(info.tracks?.data || [])];
	const total = Number(info.nb_tracks) || rows.length;
	while (kind === 'playlist' && rows.length < Math.min(total, MAX_TRACKS)) {
		const page = JSON.parse((await get(`https://api.deezer.com/playlist/${id}/tracks?index=${rows.length}&limit=${PAGE}`)).text);
		if (!page?.data?.length) break;
		rows.push(...page.data);
	}
	const entries = rows.map(row => entry(row?.title, row?.artist?.name, row?.album?.title || (kind === 'album' ? name : '')));
	return result('deezer', name, entries, total, { partial: total > entries.length });
}

/* ── YouTube ──────────────────────────────────────────────────────────────*/

async function youtube([, list]) {
	// Already YouTube's songs: nothing to look up.
	const { album, tracks, partial } = await albumTracks({ album_id: `VL${list}`, all_pages: true });
	return result('youtube', album?.title || 'YouTube playlist', [], tracks.length, { tracks, partial: Boolean(partial) });
}

/* ── SoundCloud ───────────────────────────────────────────────────────────*/

async function soundcloud([, user, slug, secret]) {
	const { text: page } = await get(`https://soundcloud.com/${user}/sets/${slug}${secret ? `/${secret}` : ''}`).catch(() => ({ text: '' }));
	let playlist = null;
	try {
		const hydration = /window\.__sc_hydration\s*=\s*(\[.*?\]);\s*<\/script>/s.exec(page);
		playlist = JSON.parse(hydration?.[1] || '[]').find(item => item?.hydratable === 'playlist')?.data || null;
	} catch { /* reported below */ }
	if (!playlist) throw new Error('SoundCloud would not share that playlist. Make it public first.');
	let tracks = (playlist.tracks || []).filter(Boolean);
	// The page carries the first few in full and only ids for the rest; SoundCloud's own web API has them.
	const missing = tracks.filter(track => !track.title).map(track => String(track.id));
	try {
		if (missing.length) {
			const clientId = await soundcloudClientId(page);
			const found = new Map();
			for (let at = 0; at < missing.length; at += 50) {
				const { text } = await get(`https://api-v2.soundcloud.com/tracks?ids=${missing.slice(at, at + 50).join(',')}&client_id=${clientId}`);
				for (const track of JSON.parse(text)) found.set(String(track?.id), track);
			}
			tracks = tracks.map(track => found.get(String(track.id)) || track);
		}
	} catch { /* the ones the page carried are still worth importing */ }
	const entries = tracks.map(track => entry(track.title, track.publisher_metadata?.artist || track.user?.username));
	const total = Number(playlist.track_count) || tracks.length;
	return result('soundcloud', String(playlist.title || ''), entries, total, { partial: entries.filter(item => item.title).length < total });
}

async function soundcloudClientId(page) {
	const scripts = [...page.matchAll(/src="(https:\/\/a-v2\.sndcdn\.com\/assets\/[^"]+\.js)"/g)].map(match => match[1]).reverse();
	for (const script of scripts) {
		const found = /client_id\s*:\s*"(\w{32})"/.exec((await get(script)).text);
		if (found) return found[1];
	}
	throw new Error('SoundCloud did not answer.');
}

/* ── Tidal ────────────────────────────────────────────────────────────────*/

async function tidal([, rawKind, id]) {
	const kind = rawKind.toLowerCase();
	const { text: page } = await get(`https://embed.tidal.com/${kind}s/${id}`).catch(() => ({ text: '' }));
	const opening = 'window.tidalMediaSessionMetadata=';
	const start = page.indexOf(opening);
	const body = start < 0 ? '' : page.slice(start + opening.length, page.indexOf('</script>', start)).trim().replace(/;$/, '');
	let tracks = {};
	try { tracks = JSON.parse(body) || {}; } catch { /* reported below */ }
	// JSON.parse sorts number-like keys; the page's own order is the playlist's.
	const order = [...body.matchAll(/"(\d+)":\{/g)].map(match => match[1]).filter(key => tracks[key]);
	const entries = order.map(key => tracks[key]).map(track => entry(track.title, String(track.artist || '').split(', ')[0], track.album));
	if (!entries.length) throw new Error('Tidal would not share that playlist. Make it public first.');
	let name = '';
	let total = 0;
	try { // its name and size are on the share page
		const share = (await get(`https://tidal.com/browse/${kind}/${id}`)).text;
		name = decode(/og:title" content="([^"]*)/.exec(share)?.[1] || '');
		total = Number(/og:description" content="[^"]*?(\d+) (?:items|tracks)/.exec(share)?.[1]) || 0;
	} catch { /* the songs are what matter */ }
	return result('tidal', name, entries, total, { partial: total > entries.length });
}

/* ── JioSaavn ─────────────────────────────────────────────────────────────*/

async function jiosaavn([, rawKind, token]) {
	const kind = rawKind.toLowerCase() === 'album' ? 'album' : 'playlist';
	let name = '';
	let total = 0;
	const entries = [];
	for (let page = 1; page <= MAX_TRACKS / 50; page += 1) {
		const data = JSON.parse((await get(`https://www.jiosaavn.com/api.php?__call=webapi.get&token=${token}&type=${kind}`
			+ `&p=${page}&n=50&includeMetaTags=0&ctx=web6dot0&api_version=4&_format=json&_marker=0`)).text);
		const songs = Array.isArray(data?.list) ? data.list : []; // "" when the token is unknown
		name ||= String(data?.title || '');
		total = Number(data?.list_count) || 0;
		entries.push(...songs.map(song => entry(song?.title, song?.more_info?.artistMap?.primary_artists?.[0]?.name, song?.more_info?.album)));
		if (!songs.length || entries.length >= total) break;
	}
	if (!entries.length) throw new Error('JioSaavn would not share that playlist.');
	return result('jiosaavn', decode(name), entries, total, { partial: total > entries.length });
}

/* ── NetEase Cloud Music ──────────────────────────────────────────────────*/

async function netease([, id]) {
	const list = JSON.parse((await get(`https://music.163.com/api/playlist/detail?id=${id}`)).text)?.result || {};
	const entries = (list.tracks || []).map(track => entry(track?.name, track?.artists?.[0]?.name, track?.album?.name));
	if (!entries.length) throw new Error('NetEase would not share that playlist.');
	const total = Number(list.trackCount) || 0;
	return result('netease', String(list.name || ''), entries, total, { partial: total > entries.length });
}

/* ── Any page that publishes its track list (schema.org): Qobuz, Bandcamp and others ──*/

async function webPage(url) {
	const { text: page } = await get(url);
	for (const [, block] of page.matchAll(/<script[^>]*application\/ld\+json[^>]*>([\s\S]*?)<\/script>/gi)) {
		let data;
		try { data = JSON.parse(block); } catch { continue; }
		for (const item of Array.isArray(data) ? data : data?.['@graph'] || [data]) {
			const found = schemaList(item);
			if (found) return found;
		}
	}
	throw new Error('Rainette can’t read a playlist from that page. Try a Spotify, Apple Music, YouTube, SoundCloud, Tidal or Deezer link, or a CSV.');
}

const schemaName = value => {
	const first = Array.isArray(value) ? value[0] : value;
	return first && typeof first === 'object' ? String(first.name || '') : String(first || '');
};

function schemaList(item) {
	if (!item || !['MusicPlaylist', 'MusicAlbum'].includes(item['@type'])) return null;
	const listed = item.track?.itemListElement || item.track || [];
	const name = String(item.name || '');
	const albumArtist = schemaName(item.byArtist);
	const entries = (Array.isArray(listed) ? listed : []).map(raw => {
		const track = raw?.item || raw || {};
		return entry(track.name, schemaName(track.byArtist) || albumArtist,
			schemaName(track.inAlbum) || (item['@type'] === 'MusicAlbum' ? name : ''));
	});
	if (!entries.some(song => song.title)) return null;
	const total = Number(item.numTracks) || 0;
	return result('web', decode(name), entries, total, { partial: total > entries.length });
}

const READERS = { spotify, apple, deezer, youtube, soundcloud, tidal, jiosaavn, netease };

/** `music_playlist_link`: { name, source, total, partial, entries: [{title, artist, album}], tracks }. */
export async function readPlaylistLink({ url = '' } = {}) {
	const text = String(url || '');
	let found = known(text);
	if (!found) {
		const short = SHORT.exec(text);
		if (short) {
			const page = await get(short[0]);
			found = known(page.url) || known(page.text);
			if (!found) throw new Error('That link did not lead to a playlist. Open it in a browser and copy the address.');
		} else if (LINK.test(text)) {
			return webPage(LINK.exec(text)[0].replace(/[).,>"']+$/, ''));
		} else {
			throw new Error('Paste a playlist link: Spotify, Apple Music, YouTube, SoundCloud, Tidal, Deezer and more.');
		}
	}
	return READERS[found.source](found.match);
}
