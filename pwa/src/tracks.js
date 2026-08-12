/* Track rows. Tap plays; swipe right queues next, swipe left queues last.
 *
 * The gesture axis-locks on first movement and never revisits it, or every
 * slightly-diagonal scroll catches a row and drags it sideways.
 */

import { el, icon, tap, toast, stagger } from './dom.js';
import { trackKey, artworkUrl, artistName, formatTime, trackDuration } from './state.js';
import { queueAddNext, queueAddEnd, currentTrack, isPlaying } from './player.js';

/* Past this many pixels sideways the action commits on release. Below it the
 * row springs back, so a hesitant swipe is a cancel rather than a surprise. */
const COMMIT_PX = 72;
/* How far a finger must travel before the axis is decided. Small enough to feel
 * immediate, large enough that a straight-down scroll never trips it. */
const AXIS_PX = 10;

/** Render a list of tracks. `onPlay(track, index)` is what a tap runs, and
 *  `emptyMessage` explains an empty list rather than leaving a blank area. */
export function renderTracks(container, tracks, { emptyMessage, onPlay, swipe = true, showDuration = true } = {}) {
	container.replaceChildren();
	if (!tracks.length) {
		container.append(el('p', 'empty', emptyMessage || 'Nothing here yet.'));
		return;
	}
	for (const [index, track] of tracks.entries()) {
		container.append(trackRow(track, {
			onPlay: () => onPlay?.(track, index),
			swipe,
			showDuration,
		}));
	}
	markPlayingRows(true);
	stagger(container, ':scope > .track-shell');
}

export function trackRow(track, { onPlay, swipe = true, showDuration = true, trailing = null } = {}) {
	// The shell holds the fixed action backdrop; the row itself is what slides.
	const shell = el('div', 'track-shell');
	shell.dataset.trackKey = trackKey(track);

	if (swipe) {
		const behind = el('div', 'track-actions');
		behind.innerHTML =
			`<span class="track-action next">${icon('queue', 18)}<b>Play next</b></span>` +
			`<span class="track-action end">${icon('listAdd', 18)}<b>Add to queue</b></span>`;
		behind.setAttribute('aria-hidden', 'true');
		shell.append(behind);
	}

	const row = el('button', 'track');
	row.type = 'button';

	const art = document.createElement('img');
	art.src = artworkUrl(track);
	art.alt = '';
	art.width = 48;
	art.height = 48;
	art.loading = 'lazy';
	art.decoding = 'async';
	art.referrerPolicy = 'no-referrer';

	const copy = el('span', 'track-copy');
	copy.append(el('b', '', track.title || 'Untitled'), el('span', '', artistName(track) || 'Unknown artist'));

	// A playing row gets a live equaliser rather than only a colour change, so
	// "this one, right now" survives a glance at a list of similar rows.
	const bars = el('span', 'track-bars');
	bars.setAttribute('aria-hidden', 'true');
	bars.innerHTML = '<i></i><i></i><i></i>';

	row.append(art, copy, bars);
	const length = trackDuration(track);
	if (showDuration && length) row.append(el('span', 'track-time', formatTime(length)));
	if (trailing) row.append(trailing);
	row.addEventListener('click', () => onPlay?.());

	shell.append(row);
	if (swipe) wireSwipe(shell, row, track);
	return shell;
}

function wireSwipe(shell, row, track) {
	let startX = 0;
	let startY = 0;
	let axis = null;         // null → undecided, 'x' → ours, 'y' → the scroller's
	let offset = 0;
	let pointerId = null;

	const settle = () => {
		row.classList.remove('swiping');
		row.style.transform = '';
		shell.classList.remove('reveal-next', 'reveal-end');
	};

	row.addEventListener('pointerdown', event => {
		if (event.pointerType === 'mouse' && event.button !== 0) return;
		pointerId = event.pointerId;
		startX = event.clientX;
		startY = event.clientY;
		axis = null;
		offset = 0;
	});

	row.addEventListener('pointermove', event => {
		if (event.pointerId !== pointerId) return;
		const dx = event.clientX - startX;
		const dy = event.clientY - startY;

		if (axis === null) {
			if (Math.abs(dx) < AXIS_PX && Math.abs(dy) < AXIS_PX) return;
			// Decided once, for the whole gesture.
			axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
			if (axis === 'x') {
				row.setPointerCapture?.(pointerId);
				row.classList.add('swiping');
			}
		}
		if (axis !== 'x') return;
		event.preventDefault();
		// Resistance past the commit point: the row keeps answering the finger
		// but stops running away with it, which is how the threshold is felt
		// rather than guessed.
		offset = Math.abs(dx) > COMMIT_PX
			? Math.sign(dx) * (COMMIT_PX + (Math.abs(dx) - COMMIT_PX) * 0.35)
			: dx;
		row.style.transform = `translateX(${offset}px)`;
		shell.classList.toggle('reveal-next', offset > 12);
		shell.classList.toggle('reveal-end', offset < -12);
	});

	const finish = event => {
		if (event.pointerId !== pointerId) return;
		pointerId = null;
		if (axis !== 'x') { axis = null; return; }
		axis = null;
		const committed = Math.abs(offset) >= COMMIT_PX;
		if (committed) {
			tap(10);
			if (offset > 0) {
				queueAddNext(track);
				toast('Playing next', { icon: 'queue' });
			} else {
				queueAddEnd(track);
				toast('Added to queue', { icon: 'listAdd' });
			}
		}
		settle();
	};

	row.addEventListener('pointerup', finish);
	row.addEventListener('pointercancel', event => {
		if (event.pointerId !== pointerId) return;
		pointerId = null;
		axis = null;
		settle();
	});

	// A tap that followed a swipe must not also play the track.
	row.addEventListener('click', event => {
		if (Math.abs(offset) > 4) { event.stopImmediatePropagation(); event.preventDefault(); offset = 0; }
	}, true);
}

/* Called on every playback tick, so it has to be free when nothing changed:
 * walking every row in the document four times a second is enough to make the
 * transport buttons miss taps. `force` is for a fresh render, whose new rows
 * carry no marks yet even though the playing track is the same one. */
let markedKey = null;
let markedPlaying = false;

/** Mark whichever rendered rows correspond to the track playing now. */
export function markPlayingRows(force = false) {
	const track = currentTrack();
	const key = track ? trackKey(track) : null;
	const playing = isPlaying();
	if (!force && key === markedKey && playing === markedPlaying) return;
	markedKey = key;
	markedPlaying = playing;
	for (const shell of document.querySelectorAll('.track-shell')) {
		const isCurrent = !!key && shell.dataset.trackKey === key;
		shell.classList.toggle('is-playing', isCurrent);
		// The bars freeze rather than disappear when paused: the row is still
		// the current one, it just is not moving.
		shell.classList.toggle('is-paused', isCurrent && !playing);
	}
}
