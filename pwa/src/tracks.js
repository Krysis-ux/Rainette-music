/* Track rows. Tap plays; swipe right queues next, swipe left queues last.
 *
 * The gesture axis-locks on first movement and never revisits it, or every
 * slightly-diagonal scroll catches a row and drags it sideways.
 */

import { el, icon, tap, toast, stagger } from './dom.js';
import { dragGesture } from './gesture.js';
/* catalog.js imports this module for renderTracks, so this closes a cycle.
 * It resolves because both directions are call-time only: nothing here runs
 * artistLink while the modules are still evaluating. */
import { artistLink } from './catalog.js';
import { trackKey, artworkUrl, artistName, formatTime, trackDuration } from './state.js';
import { queueAddNext, queueAddEnd, currentTrack, isPlaying } from './player.js';
import { isLocalTrack, localArtworkUrl } from './local.js';

/* Past this many pixels sideways the action commits on release. Below it the
 * row springs back, so a hesitant swipe is a cancel rather than a surprise. */
const COMMIT_PX = 72;
/* How far a finger must travel before the axis is decided. Small enough to feel
 * immediate, large enough that a straight-down scroll never trips it. */
const AXIS_PX = 10;
/* A press held this long is a request for the row's menu. Long enough not to
 * fire on a slow tap, short enough that it is discovered by accident once and
 * then used on purpose. */
const HOLD_MS = 480;

/* Every list wants the same menu, so it is configured once rather than passed
 * through every call. Set from app.js, which is the only layer that knows about
 * both the menu and the catalog it can navigate to. */
let onMenu = null;

export function configureTracks(options = {}) {
	onMenu = options.onMenu || onMenu;
}

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
			// The list a row belongs to is what makes "queue all" and "go to
			// artist" answerable, so the menu is given the whole list, not the row.
			onHold: onMenu ? () => onMenu(track, tracks) : null,
			swipe,
			showDuration,
		}));
	}
	markPlayingRows(true);
	stagger(container, ':scope > .track-shell');
}

export function trackRow(track, { onPlay, onHold = null, swipe = true, showDuration = true, trailing = null } = {}) {
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

	// A container, not a button. The artist name inside it has to be its own
	// control, and a button inside a button is invalid HTML that no engine
	// makes keyboard-reachable. Instead the play affordance is a stretched
	// sibling underneath everything, and anything that wants its own tap
	// raises itself above it.
	const row = el('div', 'track');

	const art = document.createElement('img');
	// A file on this phone carries its cover inside itself rather than at a URL,
	// so the row is tagged and filled in once the blob has been read.
	if (isLocalTrack(track)) {
		art.dataset.localArt = track.source_id;
		art.src = localArtworkUrl(track);
	} else {
		art.src = artworkUrl(track);
	}
	art.alt = '';
	art.width = 48;
	art.height = 48;
	art.loading = 'lazy';
	art.decoding = 'async';
	art.referrerPolicy = 'no-referrer';

	const copy = el('span', 'track-copy');
	copy.append(el('b', '', track.title || 'Untitled'), artistLink(track, { className: 'track-artist' }));

	// A playing row gets a live equaliser rather than only a colour change, so
	// "this one, right now" survives a glance at a list of similar rows.
	const bars = el('span', 'track-bars');
	bars.setAttribute('aria-hidden', 'true');
	bars.innerHTML = '<i></i><i></i><i></i>';

	// Stretched, empty, and at the bottom of the stack: it carries the row's
	// accessible name and its keyboard focus, while everything visible sits
	// above it.
	const play = el('button', 'track-play');
	play.type = 'button';
	play.setAttribute('aria-label', `Play ${track.title || 'Untitled'}`);
	play.addEventListener('click', () => onPlay?.());

	row.append(play, art, copy, bars);
	const length = trackDuration(track);
	if (showDuration && length) row.append(el('span', 'track-time', formatTime(length)));
	if (trailing) row.append(trailing);

	shell.append(row);
	wireGestures(shell, row, track, { swipe, onHold });
	return shell;
}

function wireGestures(shell, row, track, { swipe, onHold }) {
	let offset = 0;
	let held = false;
	let touching = false;

	const settle = () => {
		row.classList.remove('swiping', 'holding');
		row.style.transform = '';
		shell.classList.remove('reveal-next', 'reveal-end');
	};

	dragGesture(row, {
		axis: 'x',
		threshold: AXIS_PX,
		holdMs: onHold ? HOLD_MS : 0,
		// The artist name is its own control. A press that lands on it must not
		// arm the row's swipe or its long-press menu, or tapping the name would
		// queue the track instead of opening the artist.
		canStart: event => !event.target?.closest?.('.link-inline'),
		onStart: ({ event }) => {
			offset = 0;
			held = false;
			touching = event.pointerType !== 'mouse';
			if (onHold) row.classList.add('holding');
		},
		onHold: () => {
			held = true;
			row.classList.remove('holding');
			tap(12);
			onHold();
		},
		// Any real movement means this is a swipe or a scroll, not a hold — and
		// the pressing affordance has to come off at that moment rather than at
		// release, which is 6px of travel earlier than the axis is decided.
		onHoldCancel: () => row.classList.remove('holding'),
		onMove: ({ dx }) => {
			if (!swipe) return;
			row.classList.add('swiping');
			// Resistance past the commit point: the row keeps answering the
			// finger but stops running away with it, which is how the
			// threshold is felt rather than guessed.
			offset = Math.abs(dx) > COMMIT_PX
				? Math.sign(dx) * (COMMIT_PX + (Math.abs(dx) - COMMIT_PX) * 0.35)
				: dx;
			row.style.transform = `translateX(${offset}px)`;
			shell.classList.toggle('reveal-next', offset > 12);
			shell.classList.toggle('reveal-end', offset < -12);
		},
		onEnd: ({ committed }) => {
			if (!committed || !swipe || Math.abs(offset) < COMMIT_PX) { settle(); return; }
			tap(10);
			if (offset > 0) {
				queueAddNext(track);
				toast('Playing next', { icon: 'queue' });
			} else {
				queueAddEnd(track);
				toast('Added to queue', { icon: 'listAdd' });
			}
			settle();
		},
		// A pointercancel, or a capture lost to an iOS edge swipe — which
		// fires neither pointerup nor pointercancel and used to leave the row
		// stuck mid-swipe with no way back.
		onCancel: settle,
	});

	// Neither a swipe nor a hold may also play the track: the menu is already
	// open by the time the click arrives, and playing behind it is a surprise.
	row.addEventListener('click', event => {
		if (Math.abs(offset) > 4 || held) {
			event.stopImmediatePropagation();
			event.preventDefault();
			offset = 0;
			held = false;
		}
	}, true);

	// A hold on a touch screen otherwise raises the system selection callout on
	// top of the sheet the hold just opened. A right-click is not that gesture,
	// so the browser's own menu is left alone.
	row.addEventListener('contextmenu', event => {
		if (onHold && touching) event.preventDefault();
	});
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
