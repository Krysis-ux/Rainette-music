/* The settings screen and the sheets it opens.
 * Rows show a label and the current value; explanations live behind each sheet's "?". */

import { $, el, icon, toast } from './dom.js';
import { openSheet, actionSheet, confirmSheet, helpFooter } from './sheets.js';
import { state, STORAGE } from './state.js';
import {
	THEMES, ACCENTS, DEFAULTS, currentTheme, currentAccent,
	applyTheme, applyAccent, pref, setPref, resetPrefs,
} from './prefs.js';
import {
	setVolume, volume as currentVolume, boostAvailable, resumeContext, volumeMax,
	graphState, graphCostsBackgroundPlayback,
} from './audio.js';
import { createSlider } from './slider.js';
import { isLinked, audioElement } from './player.js';
import { nativeAvailable, nativeConfigure, nativeInfo } from './native-engine.js';
import {
	pickFiles, importOne, localArtworkUrl,
	localTrackCount, localBytes, formatBytes, clearLocalTracks,
} from './local.js';
import { downloadBackup, pickBackup, restoreBackup } from './backup.js';
import { openImportPlaylist } from './import.js';
import { command } from './bridge.js';

/* ── Building blocks ──────────────────────────────────────────────────────*/

/** A sheet with a title, its rows, and an optional "?" footer. */
function settingsSheet(title, build, help) {
	openSheet({
		title,
		className: 'sheet-catalog',
		full: true,
		build: async handle => {
			handle.body.append(el('h2', 'sheet-title sheet-drag', title));
			await build(handle);
			if (help) handle.body.append(helpFooter(help));
		},
	});
}

/** A grouped list of rows, like the settings panel itself. */
function group(...rows) {
	return el('div', 'settings-list in-sheet', ...rows.filter(Boolean));
}

/** A labelled switch bound to one boolean preference. */
function switchRow(label, name, onChanged, { onSet } = {}) {
	const row = el('button', 'settings-switch');
	row.type = 'button';
	row.setAttribute('role', 'switch');
	row.append(el('b', '', label), el('span', 'switch', ''));

	const paint = () => row.setAttribute('aria-checked', String(pref(name) === true));
	row.addEventListener('click', () => {
		setPref(name, pref(name) !== true);
		paint();
		onSet?.(pref(name));
		onChanged?.();
	});
	paint();
	return row;
}

/** A row that opens a list of choices and shows the chosen one. */
function choiceRow(label, choices, name, onChanged, { onSet } = {}) {
	const row = el('button', 'settings-choice');
	row.type = 'button';
	const value = el('span', 'settings-value');
	row.append(el('b', '', label), value);
	const paint = () => {
		const found = choices.find(([id]) => id === pref(name)) || choices[0];
		value.textContent = found[1];
	};
	row.addEventListener('click', async () => {
		await actionSheet({
			title: label,
			items: choices.map(([id, text]) => ({
				id, label: text, active: pref(name) === id,
				run: () => { setPref(name, id); paint(); onSet?.(id); onChanged?.(); },
			})),
		});
	});
	paint();
	return row;
}

/* ── Appearance ───────────────────────────────────────────────────────────*/

export function openAppearance({ onChanged } = {}) {
	settingsSheet('Appearance', ({ body }) => {
		const themes = el('div', 'theme-grid');
		for (const theme of THEMES) {
			const card = el('button', `theme-card theme-swatch-${theme.id}`);
			card.type = 'button';
			card.dataset.theme = theme.id;
			card.append(el('span', 'theme-swatch', ''), el('b', '', theme.label));
			card.addEventListener('click', () => { applyTheme(theme.id); paint(); onChanged?.(); });
			themes.append(card);
		}

		const accents = el('div', 'accent-row');
		for (const accent of ACCENTS) {
			const dot = el('button', `accent-dot accent-dot-${accent.id}`);
			dot.type = 'button';
			dot.dataset.accent = accent.id;
			dot.setAttribute('aria-label', accent.label);
			dot.title = accent.label;
			dot.addEventListener('click', () => { applyAccent(accent.id); paint(); onChanged?.(); });
			accents.append(dot);
		}

		body.append(
			themes,
			accents,
			group(
				switchRow('Silver titles', 'silver', onChanged),
				switchRow('Glass effects', 'glass', onChanged),
				switchRow('Reduce motion', 'reduceMotion', onChanged),
				switchRow('Haptics', 'haptics', onChanged),
				switchRow('Swipe actions', 'swipeActions', onChanged),
			),
		);

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
	}, [
		['Themes', 'The same four your computer has, by the same names. Midnight is true black, Graphite is the Mac app’s grey, Monochrome has no colour, Light is for daylight.'],
		['Silver titles', 'A brushed-metal sheen on large headings.'],
		['Glass effects', 'Frosted blur behind the bars and sheets. Turn it off on an older phone for smoother scrolling.'],
		['Reduce motion', 'Turns off entrance and sheet animations.'],
		['Haptics', 'A short buzz when a swipe or a drag commits.'],
		['Swipe actions', 'Swipe a track right to play it next, left to add it to the queue.'],
	]);
}

/* ── Playback ─────────────────────────────────────────────────────────────*/

const RATES = [[0.75, '0.75×'], [1, '1×'], [1.25, '1.25×'], [1.5, '1.5×'], [2, '2×']];

/** Push speed, quality and silence skipping to whichever player makes the sound. */
export function applyPlaybackPrefs() {
	const rate = Number(pref('rate')) || 1;
	const audio = audioElement();
	if (audio) {
		audio.defaultPlaybackRate = rate;
		audio.playbackRate = rate;
	}
	if (!nativeAvailable()) return;
	nativeConfigure({
		rate,
		quality: pref('quality') === 'saver' ? 'saver' : 'high',
		skip_silence: pref('skipSilence') === true,
	}).catch(() => { /* an older app without it plays at 1x, high */ });
}

function volumeRow() {
	const row = el('div', 'settings-slider-row');
	const value = el('span', 'settings-value', '');
	row.append(el('div', 'settings-slider-copy', el('b', '', 'Volume'), value));

	const describe = percent => {
		value.textContent = isLinked() ? `${percent}% on ${state.computerName || 'computer'}` : `${percent}%`;
	};
	const slider = createSlider({
		min: 0,
		max: Math.round(volumeMax() * 100),
		step: 1,
		value: Math.round(currentVolume() * 100),
		variant: 'volume',
		label: 'Volume',
		keyStep: 5,
		boostAbove: 100,
		format: percent => `${percent}%`,
		// Only a gesture may resume a suspended audio context.
		onGrab: resumeContext,
		onInput: percent => { describe(percent); setVolume(percent / 100).catch(() => {}); },
		// Checked on release: reading boostAvailable() mid-drag snapped the thumb back.
		onCommit: async percent => {
			const applied = await setVolume(percent / 100);
			if (percent <= 100 || boostAvailable() || isLinked()) return;
			slider.setValue(Math.round(applied * 100), 'force');
			describe(slider.value);
			toast('Extra volume isn’t available for this track.', { icon: 'volume' });
		},
	});
	describe(slider.value);
	row.append(slider.root);
	return row;
}

export function openPlaybackSettings({ onChanged } = {}) {
	const native = nativeAvailable();
	const tabs = [['home', 'Home'], ['search', 'Search'], ['library', 'Library']];
	settingsSheet('Playback', ({ body }) => {
		body.append(
			volumeRow(),
			group(
				choiceRow('Speed', RATES, 'rate', onChanged, { onSet: applyPlaybackPrefs }),
				switchRow('Fade on play and pause', 'fade', onChanged),
				native && globalThis.Capacitor?.getPlatform?.() === 'android'
					? switchRow('Skip silence', 'skipSilence', onChanged, { onSet: applyPlaybackPrefs })
					: null,
				switchRow('Autoplay similar', 'autoplaySimilar', onChanged),
			),
			group(
				switchRow('Remember what I play', 'history', onChanged),
				switchRow('Artist pictures', 'artistArtwork', onChanged),
				choiceRow('Open on', tabs, 'landingTab', onChanged),
			),
		);
	}, [
		['Volume', volumeMax() > 1
			? 'Above 100% Rainette amplifies the signal itself, with a limiter so louder doesn’t distort.'
			: 'The phone’s own player stops at 100%. Use the side buttons for more.'],
		['Speed', 'Plays faster or slower without changing the pitch. Applies to this phone’s player.'],
		['Fade', 'A soft ramp instead of a hard cut when you play or pause.'],
		['Skip silence', 'Drops long silent stretches, like quiet intros and gaps.'],
		['Autoplay similar', 'When the queue runs out, keep going with a mix seeded from the last track.'],
		['Remember what I play', 'Off means nothing is written to this phone’s history or play counts.'],
		['Artist pictures', 'Looks up a photo for every artist. Off saves requests.'],
		['Open on', 'Which tab the app starts on.'],
	]);
}

/* ── Audio quality ────────────────────────────────────────────────────────*/

const QUALITIES = [['high', 'High'], ['saver', 'Data saver']];

export function openQualitySettings({ onChanged } = {}) {
	settingsSheet('Audio quality', ({ body }) => {
		body.append(group(
			choiceRow('Streaming', QUALITIES, 'quality', onChanged, { onSet: applyPlaybackPrefs }),
		));
	}, [
		['High', 'The best AAC stream the source offers, about 128 kbps.'],
		['Data saver', 'The smallest stream, about 48 kbps. Uses roughly a third of the data.'],
		['Already played songs', 'Songs this phone has cached keep the quality they were saved in.'],
		['Your own files', 'Music you add from this phone plays exactly as the file is, lossless included.'],
		['With a computer', 'When this phone follows your computer, the computer’s own quality applies.'],
	]);
}

export function qualityLabel() {
	return (QUALITIES.find(([id]) => id === pref('quality')) || QUALITIES[0])[1];
}

/* ── Developer ────────────────────────────────────────────────────────────*/

const MOTION_SCALES = [[0.5, 'Fast'], [1, 'Normal'], [1.5, 'Slow'], [3, 'Very slow']];
const REGIONS = [
	['US', 'United States'], ['GB', 'United Kingdom'], ['CA', 'Canada'], ['AU', 'Australia'],
	['IE', 'Ireland'], ['DE', 'Germany'], ['FR', 'France'], ['ES', 'Spain'], ['IT', 'Italy'],
	['NL', 'Netherlands'], ['SE', 'Sweden'], ['BR', 'Brazil'], ['MX', 'Mexico'], ['JP', 'Japan'],
	['KR', 'South Korea'], ['IN', 'India'], ['PH', 'Philippines'], ['NG', 'Nigeria'], ['ZA', 'South Africa'],
];

/** What is actually running: the cached build, the worker and the audio graph. */
async function describeBuild() {
	const lines = [];
	try {
		const names = (await caches.keys()).filter(name => name.startsWith('rainette-pwa-'));
		lines.push(['Page build', names.join(', ') || 'no offline copy yet']);
		lines.push(['Service worker', navigator.serviceWorker?.controller ? 'active' : 'not in control']);
	} catch {
		lines.push(['Page build', 'unknown']);
	}
	const graph = graphState() === 'ready';
	lines.push(['Audio graph', graph ? 'built' : 'none']);
	lines.push(['In background', graph && graphCostsBackgroundPlayback() ? 'stops when hidden' : 'keeps playing']);
	if (nativeAvailable()) {
		const info = await nativeInfo().catch(() => null);
		lines.push(['App', `${globalThis.Capacitor?.getPlatform?.() || 'native'} ${info?.version || ''}`.trim()]);
		lines.push(['Player protocol', String(info?.protocol ?? '?')]);
	}
	lines.push(['Computer', state.connected ? (state.computerName || 'connected') : 'none']);
	return lines;
}

export function openDeveloper({ onChanged } = {}) {
	settingsSheet('Developer', async ({ body }) => {
		const info = el('dl', 'dev-info');
		body.append(
			group(
				choiceRow('Animation speed', MOTION_SCALES, 'motionScale', onChanged),
				switchRow('Compact rows', 'compact', onChanged),
				nativeAvailable() ? choiceRow('Catalog region', REGIONS, 'region', onChanged) : null,
			),
			info,
		);
		const copy = el('button', 'ghost small', 'Copy details');
		copy.type = 'button';
		body.append(copy);

		const lines = await describeBuild();
		for (const [term, value] of lines) info.append(el('dt', '', term), el('dd', '', value));
		copy.addEventListener('click', async () => {
			const text = lines.map(([term, value]) => `${term}: ${value}`).join('\n');
			try {
				await navigator.clipboard.writeText(text);
				toast('Copied');
			} catch {
				toast('Couldn’t copy here.', { icon: 'close' });
			}
		});
	}, [
		['Animation speed', 'Scales every transition. Slow is handy for checking motion.'],
		['Compact rows', 'Tighter track lists that fit more on screen.'],
		['Catalog region', 'Which country YouTube Music answers searches and artist pages for.'],
		['Details', 'What is running, for a bug report. Nothing here is sent anywhere.'],
	]);
}

/* ── About ────────────────────────────────────────────────────────────────*/

export function openAbout() {
	settingsSheet('About', async ({ body }) => {
		const version = nativeAvailable() ? (await nativeInfo().catch(() => null))?.version : '';
		body.append(el('div', 'about-mark',
			Object.assign(document.createElement('img'), { src: './icon.svg', alt: '', width: 64, height: 64 }),
			el('b', '', 'Rainette Music'),
			el('span', '', version ? `Version ${version}` : 'Web app'),
		));
	}, [
		['Privacy', 'Your pairing key never leaves this device. It travels in the link fragment, which browsers keep to themselves, so the site hosting this page never receives it.'],
		['Your music', 'Search and streams come from YouTube Music, or from your own computer when paired. Nothing goes through a Rainette server.'],
		['Licence', 'The phone apps are free software under the GPL-3.0.'],
	]);
}

/* ── Files on this phone ──────────────────────────────────────────────────
 * Every file in an import gets a card that shows what it is doing right now. */

/* The second line of a card, per phase. */
const IMPORT_PHASES = {
	queued:  { className: 'is-queued',  line: () => 'Waiting…' },
	reading: { className: 'is-reading', line: () => 'Reading…' },
	tagging: { className: 'is-tagging', line: () => 'Reading tags…' },
	stored:  { className: 'is-stored',  line: event => event?.row?.artist || 'No artist in the file' },
	failed:  { className: 'is-failed',  line: () => 'Could not be read' },
};

const PHASE_CLASSES = Object.values(IMPORT_PHASES).map(phase => phase.className);

/* The same spinner the play button shows while a track loads. */
const PHASE_GLYPHS = {
	queued:  '<span class="import-dot" aria-hidden="true"></span>',
	reading: '<span class="spin" aria-hidden="true"></span>',
	tagging: '<span class="spin" aria-hidden="true"></span>',
	stored:  icon('check', 18),
	failed:  icon('close', 18),
};

/* A transparent pixel: an <img> with no src draws a broken-image frame. */
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
				// localArtworkUrl owns the blob URL, so nothing here has to revoke it.
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
		// The abort lands between files, so the current one still finishes.
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
	// Keyed by index: the same file picked twice hashes to the same id.
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
		build: async ({ body, close }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Music on this phone'));

			const batchHost = el('div', 'import-host');
			const library = el('div', 'import-library');

			const wipe = el('button', 'ghost danger', 'Remove all local files');
			wipe.type = 'button';
			wipe.hidden = true;

			const refresh = async () => {
				const [count, bytes] = await Promise.all([localTrackCount(), localBytes()]);
				library.replaceChildren(count
					? el('p', 'catalog-note', `${count} file${count === 1 ? '' : 's'} · ${formatBytes(bytes)}`)
					: el('div', 'import-empty', el('b', '', 'Nothing here yet')));
				wipe.hidden = !count;
				onChanged?.();
			};

			const addFiles = el('button', 'primary', 'Add files');
			addFiles.type = 'button';
			const addFolder = el('button', 'ghost', 'Add a folder');
			addFolder.type = 'button';

			/* One path for both buttons; only the input's directory flag differs. */
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
					// The cards are the acknowledgement; a toast only when there were none.
					if (!batch && !result.cancelled) toast('Nothing playable in there.', { icon: 'close' });
				} finally {
					addFiles.disabled = false;
					addFolder.disabled = false;
				}
			};

			addFiles.addEventListener('click', () => runPick({}));
			addFolder.addEventListener('click', () => runPick({ directory: true }));
			body.append(el('div', 'import-actions', addFiles, addFolder));

			/* The computer's own files already play here, so offer a way to them. */
			const computerName = state.computerName || 'your computer';
			const fromComputer = el('button', 'ghost', `Music on ${computerName}`);
			fromComputer.type = 'button';
			fromComputer.hidden = true;
			body.append(el('div', 'import-actions', fromComputer));
			if (state.connected) {
				command('music_local_status').then(result => {
					const tracks = Number(result?.tracks) || 0;
					if (!tracks) return;
					fromComputer.hidden = false;
					fromComputer.textContent = `${tracks} song${tracks === 1 ? '' : 's'} on ${computerName}`;
					fromComputer.addEventListener('click', () => { close(); onShowLibrary?.(); });
				}).catch(() => { /* an older computer does not answer this */ });
			}

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

			body.append(wipe, helpFooter([
				['Where they go', 'Files you add stay on this phone. Nothing is uploaded, and they play with no connection.'],
				['Folders', 'A phone won’t let an app look through its storage on its own, so pick a folder and Rainette takes everything playable inside it, subfolders included.'],
				['Tags', 'Titles, artists and covers come from the files. A file with no tags uses its filename.'],
				['Quality', 'Files play exactly as they are, lossless formats included.'],
				[`Music on ${computerName}`, 'Your computer’s own music plays straight through without taking space here.'],
			]));
			await refresh();
		},
	});
}

/* ── Backup ───────────────────────────────────────────────────────────────*/

export function openBackup({ onChanged } = {}) {
	settingsSheet('Backup', ({ body }) => {
		const save = el('button', 'primary', 'Save a backup');
		save.type = 'button';
		save.addEventListener('click', () => downloadBackup());

		const status = el('p', 'sheet-message', '');

		const load = el('button', 'ghost', 'Restore');
		load.type = 'button';
		load.addEventListener('click', async () => {
			const file = await pickBackup();
			if (!file) return;
			try {
				const { playlists, taken } = await restoreBackup(file);
				const count = `${playlists} playlist${playlists === 1 ? '' : 's'}`;
				status.textContent = taken ? `Restored ${count} from ${taken.slice(0, 10)}.` : `Restored ${count}.`;
				onChanged?.();
				toast('Backup restored', { icon: 'check' });
			} catch (error) {
				status.textContent = error?.message || 'That backup could not be read.';
			}
		});

		body.append(el('div', 'import-actions', save, load), status);
	}, [
		['What it saves', 'Your playlists, settings, play counts and recent list, as a file on this phone.'],
		['What it leaves out', 'The pairing key, so a backup is never a way into your computer, and audio files, which belong in the phone’s own backup.'],
		['Where it goes', 'Wherever you save it. It is a download, not an upload.'],
	]);
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
	applyPlaybackPrefs();
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

/** Draw each row's icon and trailing chevron from its data-icon. */
function paintRowIcons() {
	for (const row of document.querySelectorAll('.settings-list > button[data-icon]')) {
		const slot = row.querySelector('.settings-icon');
		if (slot && !slot.firstChild) slot.innerHTML = icon(row.dataset.icon, 18);
		if (row.getAttribute('role') !== 'switch' && !row.querySelector('.settings-chevron')) {
			const chevron = el('span', 'settings-chevron');
			chevron.innerHTML = icon('chevronRight', 16);
			row.append(chevron);
		}
	}
}

/** Wire the settings panel's buttons. `refresh` repaints the value column. */
export function wireSettings({ refresh, onLibraryChanged, onShowLibrary }) {
	paintRowIcons();
	$('#appearanceButton')?.addEventListener('click', () => openAppearance({ onChanged: refresh }));
	$('#playbackButton')?.addEventListener('click', () => openPlaybackSettings({ onChanged: refresh }));
	$('#qualityButton')?.addEventListener('click', () => openQualitySettings({ onChanged: refresh }));
	$('#developerButton')?.addEventListener('click', () => openDeveloper({ onChanged: refresh }));
	$('#aboutButton')?.addEventListener('click', () => openAbout());
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
	if (playback) {
		const rate = Number(pref('rate')) || 1;
		playback.textContent = rate === 1 ? `${Math.round(currentVolume() * 100)}%` : `${rate}×`;
	}

	const quality = $('#qualityState');
	if (quality) quality.textContent = qualityLabel();

	const local = $('#localFilesState');
	if (local) {
		const count = await localTrackCount();
		local.textContent = count ? `${count} file${count === 1 ? '' : 's'}` : 'None yet';
	}
}

export { DEFAULTS };
