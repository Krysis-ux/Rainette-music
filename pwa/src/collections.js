/* The computer's own recents and playlists, on the phone. Both were already on
 * the companion allowlist; this client simply never asked for them. */

import { state, trackKey, readRecent } from './state.js';
import { command } from './bridge.js';

/** The computer's play history, newest first. Empty if it cannot be reached. */
export async function fetchDesktopRecent() {
	try {
		const result = await command('music_recent', {});
		return Array.isArray(result?.tracks) ? result.tracks : [];
	} catch {
		return [];
	}
}

/* What the phone played and what the computer played are one history. The
 * computer's copy wins on collision: it carries duration_s and artwork that a
 * locally-remembered search result often lacks. */
export function mergeRecent(desktop, local = readRecent()) {
	const merged = [...desktop];
	const seen = new Set(desktop.map(trackKey));
	for (const track of local) {
		if (seen.has(trackKey(track))) continue;
		seen.add(trackKey(track));
		merged.push(track);
	}
	return merged;
}

export async function fetchPlaylists() {
	const result = await command('music_playlist_list', {});
	const playlists = result?.playlists || result?.items || [];
	state.playlists = Array.isArray(playlists) ? playlists : [];
	return state.playlists;
}

export async function fetchPlaylistTracks(playlistId) {
	const result = await command('music_playlist_tracks', { playlist_id: playlistId });
	return Array.isArray(result?.tracks) ? result.tracks : [];
}

export function playlistSubtitle(playlist) {
	const count = Number(playlist?.track_count ?? playlist?.count ?? 0);
	if (!count) return 'Empty';
	return `${count} track${count === 1 ? '' : 's'}`;
}
