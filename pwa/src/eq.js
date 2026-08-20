/* The equaliser sheet. The graph it drives lives in ./audio.js, which owns the
 * one Web Audio chain that volume and the EQ both hang off.
 *
 * It only shapes audio this phone is playing. In linked mode the computer is
 * making the sound and its own EQ applies, so the sheet says so rather than
 * offering sliders that do nothing.
 */

import { el, icon } from './dom.js';
import { createSlider } from './slider.js';
import { openSheet } from './sheets.js';
import { state } from './state.js';
import { currentTrack, isLinked } from './player.js';
import {
	EQ_BANDS, EQ_MIN, EQ_MAX, EQ_PRESETS,
	eqIsOn, eqGains, eqPresetName, eqSummary,
	setEqOn, setBandGain, applyPreset, graphState, onTrackLoaded,
	graphCostsBackgroundPlayback,
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
						? 'The equalizer could not attach to this track. It will try again on the next one — playback is unaffected either way.'
						: 'The equalizer switches on with the next track you play.';
				} else {
					note.textContent = eqIsOn()
						? 'Shapes audio playing on this phone.'
						: 'Audio plays untouched.';
				}
				paintToggle();
			});

			/* The band controls, in band order. Held as handles rather than queried
			 * back out of the DOM: a preset moves all five at once, and `'force'` is
			 * the only source that may move a slider a finger might be holding. */
			const bands = [];

			const repaintSliders = () => {
				const gains = eqGains();
				bands.forEach((band, index) => band.paint(gains[index]));
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
				// Not a <label> any more: the control is a div with role="slider", so
				// there is no labelable element for a <label> to name. The accessible
				// name comes from the slider's own aria-label instead.
				const row = el('div', 'eq-band');
				row.append(el('span', 'eq-band-label', band.label));

				const readout = el('span', 'eq-band-value');
				const show = value => { readout.textContent = `${value > 0 ? '+' : ''}${value} dB`; };

				const slider = createSlider({
					min: EQ_MIN,
					max: EQ_MAX,
					step: 1,
					value: gains[index],
					variant: 'eq',
					label: `${band.label} at ${band.short} hertz`,
					format: value => `${value} decibels`,
					// 0 dB is the origin this band is measured from, so it gets a mark,
					// a magnet and the one haptic worth spending on a value in flight.
					detent: 0,
					onInput: value => {
						setBandGain(index, value);
						show(value);
						paintToggle();
					},
				});

				show(slider.value);
				bands.push({
					paint(value) { slider.setValue(value, 'force'); show(slider.value); },
				});

				row.append(slider.root, readout);
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
			/* The equaliser needs an audio graph, and WebKit suspends one
			 * whenever the page is hidden. Said here rather than discovered
			 * later, because "the music stops when I lock my phone" is not a
			 * symptom anybody would connect back to this switch. */
			if (graphCostsBackgroundPlayback()) {
				body.append(el('p', 'catalog-note',
					'On an iPhone the equalizer needs an audio graph, and iOS silences one whenever the screen locks or you switch apps. With it on, playback will not continue in the background.'));
			}
			paintToggle();

			if (!currentTrack()) {
				body.append(el('p', 'catalog-note', 'Play something to hear changes as you make them.'));
			}
		},
	});
}
