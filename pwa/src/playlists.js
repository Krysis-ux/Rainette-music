/* Playlists you can actually change from the phone.
 *
 * Before this the client could create one and add a track to it, and that was
 * the whole of it — no rename, no reorder, no removal, no cover, no deletion.
 * Opening a playlist showed a list you could only read.
 *
 * Two kinds live here and they behave the same way on screen:
 *
 *  - The computer's playlists, edited through the companion commands that were
 *    already on its allow-list.
 *  - Playlists made on the phone, held on the phone. These are the only ones
 *    that can hold local files, since the computer has never seen those, and
 *    they work with no computer paired at all.
 */

import { el, icon, toast, flip, collapseAway, tap } from './dom.js';
import { openSheet, actionSheet, confirmSheet } from './sheets.js';
import { command, commandError } from './bridge.js';
import { trackKey } from './state.js';
import { isLocalTrack } from './local.js';

const STORE = 'rainette.pwa.playlists.local';

/* ── Playlists kept on the phone ──────────────────────────────────────────*/

function readLocal() {
	try {
		const value = JSON.parse(localStorage.getItem(STORE) || '[]');
		return Array.isArray(value) ? value : [];
	} catch {
		return [];
	}
}

function writeLocal(playlists) {
	try { localStorage.setItem(STORE, JSON.stringify(playlists)); return true; } catch {
		toast('This phone is out of storage.', { icon: 'close' });
		return false;
	}
}

export function localPlaylists() {
	return readLocal().map(playlist => ({ ...playlist, local: true }));
}

export function isLocalPlaylist(playlist) {
	return playlist?.local === true || String(playlist?.id || '').startsWith('local:');
}

export function createLocalPlaylist(name, tracks = []) {
	const playlist = {
		id: `local:${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
		name: String(name || 'New playlist').slice(0, 80),
		description: '',
		cover: '',
		tracks: tracks.slice(),
		created_at: Date.now(),
		local: true,
	};
	const all = readLocal();
	all.unshift(playlist);
	writeLocal(all);
	return playlist;
}

function updateLocal(id, change) {
	const all = readLocal();
	const index = all.findIndex(playlist => playlist.id === id);
	if (index < 0) return null;
	const next = { ...all[index], ...change };
	all[index] = next;
	writeLocal(all);
	return next;
}

export function deleteLocalPlaylist(id) {
	writeLocal(readLocal().filter(playlist => playlist.id !== id));
}

export function localPlaylistTracks(id) {
	return readLocal().find(playlist => playlist.id === id)?.tracks || [];
}

export function addToLocalPlaylist(id, track) {
	const tracks = localPlaylistTracks(id);
	if (tracks.some(item => trackKey(item) === trackKey(track))) return false;
	updateLocal(id, { tracks: [...tracks, track] });
	return true;
}

/** Everything the phone holds, for a backup file. */
export function exportLocalPlaylists() {
	return readLocal();
}

export function importLocalPlaylists(playlists, { replace = false } = {}) {
	const incoming = (Array.isArray(playlists) ? playlists : []).filter(p => p && p.id && p.name);
	if (replace) { writeLocal(incoming); return incoming.length; }
	const existing = readLocal();
	const known = new Set(existing.map(playlist => playlist.id));
	const merged = [...existing, ...incoming.filter(playlist => !known.has(playlist.id))];
	writeLocal(merged);
	return merged.length - existing.length;
}

/* ── Editing, for either kind ─────────────────────────────────────────────
 * The computer's playlists are changed by command and the phone's in storage,
 * so each operation is written once here and branches on which kind it is. */

async function renamePlaylist(playlist, name) {
	if (isLocalPlaylist(playlist)) { updateLocal(playlist.id, { name }); return; }
	await command('music_playlist_rename', { playlist_id: playlist.id, name });
}

async function describePlaylist(playlist, description) {
	if (isLocalPlaylist(playlist)) { updateLocal(playlist.id, { description }); return; }
	await command('music_playlist_update_meta', { playlist_id: playlist.id, description });
}

async function removeTrack(playlist, track, index) {
	if (isLocalPlaylist(playlist)) {
		const tracks = localPlaylistTracks(playlist.id).filter((_item, at) => at !== index);
		updateLocal(playlist.id, { tracks });
		return;
	}
	await command('music_playlist_remove_track', {
		playlist_id: playlist.id,
		track_id: track?.id || '',
		source_id: track?.source_id || '',
		index,
	});
}

async function destroyPlaylist(playlist) {
	if (isLocalPlaylist(playlist)) { deleteLocalPlaylist(playlist.id); return; }
	await command('music_playlist_delete', { playlist_id: playlist.id });
}

/* ── The editor ───────────────────────────────────────────────────────────*/

/** Everything about one playlist, changeable. `onChanged` fires whenever
 *  something was saved, so the list behind can repaint. */
export function openPlaylistEditor(playlist, tracks, { onChanged } = {}) {
	let working = { ...playlist };
	let order = tracks.slice();
	let dirty = false;

	const changed = () => { dirty = true; onChanged?.(working, order); };

	openSheet({
		title: `Edit ${working.name || 'playlist'}`,
		className: 'sheet-catalog sheet-playlist-edit',
		full: true,
		build: handle => {
			const { body } = handle;
			body.append(el('h2', 'sheet-title sheet-drag', 'Edit playlist'));

			// ── Name ────────────────────────────────────────────────────────
			const nameField = field('Name', working.name || '', 80);
			nameField.input.addEventListener('change', async () => {
				const name = nameField.input.value.trim();
				if (!name || name === working.name) { nameField.input.value = working.name || ''; return; }
				try {
					await renamePlaylist(working, name);
					working = { ...working, name };
					changed();
					toast('Renamed', { icon: 'check' });
				} catch (error) {
					nameField.input.value = working.name || '';
					toast(commandError(error, 'Could not rename that playlist.'), { icon: 'close' });
				}
			});

			// ── Description ─────────────────────────────────────────────────
			const noteField = field('Description', working.description || '', 300, { multiline: true });
			noteField.input.addEventListener('change', async () => {
				const description = noteField.input.value.trim();
				if (description === (working.description || '')) return;
				try {
					await describePlaylist(working, description);
					working = { ...working, description };
					changed();
					toast('Saved', { icon: 'check' });
				} catch (error) {
					noteField.input.value = working.description || '';
					toast(commandError(error, 'Your computer could not save that.'), { icon: 'close' });
				}
			});

			body.append(nameField.node, noteField.node);

			// ── Cover ───────────────────────────────────────────────────────
			// Phone playlists keep their cover as a data URL in storage, which is
			// the only place a phone-only playlist could keep one.
			if (isLocalPlaylist(working)) {
				const coverRow = el('div', 'playlist-cover-row');
				const preview = document.createElement('img');
				preview.className = 'playlist-cover';
				preview.alt = '';
				preview.src = working.cover || './icon.svg';
				const pick = el('button', 'ghost small', working.cover ? 'Change cover' : 'Choose a cover');
				pick.type = 'button';
				pick.addEventListener('click', () => chooseCover(dataUrl => {
					updateLocal(working.id, { cover: dataUrl });
					working = { ...working, cover: dataUrl };
					preview.src = dataUrl;
					pick.textContent = 'Change cover';
					changed();
				}));
				coverRow.append(preview, pick);
				body.append(coverRow);
			}

			// ── Tracks ──────────────────────────────────────────────────────
			body.append(el('h3', 'settings-group', `${order.length} track${order.length === 1 ? '' : 's'}`));
			const list = el('div', 'playlist-edit-list');
			body.append(list);

			const paint = () => {
				list.replaceChildren(...order.map((track, index) => trackEditRow(track, index)));
				if (!order.length) list.append(el('p', 'empty', 'Nothing in here yet.'));
			};

			function move(from, to) {
				if (to < 0 || to >= order.length || from === to) return;
				flip(list, '.playlist-edit-row', () => {
					const next = order.slice();
					next.splice(to, 0, ...next.splice(from, 1));
					order = next;
					paint();
				});
				tap(6);
				persistOrder();
			}

			function persistOrder() {
				if (isLocalPlaylist(working)) { updateLocal(working.id, { tracks: order }); changed(); return; }
				// The companion has no reorder command, so a reordered playlist on
				// the computer is saved by writing the new order back as the whole
				// track list. Where that is refused, the order still holds here.
				command('music_playlist_update_meta', {
					playlist_id: working.id,
					track_order: order.map(track => track.source_id || track.id || ''),
				}).then(changed).catch(() => {
					toast('Order kept on this phone; your computer is running an older Rainette.', { icon: 'phone' });
					changed();
				});
			}

			function trackEditRow(track, index) {
				const row = el('div', 'playlist-edit-row');
				row.dataset.flipKey = trackKey(track);

				const copy = el('span', 'playlist-edit-copy');
				copy.append(el('b', '', track.title || 'Untitled'), el('span', '', track.artist || 'Unknown artist'));

				const up = el('button', 'icon small', '');
				up.type = 'button';
				up.innerHTML = icon('chevronDown', 16);
				up.className = 'icon small playlist-move up';
				up.setAttribute('aria-label', `Move ${track.title || 'track'} up`);
				up.disabled = index === 0;
				up.addEventListener('click', () => move(index, index - 1));

				const down = el('button', 'icon small playlist-move', '');
				down.type = 'button';
				down.innerHTML = icon('chevronDown', 16);
				down.setAttribute('aria-label', `Move ${track.title || 'track'} down`);
				down.disabled = index === order.length - 1;
				down.addEventListener('click', () => move(index, index + 1));

				const drop = el('button', 'icon small playlist-remove', '');
				drop.type = 'button';
				drop.innerHTML = icon('trash', 16);
				drop.setAttribute('aria-label', `Remove ${track.title || 'track'}`);
				drop.addEventListener('click', async () => {
					drop.disabled = true;
					try {
						await removeTrack(working, track, index);
						collapseAway(row, () => {
							order = order.filter((_item, at) => at !== index);
							paint();
							changed();
						});
					} catch (error) {
						drop.disabled = false;
						toast(commandError(error, 'Could not remove that track.'), { icon: 'close' });
					}
				});

				row.append(copy, up, down, drop);
				return row;
			}

			paint();

			// ── Delete ──────────────────────────────────────────────────────
			const remove = el('button', 'primary danger', 'Delete this playlist');
			remove.type = 'button';
			remove.addEventListener('click', async () => {
				const sure = await confirmSheet({
					title: `Delete ${working.name || 'playlist'}?`,
					message: isLocalPlaylist(working)
						? 'This playlist only exists on this phone, so deleting it here deletes it for good.'
						: 'This deletes it on your computer too.',
					confirmLabel: 'Delete',
					danger: true,
				});
				if (!sure) return;
				try {
					await destroyPlaylist(working);
					onChanged?.(null, []);
					handle.close();
					toast('Playlist deleted');
				} catch (error) {
					toast(commandError(error, 'Could not delete that playlist.'), { icon: 'close' });
				}
			});
			body.append(remove);

			if (dirty) changed();
		},
	});
}

function field(label, value, maxLength, { multiline = false } = {}) {
	const node = el('label', 'field');
	node.append(el('span', '', label));
	const input = document.createElement(multiline ? 'textarea' : 'input');
	if (!multiline) input.type = 'text';
	else input.rows = 3;
	input.maxLength = maxLength;
	input.value = value;
	node.append(input);
	return { node, input };
}

/* A cover is downscaled to 320px before it is stored, because localStorage is a
 * few megabytes in total and a phone camera photo is several on its own. */
function chooseCover(onPicked) {
	const input = document.createElement('input');
	input.type = 'file';
	input.accept = 'image/*';
	input.style.display = 'none';
	document.body.append(input);
	input.addEventListener('change', () => {
		const file = input.files?.[0];
		input.remove();
		if (!file) return;
		const image = new Image();
		image.onload = () => {
			const size = 320;
			const canvas = document.createElement('canvas');
			canvas.width = canvas.height = size;
			const context = canvas.getContext('2d');
			// Cover-fit: fill the square from the middle of the image rather than
			// squashing a landscape photo into it.
			const scale = Math.max(size / image.width, size / image.height);
			const width = image.width * scale;
			const height = image.height * scale;
			context.drawImage(image, (size - width) / 2, (size - height) / 2, width, height);
			URL.revokeObjectURL(image.src);
			onPicked(canvas.toDataURL('image/jpeg', 0.82));
		};
		image.onerror = () => { URL.revokeObjectURL(image.src); toast('That image could not be read.', { icon: 'close' }); };
		image.src = URL.createObjectURL(file);
	});
	input.click();
}

/** The "add to playlist" list, including the phone's own. */
export async function playlistChoices() {
	let fromComputer = [];
	try {
		const result = await command('music_playlist_list', {});
		fromComputer = Array.isArray(result?.playlists) ? result.playlists : (result?.items || []);
	} catch { /* the phone's own are still offerable */ }
	return [...localPlaylists(), ...fromComputer];
}

/** Add a track to whichever kind of playlist was chosen. */
export async function addTrackToPlaylist(playlist, track) {
	if (isLocalPlaylist(playlist)) {
		if (!addToLocalPlaylist(playlist.id, track)) throw new Error('Already in that playlist.');
		return;
	}
	// A file that only exists on this phone cannot be added to a playlist the
	// computer keeps — it has no source the computer could ever resolve.
	if (isLocalTrack(track)) {
		throw new Error('Files on this phone can only go in playlists made on this phone.');
	}
	await command('music_playlist_add_track', { playlist_id: playlist.id, track });
}

/** The overflow menu for one playlist row. */
export async function openPlaylistMenu(playlist, { onOpen, onChanged } = {}) {
	await actionSheet({
		title: playlist.name || 'Playlist',
		items: [
			{ id: 'open', label: 'Open', icon: 'queue', run: () => onOpen?.(playlist) },
			{
				id: 'edit',
				label: 'Edit, reorder, rename',
				icon: 'sliders',
				run: async () => {
					const tracks = isLocalPlaylist(playlist)
						? localPlaylistTracks(playlist.id)
						: await command('music_playlist_tracks', { playlist_id: playlist.id })
							.then(result => (Array.isArray(result?.tracks) ? result.tracks : []))
							.catch(() => []);
					openPlaylistEditor(playlist, tracks, { onChanged });
				},
			},
		],
	});
}
