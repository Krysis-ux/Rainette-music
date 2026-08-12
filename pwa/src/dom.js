/* DOM, icon and motion primitives. On a small screen an untransitioned change
 * reads as a different screen, so motion carries continuity.
 * `prefers-reduced-motion` is honoured in JS as well as CSS. */

export const $ = selector => document.querySelector(selector);

/** Create an element with an optional class and text, in one call. */
export function el(tag, className = '', text = '') {
	const node = document.createElement(tag);
	if (className) node.className = className;
	if (text) node.textContent = text;
	return node;
}

export function motionOff() {
	return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

/* A very short vibration on a committed gesture — adding to the queue, snapping
 * a sheet shut. Confirmation for an action whose result is off-screen; never for
 * one the user can already see land. Silently unsupported on iOS Safari. */
export function tap(pattern = 8) {
	if (motionOff()) return;
	try { navigator.vibrate?.(pattern); } catch { /* unsupported */ }
}

const ICONS = {
	play: '<path d="M8 5.6v12.8a1 1 0 0 0 1.53.85l10-6.4a1 1 0 0 0 0-1.7l-10-6.4A1 1 0 0 0 8 5.6z" fill="currentColor" stroke="none"/>',
	pause: '<path d="M7.5 5h3.2v14H7.5zM13.3 5h3.2v14h-3.2z" fill="currentColor" stroke="none"/>',
	prev: '<path d="M7 6v12"/><path d="M18 7.2v9.6a1 1 0 0 1-1.5.87l-8-4.8a1 1 0 0 1 0-1.74l8-4.8A1 1 0 0 1 18 7.2z" fill="currentColor" stroke="none"/>',
	next: '<path d="M17 6v12"/><path d="M6 7.2v9.6a1 1 0 0 0 1.5.87l8-4.8a1 1 0 0 0 0-1.74l-8-4.8A1 1 0 0 0 6 7.2z" fill="currentColor" stroke="none"/>',
	shuffle: '<path d="M16 3h5v5"/><path d="M4 20 21 3"/><path d="M21 16v5h-5"/><path d="m15 15 6 6"/><path d="m4 4 5 5"/>',
	loop: '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
	loopOne: '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/><path d="M11 10h1v4"/>',
	volume: '<path d="M11 5 6 9H2v6h4l5 4z" fill="currentColor" stroke="none"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
	volumeOff: '<path d="M11 5 6 9H2v6h4l5 4z" fill="currentColor" stroke="none"/><path d="m16 9 5 6"/><path d="m21 9-5 6"/>',
	queue: '<path d="M4 7h10"/><path d="M4 12h6"/><path d="M4 17h6"/><path d="M14.5 9.35v5.3a.5.5 0 0 0 .77.42l4.24-2.65a.5.5 0 0 0 0-.84L15.27 8.93a.5.5 0 0 0-.77.42z" fill="currentColor" stroke="none"/>',
	listAdd: '<path d="M4 7h13"/><path d="M4 12h9"/><path d="M4 17h9"/><path d="M18 14v6"/><path d="M15 17h6"/>',
	mic: '<path d="M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 11a7 7 0 0 1-14 0"/><path d="M12 18v4"/>',
	moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
	more: '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
	chevronDown: '<path d="m6 9 6 6 6-6"/>',
	close: '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
	plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
	trash: '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
	grip: '<path d="M8 7h.01M8 12h.01M8 17h.01M16 7h.01M16 12h.01M16 17h.01" stroke-width="2.6"/>',
	speaker: '<rect x="5" y="2" width="14" height="20" rx="2"/><circle cx="12" cy="15" r="3.2"/><path d="M12 6h.01"/>',
	bluetooth: '<path d="m7 7 10 10-5 4V3l5 4L7 17"/>',
	laptop: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M2 20h20"/>',
	phone: '<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M10.5 18.5h3"/>',
	headphones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><path d="M4 15a2 2 0 0 1 2-2h1v7H6a2 2 0 0 1-2-2z"/><path d="M20 15a2 2 0 0 0-2-2h-1v7h1a2 2 0 0 0 2-2z"/>',
	link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
	check: '<path d="m5 13 4 4L19 7"/>',
};

export function icon(name, size = 20) {
	return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONS[name] || ''}</svg>`;
}

/** An icon button with an accessible name and no visible label. */
export function iconButton(name, { label, className = '', size = 20, onClick }) {
	const button = el('button', `icon ${className}`.trim());
	button.type = 'button';
	button.innerHTML = icon(name, size);
	button.setAttribute('aria-label', label);
	button.title = label;
	if (onClick) button.addEventListener('click', onClick);
	return button;
}

/* ── Toast ─────────────────────────────────────────────────────────────────
 * Actions that take effect somewhere the user is not currently looking — adding
 * to a queue they have not opened, say — need an acknowledgement or they feel
 * like they did nothing. Actions with visible results do not get one. */

let toastTimer = 0;

export function toast(message, { icon: glyph = 'check' } = {}) {
	const host = $('#toast');
	if (!host) return;
	host.innerHTML = `${icon(glyph, 17)}<span></span>`;
	host.querySelector('span').textContent = message;
	host.classList.add('show');
	clearTimeout(toastTimer);
	toastTimer = setTimeout(() => host.classList.remove('show'), 2200);
}

/* ── Staggered entrance ────────────────────────────────────────────────────
 * A list that appears all at once reads as a flash. Offsetting each row by a
 * few milliseconds gives the eye an order to follow it in. Capped, because past
 * roughly a dozen rows the stagger stops reading as one movement and starts
 * reading as a slow list. */
export function stagger(container, selector = ':scope > *') {
	if (motionOff()) return;
	const rows = container.querySelectorAll(selector);
	rows.forEach((row, index) => {
		if (index > 11) return;
		row.style.setProperty('--enter-delay', `${index * 26}ms`);
		row.classList.add('enter');
	});
}

/* ── FLIP ──────────────────────────────────────────────────────────────────
 * Reordering the queue re-renders it, which normally teleports every row. FLIP
 * measures where rows were, lets the re-render happen, then animates each row
 * from its old position to its new one, so a moved item is visibly the same
 * item that moved. */
export function flip(container, rowSelector, mutate) {
	if (motionOff()) { mutate(); return; }
	const before = new Map();
	for (const row of container.querySelectorAll(rowSelector)) {
		if (row.dataset.flipKey) before.set(row.dataset.flipKey, row.getBoundingClientRect().top);
	}
	mutate();
	for (const row of container.querySelectorAll(rowSelector)) {
		const previous = before.get(row.dataset.flipKey);
		if (previous === undefined) continue;
		const delta = previous - row.getBoundingClientRect().top;
		if (!delta) continue;
		row.animate(
			[{ transform: `translateY(${delta}px)` }, { transform: 'none' }],
			{ duration: 260, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' },
		);
	}
}

/** Collapse a row to nothing before removing it, so deletion has a direction. */
export function collapseAway(row, done) {
	if (motionOff()) { done(); return; }
	const height = row.getBoundingClientRect().height;
	row.style.overflow = 'hidden';
	const animation = row.animate(
		[
			{ height: `${height}px`, opacity: 1, transform: 'none' },
			{ height: '0px', opacity: 0, transform: 'translateX(-14px)' },
		],
		{ duration: 220, easing: 'cubic-bezier(0.4, 0, 1, 1)', fill: 'forwards' },
	);
	animation.finished.then(done, done);
}
