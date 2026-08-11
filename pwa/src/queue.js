/* The queue sheet.
 *
 * Mirrors the desktop's queue page: what is playing, what is next, and direct
 * editing of the order. Two things it does that the desktop does not have to:
 *
 * - Reordering is a long-press-and-drag on a grip, not a mouse drag anywhere on
 *   the row, because on a phone "drag from anywhere" and "scroll" are the same
 *   gesture.
 * - Removing is a swipe, with the row collapsing rather than blinking out, so
 *   the list visibly closes over the gap.
 *
 * The list re-renders on every playback change while open. FLIP keeps that from
 * teleporting rows: a track that moved is animated from where it was, so the
 * queue reads as one list being rearranged rather than a series of new lists.
 */

import { el, icon, iconButton, tap, toast, flip, collapseAway } from './dom.js';
import { state, trackKey, artworkUrl, artistName, totalDuration } from './state.js';
import { openSheet, confirmSheet } from './sheets.js';
import {
	activeQueue, activeIndex, queueMove, queueRemove, queuePlayIndex,
	queueClearUpNext, toggleShuffle, subscribe, isLinked,
} from './player.js';

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
			handle.onRefresh = render;
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
	copy.append(el('b', '', track.title || 'Untitled'), el('span', '', artistName(track) || 'Unknown artist'));

	const play = el('button', 'queue-row-tap');
	play.type = 'button';
	play.setAttribute('aria-label', `Play ${track.title || 'this track'}`);
	play.append(art, copy);
	play.addEventListener('click', () => { queuePlayIndex(index); });

	const remove = iconButton('close', {
		label: `Remove ${track.title || 'this track'} from the queue`,
		className: 'queue-row-remove',
		size: 16,
		onClick: () => removeAt(row, index, render),
	});

	row.append(grip, play, remove);
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
	let pointerId = null;
	let holdTimer = 0;
	let dragging = false;
	let startY = 0;
	let rowHeight = 0;

	const stop = () => {
		clearTimeout(holdTimer);
		pointerId = null;
		if (!dragging) return;
		dragging = false;
		row.classList.remove('dragging');
		row.style.transform = '';
		row.parentElement?.classList.remove('reordering');
	};

	grip.addEventListener('pointerdown', event => {
		pointerId = event.pointerId;
		startY = event.clientY;
		rowHeight = row.getBoundingClientRect().height || 1;
		holdTimer = setTimeout(() => {
			dragging = true;
			tap(12);
			grip.setPointerCapture?.(pointerId);
			row.classList.add('dragging');
			row.parentElement?.classList.add('reordering');
		}, 220);
	});

	grip.addEventListener('pointermove', event => {
		if (event.pointerId !== pointerId) return;
		if (!dragging) {
			// Moved before the hold completed: this was a scroll.
			if (Math.abs(event.clientY - startY) > 8) stop();
			return;
		}
		event.preventDefault();
		const delta = event.clientY - startY;
		row.style.transform = `translateY(${delta}px)`;
		const steps = Math.round(delta / rowHeight);
		if (!steps) return;
		const target = index + steps;
		const length = activeQueue().length;
		if (target < 0 || target >= length) return;
		queueMove(index, target);
		stop();
		render?.();
	});

	grip.addEventListener('pointerup', stop);
	grip.addEventListener('pointercancel', stop);
}

/* Swipe a queue row away to remove it — the same left-swipe vocabulary the
 * library rows use, meaning the opposite thing in the one place where the
 * track is already queued. */
function wireSwipeToRemove(row, index, render) {
	const surface = row.querySelector('.queue-row-tap');
	let startX = 0;
	let startY = 0;
	let axis = null;
	let offset = 0;
	let pointerId = null;

	surface.addEventListener('pointerdown', event => {
		pointerId = event.pointerId;
		startX = event.clientX;
		startY = event.clientY;
		axis = null;
		offset = 0;
	});

	surface.addEventListener('pointermove', event => {
		if (event.pointerId !== pointerId) return;
		const dx = event.clientX - startX;
		const dy = event.clientY - startY;
		if (axis === null) {
			if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
			axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
			if (axis === 'x') surface.setPointerCapture?.(pointerId);
		}
		if (axis !== 'x' || dx > 0) return;   // removal is leftward only
		event.preventDefault();
		offset = dx;
		row.style.setProperty('--swipe', `${dx}px`);
		row.classList.toggle('removing', dx < -72);
	});

	const finish = event => {
		if (event.pointerId !== pointerId) return;
		pointerId = null;
		const committed = axis === 'x' && offset < -72;
		axis = null;
		row.style.removeProperty('--swipe');
		row.classList.remove('removing');
		if (committed) removeAt(row, index, render);
		offset = 0;
	};

	surface.addEventListener('pointerup', finish);
	surface.addEventListener('pointercancel', finish);
	// Suppress the click a completed swipe would otherwise also fire.
	surface.addEventListener('click', event => {
		if (Math.abs(offset) > 4) { event.stopImmediatePropagation(); event.preventDefault(); }
	}, true);
}
