/* Velocity handoff. A gesture that ends does not stop — it continues into an
 * animation that started at the speed the finger was moving. That continuity is
 * the difference between "the sheet I threw" and "an animation that played
 * after I let go", and no CSS easing can express it: a keyframe always starts
 * from a standstill.
 *
 * Critically damped by default: it settles without overshoot, because a sheet
 * that bounces past its resting place reads as a toy rather than a surface.
 *
 * This module drives requestAnimationFrame, which is invisible to both CSS
 * reduced-motion layers, so it answers motionOff() itself — see dom.js:30. */

import { motionOff } from './dom.js';

/* Close enough to stop. Below half a pixel nothing more is visible, and a
 * spring integrated to true rest never arrives. */
const REST_DISPLACEMENT = 0.4;   // px
const REST_VELOCITY = 0.04;      // px/ms

/* A backgrounded tab hands back a dt measured in seconds; integrated whole it
 * launches the value into the next county. */
const MAX_FRAME_MS = 32;
/* Sub-stepping keeps semi-implicit Euler stable at the stiffnesses we use. */
const SUB_STEP_MS = 4;

/**
 * Integrate `from` → `to`, entering at `velocity` (px/ms, sign-consistent with
 * `to - from`). `onFrame(value, velocity)` runs every frame; `onDone(settled)`
 * runs once, with `false` when the motion was cancelled.
 *
 * Returns `cancel()`, which stops the motion and hands back its live
 * `{ value, velocity }` so a new gesture can pick the movement up mid-flight
 * instead of waiting it out. That return value **is** the interruptibility
 * mechanism; a cancel that returns nothing forces the next gesture to either
 * restart from zero or sit out the animation.
 */
export function spring(from, to, velocity = 0, {
	stiffness = 210,
	damping = 28,
	mass = 1,
	onFrame,
	onDone,
} = {}) {
	// Reduced motion means arrival, not travel. The caller still gets its
	// frames — exactly one — so it never has to branch.
	if (motionOff()) {
		onFrame?.(to, 0);
		onDone?.(true);
		return () => ({ value: to, velocity: 0 });
	}

	let x = from;
	let v = velocity;
	let last = performance.now();
	let raf = 0;
	let finished = false;

	const step = now => {
		const dt = Math.min(MAX_FRAME_MS, now - last);
		last = now;

		// Semi-implicit Euler. `v` is carried in px/ms because that is what a
		// pointer gesture measures; the force law wants px/s, hence the two
		// conversions rather than one silent unit mismatch.
		const steps = Math.max(1, Math.ceil(dt / SUB_STEP_MS));
		const h = dt / steps / 1000;
		for (let i = 0; i < steps; i += 1) {
			const force = -stiffness * (x - to) - damping * (v * 1000);
			v += (force / mass) * h / 1000;
			x += v * h * 1000;
		}

		if (Math.abs(x - to) < REST_DISPLACEMENT && Math.abs(v) < REST_VELOCITY) {
			finished = true;
			x = to;
			v = 0;
			onFrame?.(to, 0);
			onDone?.(true);
			return;
		}
		onFrame?.(x, v);
		// A caller may cancel from inside its own onFrame. Without this the
		// next frame is queued anyway and the motion runs to completion after
		// having already reported itself cancelled — two onDone calls, and a
		// cancel handle that later reports the target instead of where the
		// motion actually stopped.
		if (finished) return;
		raf = requestAnimationFrame(step);
	};

	raf = requestAnimationFrame(step);

	/** Stop where we are. Safe to call after the motion has already settled —
	 *  it then simply reports the resting state rather than firing onDone a
	 *  second time, so a caller holding a stale handle cannot double-finish. */
	return function cancel() {
		if (finished) return { value: x, velocity: v };
		finished = true;
		cancelAnimationFrame(raf);
		onDone?.(false);
		return { value: x, velocity: v };
	};
}
