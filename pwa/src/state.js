/* Shared state and the pure helpers that read it. One place to answer "what is
 * playing?" for the mini bar, the sheet and the queue. Imports nothing — it
 * must stay a leaf or the graph cycles. */

export const STORAGE = {
	endpoint: 'rainette.pwa.endpoint',
	token: 'rainette.pwa.token',
	deviceId: 'rainette.pwa.device_id',
	recent: 'rainette.pwa.recent',
	volume: 'rainette.pwa.volume',
	repeat: 'rainette.pwa.repeat',
	linked: 'rainette.pwa.linked',
	plays: 'rainette.pwa.plays',
	theme: 'rainette.pwa.theme',
	accent: 'rainette.pwa.accent',
	prefs: 'rainette.pwa.prefs',
	artistArt: 'rainette.pwa.artist_art',
};

/* Repeat is three-state on the desktop ('off' | 'all' | 'one') and the phone
 * mirrors it exactly, so a handoff in either direction carries a mode both ends
 * already understand rather than a boolean one of them has to guess at. */
export const REPEAT_MODES = ['off', 'all', 'one'];

export const REPEAT_LABEL = {
	off: 'Repeat off',
	all: 'Repeat queue',
	one: 'Repeat this track',
};

/* `Number(null)` is 0, not NaN, so a plain Number() check treats "never set" as
 * "set to zero" — which for volume meant every fresh install started silent. */
function storedNumber(key, fallback) {
	const raw = localStorage.getItem(key);
	if (raw === null || raw === '') return fallback;
	const value = Number(raw);
	return Number.isFinite(value) && value >= 0 ? value : fallback;
}

function storedRepeat() {
	const value = localStorage.getItem(STORAGE.repeat);
	return REPEAT_MODES.includes(value) ? value : 'off';
}

export const state = {
	endpoint: localStorage.getItem(STORAGE.endpoint) || '',
	token: localStorage.getItem(STORAGE.token) || '',
	deviceId: localStorage.getItem(STORAGE.deviceId) || '',
	connected: false,
	computerName: '',

	library: [],
	searchResults: [],
	playlists: [],

	queue: [],
	queueIndex: -1,
	/* The queue order before shuffling, kept so unshuffling restores the order
	 * the user built rather than a second random one. */
	queueUnshuffled: null,
	shuffled: false,
	repeat: storedRepeat(),
	volume: storedNumber(STORAGE.volume, 1),

	currentTrack: null,
	/* True while a stream URL is being resolved on the computer. The transport
	 * shows a spinner rather than a stale play glyph, because resolution can
	 * genuinely take a few seconds over a tunnel. */
	loading: false,

	/* Linked mode: this phone mirrors the computer's own session instead of
	 * running an independent one. Persisted, because a user who linked their
	 * phone expects it to still be linked tomorrow. */
	linked: localStorage.getItem(STORAGE.linked) === '1',
	/* Playback the desktop owns, as last broadcast. Only meaningful in linked
	 * mode, and never mixed into the local queue: the phone shows it, and its
	 * transport controls drive the desktop rather than this <audio>. */
	remote: null,

	eventRevision: 0,
	eventLoopId: 0,
	streamRefreshAttempted: false,
	pairPollId: 0,
};

/* One stable identity for a track across search results, library rows, the
 * queue and the recent list, none of which are guaranteed to be the same object
 * or even to carry the same fields. */
export function trackKey(track) {
	return String(
		track?.source_id || track?.video_id || track?.url ||
		`${track?.title || ''}|${track?.artist || ''}`,
	);
}

export function artworkUrl(track) {
	return track?.thumbnail_url || track?.artwork_url || './icon.svg';
}

export function artistName(track) {
	return track?.artist || track?.uploader || track?.metadata?.artist_name || '';
}

/* The computer calls this `duration_s` everywhere; the other spellings only turn
 * up on raw search results. Seconds in every case. */
export function trackDuration(track) {
	for (const value of [track?.duration_s, track?.duration, track?.duration_seconds]) {
		const seconds = Number(value);
		if (Number.isFinite(seconds) && seconds > 0) return seconds;
	}
	return 0;
}

/* How popular a track is, as a single number to order by. The computer sends
 * this under whichever name its source used, and a phone's own play count is
 * folded in so a library with no view counts at all still sorts by something
 * real — what this listener actually plays. */
export function trackPopularity(track) {
	for (const value of [track?.view_count, track?.views, track?.play_count, track?.plays]) {
		const count = typeof value === 'string' ? parseCount(value) : Number(value);
		if (Number.isFinite(count) && count > 0) return count;
	}
	return localPlayCount(trackKey(track));
}

const COUNT_MAGNITUDE = { k: 1e3, m: 1e6, b: 1e9 };

/* "1.4M views" and "23,918 plays" are both display strings rather than numbers. */
function parseCount(value) {
	const match = String(value).trim().toLowerCase().match(/([\d.,]+)\s*([kmb])?/);
	if (!match) return 0;
	const number = Number(match[1].replace(/,/g, ''));
	if (!Number.isFinite(number)) return 0;
	return number * (COUNT_MAGNITUDE[match[2]] || 1);
}

/* ── Play counts, kept on the phone ───────────────────────────────────────
 * Written on every play so "most popular" means something even for a library
 * the computer sends no counts for. Read often and written rarely, so the map
 * is cached rather than parsed out of storage each time. */

let playCounts = null;

function readPlayCounts() {
	if (playCounts) return playCounts;
	try {
		const value = JSON.parse(localStorage.getItem(STORAGE.plays) || '{}');
		playCounts = (value && typeof value === 'object') ? value : {};
	} catch {
		playCounts = {};
	}
	return playCounts;
}

export function localPlayCount(key) {
	return Number(readPlayCounts()[key]) || 0;
}

export function countPlay(track) {
	const key = trackKey(track);
	if (!key) return;
	const counts = readPlayCounts();
	counts[key] = (Number(counts[key]) || 0) + 1;
	try { localStorage.setItem(STORAGE.plays, JSON.stringify(counts)); } catch { /* storage quota */ }
}

export function formatTime(seconds) {
	if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
	const whole = Math.floor(seconds);
	const minutes = Math.floor(whole / 60);
	if (minutes < 60) return `${minutes}:${String(whole % 60).padStart(2, '0')}`;
	return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${String(whole % 60).padStart(2, '0')}`;
}

/** Total run time of a track list, formatted for a queue summary. */
export function totalDuration(tracks) {
	const seconds = tracks.reduce((sum, track) => sum + trackDuration(track), 0);
	if (!seconds) return '';
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) return `${minutes} min`;
	return `${Math.floor(minutes / 60)} hr ${minutes % 60} min`;
}

export function nextRepeat(mode) {
	return REPEAT_MODES[(REPEAT_MODES.indexOf(mode) + 1) % REPEAT_MODES.length] || 'off';
}

export function readRecent() {
	try {
		const value = JSON.parse(localStorage.getItem(STORAGE.recent) || '[]');
		return Array.isArray(value) ? value : [];
	} catch {
		return [];
	}
}

export function rememberRecent(track) {
	const recent = [track, ...readRecent().filter(item => trackKey(item) !== trackKey(track))].slice(0, 30);
	try { localStorage.setItem(STORAGE.recent, JSON.stringify(recent)); } catch { /* storage quota */ }
	return recent;
}

export function persist(key, value) {
	try { localStorage.setItem(key, String(value)); } catch { /* storage quota */ }
}
