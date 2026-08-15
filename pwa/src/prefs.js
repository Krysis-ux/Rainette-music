/* Themes, accents, and the handful of preferences that change how the app
 * behaves rather than what it shows.
 *
 * The four themes and three accents are the desktop's, by the same names and
 * the same ids, so a phone and the computer it pairs with can be set to look
 * like each other rather than merely similar.
 *
 * Everything here is read at boot and applied before the first paint, because a
 * theme that arrives a frame late is a flash of the wrong one.
 */

import { STORAGE } from './state.js';

export const THEMES = [
	{ id: 'midnight', label: 'Midnight', hint: 'The original — near-black, green light' },
	{ id: 'dark', label: 'Dark', hint: 'Neutral grey, easier on OLED' },
	{ id: 'mono', label: 'Monochrome', hint: 'No colour at all' },
	{ id: 'light', label: 'Light', hint: 'For daylight' },
];

export const ACCENTS = [
	{ id: 'default', label: 'Rainette green' },
	{ id: 'teal', label: 'Teal' },
	{ id: 'purple', label: 'Purple' },
	{ id: 'amber', label: 'Amber' },
	{ id: 'rose', label: 'Rose' },
];

const THEME_IDS = THEMES.map(theme => theme.id);
const ACCENT_IDS = ACCENTS.map(accent => accent.id);

/* The status bar and the browser's own chrome are painted from this, so it has
 * to move with the theme or a light app keeps a black notch. */
const THEME_COLOR = {
	midnight: '#0a0d0b',
	dark: '#141614',
	mono: '#101010',
	light: '#f4f6f4',
};

function read(key, allowed, fallback) {
	try {
		const value = localStorage.getItem(key);
		return allowed.includes(value) ? value : fallback;
	} catch {
		return fallback;
	}
}

export function currentTheme() {
	return read(STORAGE.theme, THEME_IDS, 'midnight');
}

export function currentAccent() {
	return read(STORAGE.accent, ACCENT_IDS, 'default');
}

export function applyTheme(theme) {
	const id = THEME_IDS.includes(theme) ? theme : 'midnight';
	try { localStorage.setItem(STORAGE.theme, id); } catch { /* private mode */ }
	const root = document.documentElement;
	for (const other of THEME_IDS) root.classList.toggle(`theme-${other}`, other === id);
	root.dataset.theme = id;
	// `color-scheme` is what makes form controls, scrollbars and the on-screen
	// keyboard match; without it a light theme still gets dark native widgets.
	root.style.colorScheme = id === 'light' ? 'light' : 'dark';
	const meta = document.querySelector('meta[name="theme-color"]');
	if (meta) meta.setAttribute('content', THEME_COLOR[id] || THEME_COLOR.midnight);
	return id;
}

export function applyAccent(accent) {
	const id = ACCENT_IDS.includes(accent) ? accent : 'default';
	try { localStorage.setItem(STORAGE.accent, id); } catch { /* private mode */ }
	const root = document.documentElement;
	for (const other of ACCENT_IDS) root.classList.toggle(`accent-${other}`, other === id);
	return id;
}

/* ── Preferences ──────────────────────────────────────────────────────────
 * One object, one storage key. Each entry names its default, so a preference
 * added later is simply absent from an existing user's stored copy and falls
 * back rather than reading as `undefined` somewhere far away. */

export const DEFAULTS = {
	/* Where the app opens. "Wherever I was" is not offered: a music app that
	 * reopens on a settings screen is a music app that lost your place. */
	landingTab: 'home',
	/* A soft ramp instead of a hard cut on play and pause. */
	fade: true,
	fadeMs: 320,
	/* Keep going when the queue runs dry, seeded from the last track. */
	autoplaySimilar: false,
	/* Skip the gap between tracks by starting the next one a moment early. */
	crossfadeMs: 0,
	/* Entrance animations off, for motion sensitivity or an older phone. */
	reduceMotion: false,
	/* Haptic tap on committed gestures. */
	haptics: true,
	/* Show the swipe-to-queue hint rail behind every track row. */
	swipeActions: true,
	/* Remember what played, on this phone. Off means nothing is written down. */
	history: true,
	/* Artwork resolution for artists costs a request per new name. */
	artistArtwork: true,
};

let cache = null;

function readAll() {
	if (cache) return cache;
	try {
		const stored = JSON.parse(localStorage.getItem(STORAGE.prefs) || '{}');
		cache = { ...DEFAULTS, ...(stored && typeof stored === 'object' ? stored : {}) };
	} catch {
		cache = { ...DEFAULTS };
	}
	return cache;
}

export function pref(name) {
	const value = readAll()[name];
	return value === undefined ? DEFAULTS[name] : value;
}

export function setPref(name, value) {
	const all = { ...readAll(), [name]: value };
	cache = all;
	try { localStorage.setItem(STORAGE.prefs, JSON.stringify(all)); } catch { /* quota */ }
	if (name === 'reduceMotion' || name === 'haptics') applyMotion();
	return value;
}

export function resetPrefs() {
	cache = { ...DEFAULTS };
	try { localStorage.removeItem(STORAGE.prefs); } catch { /* quota */ }
	applyMotion();
}

/** Everything worth putting in a backup: the settings, not the state. */
export function exportPrefs() {
	return { theme: currentTheme(), accent: currentAccent(), prefs: { ...readAll() } };
}

export function importPrefs(payload) {
	if (!payload || typeof payload !== 'object') return;
	if (payload.theme) applyTheme(payload.theme);
	if (payload.accent) applyAccent(payload.accent);
	if (payload.prefs && typeof payload.prefs === 'object') {
		cache = { ...DEFAULTS, ...payload.prefs };
		try { localStorage.setItem(STORAGE.prefs, JSON.stringify(cache)); } catch { /* quota */ }
	}
	applyMotion();
}

/* The preference and the OS setting are the same switch as far as the app is
 * concerned: either one turns the animations off. dom.js reads the class. */
function applyMotion() {
	const root = document.documentElement;
	root.classList.toggle('reduce-motion', pref('reduceMotion') === true);
	root.classList.toggle('no-haptics', pref('haptics') === false);
	root.classList.toggle('no-swipe-actions', pref('swipeActions') === false);
}

// Applied at import time, which is before the first paint, so the app never
// shows one theme and then swaps to another.
applyTheme(currentTheme());
applyAccent(currentAccent());
applyMotion();
