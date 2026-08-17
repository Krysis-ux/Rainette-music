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
import { setVolume, volume as currentVolume, boostAvailable, resumeContext, VOLUME_MAX } from './audio.js';
import { createSlider } from './slider.js';
import { isLinked } from './player.js';
import {
	pickFiles, importOne, localArtworkUrl,
	localTrackCount, localBytes, formatBytes, clearLocalTracks,
} from './local.js';
import { downloadBackup, pickBackup, restoreBackup } from './backup.js';
import { openImportPlaylist } from './import.js';
import { command } from './bridge.js';

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

			const describeVolume = percent => {
				const hint = volumeLabel.querySelector('small');
				if (isLinked()) hint.textContent = `${percent}% on ${state.computerName || 'your computer'}`;
				else if (percent > 100) hint.textContent = `${percent}% — boosted past this phone's normal maximum`;
				else hint.textContent = `${percent}%`;
			};

			const slider = createSlider({
				min: 0,
				max: Math.round(VOLUME_MAX * 100),
				step: 1,
				value: Math.round(currentVolume() * 100),
				variant: 'volume',
				label: 'Volume',
				keyStep: 5,
				boostAbove: 100,
				format: percent => `${percent}%`,
				// A suspended Web Audio context is silence with no error anywhere, and
				// only a gesture may resume it. This is one.
				onGrab: resumeContext,
				onInput: percent => { describeVolume(percent); setVolume(percent / 100).catch(() => {}); },
				// Checked once, on release. Reading boostAvailable() on every frame of
				// a drag races ensureGraph() still building and snaps the thumb back
				// mid-gesture — which is why the snap-back looked intermittent.
				onCommit: async percent => {
					const applied = await setVolume(percent / 100);
					if (percent <= 100 || boostAvailable() || isLinked()) return;
					slider.setValue(Math.round(applied * 100), 'force');
					describeVolume(slider.value);
					toast('Extra volume is not available for this track. Try the next one.', { icon: 'volume' });
				},
			});

			describeVolume(slider.value);
			volumeRow.append(volumeLabel, slider.root);
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

/* ── Files on this phone ──────────────────────────────────────────────────
 *
 * Importing a folder of music is the one moment this app is visibly doing work
 * on the user's behalf, and it used to spend that moment on a single line
 * reading "12 files · 84.1 MB used on this phone". A number that only moves at
 * the end cannot tell you whether anything is happening, which of your files
 * arrived, or which one is the reason it stopped.
 *
 * So every file gets a card, drawn before a single byte is read, and each card
 * says which of four things it is currently doing. `local.js` has always
 * reported that per file; nothing was listening.
 */

/* What each phase puts on the second line of a card. `stored` is the only one
 * that has learned anything the filename did not already say. */
const IMPORT_PHASES = {
	queued:  { className: 'is-queued',  line: () => 'Waiting…' },
	reading: { className: 'is-reading', line: () => 'Reading…' },
	tagging: { className: 'is-tagging', line: () => 'Reading tags…' },
	stored:  { className: 'is-stored',  line: event => event?.row?.artist || 'No artist in the file' },
	failed:  { className: 'is-failed',  line: () => 'Could not be read' },
};

const PHASE_CLASSES = Object.values(IMPORT_PHASES).map(phase => phase.className);

/* A ring rather than an icon, matching the one the play button already shows
 * while a track resolves — the same wait should look the same everywhere. */
const PHASE_GLYPHS = {
	queued:  '<span class="import-dot" aria-hidden="true"></span>',
	reading: '<span class="spin" aria-hidden="true"></span>',
	tagging: '<span class="spin" aria-hidden="true"></span>',
	stored:  icon('check', 18),
	failed:  icon('close', 18),
};

/* A transparent pixel, so the artwork slot is an <img> from the start without
 * being a *broken* one. An <img> with sized boxes and no `src` draws the engine's
 * own broken-image chrome — a hairline frame around an empty square, on every
 * row of a hundred-file import. This lets the CSS's grey placeholder be the only
 * thing on screen until a real cover replaces it. */
const BLANK_PIXEL = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';

/** One file, as a row that can change its mind four times. */
function importCard(entry, { onRetry }) {
	const root = el('div', 'import-card is-queued');
	root.dataset.importId = entry.id;
	root.setAttribute('role', 'listitem');

	const art = el('img', 'import-card-art');
	art.alt = '';
	art.decoding = 'async';
	art.src = BLANK_PIXEL;

	const title = el('b', '', entry.name);
	title.title = entry.path || entry.name;
	const status = el('span', '', IMPORT_PHASES.queued.line());
	const retry = el('button', 'import-retry', 'Retry');
	retry.type = 'button';
	retry.hidden = true;
	retry.setAttribute('aria-label', `Try ${entry.name} again`);
	retry.addEventListener('click', () => onRetry(entry, api));

	const line = el('div', 'import-card-line', status, retry);
	const copy = el('div', 'import-card-copy', title, line);

	const state = el('div', 'import-card-state');
	state.innerHTML = PHASE_GLYPHS.queued;
	const trail = el('div', 'import-card-trail',
		el('small', 'import-card-size', formatBytes(entry.size)),
		state,
	);

	root.append(art, copy, trail);

	const api = {
		root,
		setPhase(phase, event) {
			const shape = IMPORT_PHASES[phase] || IMPORT_PHASES.queued;
			root.classList.remove(...PHASE_CLASSES);
			root.classList.add(shape.className);
			status.textContent = shape.line(event);
			state.innerHTML = PHASE_GLYPHS[phase] || PHASE_GLYPHS.queued;
			retry.hidden = phase !== 'failed';
			if (phase === 'stored' && event?.row) {
				title.textContent = event.row.title || entry.name;
				// Only when there is art to show. localArtworkUrl owns the URL
				// and hands the same one to every surface, so nothing here mints
				// a blob URL it would then have to remember to revoke.
				if (event.row.artwork) {
					art.dataset.localArt = event.row.id;
					art.src = localArtworkUrl({ source_id: event.row.id });
				}
			}
		},
		/** A file the cancel button stopped before it was ever reached. */
		abandon() {
			if (root.classList.contains('is-stored') || root.classList.contains('is-failed')) return;
			root.classList.remove(...PHASE_CLASSES);
			root.classList.add('is-queued');
			status.textContent = 'Not imported';
			state.innerHTML = PHASE_GLYPHS.queued;
		},
	};
	return api;
}

/** The batch: a pinned header with aggregate progress and a working cancel, and
 *  one card per file under it. Returns the handle the picker reports into. */
function importBatch(host, { plan, files, controller, onStored }) {
	const total = plan.length;

	const heading = el('b', '', `Reading ${total} file${total === 1 ? '' : 's'}`);
	heading.setAttribute('aria-live', 'polite');

	const stop = el('button', 'ghost small', 'Cancel');
	stop.type = 'button';
	stop.addEventListener('click', () => {
		if (stop.dataset.role === 'clear') { host.replaceChildren(); return; }
		// The abort lands between files, so the one already being read still
		// finishes and lands. Saying "Stopping" rather than "Stopped" is the
		// difference between a promise kept and one broken by a 12 MB file.
		controller.abort();
		stop.disabled = true;
		stop.textContent = 'Stopping…';
	});

	const bar = el('div', 'import-progress', el('span', ''));
	bar.setAttribute('role', 'progressbar');
	bar.setAttribute('aria-valuemin', '0');
	bar.setAttribute('aria-valuemax', String(total));
	bar.setAttribute('aria-valuenow', '0');
	bar.setAttribute('aria-label', 'Import progress');

	const count = el('small', 'import-count', `0 of ${total}`);
	count.setAttribute('aria-hidden', 'true');
	const progress = el('div', 'import-progress-row', bar, count);

	const pin = el('div', 'import-batch-pin',
		el('div', 'import-batch-head', heading, stop),
		progress,
	);

	const list = el('div', 'import-list');
	list.setAttribute('role', 'list');

	const cards = new Map();
	const retryOne = async (entry, card) => {
		card.setPhase('reading');
		try {
			const row = await importOne(files[entry.index]);
			card.setPhase('stored', { row });
			onStored?.();
		} catch (error) {
			card.setPhase('failed', { error });
		}
	};
	// Keyed by index, not id: the same file picked twice in one folder hashes to
	// the same id, and two cards must still be able to move independently.
	for (const entry of plan) {
		const card = importCard(entry, { onRetry: retryOne });
		cards.set(entry.index, card);
		list.append(card.root);
	}

	const root = el('div', 'import-batch', pin, list);
	host.replaceChildren(root);

	const setDone = done => {
		bar.style.setProperty('--done', String(total ? done / total : 0));
		bar.setAttribute('aria-valuenow', String(done));
		count.textContent = `${done} of ${total}`;
	};

	return {
		progress(done, _total, event) {
			setDone(done);
			if (event) cards.get(event.index)?.setPhase(event.phase, event);
		},
		settle({ added = 0, skipped = 0, cancelled = false } = {}) {
			if (cancelled) for (const card of cards.values()) card.abandon();
			setDone(cancelled ? added + skipped : total);
			heading.textContent = cancelled
				? `Stopped after ${added + skipped} of ${total}`
				: skipped
					? `Added ${added} · ${skipped} could not be read`
					: `Added ${added} file${added === 1 ? '' : 's'}`;
			stop.disabled = false;
			stop.dataset.role = 'clear';
			stop.textContent = 'Clear';
			stop.setAttribute('aria-label', 'Clear this import list');
		},
	};
}

export function openLocalFiles({ onChanged, onShowLibrary } = {}) {
	openSheet({
		title: 'Music on this phone',
		className: 'sheet-catalog',
		full: true,
		build: async ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Music on this phone'));
			body.append(el('p', 'sheet-message',
				'Add MP3s and other audio from this phone. They stay on this phone: nothing is uploaded to Rainette, to your computer, or to any website, and they play with no connection at all.'));

			const batchHost = el('div', 'import-host');
			const library = el('div', 'import-library');

			const wipe = el('button', 'ghost danger', 'Remove all local files');
			wipe.type = 'button';
			wipe.hidden = true;

			const refresh = async () => {
				const [count, bytes] = await Promise.all([localTrackCount(), localBytes()]);
				if (count) {
					library.replaceChildren(el('p', 'catalog-note',
						`${count} file${count === 1 ? '' : 's'} · ${formatBytes(bytes)} used on this phone`));
				} else {
					library.replaceChildren(el('div', 'import-empty',
						el('b', '', 'Nothing here yet'),
						el('span', '', 'Music you add stays on this phone. It plays with no connection at all, and nothing is uploaded — not to Rainette, not to your computer, not anywhere.'),
					));
				}
				// Nothing to remove is not a reason to offer removing it.
				wipe.hidden = !count;
				onChanged?.();
			};

			const addFiles = el('button', 'primary', 'Add music files');
			addFiles.type = 'button';
			const addFolder = el('button', 'ghost', 'Add a folder of music');
			addFolder.type = 'button';

			/* One path for both buttons, because the only difference between them
			 * is a flag on the input element. */
			const runPick = async options => {
				addFiles.disabled = true;
				addFolder.disabled = true;
				const controller = new AbortController();
				let batch = null;
				try {
					const result = await pickFiles({
						...options,
						signal: controller.signal,
						onPlan: (plan, files) => {
							batch = importBatch(batchHost, { plan, files, controller, onStored: refresh });
						},
						onProgress: (done, total, event) => batch?.progress(done, total, event),
					});
					batch?.settle(result);
					await refresh();
					// The cards are the acknowledgement. A toast is only owed when
					// there was nothing to draw them from.
					if (!batch && !result.cancelled) {
						toast('Nothing playable in there.', { icon: 'close' });
					}
				} finally {
					addFiles.disabled = false;
					addFolder.disabled = false;
				}
			};

			addFiles.addEventListener('click', () => runPick({}));
			addFolder.addEventListener('click', () => runPick({ directory: true }));

			// Not `.sheet-buttons`: that lays two buttons out side by side at 46%
			// each, and "Add a folder of music" wraps to two lines in the half it
			// gets. These are a primary and its alternative, not a pair of equals.
			body.append(el('div', 'import-actions', addFiles, addFolder));
			body.append(el('p', 'hint',
				'Your phone won’t let a website look through its storage on its own, so pick a folder and Rainette takes everything playable inside it — including everything in the folders under it.'));

			/* The thing people actually ask for is "import all the music on my
			 * phone", and no browser can do it. The nearest honest version is the
			 * computer sending its library across, which needs a desktop that can
			 * answer — so it is shown, disabled, with the reason, in the same
			 * register as the boost notice on the volume slider. */
			/* The thing people ask for is "bring over all the music on my
			 * computer", and the honest answer is that it is already here —
			 * the computer's own files play straight through, the same way
			 * anything else it holds does. Copying gigabytes into this phone's
			 * storage would be the wrong favour; what is worth saying is how
			 * much is there and where to find it. */
			const computerName = state.computerName || 'your computer';
			const fromComputer = el('button', 'ghost', `Music on ${computerName}`);
			fromComputer.type = 'button';
			fromComputer.disabled = true;
			const fromComputerNote = el('p', 'hint', `Checking what ${computerName} has…`);
			body.append(el('div', 'import-actions', fromComputer), fromComputerNote);

			command('music_local_status').then(result => {
				const tracks = Number(result?.tracks) || 0;
				const roots = Number(result?.roots?.length) || 0;
				if (!roots) {
					fromComputerNote.textContent = `${computerName} has no music folders set up yet. Add one there, in Settings, and everything inside it plays here without taking up space on this phone.`;
					return;
				}
				if (!tracks) {
					fromComputerNote.textContent = `${computerName} is watching ${roots === 1 ? 'a folder' : `${roots} folders`} but has not found anything playable in ${roots === 1 ? 'it' : 'them'} yet.`;
					return;
				}
				fromComputer.disabled = false;
				fromComputer.textContent = `${tracks} song${tracks === 1 ? '' : 's'} on ${computerName}`;
				fromComputer.addEventListener('click', () => { handle.close(); onShowLibrary?.(); });
				fromComputerNote.textContent = 'These play straight from your computer, so they cost nothing here. Add files below only for the ones you want with no connection at all.';
			}).catch(() => {
				// An older computer simply does not answer this, which is not a
				// failure worth a red line — it is a feature that is not there.
				fromComputerNote.textContent = `${computerName} is running an older Rainette that cannot share its own files yet.`;
			});

			body.append(batchHost, library);

			wipe.addEventListener('click', async () => {
				const sure = await confirmSheet({
					title: 'Remove every local file?',
					message: 'This deletes the copies Rainette holds. The originals in your phone’s own files are untouched.',
					confirmLabel: 'Remove all',
					danger: true,
				});
				if (!sure) return;
				await clearLocalTracks();
				batchHost.replaceChildren();
				await refresh();
				toast('Local files removed');
			});

			body.append(wipe);
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
export function wireSettings({ refresh, onLibraryChanged, onShowLibrary }) {
	$('#appearanceButton')?.addEventListener('click', () => openAppearance({ onChanged: refresh }));
	$('#playbackButton')?.addEventListener('click', () => openPlaybackSettings({ onChanged: refresh }));
	$('#localFilesButton')?.addEventListener('click', () => openLocalFiles({
		onChanged: () => { refresh(); onLibraryChanged?.(); },
		onShowLibrary,
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
