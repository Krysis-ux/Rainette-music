/* One pointer-drag contract. */

/* A single-sample derivative is the velocity of the last 16ms, and the last 16ms of a flick is the
 * part where the finger is already slowing down — which is why a fast throw used to read as ≈0 and
 * spring back. */
const VELOCITY_WINDOW_MS = 100;

/** Wire a drag on `element`. - `axis` — `'x'` | `'y'` | `'free'`. */
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

	/* Unconditional, and it runs *before* pointerdown does anything else. */
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

		// Any real movement means this is a drag or a scroll, not a hold.
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

	/* Pointer events alone cannot hold a vertical drag on a scrollable panel. */
	element.addEventListener('touchmove', event => {
		if (pointerId === null || axis !== 'y' || !preventScroll) return;
		if (!event.cancelable || event.touches.length !== 1) return;
		const dy = event.touches[0].clientY - startY;
		if (engaged) { event.preventDefault(); return; }
		const mode = typeof direction === 'function' ? direction() : direction;
		if (mode === 'positive' && dy <= 0) return;
		if (mode === 'negative' && dy >= 0) return;
		if (Math.abs(dy) < 2) return;
		event.preventDefault();
	}, { passive: false, signal: controller.signal });

	/** The pointer was released, or taken away from a drag that had already
	 *  engaged — which amounts to the same thing for the caller. */
	const finish = () => {
		const data = payload();
		const id = pointerId;
		reset();
		if (id !== null) releaseCapture(id);
		onEnd?.(data);
	};

	listen('pointerup', event => {
		if (event.pointerId !== pointerId) return;
		lastX = event.clientX;
		lastY = event.clientY;
		finish();
	});

	listen('pointercancel', event => {
		if (event.pointerId !== pointerId) return;
		abandon();
	});

	/* An iOS system gesture can take the pointer away and fire neither pointerup nor pointercancel,
	 * which is what leaves a row stuck mid-swipe. */
	listen('lostpointercapture', event => {
		if (event.pointerId !== pointerId) return;
		/* A touch starts captured by whatever is under the finger — the cover image, a row's title. */
		if (event.target !== element && element.hasPointerCapture?.(event.pointerId)) return;
		if (engaged) { finish(); return; }
		abandon();
	});

	return {
		/** End the gesture in flight, from inside a callback. */
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
