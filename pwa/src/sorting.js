/* Sorting, and the little control that drives it.
 *
 * Every list in this client arrived in whatever order the computer happened to
 * send it, with no way to reorder it. The desktop sorts, so the phone reading
 * the same library should too.
 *
 * The chosen mode is remembered per scope, because a sort is a preference about
 * a kind of list rather than about one visit to it: a library sorted by artist
 * should still be sorted by artist tomorrow.
 */

import { el, icon } from './dom.js';
import { actionSheet } from './sheets.js';
import { artistName, trackDuration, trackPopularity } from './state.js';

const STORE_PREFIX = 'rainette.pwa.sort.';

/* `undefined` compares as greater than everything under localeCompare, which
 * buries untitled rows at the bottom in one direction and floats them in the
 * other. Coercing here keeps a missing field consistently last. */
function text(value) {
	return String(value ?? '').trim();
}

function byText(pick) {
	return (a, b) => {
		const left = text(pick(a));
		const right = text(pick(b));
		if (!left) return right ? 1 : 0;
		if (!right) return -1;
		return left.localeCompare(right, undefined, { sensitivity: 'base', numeric: true });
	};
}

function albumOf(track) {
	return track?.metadata?.album_name || track?.metadata?.album?.name || track?.album || '';
}

/* Sorts available for a list of tracks. 'default' keeps the order the computer
 * sent, which for an album is its running order and for search is relevance —
 * both of which are real orders worth being able to get back to. */
/* Popularity is play counts where the computer supplied them. Where it did not,
 * every track scores zero and the sort is a no-op that leaves the list in the
 * order it arrived — which for a search *is* relevance, so the answer stays
 * sensible rather than becoming arbitrary. */
function byPopularity(a, b) {
	const left = trackPopularity(a);
	const right = trackPopularity(b);
	if (left === right) return 0;
	return right - left;
}

export const TRACK_SORTS = [
	{ id: 'default', label: 'Default order', hint: 'As your computer sent it' },
	{ id: 'popular', label: 'Most popular', hint: 'Most played first', compare: byPopularity },
	{ id: 'least', label: 'Least popular', hint: 'Deep cuts first', compare: (a, b) => -byPopularity(a, b) },
	{ id: 'title', label: 'Title', compare: byText(track => track?.title) },
	{ id: 'artist', label: 'Artist', compare: byText(artistName) },
	{ id: 'album', label: 'Album', compare: byText(albumOf) },
	{ id: 'longest', label: 'Longest first', compare: (a, b) => trackDuration(b) - trackDuration(a) },
	{ id: 'shortest', label: 'Shortest first', compare: (a, b) => trackDuration(a) - trackDuration(b) },
];

/* Artists and albums are not tracks and have no duration or album of their own,
 * so they get their own short list rather than a track sort that mostly no-ops
 * on them. */
export const ARTIST_SORTS = [
	{ id: 'default', label: 'Default order', hint: 'As your computer sent it' },
	{ id: 'name', label: 'Name', compare: byText(artist => artist?.name) },
	{ id: 'popular', label: 'Most followers', compare: (a, b) => subscriberCount(b) - subscriberCount(a) },
];

/* "1.2M subscribers" is a display string, not a number. Reading the magnitude
 * off the suffix is enough to order a list by it. */
const MAGNITUDE = { k: 1e3, m: 1e6, b: 1e9 };

export function subscriberCount(artist) {
	const raw = String(artist?.subscribers || '').trim().toLowerCase();
	const match = raw.match(/^([\d.,]+)\s*([kmb])?/);
	if (!match) return 0;
	const value = Number(match[1].replace(/,/g, ''));
	if (!Number.isFinite(value)) return 0;
	return value * (MAGNITUDE[match[2]] || 1);
}

export function sortArtists(artists, id) {
	return applySort(artists, ARTIST_SORTS, id);
}

export const RELEASE_SORTS = [
	{ id: 'default', label: 'Default order', hint: 'As your computer sent it' },
	{ id: 'newest', label: 'Newest first', compare: (a, b) => Number(b?.year || 0) - Number(a?.year || 0) },
	{ id: 'oldest', label: 'Oldest first', compare: (a, b) => Number(a?.year || 0) - Number(b?.year || 0) },
	{ id: 'title', label: 'Title', compare: byText(album => album?.title) },
];

function findMode(modes, id) {
	return modes.find(mode => mode.id === id) || modes[0];
}

/* Array.prototype.sort is stable, so ties keep the order the computer sent —
 * two tracks with the same artist stay in album order rather than shuffling
 * between renders. */
function applySort(list, modes, id) {
	const mode = findMode(modes, id);
	if (!mode?.compare) return list;
	return list.slice().sort(mode.compare);
}

export function sortTracks(tracks, id) {
	return applySort(tracks, TRACK_SORTS, id);
}

export function sortReleases(releases, id) {
	return applySort(releases, RELEASE_SORTS, id);
}

export function readSort(scope, modes = TRACK_SORTS) {
	try {
		const stored = localStorage.getItem(STORE_PREFIX + scope);
		if (stored && modes.some(mode => mode.id === stored)) return stored;
	} catch { /* private mode */ }
	return modes[0].id;
}

function writeSort(scope, id) {
	try { localStorage.setItem(STORE_PREFIX + scope, id); } catch { /* storage quota */ }
}

/** A sort button that opens the mode list and reports the choice.
 *
 *  Returns `{ node, apply, current }`. `apply()` fires `onChange` with the
 *  remembered mode, so a caller renders once and does not have to duplicate the
 *  "what was it last time" logic.
 */
export function sortControl({ scope, modes = TRACK_SORTS, onChange, label = 'Sort' }) {
	let current = readSort(scope, modes);

	const node = el('div', 'sort-bar');
	const button = el('button', 'chip sort-chip');
	button.type = 'button';

	const paint = () => {
		const mode = findMode(modes, current);
		button.innerHTML = icon('sort', 15);
		button.append(el('span', '', mode.id === 'default' ? label : mode.label));
		button.classList.toggle('on', mode.id !== 'default');
		button.setAttribute('aria-label', `${label}: ${mode.label}`);
	};

	button.addEventListener('click', async () => {
		await actionSheet({
			title: 'Sort by',
			items: modes.map(mode => ({
				id: mode.id,
				label: mode.label,
				hint: mode.hint || '',
				active: mode.id === current,
				run: () => {
					if (mode.id === current) return;
					current = mode.id;
					writeSort(scope, current);
					paint();
					onChange?.(current);
				},
			})),
		});
	});

	paint();
	node.append(button);

	return {
		node,
		apply: () => onChange?.(current),
		current: () => current,
	};
}
