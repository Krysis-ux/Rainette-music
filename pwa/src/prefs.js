/* Themes, accents and behaviour preferences.
 * Applied at import, before the first paint, so a theme never flashes in late. */

import { STORAGE } from './state.js';

export const THEMES = [
	{ id: 'midnight', label: 'Midnight', hint: 'True black, like the iPhone itself' },
	{ id: 'dark', label: 'Graphite', hint: 'The Mac app\u2019s dark grey' },
	{ id: 'mono', label: 'Monochrome', hint: 'No colour at all' },
	{ id: 'light', label: 'Light', hint: 'For daylight' },
];

export const ACCENTS = [
	{ id: 'default', label: 'Blue' },
	{ id: 'green', label: 'Rainette green' },
	{ id: 'teal', label: 'Teal' },
	{ id: 'purple', label: 'Purple' },
	{ id: 'amber', label: 'Amber' },
	{ id: 'rose', label: 'Rose' },
	{ id: 'silver', label: 'Silver' },
];

const THEME_IDS = THEMES.map(theme => theme.id);
const ACCENT_IDS = ACCENTS.map(accent => accent.id);

/* The status bar and the browser's own chrome are painted from this, so it has
 * to move with the theme or a light app keeps a black notch. */
const THEME_COLOR = {
	midnight: '#000000',
	dark: '#1e1e20',
	mono: '#101010',
	light: '#ffffff',
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
	// In the Android app the status bar sits over the page (newer WebViews) or over the window behind
	// it (older ones).
	try {
		const cap = globalThis.Capacitor;
		if (cap?.isPluginAvailable?.('SystemBars')) {
			cap.nativePromise('SystemBars', 'setStyle', { style: id === 'light' ? 'LIGHT' : 'DARK' }).catch(() => {});
		}
		if (cap?.isPluginAvailable?.('RainettePlayer')) {
			cap.nativePromise('RainettePlayer', 'setSystemBarColor', { color: THEME_COLOR[id] || THEME_COLOR.midnight }).catch(() => {});
		}
	} catch { /* not the app */ }
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
 * One object, one storage key. */

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
	/* Streaming quality on the phone: 'high' or 'saver'. */
	quality: 'high',
	/* Playback speed. */
	rate: 1,
	/* Android only: drop silent stretches. */
	skipSilence: false,
	/* Brushed-silver large titles. */
	silver: true,
	/* Frosted glass behind bars and sheets; off is faster on old phones. */
	glass: true,
	/* Developer: animation speed multiplier and tighter rows. */
	motionScale: 1,
	compact: false,
	/* Developer: the catalog region YouTube Music answers for. */
	region: 'US',
};

let cache = null;

/* Told about every change so the sync layer can mirror it. A callback rather
 * than an import, because prefsync.js imports this module and the reverse edge
 * would make the pair circular. */
let onPrefChanged = () => {};

export function observePrefs(handler) {
	onPrefChanged = handler || onPrefChanged;
}

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
	if (LOOK_KEYS.has(name)) applyMotion();
	onPrefChanged(name);
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
const LOOK_KEYS = new Set(['reduceMotion', 'haptics', 'swipeActions', 'silver', 'glass', 'motionScale', 'compact']);

function applyMotion() {
	const root = document.documentElement;
	root.classList.toggle('reduce-motion', pref('reduceMotion') === true);
	root.classList.toggle('no-haptics', pref('haptics') === false);
	root.classList.toggle('no-swipe-actions', pref('swipeActions') === false);
	root.classList.toggle('silver-titles', pref('silver') !== false);
	root.classList.toggle('no-glass', pref('glass') === false);
	root.classList.toggle('compact', pref('compact') === true);
	const scale = Number(pref('motionScale'));
	root.style.setProperty('--motion-scale', String(scale > 0 && scale <= 3 ? scale : 1));
}

// Applied at import time, which is before the first paint, so the app never
// shows one theme and then swaps to another.
applyTheme(currentTheme());
applyAccent(currentAccent());
applyMotion();
