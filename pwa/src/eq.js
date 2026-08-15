/* A five-band equaliser for the phone's own audio, with the same bands, the
 * same range and the same presets as the desktop player — a preset named "Bass
 * Boost" has to mean the same thing on both devices or it is a different
 * feature wearing the same name.
 *
 * It only shapes audio this phone is playing. In linked mode the computer is
 * the one making sound and its own EQ applies, so the sheet says so rather than
 * offering sliders that do nothing.
 *
 * The awkward part is Web Audio's cross-origin rule. `createMediaElementSource`
 * on an element whose media came from another origin without CORS does not
 * fail — it outputs silence, permanently, because the routing cannot be undone.
 * Audio here comes from the companion on a different host, so the graph is only
 * built after a request has proved that origin answers with CORS headers, and
 * never speculatively.
 */

import { el, icon, toast } from './dom.js';
import { openSheet } from './sheets.js';
import { state } from './state.js';
import { audioElement, reloadCurrent, currentTrack, isLinked } from './player.js';

const STORE = {
	on: 'rainette.pwa.eq.on',
	gains: 'rainette.pwa.eq.gains',
};

/* Matches web/miniplayer.js EQ_BANDS exactly. */
export const EQ_BANDS = [
	{ f: 60, type: 'lowshelf', label: 'Bass', short: '60' },
	{ f: 250, type: 'peaking', label: 'Low', short: '250' },
	{ f: 1000, type: 'peaking', label: 'Mid', short: '1k' },
	{ f: 4000, type: 'peaking', label: 'High', short: '4k' },
	{ f: 12000, type: 'highshelf', label: 'Treble', short: '12k' },
];

const EQ_MIN = -12;
const EQ_MAX = 12;

export const EQ_PRESETS = {
	Flat: [0, 0, 0, 0, 0],
	'Bass Boost': [8, 4, 0, 0, 1],
	Vocal: [-2, 0, 4, 3, 1],
	Treble: [0, 0, 0, 4, 8],
};

function clamp(value, min, max) {
	return Math.max(min, Math.min(max, value));
}

function readGains() {
	try {
		const stored = JSON.parse(localStorage.getItem(STORE.gains) || 'null');
		if (Array.isArray(stored) && stored.length === EQ_BANDS.length) {
			return stored.map(value => clamp(Number(value) || 0, EQ_MIN, EQ_MAX));
		}
	} catch { /* fall through to flat */ }
	return EQ_BANDS.map(() => 0);
}

const eq = {
	on: localStorage.getItem(STORE.on) === '1',
	gains: readGains(),
};

function persist() {
	try {
		localStorage.setItem(STORE.on, eq.on ? '1' : '0');
		localStorage.setItem(STORE.gains, JSON.stringify(eq.gains));
	} catch { /* storage quota */ }
}

export function eqIsOn() {
	return eq.on;
}

/** The name of the preset currently matched, or '' for a custom curve. */
export function eqPresetName() {
	for (const [name, gains] of Object.entries(EQ_PRESETS)) {
		if (gains.every((value, index) => value === eq.gains[index])) return name;
	}
	return '';
}

export function eqSummary() {
	if (!eq.on) return 'Off';
	return eqPresetName() || 'Custom';
}

/* ── The graph ────────────────────────────────────────────────────────────*/

let context = null;
let source = null;
let filters = [];
/* 'idle' → never attempted, 'ready' → built and live, 'blocked' → this
 * computer's audio cannot be routed through Web Audio on this page. */
let graph = 'idle';

/* One byte is enough to learn whether the browser will let this page read the
 * response. A media element gives no such signal until it is already too late. */
async function corsAllows(url) {
	try {
		const response = await fetch(url, {
			method: 'GET',
			headers: { Range: 'bytes=0-0' },
			mode: 'cors',
			cache: 'no-store',
		});
		return response.ok || response.status === 206;
	} catch {
		return false;
	}
}

function buildGraph(audio) {
	const Context = window.AudioContext || window.webkitAudioContext;
	if (!Context) return false;
	try {
		context = context || new Context();
		source = context.createMediaElementSource(audio);
		let node = source;
		filters = EQ_BANDS.map(band => {
			const filter = context.createBiquadFilter();
			filter.type = band.type;
			filter.frequency.value = band.f;
			if (band.type === 'peaking') filter.Q.value = 1.0;
			filter.gain.value = 0;
			node.connect(filter);
			node = filter;
			return filter;
		});
		node.connect(context.destination);
		return true;
	} catch {
		return false;
	}
}

function applyGains() {
	if (graph !== 'ready') return;
	eq.gains.forEach((gain, index) => {
		const filter = filters[index];
		if (filter) filter.gain.value = eq.on ? clamp(gain, EQ_MIN, EQ_MAX) : 0;
	});
}

/* Turning the EQ on has to happen before the media loads, so the track playing
 * now is reloaded at the point it had reached. Doing it in this order — probe,
 * flag, build, reload — means a phone that cannot support the graph never gets
 * one, and so never goes silent. */
async function ensureGraph() {
	if (graph === 'ready') return true;
	if (graph === 'blocked') return false;

	const audio = audioElement();
	const url = audio.currentSrc || audio.src;
	// Nothing is loaded yet, so there is nothing to probe and nothing to break.
	// Wait for a track: the toggle stays on and this runs again when one plays.
	if (!url) return false;

	// Whether this track's media was already fetched under CORS decides if the
	// element has to be reloaded at all — see the startup note below.
	const alreadyCors = audio.crossOrigin === 'anonymous';

	if (!(await corsAllows(url))) {
		graph = 'blocked';
		if (alreadyCors) {
			// Playback was gambled on a CORS mode this computer turns out not to
			// grant. Give the attribute back and reload, or the music stays dead
			// for a feature the user is not even using.
			audio.removeAttribute('crossorigin');
			await reloadCurrent();
		}
		return false;
	}

	audio.crossOrigin = 'anonymous';
	if (!buildGraph(audio)) {
		graph = 'blocked';
		if (!alreadyCors) audio.removeAttribute('crossorigin');
		return false;
	}
	graph = 'ready';
	// Media fetched before the attribute was set is still the opaque copy, which
	// the graph would render as silence. Reloading refetches it readable.
	if (!alreadyCors) await reloadCurrent();
	applyGains();
	return true;
}

/* An EQ that was on when the app last closed is on again now, so the very first
 * track is fetched under CORS from the start. Without this every session would
 * begin by loading a track, discovering it cannot be routed, and reloading it —
 * a restart the user hears, once, for a setting they already made. */
if (eq.on) audioElement().crossOrigin = 'anonymous';

/** Called by the player whenever a new track has loaded, so an EQ that was
 *  switched on with nothing playing takes effect on the first track. */
export async function eqOnTrackLoaded() {
	if (!eq.on || graph !== 'idle') { applyGains(); return; }
	await ensureGraph();
}

export async function setEqOn(on) {
	eq.on = !!on;
	persist();
	if (!eq.on) { applyGains(); return true; }

	const ok = await ensureGraph();
	if (!ok && graph === 'blocked') {
		eq.on = false;
		persist();
		return false;
	}
	applyGains();
	return true;
}

export function setBandGain(index, value) {
	if (index < 0 || index >= EQ_BANDS.length) return;
	eq.gains = eq.gains.map((gain, position) => (position === index ? clamp(Number(value) || 0, EQ_MIN, EQ_MAX) : gain));
	persist();
	applyGains();
}

export function applyPreset(name) {
	const preset = EQ_PRESETS[name];
	if (!preset) return;
	eq.gains = preset.slice();
	persist();
	applyGains();
}

/* ── The sheet ────────────────────────────────────────────────────────────*/

export function openEqualizer() {
	openSheet({
		title: 'Equalizer',
		className: 'sheet-eq',
		full: true,
		build: ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Equalizer'));

			if (isLinked()) {
				body.append(el('p', 'sheet-message',
					`This phone is linked to ${state.computerName || 'your computer'}, so the computer is making the sound and its own equalizer applies. Switch “Play on” to this phone to shape the audio here.`));
				return;
			}

			const toggleRow = el('button', 'eq-toggle');
			toggleRow.type = 'button';
			toggleRow.append(el('span', '', 'Equalizer'));
			const toggleState = el('span', 'settings-state');
			toggleRow.append(toggleState);

			const note = el('p', 'sheet-message', '');
			const presets = el('div', 'eq-presets');
			const sliders = el('div', 'eq-bands');

			const paintToggle = () => {
				toggleRow.setAttribute('aria-pressed', String(eq.on));
				toggleState.innerHTML = icon(eq.on ? 'check' : 'close', 18);
				sliders.classList.toggle('is-off', !eq.on);
				presets.classList.toggle('is-off', !eq.on);
				for (const chip of presets.querySelectorAll('button')) {
					chip.classList.toggle('on', eq.on && chip.dataset.preset === eqPresetName());
				}
			};

			toggleRow.addEventListener('click', async () => {
				const wanted = !eq.on;
				const ok = await setEqOn(wanted);
				if (wanted && !ok) {
					note.textContent = graph === 'blocked'
						? 'This phone cannot route your computer\'s audio through an equalizer. Playback is unaffected.'
						: 'The equalizer switches on with the next track you play.';
					// A blocked graph turned the switch back off; an unloaded one
					// left it on, waiting for something to play.
				} else {
					note.textContent = eq.on
						? 'Shapes audio playing on this phone.'
						: 'Audio plays untouched.';
				}
				paintToggle();
			});

			for (const name of Object.keys(EQ_PRESETS)) {
				const chip = el('button', 'chip', name);
				chip.type = 'button';
				chip.dataset.preset = name;
				chip.addEventListener('click', () => {
					applyPreset(name);
					for (const [index, input] of sliders.querySelectorAll('input').entries()) {
						input.value = String(eq.gains[index]);
						paintBand(input);
					}
					paintToggle();
				});
				presets.append(chip);
			}

			EQ_BANDS.forEach((band, index) => {
				const row = el('label', 'eq-band');
				row.append(el('span', 'eq-band-label', band.label));

				const input = document.createElement('input');
				input.type = 'range';
				input.min = String(EQ_MIN);
				input.max = String(EQ_MAX);
				input.step = '1';
				input.value = String(eq.gains[index]);
				input.setAttribute('aria-label', `${band.label} at ${band.short} hertz`);

				const readout = el('span', 'eq-band-value');
				const paint = () => {
					const value = Number(input.value);
					readout.textContent = `${value > 0 ? '+' : ''}${value} dB`;
					input.setAttribute('aria-valuetext', `${value} decibels`);
				};
				input.paintBand = paint;
				input.addEventListener('input', () => {
					setBandGain(index, Number(input.value));
					paint();
					paintToggle();
				});
				paint();

				row.append(input, readout);
				sliders.append(row);
			});

			note.textContent = eq.on ? 'Shapes audio playing on this phone.' : 'Audio plays untouched.';
			body.append(toggleRow, presets, sliders, note);
			paintToggle();

			if (!currentTrack()) {
				body.append(el('p', 'catalog-note', 'Play something to hear changes as you make them.'));
			}
		},
	});
}

function paintBand(input) {
	input.paintBand?.();
}
