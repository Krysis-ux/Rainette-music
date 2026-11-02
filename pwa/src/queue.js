/* The queue sheet: what is playing, what is next, and reordering. Reorder is
 * a drag on a grip, because "drag from anywhere" and "scroll" are the same
 * gesture on a phone. Remove is a swipe. */

import { el, icon, iconButton, tap, toast, flip, collapseAway } from './dom.js';
import { dragGesture } from './gesture.js';
import { state, trackKey, artworkUrl, artworkFor, artistName, totalDuration } from './state.js';
import { artistLink } from './catalog.js';
import { openSheet, confirmSheet } from './sheets.js';
import {
	activeQueue, activeIndex, queueMove, queueRemove, queuePlayIndex,
	queueClearUpNext, toggleShuffle, subscribe, isLinked,
} from './player.js';

/* Near the sheet's top or bottom edge a dragged row scrolls the list, so a
 * song can travel further than one screen. */
const EDGE_PX = 64;
const EDGE_SCROLL_PX = 9;
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

			/* Playback reports every second, and redrawing the list on each
			 * report rebuilt the row under a dragging finger, which ended the
			 * drag. It redraws when the queue itself changes, never mid-drag. */
			let drawn = '';
			const render = () => {
				const tracks = activeQueue();
				const index = activeIndex();
				const shape = `${index}|${state.shuffled}|${isLinked()}|${tracks.map(trackKey).join(',')}`;
				if (shape === drawn || list.classList.contains('reordering')) return;
				drawn = shape;
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
			list.addEventListener('reorderend', render);
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
	art.src = artworkFor(artworkUrl(track), 42);
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
	// What is playing stays where it is; only what comes next moves.
	grip.classList.toggle('is-inert', current || isLinked());
	if (!isLinked() && !current) wireReorder(row, grip, index, render);
	wireSwipeToRemove(row, index, render);
	return row;
}

function removeAt(row, index, render) {
	collapseAway(row, () => { queueRemove(index); render?.(); });
	toast('Removed from queue', { icon: 'trash' });
}

/* Drag a row by its grip. The row follows the finger, the rows it passes step
 * aside, and the queue changes once, on release. It used to move the song one
 * place, end the drag and redraw, so getting a song from the bottom to "next"
 * took a separate press per row. The grip owns its touch (touch-action: none),
 * so no hold is needed; the rest of the row still scrolls the list. */
function wireReorder(row, grip, index, render) {
	let others = [];
	let pitch = 1;
	let target = index;
	let scroller = null;
	let scrollStart = 0;
	let travel = 0;
	let edgeSpeed = 0;
	let frame = 0;

	const lift = () => {
		scroller = row.closest('.sheet-body') || document.scrollingElement;
		scrollStart = scroller.scrollTop;
		others = [...row.parentElement.querySelectorAll('.queue-row:not(.is-current)')].filter(other => other !== row);
		// Row to row, gap included, read from the list rather than assumed.
		const next = row.nextElementSibling?.classList.contains('queue-row') ? row.nextElementSibling : row.previousElementSibling;
		pitch = Math.abs((next?.getBoundingClientRect().top ?? 0) - row.getBoundingClientRect().top) || row.offsetHeight || 1;
		row.classList.add('dragging');
		row.parentElement.classList.add('reordering');
		tap(12);
	};

	const paint = () => {
		const moved = travel + scroller.scrollTop - scrollStart;
		row.style.transform = `translateY(${moved}px)`;
		target = Math.max(activeIndex() + 1, Math.min(activeQueue().length - 1, index + Math.round(moved / pitch)));
		for (const other of others) {
			const at = Number(other.dataset.queueIndex);
			const shift = (target > index && at > index && at <= target) ? -pitch
				: (target < index && at < index && at >= target) ? pitch : 0;
			other.style.transform = shift ? `translateY(${shift}px)` : '';
		}
	};

	const scrollAtEdge = () => {
		frame = 0;
		if (!edgeSpeed) return;
		scroller.scrollTop += edgeSpeed;
		paint();
		frame = requestAnimationFrame(scrollAtEdge);
	};

	const drop = commit => {
		cancelAnimationFrame(frame);
		frame = 0;
		edgeSpeed = 0;
		if (!row.classList.contains('dragging')) return;
		for (const other of others) other.style.transform = '';
		row.style.transform = '';
		row.classList.remove('dragging');
		const list = row.parentElement;
		list?.classList.remove('reordering');
		if (commit && target !== index) {
			queueMove(index, target);
			tap(6);
		}
		// Anything the list skipped while the row was held is drawn now.
		list?.dispatchEvent(new Event('reorderend'));
	};

	dragGesture(grip, {
		axis: 'y',
		threshold: 3,
		onMove: ({ dy, event }) => {
			if (!row.classList.contains('dragging')) lift();
			travel = dy;
			const edges = scroller.getBoundingClientRect();
			edgeSpeed = event.clientY < edges.top + EDGE_PX ? -EDGE_SCROLL_PX
				: event.clientY > edges.bottom - EDGE_PX ? EDGE_SCROLL_PX : 0;
			if (edgeSpeed && !frame) frame = requestAnimationFrame(scrollAtEdge);
			paint();
		},
		onEnd: () => drop(true),
		onCancel: () => drop(false),
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
