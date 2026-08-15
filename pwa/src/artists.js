/* Who made the music, worked out from the music itself.
 *
 * Two problems live here, and they are the same problem twice:
 *
 *  - A library is a list of tracks. The artists behind it have to be derived,
 *    and a track carries its *album* cover, never the artist's picture — which
 *    is why the library's artist rows all showed the wrong image, or the app
 *    icon, until you opened one.
 *  - A search whose computer answers with songs but no artists (an older
 *    Rainette, or one without ytmusicapi) left the Artists tab empty even though
 *    every song in the results names somebody.
 *
 * Artwork is resolved in one batched command for a whole screen, cached in
 * localStorage for good, and only ever asked for once per name.
 */

import { command } from './bridge.js';
import { STORAGE, artistName } from './state.js';

/** One artist, normalised, from whatever shape the caller has. */
export function artistRef(artist) {
	return {
		id: String(artist?.id || artist?.artist_id || artist?.browse_id || artist?.channel_id || ''),
		name: String(artist?.name || artist?.artist || 'Unknown artist'),
		art: artist?.thumbnail_url || artist?.artwork_url || artist?.art || '',
		subscribers: String(artist?.subscribers || ''),
	};
}

function key(name) {
	return String(name || '').trim().toLowerCase();
}

/* ── The artwork cache ────────────────────────────────────────────────────
 * Keyed by the name we asked about rather than the catalog's id, because the
 * name is all a derived artist has. Held in memory and mirrored to storage, so
 * a second visit to the library paints instantly and asks for nothing. */

let cache = null;

function readCache() {
	if (cache) return cache;
	try {
		const stored = JSON.parse(localStorage.getItem(STORAGE.artistArt) || '{}');
		cache = (stored && typeof stored === 'object') ? stored : {};
	} catch {
		cache = {};
	}
	return cache;
}

function writeCache() {
	try { localStorage.setItem(STORAGE.artistArt, JSON.stringify(cache)); } catch { /* quota */ }
}

/** What we already know about an artist, without asking anyone. */
export function knownArtist(name) {
	return readCache()[key(name)] || null;
}

/* A name we asked about and got nothing back for is remembered as a miss, so a
 * library full of artists the catalog does not carry does not re-ask on every
 * visit. Misses expire, because "not found" is often "not found today". */
const MISS_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function isPending(entry) {
	return entry && !entry.art && Date.now() - (entry.at || 0) < MISS_TTL_MS;
}

const inFlight = new Set();

/** Names still worth asking the computer about. */
function unresolved(names) {
	const store = readCache();
	return names.filter(name => {
		const id = key(name);
		if (!id || inFlight.has(id)) return false;
		const entry = store[id];
		return !(entry?.art) && !isPending(entry);
	});
}

/** Resolve artwork for these artists. `onResolved(name, entry)` fires per hit,
 *  so rows can fill in as answers land rather than after all of them do.
 *  Never throws: artwork is decoration, and a computer that cannot supply it
 *  must not take the list down with it. */
export async function resolveArtistImages(names, onResolved) {
	const wanted = unresolved(names);
	if (!wanted.length) return;
	for (const name of wanted) inFlight.add(key(name));

	try {
		const result = await command('music_artist_images', { names: wanted }, 25000);
		const found = Array.isArray(result?.artists) ? result.artists : [];
		const store = readCache();

		for (const raw of found) {
			const ref = artistRef(raw);
			// The command echoes the query back, because the catalog rarely spells
			// a name the way the library does.
			const asked = key(raw?.query || ref.name);
			if (!asked) continue;
			store[asked] = { art: ref.art, id: ref.id, subscribers: ref.subscribers, at: Date.now() };
			if (ref.art) onResolved?.(asked, store[asked]);
		}
		// Anything not answered is a miss, recorded so it is not asked again today.
		for (const name of wanted) {
			if (!store[key(name)]) store[key(name)] = { art: '', id: '', subscribers: '', at: Date.now() };
		}
		writeCache();
	} catch {
		// An older computer refuses the command outright. Nothing is cached, so a
		// newer one will be asked the first time it is reachable.
	} finally {
		for (const name of wanted) inFlight.delete(key(name));
	}
}

/* ── Deriving artists from tracks ─────────────────────────────────────────*/

/** The artists behind a list of tracks, each with how many tracks they have
 *  here. Anything already known about them is folded in. */
export function artistsFromTracks(tracks, seed = []) {
	const byName = new Map();

	for (const entry of seed.map(artistRef)) {
		if (entry.name) byName.set(key(entry.name), { ...entry, count: 0, followed: true });
	}

	for (const track of tracks) {
		const name = artistName(track);
		if (!name) continue;
		const id = key(name);
		const existing = byName.get(id);
		if (existing) {
			existing.count += 1;
			existing.id = existing.id || track?.metadata?.artist_id || '';
		} else {
			byName.set(id, {
				id: track?.metadata?.artist_id || '',
				name,
				art: '',
				subscribers: '',
				count: 1,
				followed: false,
			});
		}
	}

	// The cache is the only source of a genuine artist picture; a track's own
	// thumbnail is its album cover and putting it here is what made the library
	// look like it was showing artists when it was showing sleeves.
	for (const artist of byName.values()) {
		const known = knownArtist(artist.name);
		if (known?.art) artist.art = known.art;
		if (known?.id) artist.id = artist.id || known.id;
		if (known?.subscribers) artist.subscribers = artist.subscribers || known.subscribers;
	}

	return [...byName.values()];
}

/** Artists for a search, using what the computer sent and filling the gaps from
 *  the songs it sent alongside. */
export function searchArtists(fromComputer, songs) {
	const supplied = (Array.isArray(fromComputer) ? fromComputer : []).map(artistRef);
	const seen = new Set(supplied.map(artist => key(artist.name)));

	const derived = artistsFromTracks(songs)
		.filter(artist => artist.name && !seen.has(key(artist.name)))
		// Somebody who appears once in a song list is usually a feature credit
		// rather than a result worth its own row; two is the cheapest signal that
		// the search was actually about them.
		.sort((a, b) => b.count - a.count);

	return [...supplied, ...derived];
}
