/* The equaliser sheet. The graph it drives lives in ./audio.js, which owns the
 * one Web Audio chain that volume and the EQ both hang off.
 *
 * It only shapes audio this phone is playing. In linked mode the computer is
 * making the sound and its own EQ applies, so the sheet says so rather than
 * offering sliders that do nothing.
 */

import { el, icon } from './dom.js';
import { openSheet } from './sheets.js';
import { state } from './state.js';
import { currentTrack, isLinked } from './player.js';
import {
	EQ_BANDS, EQ_MIN, EQ_MAX, EQ_PRESETS,
	eqIsOn, eqGains, eqPresetName, eqSummary,
	setEqOn, setBandGain, applyPreset, graphState, onTrackLoaded,
} from './audio.js';

export { eqSummary, eqPresetName, eqIsOn };

/** Called by the player when a track's media loads, so a setting made with
 *  nothing playing takes effect on the first track that does. */
export function eqOnTrackLoaded() {
	return onTrackLoaded();
}

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
				const on = eqIsOn();
				toggleRow.setAttribute('aria-pressed', String(on));
				toggleState.innerHTML = icon(on ? 'check' : 'close', 18);
				sliders.classList.toggle('is-off', !on);
				presets.classList.toggle('is-off', !on);
				const preset = eqPresetName();
				for (const chip of presets.querySelectorAll('button')) {
					chip.classList.toggle('on', on && chip.dataset.preset === preset);
				}
			};

			toggleRow.addEventListener('click', async () => {
				const wanted = !eqIsOn();
				const ok = await setEqOn(wanted);
				if (wanted && !ok) {
					// A blocked graph turned the switch back off; an unloaded one
					// left it on, waiting for something to play.
					note.textContent = graphState() === 'blocked'
						? 'This phone cannot route your computer’s audio through an equalizer. Update Rainette on the computer, then try again — playback is unaffected either way.'
						: 'The equalizer switches on with the next track you play.';
				} else {
					note.textContent = eqIsOn()
						? 'Shapes audio playing on this phone.'
						: 'Audio plays untouched.';
				}
				paintToggle();
			});

			const repaintSliders = () => {
				const gains = eqGains();
				for (const [index, input] of sliders.querySelectorAll('input').entries()) {
					input.value = String(gains[index]);
					input.paintBand?.();
				}
			};

			for (const name of Object.keys(EQ_PRESETS)) {
				const chip = el('button', 'chip', name);
				chip.type = 'button';
				chip.dataset.preset = name;
				chip.addEventListener('click', () => {
					applyPreset(name);
					repaintSliders();
					paintToggle();
				});
				presets.append(chip);
			}

			const gains = eqGains();
			EQ_BANDS.forEach((band, index) => {
				const row = el('label', 'eq-band');
				row.append(el('span', 'eq-band-label', band.label));

				const input = document.createElement('input');
				input.type = 'range';
				input.min = String(EQ_MIN);
				input.max = String(EQ_MAX);
				input.step = '1';
				input.value = String(gains[index]);
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

			const reset = el('button', 'ghost small', 'Reset to flat');
			reset.type = 'button';
			reset.addEventListener('click', () => {
				applyPreset('Flat');
				repaintSliders();
				paintToggle();
			});

			note.textContent = eqIsOn() ? 'Shapes audio playing on this phone.' : 'Audio plays untouched.';
			body.append(toggleRow, presets, sliders, reset, note);
			paintToggle();

			if (!currentTrack()) {
				body.append(el('p', 'catalog-note', 'Play something to hear changes as you make them.'));
			}
		},
	});
}
