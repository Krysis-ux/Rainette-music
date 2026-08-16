/* One pointer-drag contract.
 *
 * Four call sites had four subtly different versions of this and each one had a
 * different bug: sheets.js never cleared a stale pointerId, queue.js never
 * released capture, none of the four listened for lostpointercapture. tracks.js
 * decided its axis correctly and is the one the rest are modelled on.
 *
 * Nothing here touches the DOM beyond listeners and pointer capture — what a
 * drag *means* belongs to the caller, which is the only layer that knows its own
 * commit threshold. */

/* A single-sample derivative is the velocity of the last 16ms, and the last
 * 16ms of a flick is the part where the finger is already slowing down — which
 * is why a fast throw used to read as ≈0 and spring back. A 100ms window is the
 * shortest one that still describes the throw rather than its tail. */
const VELOCITY_WINDOW_MS = 100;

/**
 * Wire a drag on `element`.
 *
 * - `axis`      — `'x'` | `'y'` | `'free'`. Anything but `free` hands the
 *                 gesture to the scroller when the finger picks the other axis.
 * - `threshold` — px of travel before the axis is decided.
 * - `direction` — `'both'` | `'positive'` | `'negative'`, or a function
 *                 returning one, resolved when the axis is being decided. Which
 *                 sign along the axis may *engage* the drag. The sheet only
 *                 engages downward, or an upward swipe at the top of a list
 *                 drags the panel instead of scrolling — but a sheet already
 *                 displaced mid-close has to engage upward too, which is why
 *                 this can be a decision rather than a constant.
 * - `holdMs`    — >0 arms a long press.
 * - `holdToDrag`— the hold, not the threshold, is what engages the drag; any
 *                 movement before it fires abandons the gesture (a scroll).
 *
 * Callbacks: `onStart({ x, y, event })`, `onMove({ dx, dy, velocity, event })`,
 * `onEnd({ dx, dy, velocity, committed, held })` on a genuine release, and
 * `onCancel(...)` when the pointer is taken away instead — a pointercancel, a
 * capture lost to an iOS edge swipe, or a hold abandoned by movement.
 * `onHoldCancel()` fires when movement disarms a hold that had not yet fired,
 * which is where a "pressing" affordance has to come back off.
 * `committed` means "the drag was ours and it ended in a release"; what to do
 * about it is the caller's call.
 *
 * Returns `{ destroy() }`.
 */
export function dragGesture(element, {
	axis = 'x',
	threshold = 10,
	direction = 'both',
	holdMs = 0,
	holdToDrag = false,
	holdSlop = 6,
	preventScroll = true,
	canStart = () => true,
	onStart,
	onMove,
	onEnd,
	onHold,
	onHoldCancel,
	onCancel,
} = {}) {
	const controller = new AbortController();
	const listen = (type, handler) => element.addEventListener(type, handler, { signal: controller.signal });

	let pointerId = null;
	let startX = 0;
	let startY = 0;
	let lastX = 0;
	let lastY = 0;
	/* null → undecided, 'x' | 'y' | 'free' → ours, 'other' → the scroller's.
	 * Decided once per gesture and never revisited, or every slightly-diagonal
	 * scroll catches the element and drags it sideways. */
	let lock = null;
	let engaged = false;
	let held = false;
	let holdTimer = 0;
	const samples = [];

	/* Unconditional, and it runs *before* pointerdown does anything else. This
	 * is the discipline sheets.js:126 lacked: it returned early without
	 * clearing pointerId, so a recycled id from a prior gesture matched in
	 * pointermove and the panel started dragging from a pointer it never
	 * accepted. */
	const reset = () => {
		clearTimeout(holdTimer);
		holdTimer = 0;
		pointerId = null;
		lock = null;
		engaged = false;
		held = false;
		samples.length = 0;
	};

	const pushSample = event => {
		samples.push({ t: event.timeStamp, x: event.clientX, y: event.clientY });
		while (samples.length > 2 && event.timeStamp - samples[0].t > VELOCITY_WINDOW_MS) {
			samples.shift();
		}
	};

	const axisVelocity = key => {
		if (samples.length < 2) return 0;
		const first = samples[0];
		const last = samples[samples.length - 1];
		const dt = last.t - first.t;
		return dt > 0 ? (last[key] - first[key]) / dt : 0;   // px/ms
	};

	const velocity = () => ({ x: axisVelocity('x'), y: axisVelocity('y') });

	const capture = () => {
		try { element.setPointerCapture?.(pointerId); } catch { /* not capturable */ }
	};

	const releaseCapture = id => {
		try { element.releasePointerCapture?.(id); } catch { /* already gone */ }
	};

	const engage = () => {
		engaged = true;
		capture();
	};

	const payload = () => ({
		dx: lastX - startX,
		dy: lastY - startY,
		velocity: velocity(),
		committed: engaged,
		held,
	});

	/** The pointer was taken away rather than released. */
	const abandon = () => {
		const data = { ...payload(), committed: false };
		const id = pointerId;
		reset();
		if (id !== null) releaseCapture(id);
		onCancel?.(data);
	};

	listen('pointerdown', event => {
		if (event.pointerType === 'mouse' && event.button !== 0) return;
		reset();
		if (!canStart(event)) return;
		pointerId = event.pointerId;
		startX = lastX = event.clientX;
		startY = lastY = event.clientY;
		pushSample(event);
		onStart?.({ x: event.clientX, y: event.clientY, event });
		if (holdMs > 0 && (onHold || holdToDrag)) {
			holdTimer = setTimeout(() => {
				holdTimer = 0;
				held = true;
				if (holdToDrag) engage();
				onHold?.();
			}, holdMs);
		}
	});

	listen('pointermove', event => {
		if (event.pointerId !== pointerId) return;
		pushSample(event);
		lastX = event.clientX;
		lastY = event.clientY;
		const dx = lastX - startX;
		const dy = lastY - startY;

		// Any real movement means this is a drag or a scroll, not a hold. Note
		// this slop is deliberately smaller than `threshold`: the press
		// affordance has to come off before the drag takes over, or a finger
		// that moved four pixels still looks like it is being held.
		if (holdTimer && (Math.abs(dx) > holdSlop || Math.abs(dy) > holdSlop)) {
			clearTimeout(holdTimer);
			holdTimer = 0;
			onHoldCancel?.();
			// A gesture whose drag is armed by the hold has nothing left to be.
			if (holdToDrag) { abandon(); return; }
		}

		if (!engaged) {
			if (holdToDrag) return;
			if (lock === 'other') return;
			if (Math.abs(dx) < threshold && Math.abs(dy) < threshold) return;
			const dominant = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
			if (axis !== 'free' && dominant !== axis) { lock = 'other'; return; }
			const along = axis === 'y' ? dy : dx;
			const mode = typeof direction === 'function' ? direction() : direction;
			// Wrong way along our own axis: stay undecided rather than give the
			// gesture away, so a finger that wanders and comes back still works.
			if (mode === 'positive' && along < threshold) return;
			if (mode === 'negative' && along > -threshold) return;
			lock = axis;
			engage();
		}

		if (preventScroll && event.cancelable) event.preventDefault();
		onMove?.({ dx, dy, velocity: velocity(), event });
	});

	listen('pointerup', event => {
		if (event.pointerId !== pointerId) return;
		lastX = event.clientX;
		lastY = event.clientY;
		const data = payload();
		const id = pointerId;
		reset();
		releaseCapture(id);
		onEnd?.(data);
	});

	listen('pointercancel', event => {
		if (event.pointerId !== pointerId) return;
		abandon();
	});

	/* An iOS system edge-swipe steals the pointer and fires neither pointerup
	 * nor pointercancel, which is what leaves a row stuck mid-swipe. It also
	 * fires on a normal release, right after pointerup — harmless, because
	 * pointerId is null by then and this returns immediately. */
	listen('lostpointercapture', event => {
		if (event.pointerId !== pointerId) return;
		abandon();
	});

	return {
		/** End the gesture in flight, from inside a callback. The queue's
		 *  reorder needs this: committing a move re-renders the list out from
		 *  under the very element the pointer is captured on, and without an
		 *  explicit end the next pointermove keeps writing transforms onto a
		 *  row that is no longer in the document. */
		cancel() {
			if (pointerId === null) return;
			abandon();
		},
		destroy() {
			reset();
			controller.abort();
		},
	};
}
