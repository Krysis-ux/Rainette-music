/* A playlist from a pasted link, read on the phone: Spotify, Apple Music, Deezer, YouTube.
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
];
// Share buttons hand out these; only they are ever opened as given.
const SHORT = /https:\/\/(?:spotify\.link|link\.deezer\.com|deezer\.page\.link)\/[A-Za-z0-9/_-]+/i;

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
		headers: { 'User-Agent': DESKTOP_UA, ...(data ? { 'Content-Type': 'application/json' } : {}), ...headers },
	});
	if (Number(response?.status) >= 400) throw new Error(`HTTP ${response.status}`);
	const body = response?.data;
	return { url: String(response?.url || url), text: typeof body === 'string' ? body : JSON.stringify(body ?? '') };
}

const entry = (title, artist = '', album = '') =>
	({ title: String(title || '').trim(), artist: String(artist || '').trim(), album: String(album || '').trim() });

function result(source, name, entries, total = 0, extra = {}) {
	const kept = entries.filter(item => item.title).slice(0, MAX_TRACKS);
	return { name, source, total: Math.max(total, kept.length), partial: false, entries: kept, tracks: [], ...extra };
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
	const { album, tracks } = await albumTracks({ album_id: `VL${list}` });
	return result('youtube', album?.title || 'YouTube playlist', [], tracks.length, { tracks });
}

const READERS = { spotify, apple, deezer, youtube };

/** `music_playlist_link`: { name, source, total, partial, entries: [{title, artist, album}], tracks }. */
export async function readPlaylistLink({ url = '' } = {}) {
	const text = String(url || '');
	let found = known(text);
	if (!found) {
		const short = SHORT.exec(text);
		if (!short) throw new Error('Paste a Spotify, Apple Music, Deezer or YouTube playlist link.');
		const page = await get(short[0]);
		found = known(page.url) || known(page.text);
		if (!found) throw new Error('That link did not lead to a playlist. Open it in a browser and copy the address.');
	}
	return READERS[found.source](found.match);
}
