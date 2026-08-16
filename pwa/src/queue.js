/* The queue sheet: what is playing, what is next, and reordering. Reorder is
 * long-press-drag on a grip, because "drag from anywhere" and "scroll" are the
 * same gesture on a phone. Remove is a swipe. */

import { el, icon, iconButton, tap, toast, flip, collapseAway } from './dom.js';
import { dragGesture } from './gesture.js';
import { state, trackKey, artworkUrl, artistName, totalDuration } from './state.js';
import { artistLink } from './catalog.js';
import { openSheet, confirmSheet } from './sheets.js';
import {
	activeQueue, activeIndex, queueMove, queueRemove, queuePlayIndex,
	queueClearUpNext, toggleShuffle, subscribe, isLinked,
} from './player.js';

/* Long enough that a scroll starting on the grip is not a reorder, short
 * enough that a deliberate press does not feel like waiting. */
const HOLD_TO_REORDER_MS = 220;
/* How far left a row goes before releasing removes it. */
const REMOVE_PX = 72;

export function openQueueSheet() {
	openSheet({
		title: 'Queue',
		className: 'sheet-queue',
		build: handle => {
			const head = el('div', 'queue-head sheet-drag');
			head.append(el('h2', 'sheet-title', 'Queue'));
			const summary = el('p', 'queue-summary');
			head.append(summary);

			const tools = el('div', 'queue-tools');
			const shuffle = el('button', 'chip', 'Shuffle');
			shuffle.type = 'button';
			shuffle.addEventListener('click', () => { toggleShuffle(); tap(); render(); });
			const clear = el('button', 'chip', 'Clear up next');
			clear.type = 'button';
			clear.addEventListener('click', async () => {
				if (await confirmSheet({
					title: 'Clear what is up next?',
					message: 'The track playing now keeps playing. Everything queued after it is removed.',
					confirmLabel: 'Clear',
					danger: true,
				})) { queueClearUpNext(); render(); }
			});
			tools.append(shuffle, clear);
			head.append(tools);

			const list = el('div', 'queue-list');
			handle.body.append(head, list);

			const render = () => {
				const tracks = activeQueue();
				const index = activeIndex();
				const upNext = tracks.slice(index + 1);
				summary.textContent = tracks.length
					? `${tracks.length} track${tracks.length === 1 ? '' : 's'}${totalDuration(tracks) ? ` · ${totalDuration(tracks)}` : ''}`
					: 'Nothing queued';
				shuffle.disabled = tracks.length < 2;
				clear.disabled = !upNext.length;
				shuffle.classList.toggle('on', state.shuffled);

				flip(list, '.queue-row', () => {
					list.replaceChildren();
					if (!tracks.length) {
						list.append(el('p', 'empty', 'Play something, then swipe a track to build a queue.'));
						return;
					}
					if (tracks[index]) {
						list.append(el('h3', 'queue-label', 'Now playing'));
						list.append(queueRow(tracks[index], index, { current: true, render }));
					}
					if (upNext.length) {
						list.append(el('h3', 'queue-label', 'Up next'));
						for (const [offset, track] of upNext.entries()) {
							list.append(queueRow(track, index + 1 + offset, { render }));
						}
					}
				});
			};

			render();
			// Stay live while open: a track ending must advance this list too.
			const stop = subscribe(render);
			new MutationObserver((_records, observer) => {
				if (handle.root.isConnected) return;
				observer.disconnect();
				stop();
			}).observe(document.body, { childList: true });
		},
	});
}

function queueRow(track, index, { current = false, render } = {}) {
	const row = el('div', `queue-row${current ? ' is-current' : ''}`);
	row.dataset.queueIndex = String(index);
	row.dataset.flipKey = `${trackKey(track)}:${index}`;

	const grip = el('span', 'queue-row-grip');
	grip.innerHTML = icon('grip', 18);
	grip.setAttribute('aria-hidden', 'true');

	const art = document.createElement('img');
	art.src = artworkUrl(track);
	art.alt = '';
	art.width = 42;
	art.height = 42;
	art.loading = 'lazy';
	art.referrerPolicy = 'no-referrer';

	const copy = el('span', 'queue-row-copy');
	copy.append(el('b', '', track.title || 'Untitled'), artistLink(track, { className: 'queue-row-artist' }));

	// Stretched under the artwork and copy rather than wrapped around them, so
	// the artist name can be its own control without nesting a button.
	const tapArea = el('div', 'queue-row-tap');
	const play = el('button', 'queue-row-play');
	play.type = 'button';
	play.setAttribute('aria-label', `Play ${track.title || 'this track'}`);
	play.addEventListener('click', () => { queuePlayIndex(index); });
	tapArea.append(play, art, copy);

	const remove = iconButton('close', {
		label: `Remove ${track.title || 'this track'} from the queue`,
		className: 'queue-row-remove',
		size: 16,
		onClick: () => removeAt(row, index, render),
	});

	row.append(grip, tapArea, remove);
	// Reordering a queue the desktop owns would have to round-trip through it;
	// the desktop is the authority there, so the grip is simply not offered.
	if (!isLinked()) wireReorder(row, grip, index, render);
	wireSwipeToRemove(row, index, render);
	return row;
}

function removeAt(row, index, render) {
	collapseAway(row, () => { queueRemove(index); render?.(); });
	toast('Removed from queue', { icon: 'trash' });
}

/* Long-press the grip, then drag. The list scrolls normally until the press is
 * held, which is what keeps a one-handed scroll from reordering the queue by
 * accident. */
function wireReorder(row, grip, index, render) {
	let rowHeight = 1;

	const stop = () => {
		row.classList.remove('dragging');
		row.style.transform = '';
		row.parentElement?.classList.remove('reordering');
	};

	const reorder = dragGesture(grip, {
		axis: 'y',
		holdMs: HOLD_TO_REORDER_MS,
		// The hold, not a distance, is what arms this one — moving first is a
		// scroll and abandons the gesture outright.
		holdToDrag: true,
		holdSlop: 8,
		onStart: () => { rowHeight = row.getBoundingClientRect().height || 1; },
		onHold: () => {
			tap(12);
			row.classList.add('dragging');
			row.parentElement?.classList.add('reordering');
		},
		onMove: ({ dy }) => {
			row.style.transform = `translateY(${dy}px)`;
			const steps = Math.round(dy / rowHeight);
			if (!steps) return;
			const target = index + steps;
			if (target < 0 || target >= activeQueue().length) return;
			queueMove(index, target);
			stop();
			// The re-render below replaces this very row, so the gesture is
			// ended explicitly rather than left captured on a detached node.
			reorder.cancel();
			render?.();
		},
		onEnd: stop,
		onCancel: stop,
	});
}

/* Swipe a queue row away to remove it — the same left-swipe vocabulary the
 * library rows use, meaning the opposite thing in the one place where the
 * track is already queued. */
function wireSwipeToRemove(row, index, render) {
	const surface = row.querySelector('.queue-row-tap');
	let offset = 0;

	const settle = () => {
		row.style.removeProperty('--swipe');
		row.classList.remove('removing');
	};

	dragGesture(surface, {
		axis: 'x',
		// A press on the artist name belongs to that link, not to the row.
		canStart: event => !event.target?.closest?.('.link-inline'),
		onStart: () => { offset = 0; },
		onMove: ({ dx }) => {
			if (dx > 0) return;                 // removal is leftward only
			offset = dx;
			row.style.setProperty('--swipe', `${dx}px`);
			row.classList.toggle('removing', dx < -REMOVE_PX);
		},
		onEnd: ({ committed }) => {
			const remove = committed && offset < -REMOVE_PX;
			settle();
			if (remove) removeAt(row, index, render);
		},
		onCancel: settle,
	});

	// Suppress the click a swipe would otherwise also fire. The offset is
	// cleared here rather than in the end handler, which runs first — zeroing
	// it there left every swipe ending in a play.
	surface.addEventListener('click', event => {
		if (Math.abs(offset) > 4) { event.stopImmediatePropagation(); event.preventDefault(); }
		offset = 0;
	}, true);
}
