/* The settings screen, and the sheets it opens.
 *
 * Every row here changes something real. That was the complaint worth taking
 * seriously about the old panel: several of its rows opened a page that looked
 * like a control and then did nothing, which is worse than not offering it.
 */

import { $, el, icon, toast } from './dom.js';
import { openSheet, actionSheet, confirmSheet } from './sheets.js';
import { state, STORAGE } from './state.js';
import {
	THEMES, ACCENTS, DEFAULTS, currentTheme, currentAccent,
	applyTheme, applyAccent, pref, setPref, resetPrefs,
} from './prefs.js';
import { setVolume, volume as currentVolume, boostAvailable, VOLUME_MAX } from './audio.js';
import { isLinked } from './player.js';
import { pickFiles, localTrackCount, localBytes, formatBytes, clearLocalTracks } from './local.js';
import { downloadBackup, pickBackup, restoreBackup } from './backup.js';
import { openImportPlaylist } from './import.js';

/* ── Appearance ───────────────────────────────────────────────────────────*/

export function openAppearance({ onChanged } = {}) {
	openSheet({
		title: 'Appearance',
		className: 'sheet-catalog',
		full: true,
		build: ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Appearance'));
			body.append(el('p', 'sheet-message',
				'The same four themes your computer has, by the same names, so the two can be set to match.'));

			body.append(el('h3', 'settings-group', 'Theme'));
			const themes = el('div', 'theme-grid');
			for (const theme of THEMES) {
				const card = el('button', `theme-card theme-swatch-${theme.id}`);
				card.type = 'button';
				card.dataset.theme = theme.id;
				card.append(
					el('span', 'theme-swatch', ''),
					el('b', '', theme.label),
					el('small', '', theme.hint),
				);
				card.addEventListener('click', () => {
					applyTheme(theme.id);
					paint();
					onChanged?.();
				});
				themes.append(card);
			}
			body.append(themes);

			body.append(el('h3', 'settings-group', 'Accent'));
			const accents = el('div', 'accent-row');
			for (const accent of ACCENTS) {
				const dot = el('button', `accent-dot accent-dot-${accent.id}`);
				dot.type = 'button';
				dot.dataset.accent = accent.id;
				dot.setAttribute('aria-label', accent.label);
				dot.title = accent.label;
				dot.addEventListener('click', () => {
					applyAccent(accent.id);
					paint();
					onChanged?.();
				});
				accents.append(dot);
			}
			body.append(accents);

			body.append(toggleRow('Reduce motion', 'Turns off the entrance and sheet animations.',
				'reduceMotion', onChanged));
			body.append(toggleRow('Haptic feedback', 'A short buzz when a swipe or a drag commits.',
				'haptics', onChanged));
			body.append(toggleRow('Swipe actions on rows', 'Swipe a track right to play next, left to queue it.',
				'swipeActions', onChanged));

			function paint() {
				const theme = currentTheme();
				for (const card of themes.querySelectorAll('button')) {
					const on = card.dataset.theme === theme;
					card.classList.toggle('active', on);
					card.setAttribute('aria-pressed', String(on));
				}
				const accent = currentAccent();
				for (const dot of accents.querySelectorAll('button')) {
					const on = dot.dataset.accent === accent;
					dot.classList.toggle('active', on);
					dot.setAttribute('aria-pressed', String(on));
				}
			}
			paint();
		},
	});
}

/** A labelled switch bound to one preference. */
function toggleRow(label, hint, name, onChanged) {
	const row = el('button', 'settings-toggle');
	row.type = 'button';
	const copy = el('span', '');
	copy.append(el('b', '', label), el('small', '', hint));
	const mark = el('span', 'settings-state');
	row.append(copy, mark);

	const paint = () => {
		const on = pref(name) === true;
		row.setAttribute('aria-pressed', String(on));
		row.classList.toggle('on', on);
		mark.innerHTML = icon(on ? 'check' : 'close', 18);
	};
	row.addEventListener('click', () => {
		setPref(name, pref(name) !== true);
		paint();
		onChanged?.();
	});
	paint();
	return row;
}

/* ── Playback ─────────────────────────────────────────────────────────────*/

export function openPlaybackSettings({ onChanged } = {}) {
	openSheet({
		title: 'Playback',
		className: 'sheet-catalog',
		full: true,
		build: ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Playback'));

			// ── Volume ──────────────────────────────────────────────────────
			const volumeRow = el('div', 'settings-slider-row');
			const volumeLabel = el('div', 'settings-slider-copy');
			volumeLabel.append(el('b', '', 'Volume'), el('small', '', ''));
			const slider = document.createElement('input');
			slider.type = 'range';
			slider.min = '0';
			slider.max = String(Math.round(VOLUME_MAX * 100));
			slider.step = '1';
			slider.value = String(Math.round(currentVolume() * 100));
			slider.setAttribute('aria-label', 'Volume');

			const describeVolume = () => {
				const percent = Number(slider.value);
				const hint = volumeLabel.querySelector('small');
				if (isLinked()) hint.textContent = `${percent}% on ${state.computerName || 'your computer'}`;
				else if (percent > 100) hint.textContent = `${percent}% — boosted past this phone's normal maximum`;
				else hint.textContent = `${percent}%`;
				slider.setAttribute('aria-valuetext', `${percent}%`);
			};
			slider.addEventListener('input', async () => {
				const applied = await setVolume(Number(slider.value) / 100);
				if (Number(slider.value) > 100 && !boostAvailable() && !isLinked()) {
					slider.value = String(Math.round(applied * 100));
					toast('Boost needs a newer Rainette on your computer.', { icon: 'volume' });
				}
				describeVolume();
			});
			describeVolume();
			volumeRow.append(volumeLabel, slider);
			body.append(volumeRow);
			body.append(el('p', 'catalog-note',
				'Above 100% Rainette amplifies the signal itself, with a limiter after it so louder does not become distorted.'));

			// ── Behaviour ───────────────────────────────────────────────────
			body.append(el('h3', 'settings-group', 'Behaviour'));
			body.append(toggleRow('Fade on play and pause',
				'A soft ramp instead of a hard cut.', 'fade', onChanged));
			body.append(toggleRow('Autoplay similar when the queue ends',
				'Keep going with a mix seeded from the last track.', 'autoplaySimilar', onChanged));
			body.append(toggleRow('Remember what I play',
				'Off means nothing is written to this phone’s history or play counts.', 'history', onChanged));
			body.append(toggleRow('Fetch artist pictures',
				'Looks up a photo for artists in your library. Off saves requests.', 'artistArtwork', onChanged));

			// ── Landing tab ─────────────────────────────────────────────────
			const tabs = [['home', 'Home'], ['search', 'Search'], ['library', 'Library']];
			const landing = el('button', 'settings-choice');
			landing.type = 'button';
			const landingCopy = el('span', '');
			landingCopy.append(el('b', '', 'Open Rainette on'), el('small', '', ''));
			const landingValue = el('span', 'settings-value');
			landing.append(landingCopy, landingValue);
			const paintLanding = () => {
				const found = tabs.find(([id]) => id === pref('landingTab')) || tabs[0];
				landingValue.textContent = found[1];
				landingCopy.querySelector('small').textContent = 'Which tab this app starts on.';
			};
			landing.addEventListener('click', async () => {
				await actionSheet({
					title: 'Open Rainette on',
					items: tabs.map(([id, label]) => ({
						id, label, active: pref('landingTab') === id,
						run: () => { setPref('landingTab', id); paintLanding(); onChanged?.(); },
					})),
				});
			});
			paintLanding();
			body.append(landing);
		},
	});
}

/* ── Files on this phone ──────────────────────────────────────────────────*/

export function openLocalFiles({ onChanged } = {}) {
	openSheet({
		title: 'Music on this phone',
		className: 'sheet-catalog',
		full: true,
		build: async ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Music on this phone'));
			body.append(el('p', 'sheet-message',
				'Add MP3s and other audio from this phone. They stay on this phone: nothing is uploaded to Rainette, to your computer, or to any website, and they play with no connection at all.'));

			const summary = el('p', 'catalog-note', 'Counting…');
			body.append(summary);

			const refresh = async () => {
				const [count, bytes] = await Promise.all([localTrackCount(), localBytes()]);
				summary.textContent = count
					? `${count} file${count === 1 ? '' : 's'} · ${formatBytes(bytes)} used on this phone`
					: 'No files added yet.';
				onChanged?.();
			};

			const addFiles = el('button', 'primary', 'Add music files');
			addFiles.type = 'button';
			addFiles.addEventListener('click', async () => {
				addFiles.disabled = true;
				try {
					const { added, skipped } = await pickFiles();
					if (added) toast(`Added ${added} file${added === 1 ? '' : 's'}`, { icon: 'check' });
					else if (skipped) toast('None of those could be read.', { icon: 'close' });
					await refresh();
				} finally {
					addFiles.disabled = false;
				}
			});

			const addFolder = el('button', 'ghost', 'Add a whole folder');
			addFolder.type = 'button';
			addFolder.addEventListener('click', async () => {
				addFolder.disabled = true;
				try {
					const { added } = await pickFiles({ directory: true });
					if (added) toast(`Added ${added} file${added === 1 ? '' : 's'}`, { icon: 'check' });
					await refresh();
				} finally {
					addFolder.disabled = false;
				}
			});

			const wipe = el('button', 'ghost danger', 'Remove all local files');
			wipe.type = 'button';
			wipe.addEventListener('click', async () => {
				const sure = await confirmSheet({
					title: 'Remove every local file?',
					message: 'This deletes the copies Rainette holds. The originals in your phone’s own files are untouched.',
					confirmLabel: 'Remove all',
					danger: true,
				});
				if (!sure) return;
				await clearLocalTracks();
				await refresh();
				toast('Local files removed');
			});

			body.append(addFiles, addFolder, wipe);
			body.append(el('p', 'catalog-note',
				'Titles, artists and cover art are read out of the files themselves. A file with no tags falls back to its filename.'));
			await refresh();
		},
	});
}

/* ── Backup ───────────────────────────────────────────────────────────────*/

export function openBackup({ onChanged } = {}) {
	openSheet({
		title: 'Backup',
		className: 'sheet-catalog',
		full: true,
		build: ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Backup'));
			body.append(el('p', 'sheet-message',
				'Saves the playlists you made here, your settings, your play counts and your recent list to a file on this phone. It is a download, not an upload — the file goes wherever you put it and nowhere else.'));

			const save = el('button', 'primary', 'Save a backup file');
			save.type = 'button';
			save.addEventListener('click', () => downloadBackup());

			const status = el('p', 'sheet-message', '');

			const load = el('button', 'ghost', 'Restore from a backup');
			load.type = 'button';
			load.addEventListener('click', async () => {
				const file = await pickBackup();
				if (!file) return;
				try {
					const { playlists, taken } = await restoreBackup(file);
					status.textContent = taken
						? `Restored ${playlists} playlist${playlists === 1 ? '' : 's'} from the backup taken ${taken.slice(0, 10)}.`
						: `Restored ${playlists} playlist${playlists === 1 ? '' : 's'}.`;
					onChanged?.();
					toast('Backup restored', { icon: 'check' });
				} catch (error) {
					status.textContent = error?.message || 'That backup could not be read.';
				}
			});

			body.append(save, load, status);
			body.append(el('p', 'catalog-note',
				'The pairing key is deliberately left out, so a backup file is never a way into your computer. Audio files are left out too — they belong in your phone’s own backup.'));
		},
	});
}

/* ── Reset ────────────────────────────────────────────────────────────────*/

export async function resetEverything() {
	const sure = await confirmSheet({
		title: 'Reset all settings?',
		message: 'Themes, playback preferences and sort orders go back to how they started. Your pairing, playlists and local files are kept.',
		confirmLabel: 'Reset settings',
		danger: true,
	});
	if (!sure) return false;
	resetPrefs();
	applyTheme('midnight');
	applyAccent('default');
	for (const key of Object.keys(localStorage)) {
		if (key.startsWith('rainette.pwa.sort.')) localStorage.removeItem(key);
	}
	await setVolume(1);
	toast('Settings reset');
	return true;
}

/* ── Rows the panel shows ─────────────────────────────────────────────────*/

/** Wire the settings panel's buttons. `refresh` repaints the value column. */
export function wireSettings({ refresh, onLibraryChanged }) {
	$('#appearanceButton')?.addEventListener('click', () => openAppearance({ onChanged: refresh }));
	$('#playbackButton')?.addEventListener('click', () => openPlaybackSettings({ onChanged: refresh }));
	$('#localFilesButton')?.addEventListener('click', () => openLocalFiles({
		onChanged: () => { refresh(); onLibraryChanged?.(); },
	}));
	$('#backupButton')?.addEventListener('click', () => openBackup({
		onChanged: () => { refresh(); onLibraryChanged?.(); },
	}));
	$('#importButton')?.addEventListener('click', () => openImportPlaylist({
		onImported: () => { refresh(); onLibraryChanged?.(); },
	}));
	$('#resetButton')?.addEventListener('click', () => resetEverything().then(refresh));
}

/** The value shown on the right of each settings row. */
export async function paintSettingsValues() {
	const theme = THEMES.find(entry => entry.id === currentTheme());
	const appearance = $('#appearanceState');
	if (appearance) appearance.textContent = theme?.label || 'Midnight';

	const playback = $('#playbackState');
	if (playback) playback.textContent = `${Math.round(currentVolume() * 100)}%`;

	const local = $('#localFilesState');
	if (local) {
		const count = await localTrackCount();
		local.textContent = count ? `${count} file${count === 1 ? '' : 's'}` : 'None yet';
	}
}

export { DEFAULTS };
