/* Sheets: the phone client's one modal surface.
 *
 * Three things use it — the now-playing card, the queue, and action menus — and
 * they share the behaviour people already expect of a sheet on a phone: it
 * rises from the bottom, it can be dragged back down, and letting go part-way
 * either settles it back or throws it closed depending on how hard it was
 * moving. That last part matters more than the distance travelled: a short fast
 * flick is a dismissal, a long slow drag that stopped is a change of mind.
 *
 * Sheets stack. Opening the queue from the now-playing card must not destroy
 * the card underneath, so each sheet keeps its own element and the topmost one
 * owns Escape and the back gesture.
 */

import { el, icon, motionOff, tap } from './dom.js';

const open = [];

/* Past this speed, direction wins over position: a flick down closes the sheet
 * from anywhere. Tuned to sit above an accidental drift and below a deliberate
 * flick (px per millisecond). */
const DISMISS_VELOCITY = 0.55;
/* Without the velocity to carry it, a sheet has to be more than a third of the
 * way gone before letting go means closing it. */
const DISMISS_FRACTION = 0.33;

function topSheet() {
	return open[open.length - 1] || null;
}

/**
 * Open a sheet.
 *
 * `build(api)` fills the body; `api.close()` dismisses, `api.body` is the
 * scrolling content element. Returns a handle so callers can close or refresh
 * a sheet they still hold.
 */
export function openSheet({ title = '', className = '', full = false, build }) {
	const root = el('div', `sheet ${className}`.trim());
	root.setAttribute('role', 'dialog');
	root.setAttribute('aria-modal', 'true');
	if (title) root.setAttribute('aria-label', title);

	const scrim = el('div', 'sheet-scrim');
	const panel = el('div', `sheet-panel${full ? ' full' : ''}`);
	const grabber = el('div', 'sheet-grabber');
	grabber.innerHTML = '<span aria-hidden="true"></span>';
	const body = el('div', 'sheet-body');
	panel.append(grabber, body);
	root.append(scrim, panel);
	document.body.append(root);
	// The page behind must not scroll under a sheet; on iOS that is the
	// difference between a sheet and a sheet that fights the user.
	document.body.classList.add('sheet-open');

	let closing = false;
	const handle = {
		root,
		body,
		panel,
		/* `fromHistory` is set only by the popstate handler. Without it, closing
		 * a sheet that a back gesture already popped would run history.back() a
		 * second time and navigate the user out of the app. */
		close(fromHistory = false) {
			if (closing) return;
			closing = true;
			const index = open.indexOf(handle);
			if (index >= 0) open.splice(index, 1);
			if (!open.length) document.body.classList.remove('sheet-open');
			if (!fromHistory && history.state?.rainetteSheet) history.back();
			if (motionOff()) { root.remove(); return; }
			root.classList.add('closing');
			panel.addEventListener('animationend', () => root.remove(), { once: true });
			// A dropped animationend (backgrounded tab) must not leak the node.
			setTimeout(() => root.remove(), 420);
		},
	};

	scrim.addEventListener('click', () => handle.close());
	wireDrag(panel, handle);
	build(handle);
	open.push(handle);
	// One history entry per sheet, so Android's back gesture dismisses the
	// sheet — as it does for every native app — instead of leaving Rainette.
	history.pushState({ rainetteSheet: true }, '');
	if (!motionOff()) root.classList.add('opening');
	return handle;
}

/* Drag-to-dismiss.
 *
 * Only starts from the grabber, the header, or a body already scrolled to the
 * top — otherwise a downward swipe meant to scroll a long queue would drag the
 * sheet away instead, which is the single most common way this interaction is
 * got wrong. */
function wireDrag(panel, handle) {
	const body = handle.body;
	let startY = 0;
	let lastY = 0;
	let lastAt = 0;
	let velocity = 0;
	let dragging = false;
	let pointerId = null;

	const canStart = target => {
		if (target.closest('.sheet-grabber, .sheet-drag')) return true;
		if (target.closest('input[type="range"], .queue-row-grip')) return false;
		return body.scrollTop <= 0;
	};

	panel.addEventListener('pointerdown', event => {
		if (event.button !== 0 && event.pointerType === 'mouse') return;
		if (!canStart(event.target)) return;
		pointerId = event.pointerId;
		startY = lastY = event.clientY;
		lastAt = event.timeStamp;
		velocity = 0;
		dragging = false;
	});

	panel.addEventListener('pointermove', event => {
		if (event.pointerId !== pointerId) return;
		const delta = event.clientY - startY;
		if (!dragging) {
			if (delta < 8) return;          // still ambiguous, or an upward move
			dragging = true;
			panel.setPointerCapture?.(pointerId);
			panel.classList.add('dragging');
		}
		const elapsed = Math.max(1, event.timeStamp - lastAt);
		velocity = (event.clientY - lastY) / elapsed;
		lastY = event.clientY;
		lastAt = event.timeStamp;
		// Upward drag is resisted rather than blocked, so the sheet still
		// answers the gesture instead of feeling frozen.
		const offset = delta < 0 ? delta / 6 : delta;
		panel.style.transform = `translateY(${offset}px)`;
		panel.style.setProperty('--drag-fade', String(Math.max(0, 1 - offset / (panel.offsetHeight || 1))));
	});

	const end = event => {
		if (event.pointerId !== pointerId) return;
		pointerId = null;
		if (!dragging) return;
		dragging = false;
		panel.classList.remove('dragging');
		const travelled = Math.max(0, lastY - startY);
		const far = travelled > (panel.offsetHeight || 1) * DISMISS_FRACTION;
		if (velocity > DISMISS_VELOCITY || far) {
			tap(6);
			handle.close();
			return;
		}
		panel.style.transform = '';
		panel.style.removeProperty('--drag-fade');
	};

	panel.addEventListener('pointerup', end);
	panel.addEventListener('pointercancel', end);
}

document.addEventListener('keydown', event => {
	if (event.key !== 'Escape') return;
	const sheet = topSheet();
	if (sheet) { event.preventDefault(); sheet.close(); }
});

window.addEventListener('popstate', () => {
	topSheet()?.close(true);
});

/**
 * A menu of choices, as a sheet.
 *
 * `items` are `{ label, hint, icon, danger, disabled, run }`. Resolves with the
 * chosen item's id (or label) once the sheet has closed, or null on dismissal.
 */
export function actionSheet({ title = 'Actions', items = [] } = {}) {
	return new Promise(resolve => {
		let answer = null;
		const handle = openSheet({
			title,
			className: 'sheet-actions',
			build: ({ body, close }) => {
				body.append(el('h2', 'sheet-title sheet-drag', title));
				const list = el('div', 'action-list');
				for (const item of items.filter(Boolean)) {
					const button = el('button', `action-item${item.danger ? ' danger' : ''}`);
					button.type = 'button';
					button.disabled = !!item.disabled;
					if (item.icon) {
						const glyph = el('span', 'action-icon');
						glyph.innerHTML = icon(item.icon, 19);
						button.append(glyph);
					}
					button.append(el('span', 'action-label', item.label));
					if (item.hint) button.append(el('span', 'action-hint', item.hint));
					button.addEventListener('click', () => {
						answer = item.id || item.label;
						close();
						item.run?.();
					});
					list.append(button);
				}
				if (!items.length) list.append(el('p', 'empty', 'Nothing available here yet.'));
				body.append(list);
			},
		});
		// Resolve on removal so callers can rely on the sheet being gone.
		new MutationObserver((_records, observer) => {
			if (handle.root.isConnected) return;
			observer.disconnect();
			resolve(answer);
		}).observe(document.body, { childList: true });
	});
}

/** A one-line confirmation, as a sheet. Resolves true when confirmed. */
export function confirmSheet({ title, message, confirmLabel = 'Confirm', danger = false }) {
	return new Promise(resolve => {
		let answer = false;
		const handle = openSheet({
			title,
			className: 'sheet-actions',
			build: ({ body, close }) => {
				body.append(el('h2', 'sheet-title sheet-drag', title));
				if (message) body.append(el('p', 'sheet-message', message));
				const confirm = el('button', `primary${danger ? ' danger' : ''}`, confirmLabel);
				confirm.type = 'button';
				confirm.addEventListener('click', () => { answer = true; close(); });
				const cancel = el('button', 'ghost', 'Cancel');
				cancel.type = 'button';
				cancel.addEventListener('click', close);
				const row = el('div', 'sheet-buttons');
				row.append(confirm, cancel);
				body.append(row);
			},
		});
		new MutationObserver((_records, observer) => {
			if (handle.root.isConnected) return;
			observer.disconnect();
			resolve(answer);
		}).observe(document.body, { childList: true });
	});
}

export function closeAllSheets() {
	while (open.length) open[open.length - 1].close();
}

export function anySheetOpen() {
	return open.length > 0;
}

/** Ask the top sheet to redraw itself, used when playback state changes. */
export function refreshSheets() {
	for (const sheet of open) sheet.onRefresh?.();
}
