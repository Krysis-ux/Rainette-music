/* Keeping the session alive across the things a phone actually does to it.
 *
 * A phone sleeps. It loses Wi-Fi in a lift. It gets swiped away and restored
 * from the back-forward cache. None of that was handled: there was no
 * visibilitychange, pageshow or freeze handler anywhere in this client, and the
 * only reconnect was a flat 1800ms retry with no backoff and no cap. So a phone
 * that slept for ten minutes woke with a dead long-poll, and the fix from the
 * user's side was to close the app and open it again.
 *
 * This module owns that lifecycle. It does not own the loop itself — sync.js
 * does — it owns the question of when the loop should be nudged.
 */

/* Backoff between reconnect attempts. Half-range jitter, not full: two phones
 * waking from the same lift at the same moment must not retry in lockstep and
 * arrive at the tunnel together, but a reconnect should still feel prompt. */
const BASE_MS = 1000;
const CAP_MS = 30000;

/* Below this a return to the foreground is just a glance, and re-polling would
 * cost more than it recovers. */
const RESUME_GAP_MS = 2000;

/* Past this the computer may have restarted, the tunnel may have moved, and the
 * event log may have evicted this device entirely. Re-check who we are talking
 * to rather than assuming the answer we cached. */
const STALE_SESSION_MS = 60000;

let handlers = {
	onResume: () => {},
	onRecheck: () => {},
	/* Called as the app goes away — the last chance to flush anything pending
	 * before the page is frozen or discarded. */
	onSuspend: () => {},
	isConnected: () => false,
};

let suspendedAt = 0;
let started = false;

export function configureConnection(options = {}) {
	handlers = { ...handlers, ...options };
}

/** Delay before reconnect attempt `attempt` (0-based). */
export function backoffDelay(attempt) {
	const ceiling = Math.min(CAP_MS, BASE_MS * 2 ** Math.max(0, attempt));
	return Math.round(ceiling * (0.5 + Math.random() * 0.5));
}

function wake(reason) {
	if (!handlers.isConnected()) return;
	const gap = suspendedAt ? Date.now() - suspendedAt : 0;
	suspendedAt = 0;
	if (reason === 'online') { handlers.onResume(reason); handlers.onRecheck(reason); return; }
	if (gap < RESUME_GAP_MS) return;
	handlers.onResume(reason);
	// A long sleep is the case where our idea of the computer may simply be
	// wrong — not just our poll being stale.
	if (gap >= STALE_SESSION_MS) handlers.onRecheck(reason);
}

/** Install the lifecycle listeners. Safe to call more than once. */
export function startConnectionWatch() {
	if (started) return;
	started = true;

	document.addEventListener('visibilitychange', () => {
		if (document.hidden) { suspendedAt = Date.now(); handlers.onSuspend(); return; }
		wake('visible');
	});

	// Restored from the back-forward cache. The page was never torn down, so no
	// other event fires and the session looks fine while its poll is long dead.
	window.addEventListener('pageshow', event => {
		if (!event.persisted) return;
		suspendedAt = suspendedAt || Date.now() - STALE_SESSION_MS;
		wake('pageshow');
	});

	// Chromium fires these where Safari fires visibilitychange; handling both
	// costs nothing and neither engine covers the other's case.
	document.addEventListener('freeze', () => { suspendedAt = Date.now(); handlers.onSuspend(); });
	document.addEventListener('resume', () => wake('resume'));

	// A network that came back is not a timer to wait out.
	window.addEventListener('online', () => wake('online'));
}

/** For tests and for teardown. */
export function stopConnectionWatch() {
	started = false;
	suspendedAt = 0;
}
