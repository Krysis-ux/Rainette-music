/* The secondary surfaces the now-playing card reaches: playlists, lyrics, the
 * sleep timer, the output picker, and a track's overflow menu.
 *
 * These are the desktop features the phone client was missing rather than new
 * ideas — the computer already exposes every command they need, and the phone
 * simply never called them. Each one is a sheet, so they stack over the card
 * instead of replacing it, and dismissing one returns you to where you were.
 */

import { el, toast, tap } from './dom.js';
import { state, STORAGE, persist, artistName } from './state.js';
import { openSheet, actionSheet } from './sheets.js';
import { command } from './bridge.js';
import { queueAddNext, queueAddEnd, isPlaying, toggle } from './player.js';

/* ── Add to playlist ───────────────────────────────────────────────────────*/

export async function openAddToPlaylist(track) {
	let playlists = state.playlists;
	try {
		const result = await command('music_playlist_list', {});
		playlists = Array.isArray(result?.playlists) ? result.playlists : (result?.items || []);
		state.playlists = playlists;
	} catch {
		// A stale list still lets somebody add to a playlist they already know
		// about, which beats refusing outright because one request failed.
	}

	const items = playlists.map(playlist => ({
		id: `pl:${playlist.id}`,
		label: playlist.name || 'Untitled playlist',
		hint: playlist.track_count ? `${playlist.track_count} tracks` : '',
		icon: 'listAdd',
		run: async () => {
			try {
				await command('music_playlist_add_track', { playlist_id: playlist.id, track });
				toast(`Added to ${playlist.name || 'playlist'}`);
			} catch (error) {
				toast(error?.message || 'Could not add that track.', { icon: 'close' });
			}
		},
	}));

	items.push({
		id: 'new',
		label: 'New playlist',
		icon: 'plus',
		run: () => openNewPlaylist(track),
	});

	await actionSheet({ title: 'Add to playlist', items });
}

function openNewPlaylist(track) {
	openSheet({
		title: 'New playlist',
		className: 'sheet-actions',
		build: ({ body, close }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'New playlist'));
			const field = el('label', 'field');
			field.append(el('span', '', 'Name'));
			const input = document.createElement('input');
			input.type = 'text';
			input.maxLength = 80;
			input.placeholder = 'Late night';
			input.autocapitalize = 'words';
			field.append(input);

			const create = el('button', 'primary', 'Create and add');
			create.type = 'button';
			create.addEventListener('click', async () => {
				const name = input.value.trim();
				if (!name) { input.focus(); return; }
				create.disabled = true;
				try {
					const made = await command('music_playlist_create', { name });
					const id = made?.playlist?.id || made?.id;
					if (id) await command('music_playlist_add_track', { playlist_id: id, track });
					toast(`Added to ${name}`);
					close();
				} catch (error) {
					create.disabled = false;
					toast(error?.message || 'Could not create that playlist.', { icon: 'close' });
				}
			});

			body.append(field, create);
			// Deferred so the sheet's entrance is not interrupted by the keyboard
			// sliding up over it mid-animation.
			setTimeout(() => input.focus(), 260);
		},
	});
}

/* ── Lyrics ────────────────────────────────────────────────────────────────
 * Fetched by the computer from LRCLIB and cached there, so opening the same
 * track twice is instant and the phone never talks to a third party itself. */

export function openLyrics(track) {
	openSheet({
		title: 'Lyrics',
		className: 'sheet-lyrics',
		full: true,
		build: async ({ body }) => {
			const head = el('div', 'lyrics-head sheet-drag');
			head.append(el('h2', 'sheet-title', track.title || 'Lyrics'));
			head.append(el('p', 'lyrics-artist', artistName(track) || ''));
			const content = el('div', 'lyrics-body');
			content.append(el('p', 'empty', 'Looking for lyrics…'));
			body.append(head, content);

			let result;
			try {
				result = await command('music_lyrics', { track }, 20000);
			} catch (error) {
				content.replaceChildren(el('p', 'empty', error?.message || 'Could not reach your computer for lyrics.'));
				return;
			}
			if (result?.instrumental) {
				content.replaceChildren(el('p', 'empty', 'This one is instrumental.'));
				return;
			}
			const text = String(result?.text || result?.plain || '').trim();
			if (!text) {
				content.replaceChildren(el('p', 'empty', 'No lyrics found for this track.'));
				return;
			}
			content.replaceChildren();
			for (const line of text.split('\n')) {
				// A blank line is a stanza break and carries real structure, so
				// it is kept as spacing rather than collapsed away.
				content.append(el('p', line.trim() ? 'lyrics-line' : 'lyrics-gap', line.trim()));
			}
		},
	});
}

/* ── Sleep timer ───────────────────────────────────────────────────────────
 * Runs on the phone and pauses the phone's own transport, which is the only
 * device it can honestly promise to stop. */

const sleep = { until: 0, endOfTrack: false, timerId: 0 };

export function sleepActive() {
	return sleep.endOfTrack || sleep.until > Date.now();
}

export function sleepLabel() {
	if (sleep.endOfTrack) return 'After track';
	if (sleep.until > Date.now()) return `${Math.max(1, Math.ceil((sleep.until - Date.now()) / 60000))} min`;
	return 'Sleep';
}

export function cancelSleep() {
	clearTimeout(sleep.timerId);
	sleep.timerId = 0;
	sleep.until = 0;
	sleep.endOfTrack = false;
}

/** True when a finishing track should stop playback, consumed by app.js. */
export function sleepShouldStopAfterTrack() {
	if (!sleep.endOfTrack) return false;
	cancelSleep();
	return true;
}

export async function openSleepTimer() {
	const items = [15, 30, 45, 60].map(minutes => ({
		id: `sleep:${minutes}`,
		label: `Stop in ${minutes} minutes`,
		icon: 'moon',
		run: () => {
			cancelSleep();
			sleep.until = Date.now() + minutes * 60000;
			sleep.timerId = setTimeout(() => {
				cancelSleep();
				if (isPlaying()) toggle();
			}, minutes * 60000);
			toast(`Stopping in ${minutes} minutes`, { icon: 'moon' });
		},
	}));
	items.push({
		id: 'sleep:track',
		label: 'Stop after this track',
		icon: 'moon',
		run: () => { cancelSleep(); sleep.endOfTrack = true; toast('Stopping after this track', { icon: 'moon' }); },
	});
	if (sleepActive()) {
		items.push({ id: 'sleep:off', label: 'Cancel sleep timer', danger: true, run: () => { cancelSleep(); toast('Sleep timer cancelled'); } });
	}
	await actionSheet({ title: 'Sleep timer', items });
}

/* ── Play on ───────────────────────────────────────────────────────────────
 * The phone's half of the same picker the desktop has. It lists this phone, the
 * computer, and the computer's audio outputs — so a Bluetooth speaker paired to
 * the computer is nameable from the phone, which is where somebody holding the
 * phone would look for it. */

const OUTPUT_ICONS = {
	bluetooth: 'bluetooth', headphones: 'headphones', builtin: 'laptop',
	hdmi: 'speaker', airplay: 'speaker', usb: 'speaker', virtual: 'speaker',
};

export async function openOutputPicker() {
	let devices = [];
	try {
		const result = await command('music_output_devices', {}, 8000);
		devices = Array.isArray(result?.devices) ? result.devices : [];
	} catch { /* the list is a convenience; the two endpoints below always work */ }

	const items = [{
		id: 'phone',
		label: 'This phone',
		icon: 'phone',
		hint: state.linked ? '' : 'Playing here',
		run: () => setLinked(false),
	}];

	const computer = state.computerName || 'Your computer';
	items.push({
		id: 'desktop',
		label: computer,
		icon: 'laptop',
		hint: state.linked ? 'Playing here' : 'Follow this computer',
		run: () => setLinked(true),
	});

	for (const device of devices) {
		if (device.kind === 'builtin') continue;   // already covered by the computer row
		items.push({
			id: device.id,
			label: device.name,
			icon: OUTPUT_ICONS[device.kind] || 'speaker',
			hint: device.is_default ? `Connected to ${computer}` : 'On your computer',
			run: () => {
				// The phone cannot re-route the computer's audio, and pretending
				// otherwise would be the worst possible answer here.
				setLinked(true);
				toast(`Following ${computer}`, { icon: 'link' });
			},
		});
	}

	await actionSheet({ title: 'Play on', items });
}

/* Linked mode is what "synced with the computer" actually is: the phone stops
 * running its own session and mirrors the desktop's. Exported so the settings
 * panel can toggle the same thing. */
let onLinkChange = () => {};

export function configureExtras(options) {
	onLinkChange = options.onLinkChange || onLinkChange;
}

export function setLinked(linked) {
	if (state.linked === linked) return;
	state.linked = linked;
	persist(STORAGE.linked, linked ? '1' : '0');
	if (!linked) state.remote = null;
	tap();
	onLinkChange(linked);
}

/* ── Track overflow menu ───────────────────────────────────────────────────*/

export async function openTrackMenu(track, list = []) {
	await actionSheet({
		title: track.title || 'Track',
		items: [
			{ id: 'next', label: 'Play next', icon: 'queue', run: () => { queueAddNext(track); toast('Playing next', { icon: 'queue' }); } },
			{ id: 'end', label: 'Add to queue', icon: 'listAdd', run: () => { queueAddEnd(track); toast('Added to queue', { icon: 'listAdd' }); } },
			{ id: 'playlist', label: 'Add to playlist', icon: 'plus', run: () => openAddToPlaylist(track) },
			{ id: 'lyrics', label: 'Lyrics', icon: 'mic', run: () => openLyrics(track) },
			list.length ? { id: 'queue-all', label: `Queue all ${list.length}`, icon: 'listAdd', run: () => { for (const item of list) queueAddEnd(item); toast(`Queued ${list.length} tracks`, { icon: 'listAdd' }); } } : null,
		].filter(Boolean),
	});
}
