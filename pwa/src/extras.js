/* The surfaces the now-playing card reaches: playlists, lyrics, sleep timer,
 * output picker, track menu. Each is a sheet, so they stack over the card
 * rather than replacing it. */

import { el, toast, tap, motionOff } from './dom.js';
import { state, STORAGE, persist, artistName, trackKey } from './state.js';
import { openSheet, actionSheet } from './sheets.js';
import { command, commandError } from './bridge.js';
import { openArtist, openAlbum, trackArtist, trackAlbum } from './catalog.js';
import { playlistChoices, addTrackToPlaylist, isLocalPlaylist, createLocalPlaylist } from './playlists.js';
import { isLocalTrack } from './local.js';
import {
	queueAddNext, queueAddEnd, isPlaying, toggle, playTrack, pauseLocal, localSession,
	currentTrack, currentTime, seekTo, subscribe,
} from './player.js';

/* ── Add to playlist ───────────────────────────────────────────────────────*/

export async function openAddToPlaylist(track) {
	// The phone's own playlists come first and are always available; the
	// computer's are added when it answers. A stale list still lets somebody add
	// to a playlist they already know about, which beats refusing outright.
	const playlists = await playlistChoices();
	state.playlists = playlists.filter(playlist => !isLocalPlaylist(playlist));

	const items = playlists.map(playlist => ({
		id: `pl:${playlist.id}`,
		label: playlist.name || 'Untitled playlist',
		hint: isLocalPlaylist(playlist)
			? `On this phone · ${playlist.tracks?.length || 0}`
			: (playlist.track_count ? `${playlist.track_count} tracks` : ''),
		icon: 'listAdd',
		run: async () => {
			try {
				await addTrackToPlaylist(playlist, track);
				toast(`Added to ${playlist.name || 'playlist'}`);
			} catch (error) {
				toast(error?.message || 'Could not add that track.', { icon: 'close' });
			}
		},
	}));

	items.push({
		id: 'new',
		label: 'New playlist on this phone',
		hint: 'Kept here, works offline',
		icon: 'plus',
		run: () => openNewLocalPlaylist(track),
	});
	items.push({
		id: 'new-remote',
		label: 'New playlist on my computer',
		icon: 'plus',
		run: () => openNewPlaylist(track),
	});

	await actionSheet({ title: 'Add to playlist', items });
}

/* A playlist made here can hold files that only exist here, which is the one
 * thing the computer's playlists can never do. */
function openNewLocalPlaylist(track) {
	openSheet({
		title: 'New playlist',
		className: 'sheet-actions',
		build: ({ body, close }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'New playlist on this phone'));
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
			create.addEventListener('click', () => {
				const name = input.value.trim();
				if (!name) { input.focus(); return; }
				createLocalPlaylist(name, [track]);
				toast(`Added to ${name}`);
				close();
			});

			body.append(field, create);
			setTimeout(() => input.focus(), 260);
		},
	});
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
 * Fetched and cached by the computer, so the phone never talks to LRCLIB. When
 * a timed LRC comes back the sheet follows the song and a tapped line seeks. */

/** Parse LRC ("[mm:ss.xx]text", repeated tags for a chorus) into timed lines. */
function parseSyncedLyrics(lrc) {
	if (!lrc) return [];
	const tagPattern = /\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]/g;
	const lines = [];
	for (const rawLine of String(lrc).split('\n')) {
		const tags = [...rawLine.matchAll(tagPattern)];
		if (!tags.length) continue;
		const text = rawLine.replace(tagPattern, '').trim();
		for (const tag of tags) {
			const fraction = tag[3] ? Number(`0.${tag[3]}`) : 0;
			lines.push({ time: Number(tag[1]) * 60 + Number(tag[2]) + fraction, text });
		}
	}
	return lines.sort((a, b) => a.time - b.time);
}

/* Any scroll of their own means the user is reading somewhere else, so the
 * follow stops until they stop, rather than yanking the view back mid-verse. */
const MANUAL_SCROLL_GRACE_MS = 3200;

function renderSyncedLyrics(content, body, lines, isLive) {
	const rows = lines.map((line, index) => {
		const row = el('p', 'lyrics-line' + (line.text ? '' : ' lyrics-gap'), line.text);
		if (isLive) {
			row.classList.add('seekable');
			row.setAttribute('role', 'button');
			row.tabIndex = 0;
			const go = () => { seekTo(line.time); tap(6); };
			row.addEventListener('click', go);
			row.addEventListener('keydown', event => {
				if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); go(); }
			});
		}
		row.dataset.lyricIndex = String(index);
		return row;
	});
	content.replaceChildren(...rows);
	if (!isLive) return () => {};

	let active = -1;
	let manualUntil = 0;
	const suspend = () => { manualUntil = Date.now() + MANUAL_SCROLL_GRACE_MS; };
	body.addEventListener('wheel', suspend, { passive: true });
	body.addEventListener('pointerdown', suspend);

	const centre = row => {
		const bodyBox = body.getBoundingClientRect();
		const rowBox = row.getBoundingClientRect();
		const delta = (rowBox.top - bodyBox.top) - (body.clientHeight - rowBox.height) / 2;
		body.scrollTo({ top: Math.max(0, body.scrollTop + delta), behavior: motionOff() ? 'auto' : 'smooth' });
	};

	const follow = () => {
		const at = currentTime();
		let index = -1;
		for (let i = 0; i < lines.length; i += 1) {
			if (lines[i].time <= at) index = i; else break;
		}
		if (index === active) return;
		rows[active]?.classList.remove('is-current');
		rows[active]?.removeAttribute('aria-current');
		active = index;
		const row = rows[active];
		if (!row) return;
		row.classList.add('is-current');
		row.setAttribute('aria-current', 'true');
		if (Date.now() >= manualUntil) centre(row);
	};

	follow();
	return subscribe(follow);
}

export function openLyrics(track) {
	openSheet({
		title: 'Lyrics',
		className: 'sheet-lyrics',
		full: true,
		build: async handle => {
			const { body } = handle;
			const head = el('div', 'lyrics-head sheet-drag');
			head.append(el('h2', 'sheet-title', track.title || 'Lyrics'));
			head.append(el('p', 'lyrics-artist', artistName(track) || ''));
			const content = el('div', 'lyrics-body');
			content.append(el('p', 'empty', 'Looking for lyrics…'));
			body.append(head, content);

			let stopFollowing = () => {};
			new MutationObserver((_records, observer) => {
				if (handle.root.isConnected) return;
				observer.disconnect();
				stopFollowing();
			}).observe(document.body, { childList: true });

			let result;
			try {
				result = await command('music_lyrics', { track }, 20000);
			} catch (error) {
				content.replaceChildren(el('p', 'empty',
					commandError(error, 'Could not reach your computer for lyrics.')));
				return;
			}
			if (result?.instrumental) {
				content.replaceChildren(el('p', 'empty', 'This one is instrumental.'));
				return;
			}

			// Following only makes sense for the track that is actually playing;
			// opened from a list row, these are somebody else's words.
			const playing = currentTrack();
			const isLive = !!playing && trackKey(playing) === trackKey(track);
			const timed = parseSyncedLyrics(result?.synced);
			if (timed.length) {
				stopFollowing = renderSyncedLyrics(content, body, timed, isLive);
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
 * Picking a device here moves the music to it. The old version only changed
 * which device this phone was watching, so every row except the one already
 * playing appeared to do nothing. */

const OUTPUT_ICONS = {
	bluetooth: 'bluetooth', headphones: 'headphones', builtin: 'laptop',
	hdmi: 'speaker', airplay: 'speaker', usb: 'speaker', virtual: 'speaker',
};

/* Take the computer's session over at the point it reached, then silence the
 * computer — in that order, so a phone that cannot load the track leaves the
 * music playing where it was. */
async function playOnThisPhone() {
	const remote = state.remote;
	const wasLinked = state.linked;
	setLinked(false);
	if (!wasLinked || !remote?.track) return;

	const queue = Array.isArray(remote.queue) && remote.queue.length ? remote.queue : [remote.track];
	const index = Math.max(0, Math.min(Number(remote.index) || 0, queue.length - 1));
	try {
		await playTrack(queue[index], queue, index, {
			resumeAt: Number(remote.current_time) || 0,
			startPaused: !remote.playing,
		});
	} catch (error) {
		toast(error?.message || 'This phone could not take the track over.', { icon: 'close' });
		return;
	}
	command('music_remote_control', { action: 'pause', reason: 'output_transfer' }).catch(() => {});
	toast('Playing on this phone', { icon: 'phone' });
}

/* The same handshake the desktop uses to hand playback to a phone, run the
 * other way: the computer confirms it has loaded the queue before this phone
 * goes quiet. */
async function playOnComputer() {
	const computer = state.computerName || 'your computer';
	const session = localSession();
	if (!session.queue.length) { setLinked(true); return; }

	try {
		const result = await command('music_output_transfer', {
			target_device_id: 'desktop',
			source_device_id: state.deviceId || 'phone',
			queue: session.queue,
			index: session.index,
			current_time: session.current_time,
			playing: session.playing,
			repeat: session.repeat,
			loop: session.repeat !== 'off',
		}, 40000);
		if (result?.ok === false) throw new Error(result.msg || '');
	} catch (error) {
		toast(error?.message || `${computer} did not take the track over.`, { icon: 'close' });
		return;
	}
	pauseLocal();
	setLinked(true);
	toast(`Playing on ${computer}`, { icon: 'laptop' });
}

export async function openOutputPicker() {
	let devices = [];
	try {
		const result = await command('music_output_devices', {}, 8000);
		devices = Array.isArray(result?.devices) ? result.devices : [];
	} catch { /* the list is a convenience; the two rows below always work */ }

	const computer = state.computerName || 'Your computer';
	const onPhone = !state.linked;

	const items = [{
		id: 'phone',
		label: 'This phone',
		icon: 'phone',
		hint: onPhone ? 'Playing here' : '',
		active: onPhone,
		run: () => playOnThisPhone(),
	}, {
		id: 'desktop',
		label: computer,
		icon: 'laptop',
		hint: onPhone ? '' : 'Playing here',
		active: !onPhone,
		run: () => playOnComputer(),
	}];

	for (const device of devices) {
		if (device.kind === 'builtin') continue;   // the computer row already is this
		items.push({
			id: device.id,
			label: device.name,
			icon: OUTPUT_ICONS[device.kind] || 'speaker',
			hint: device.is_default ? `In use on ${computer}` : `Plugged into ${computer}`,
			// Which speaker the computer uses is a system-level choice there, so
			// this follows the computer and says where the rest of it happens
			// rather than silently doing nothing.
			run: () => {
				setLinked(true);
				toast(`Following ${computer} — pick the speaker there`, { icon: 'laptop' });
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

/* ── Track overflow menu ───────────────────────────────────────────────────
 * Reachable from the now-playing card and from a press-and-hold on any row,
 * which is what makes an artist reachable from every list in the app rather
 * than only from a search result. */

export async function openTrackMenu(track, list = []) {
	const artist = trackArtist(track);
	const album = trackAlbum(track);

	await actionSheet({
		title: track.title || 'Track',
		items: [
			{ id: 'next', label: 'Play next', icon: 'queue', run: () => { queueAddNext(track); toast('Playing next', { icon: 'queue' }); } },
			{ id: 'end', label: 'Add to queue', icon: 'listAdd', run: () => { queueAddEnd(track); toast('Added to queue', { icon: 'listAdd' }); } },
			artist ? { id: 'artist', label: `Go to ${artist.name}`, icon: 'artist', run: () => openArtist(artist) } : null,
			album ? { id: 'album', label: `Go to ${album.title}`, icon: 'album', run: () => openAlbum(album) } : null,
			{ id: 'playlist', label: 'Add to playlist', icon: 'plus', run: () => openAddToPlaylist(track) },
			{ id: 'lyrics', label: 'Lyrics', icon: 'mic', run: () => openLyrics(track) },
			list.length > 1 ? { id: 'queue-all', label: `Queue all ${list.length}`, icon: 'listAdd', run: () => { for (const item of list) queueAddEnd(item); toast(`Queued ${list.length} tracks`, { icon: 'listAdd' }); } } : null,
		].filter(Boolean),
	});
}
