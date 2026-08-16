/* One slider. Four call sites: seek, volume, the five EQ bands, settings volume.
 *
 * A native range input cannot separate the visual thumb from the hit target —
 * `::-webkit-slider-thumb`'s box is both — so honouring --touch there means a
 * 48px dot. Here the rail is 4px because that is what reads as a progress line
 * and the row is 48px because that is what a thumb can find. Those are
 * different numbers and this component keeps them apart.
 *
 * The other half of the reason it exists: the interaction contract — the
 * render-vs-drag guard, the settle window, capture ordering, haptics — is
 * component state. With four native ranges each site re-implemented it, which
 * is how commitSeek ended up bound to `change`.
 *
 * The cost, paid in full below: a11y is hand-written. role, aria-value*,
 * arrows, Home/End, PageUp/Down and an aria-valuetext that says "1:34 of 3:20"
 * rather than "94".
 */

import { el, tap } from './dom.js';

/* How long after a commit the thumb keeps its own value. seekTo() writes
 * audio.currentTime, which fires timeupdate synchronously — and that first tick
 * still carries the pre-seek position. Without a window the thumb is yanked
 * back to where the drag started, by our own seek. */
const SETTLE_MS = 400;
/* keyup is unreliable when focus moves during a key repeat, so a held arrow
 * also releases on idle rather than hanging the guard open for ever. */
const KEY_IDLE_MS = 250;
/* Fallback half-thumb, used only before the element is in the document and the
 * real --rs-thumb-lg can be read. */
const THUMB_INSET = 11;

const clamp01 = n => Math.min(1, Math.max(0, n));

export function createSlider({
	min = 0,
	max = 100,
	step = 1,
	value = 0,
	variant = 'default',
	label,
	format = null,
	detent = null,
	keyStep = null,
	pageStep = null,
	settleTolerance = 0,
	boostAbove = null,
	onInput = null,
	onCommit = null,
	onGrab = null,
	haptic = true,
} = {}) {
	const root = el('div', 'rs');
	root.dataset.variant = variant;

	const rail = el('div', 'rs-rail');
	rail.setAttribute('aria-hidden', 'true');
	const fill = el('div', 'rs-fill');
	rail.append(fill);
	if (detent !== null) rail.append(el('div', 'rs-detent'));

	const thumb = el('div', 'rs-thumb');
	thumb.setAttribute('aria-hidden', 'true');

	// The only focusable, only interactive node. Rail, fill, thumb and detent
	// are presentation, so the accessibility tree holds exactly one node per
	// slider rather than five.
	const hit = el('div', 'rs-hit');
	hit.setAttribute('role', 'slider');
	hit.tabIndex = 0;
	hit.setAttribute('aria-label', label || 'Slider');
	hit.setAttribute('aria-orientation', 'horizontal');

	root.append(rail, thumb, hit);

	let current = value;
	let disabled = false;
	let thumbInset = THUMB_INSET;

	/* Three states, not two. `interacting` is a live gesture; `settleUntil` is a
	 * commit still travelling to the source of truth; neither may be overwritten
	 * by the stream. */
	let pointerId = null;
	let keyHeld = false;
	let keyIdle = 0;
	let settleUntil = 0;
	let committedValue = null;

	const interacting = () => pointerId !== null || keyHeld;
	/* max <= min is a seek bar whose duration has not arrived. There is nothing
	 * to point at, so it says so rather than accepting a drag into nowhere. */
	const inert = () => disabled || !(max > min);

	const arrowStep = () => (keyStep === null ? (step || 1) : keyStep);
	const bigStep = () => (pageStep === null ? (max - min) / 10 : pageStep);
	/* One full step of magnet, so 0 dB is twice as wide as its neighbours —
	 * which is what makes a detent something you land on rather than aim at. */
	const magnet = () => (detent === null ? 0 : Math.max(step || 1, (max - min) * 0.015));

	function clampSnap(raw) {
		let next = Math.min(max, Math.max(min, raw));
		if (detent !== null && Math.abs(next - detent) <= magnet()) {
			next = detent;
		} else if (step > 0) {
			next = min + Math.round((next - min) / step) * step;
			next = Math.min(max, Math.max(min, next));
		}
		// Float dust from (max - min) / step arithmetic reads as "99.99999%".
		return Number(next.toFixed(6));
	}

	function describe(v) {
		if (format) return format(v, { min, max });
		return String(v);
	}

	function paint() {
		const span = max - min;
		const ratio = span > 0 ? clamp01((current - min) / span) : 0;
		root.style.setProperty('--rs-value', String(ratio));

		if (detent !== null) {
			const at = span > 0 ? clamp01((detent - min) / span) : 0.5;
			root.style.setProperty('--rs-detent-at', String(at));
			// A band cut to -6 is not "less full", it is negative — so the fill
			// starts at the detent and runs toward the thumb, in either
			// direction. translate-then-scale is what expresses that with one
			// composited transform; scaleX alone can only grow from an edge.
			root.style.setProperty('--rs-from', String(Math.min(ratio, at)));
			root.style.setProperty('--rs-span', String(Math.abs(ratio - at)));
		}

		hit.setAttribute('aria-valuemin', String(min));
		hit.setAttribute('aria-valuemax', String(max));
		hit.setAttribute('aria-valuenow', String(current));
		hit.setAttribute('aria-valuetext', describe(current));
		if (inert()) hit.setAttribute('aria-disabled', 'true');
		else hit.removeAttribute('aria-disabled');
		root.classList.toggle('is-disabled', disabled);

		if (boostAbove !== null) root.classList.toggle('is-boosted', current > boostAbove);
	}

	/** `source`: 'stream' is guarded, 'user' and 'force' are not. */
	function setValue(v, source = 'force') {
		const next = clampSnap(v);
		if (source === 'stream') {
			// A live gesture always wins.
			if (interacting()) return;
			if (performance.now() < settleUntil) {
				// …unless the stream has caught up, in which case release the
				// window early rather than sitting out a fixed 400ms.
				if (committedValue === null || Math.abs(next - committedValue) > settleTolerance) return;
				settleUntil = 0;
				committedValue = null;
			}
		}
		current = next;
		paint();
	}

	/** Set from a gesture: paint, then tell the caller, then fire the feel. */
	function applyUser(raw) {
		const before = current;
		setValue(raw, 'user');
		if (current === before) return;
		onInput?.(current);
		feel(before, current);
	}

	/* Haptics fire for a committed gesture whose result is off-screen, never for
	 * a value the user is watching change — so there is no per-step tick on a
	 * drag. These two are the exceptions because both are *mode* changes rather
	 * than value changes, and both are stateless crossing tests: entering only,
	 * once, however many frames the crossing took. */
	function feel(before, after) {
		if (!haptic) return;
		// The one value in an EQ worth feeling is the one you are trying to
		// return to.
		if (detent !== null && after === detent && before !== detent) tap(3);
		// Past unity the app is amplifying rather than attenuating, and that is
		// worth saying.
		if (boostAbove !== null && after > boostAbove && before <= boostAbove) tap(8);
	}

	function measureThumb() {
		const raw = getComputedStyle(root).getPropertyValue('--rs-thumb-lg');
		const parsed = Number.parseFloat(raw);
		if (Number.isFinite(parsed) && parsed > 0) thumbInset = parsed / 2;
	}

	/* The rail's rect, not the hit's, and inset by half a thumb at each end —
	 * otherwise the thumb's centre cannot reach either extreme while the finger
	 * is on it, and the last few percent of the range are unreachable. */
	function ratioAt(clientX) {
		const rect = rail.getBoundingClientRect();
		const usable = Math.max(1, rect.width - thumbInset * 2);
		return clamp01((clientX - rect.left - thumbInset) / usable);
	}

	function applyFromClientX(clientX) {
		applyUser(min + ratioAt(clientX) * (max - min));
	}

	function commit() {
		committedValue = current;
		settleUntil = performance.now() + SETTLE_MS;
		if (haptic) tap(6);
		onCommit?.(current);
	}

	// ── Pointer ─────────────────────────────────────────────────────────────
	// Ordering copied from the desktop's wireSeekBar: act, *then* capture. A
	// setPointerCapture that throws must never swallow the interaction.

	hit.addEventListener('pointerdown', event => {
		if (event.pointerType === 'mouse' && event.button !== 0) return;
		pointerId = null;
		if (inert()) return;
		pointerId = event.pointerId;
		root.classList.add('is-grabbing');
		measureThumb();
		onGrab?.();
		applyFromClientX(event.clientX);
		try { hit.setPointerCapture?.(event.pointerId); } catch { /* not capturable */ }
		if (event.cancelable) event.preventDefault();
		if (haptic) tap(4);
	});

	hit.addEventListener('pointermove', event => {
		if (event.pointerId !== pointerId) return;
		if (event.cancelable) event.preventDefault();
		applyFromClientX(event.clientX);
	});

	const release = event => {
		if (event.pointerId !== pointerId) return;
		const id = pointerId;
		pointerId = null;
		root.classList.remove('is-grabbing');
		try { hit.releasePointerCapture?.(id); } catch { /* already gone */ }
		commit();
	};

	hit.addEventListener('pointerup', release);
	hit.addEventListener('pointercancel', release);
	// A capture lost to a system gesture (iOS edge swipe) fires neither of the
	// two above, and would otherwise hold the guard open for the rest of the
	// session — the thumb frozen while the track plays on underneath it.
	hit.addEventListener('lostpointercapture', release);

	// ── Keyboard ────────────────────────────────────────────────────────────

	function beginKey() {
		keyHeld = true;
		clearTimeout(keyIdle);
		keyIdle = setTimeout(endKey, KEY_IDLE_MS);
	}

	function endKey() {
		clearTimeout(keyIdle);
		keyIdle = 0;
		if (!keyHeld) return;
		keyHeld = false;
		commit();
	}

	hit.addEventListener('keydown', event => {
		if (inert()) return;
		if (event.altKey || event.ctrlKey || event.metaKey) return;
		let next = current;
		switch (event.key) {
			case 'ArrowRight': case 'ArrowUp': next = current + arrowStep(); break;
			case 'ArrowLeft': case 'ArrowDown': next = current - arrowStep(); break;
			case 'PageUp': next = current + bigStep(); break;
			case 'PageDown': next = current - bigStep(); break;
			case 'Home': next = min; break;
			case 'End': next = max; break;
			default: return;
		}
		event.preventDefault();
		// A held arrow is one guarded interaction with one commit, not one per
		// repeat — otherwise every repeat starts a fresh 400ms settle window and
		// the stream never gets the thumb back.
		beginKey();
		applyUser(next);
	});

	hit.addEventListener('keyup', endKey);

	hit.addEventListener('focus', () => root.classList.add('is-focus'));
	hit.addEventListener('blur', () => {
		root.classList.remove('is-focus');
		endKey();
	});

	setValue(value, 'force');

	return {
		root,
		setValue,
		setRange(nextMin, nextMax) {
			min = nextMin;
			max = nextMax;
			current = Math.min(max, Math.max(min, current));
			paint();
		},
		setDisabled(on) {
			disabled = !!on;
			hit.tabIndex = disabled ? -1 : 0;
			paint();
		},
		get value() { return current; },
		get interacting() { return interacting(); },
		destroy() {
			clearTimeout(keyIdle);
			pointerId = null;
			keyHeld = false;
			root.remove();
		},
	};
}
