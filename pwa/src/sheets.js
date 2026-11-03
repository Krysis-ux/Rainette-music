/* The one modal surface: now-playing, queue, action menus. Dismissal is judged
 * on velocity, not distance — a flick is a dismissal, a long drag that stopped
 * is a change of mind. The topmost sheet owns Escape and the back gesture. */

import { el, icon, motionOff, tap } from './dom.js';
import { dragGesture } from './gesture.js';
import { spring } from './motion.js';

const open = [];

/* Past this speed, direction wins over position: a flick down closes the sheet
 * from anywhere. Tuned to sit above an accidental drift and below a deliberate
 * flick (px per millisecond). */
const DISMISS_VELOCITY = 0.55;
/* Without the velocity to carry it, a sheet has to be more than a third of the
 * way gone before letting go means closing it. */
const DISMISS_FRACTION = 0.33;

/* An upward drag is resisted rather than blocked, so the sheet answers the gesture at first touch
 * and becomes immovable at the limit. */
const RUBBER_LIMIT = 120;

function rubber(delta) {
	if (delta >= 0) return delta;
	const over = -delta;
	return -(RUBBER_LIMIT * (1 - Math.exp(-over / RUBBER_LIMIT)));
}

/* How far from the true top a sheet's content may still be and have a pull-down dismiss it. */
const SHEET_TOP_SLACK_PX = 2;
/* Slowest a continuation close is allowed to start, so a sheet dragged past the
 * threshold and released dead still leaves rather than creeping. px/ms. */
const MIN_CLOSE_VELOCITY = 0.6;

function topSheet() {
	return open[open.length - 1] || null;
}

/* Each sheet owns one history entry, so closing one programmatically has to pop that entry — and
 * the pop comes back as a popstate indistinguishable from the user's own back gesture. */
let selfPops = 0;
/* Work that must not start until our pop has landed. Anything that pushes a new
 * history entry belongs here; running it earlier hands it to the pending pop. */
const afterPop = [];

function drainAfterPop() {
	const queued = afterPop.splice(0);
	for (const run of queued) {
		try { run(); } catch { /* one queued action must not strand the rest */ }
	}
}

/** Run `fn` once any in-flight sheet close has finished popping its entry. */
export function afterSheetSettles(fn) {
	if (!selfPops) { fn(); return; }
	afterPop.push(fn);
	// A popstate that never arrives (a browser that refuses the back, a history
	// entry something else replaced) must not strand the action for good. The
	// drain is idempotent, so arriving late costs nothing.
	setTimeout(drainAfterPop, 400);
}

/** Open a sheet. `build(api)` fills the body; `api.body` is the scroller and
 *  `api.close()` dismisses. Returns a handle the caller can keep. */
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
			if (!fromHistory && history.state?.rainetteSheet) {
				selfPops += 1;
				history.back();
			}
			// The keyframe must stay out of a close that JS is already driving: a CSS animation outranks the
			// inline transform, so re-adding `.closing` over a panel the spring has carried to the bottom
			// would snap it back to zero for one frame and play sheet-down from there.
			if (motionOff() || panel.dataset.jsClose === '1') { root.remove(); return; }
			// A close arriving from elsewhere — Escape, the back gesture, a
			// scrim tap — while a snap-back is still running.
			handle.releaseDrag?.();
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

/* How far a sheet dims as it is pulled down, toward the page behind it. */
const DRAG_MIN_OPACITY = 0.35;

/* Drag-to-dismiss, starting only from the grabber, the header, or a body already scrolled to the
 * top. */
function wireDrag(panel, handle) {
	const body = handle.body;
	/* The live motion's cancel handle. */
	let inFlight = null;
	let baseY = 0;
	let carried = 0;
	let caught = false;
	let engaged = false;
	/* Where the panel was last put. Kept rather than read back out of the
	 * computed transform, so a release never costs a style flush. */
	let paintedY = 0;

	const height = () => panel.offsetHeight || window.innerHeight || 1;

	/* Opacity is written directly, never through a custom property: a custom property is inherited,
	 * so setting one on the panel restyled every node in it on every frame. */
	const paint = y => {
		paintedY = y;
		panel.style.transform = `translateY(${y}px)`;
		panel.style.opacity = String(DRAG_MIN_OPACITY + (1 - DRAG_MIN_OPACITY) * Math.max(0, 1 - y / height()));
	};

	const clear = () => {
		paintedY = 0;
		panel.classList.remove('dragging');
		panel.style.transform = '';
		panel.style.opacity = '';
	};

	/* The panel is where the finger left it, so the close continues from there. */
	/* Under reduced motion `spring()` delivers its single frame and its onDone synchronously, before
	 * it has returned — so assigning its cancel handle to `inFlight` afterwards would leave a
	 * finished motion looking live, and the next tap would "catch" it. */
	const track = start => {
		let live = true;
		const cancel = start(() => { live = false; });
		inFlight = live ? cancel : null;
	};

	const closeWithMomentum = (fromY, velocityY) => {
		if (motionOff()) { clear(); handle.close(); return; }
		const target = height();
		panel.dataset.jsClose = '1';
		panel.classList.add('dragging');      // transition: none, animation: none
		handle.root.classList.add('closing'); // the scrim still fades via CSS
		track(done => spring(fromY, target, Math.max(velocityY, MIN_CLOSE_VELOCITY), {
			stiffness: 180,
			damping: 30,
			onFrame: paint,
			onDone: settled => {
				done();
				if (!settled) return;         // caught mid-flight; the catcher owns it
				inFlight = null;
				handle.close();
			},
		}));
	};

	const snapBack = (fromY, velocityY) => {
		track(done => spring(fromY, 0, velocityY, {
			stiffness: 210,
			damping: 28,
			onFrame: paint,
			onDone: settled => {
				done();
				if (!settled) return;
				inFlight = null;
				clear();
			},
		}));
	};

	/* An Escape, a back gesture or a scrim tap can land while a snap-back is still running. */
	handle.releaseDrag = () => {
		if (inFlight) { inFlight(); inFlight = null; }
		clear();
	};

	/* Read where the panel actually is right now, including mid-keyframe. */
	const currentY = () => {
		const raw = getComputedStyle(panel).transform;
		if (!raw || raw === 'none') return 0;
		try { return new DOMMatrixReadOnly(raw).m42 || 0; } catch { return 0; }
	};

	dragGesture(panel, {
		axis: 'y',
		threshold: 8,
		/* Only a downward move may engage a sheet at rest. */
		direction: () => (caught ? 'both' : 'positive'),
		canStart: event => {
			const target = event.target;
			if (target.closest('.sheet-grabber, .sheet-drag')) return true;
			// The sliders own their own pointers outright; the grip owns the
			// queue reorder.
			if (target.closest('.rs, .queue-row-grip')) return false;
			// Not `<= 0`.
			return body.scrollTop <= SHEET_TOP_SLACK_PX;
		},
		onStart: () => {
			engaged = false;
			caught = false;
			carried = 0;
			baseY = 0;
			if (!inFlight) return;
			// Stop the motion and take over its position and speed.
			const state = inFlight();
			inFlight = null;
			baseY = state.value;
			carried = state.velocity;
			caught = true;
			// A sheet caught mid-close is a sheet that is not closing any more.
			handle.root.classList.remove('closing');
			delete panel.dataset.jsClose;
			panel.classList.add('dragging');
			paint(baseY);
		},
		onMove: ({ dy }) => {
			if (!engaged) {
				engaged = true;
				if (!caught) {
					// Adopt an in-flight CSS entrance rather than jumping the
					// panel to its resting place under the finger.
					baseY = currentY();
					panel.classList.add('dragging');
					panel.getAnimations().forEach(animation => animation.cancel());
				}
			}
			paint(rubber(baseY + dy));
		},
		onEnd: ({ velocity }) => {
			if (!engaged) {
				// A tap that caught a moving sheet and let go without dragging.
				// The motion it interrupted is the one that should resume.
				if (caught) resume(paintedY, carried);
				return;
			}
			engaged = false;
			const at = Math.max(0, paintedY);
			// `velocity.y` is a 100ms windowed estimate, so a flick that
			// decelerated over its final frames — which is what fingers do —
			// still reads as the throw it was rather than as ≈0.
			if (velocity.y > DISMISS_VELOCITY || at > height() * DISMISS_FRACTION) {
				tap(6);
				closeWithMomentum(at, velocity.y);
				return;
			}
			// Seeded with the release velocity, so a sheet let go gently
			// decelerates into its resting place rather than easing from a
			// standstill it was never at.
			snapBack(at, velocity.y);
		},
		onCancel: () => {
			if (!engaged && !caught) return;
			engaged = false;
			snapBack(Math.max(0, paintedY), 0);
		},
	});

	/* Whatever the interrupted motion was doing, keep doing it. */
	function resume(fromY, velocityY) {
		if (velocityY > DISMISS_VELOCITY || fromY > height() * DISMISS_FRACTION) {
			closeWithMomentum(fromY, velocityY);
		} else {
			snapBack(fromY, velocityY);
		}
	}
}

document.addEventListener('keydown', event => {
	if (event.key !== 'Escape') return;
	const sheet = topSheet();
	if (sheet) { event.preventDefault(); sheet.close(); }
});

window.addEventListener('popstate', () => {
	// A pop we asked for is already accounted for; only a real back gesture may
	// dismiss the sheet now on top.
	if (selfPops > 0) {
		selfPops -= 1;
		drainAfterPop();
		return;
	}
	topSheet()?.close(true);
});

/** A menu of choices. `items` are `{ label, hint, icon, active, danger, run }`.
 *  Resolves with the chosen id once closed, or null on dismissal. */
export function actionSheet({ title = 'Actions', items = [], help = null } = {}) {
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
					if (item.active) {
						button.classList.add('active');
						button.setAttribute('aria-current', 'true');
						const mark = el('span', 'action-check');
						mark.innerHTML = icon('check', 18);
						button.append(mark);
					}
					button.addEventListener('click', () => {
						answer = item.id || item.label;
						close();
						// Deferred, because most of these open another sheet and a
						// pushState issued before this one's pop lands is what the
						// pop then consumes.
						afterSheetSettles(() => item.run?.());
					});
					list.append(button);
				}
				if (!items.length) list.append(el('p', 'empty', 'Nothing available here yet.'));
				body.append(list);
				if (help) body.append(helpFooter(help));
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
export function confirmSheet({ title, message, confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = false }) {
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
				const cancel = el('button', 'ghost', cancelLabel);
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

/** A "?" at the foot of a sheet. Explanations stay hidden until it is tapped.
 *  `entries` is a list of [label, explanation] pairs. */
export function helpFooter(entries) {
	const toggle = el('button', 'help-toggle');
	toggle.type = 'button';
	toggle.innerHTML = icon('help', 22);
	toggle.setAttribute('aria-label', 'What these do');
	toggle.setAttribute('aria-expanded', 'false');

	const list = el('dl', 'help-list');
	list.hidden = true;
	for (const [term, text] of entries) list.append(el('dt', '', term), el('dd', '', text));

	toggle.addEventListener('click', () => {
		list.hidden = !list.hidden;
		toggle.setAttribute('aria-expanded', String(!list.hidden));
		toggle.classList.toggle('on', !list.hidden);
		if (!list.hidden) list.scrollIntoView({ block: 'nearest', behavior: motionOff() ? 'auto' : 'smooth' });
	});
	return el('div', 'help', toggle, list);
}
