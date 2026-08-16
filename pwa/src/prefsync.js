/* Keeping this phone's settings on the computer, so a recognised phone gets
 * them back.
 *
 * Keyed by device id, which makes this backup-and-restore rather than roaming:
 * no other device ever reads this row. That is why theme and accent are
 * included — a phone that reinstalls the PWA wants its own look back — and why
 * nothing here is ever shown to another device.
 *
 * The merge is per key, not per blob. Prefs live in one JSON object, so a
 * whole-object revision would force discarding one side whenever two devices
 * touched different keys; per-key stamps make it commutative and both survive.
 */

import { state, STORAGE, readJson, persist } from './state.js';
import { command } from './bridge.js';
import { DEFAULTS, pref, setPref, currentTheme, currentAccent, applyTheme, applyAccent } from './prefs.js';

/* When each key last changed on this phone. Kept beside the prefs rather than
 * inside them, so the stored settings stay the plain object every other reader
 * already expects. */
const MTIMES = 'rainette.pwa.prefs.mtimes';

/* Two keys are deliberately not mirrored:
 *  - volume changes many times a minute and is session-volatile, so syncing it
 *    is pure write amplification;
 *  - linked mode is already server-authoritative in music_devices, and
 *    mirroring it here would create a second writer for one fact. */
const SYNCED_KEYS = [...Object.keys(DEFAULTS), 'theme', 'accent'];

/* Long enough to collapse a burst of toggles, short enough that closing the app
 * straight after changing something still saves it. */
const DEBOUNCE_MS = 2000;

let timer = 0;
let dirty = new Set();

function readMtimes() {
	return readJson(MTIMES, {}) || {};
}

function localValue(key) {
	if (key === 'theme') return currentTheme();
	if (key === 'accent') return currentAccent();
	return pref(key);
}

function applyValue(key, value) {
	if (key === 'theme') { applyTheme(value); return; }
	if (key === 'accent') { applyAccent(value); return; }
	setPref(key, value);
}

/** Note that a key changed here, and schedule a push. */
export function markPrefChanged(key) {
	if (!SYNCED_KEYS.includes(key)) return;
	const mtimes = readMtimes();
	mtimes[key] = Date.now();
	persist(MTIMES, mtimes);
	dirty.add(key);
	schedulePush();
}

function schedulePush() {
	clearTimeout(timer);
	timer = setTimeout(() => { pushPrefs().catch(() => {}); }, DEBOUNCE_MS);
}

/** Send anything changed since the last push. */
export async function pushPrefs() {
	clearTimeout(timer);
	if (!state.connected || !dirty.size) return;
	const mtimes = readMtimes();
	const entries = [...dirty].map(key => ({
		key, value: localValue(key), updated_ms: Number(mtimes[key]) || Date.now(),
	}));
	dirty = new Set();
	const result = await command('music_device_settings_put', { entries }).catch(() => null);
	if (result?.entries) absorbServerMtimes(result.entries);
}

/* The computer's clock arbitrates. Taking its stamps back means a phone with a
 * wrong date cannot pin a key forever by claiming a time far in the future. */
function absorbServerMtimes(entries) {
	const mtimes = readMtimes();
	for (const entry of entries) {
		if (entry && entry.key) mtimes[entry.key] = Number(entry.updated_ms) || 0;
	}
	persist(MTIMES, mtimes);
}

/** Pull the computer's copy and adopt anything newer than ours.
 *
 *  Runs once per connection. Anything this phone holds more recently is pushed
 *  straight back, so reconnecting settles the two sides in one round trip
 *  rather than leaving them to drift until the next toggle. */
export async function syncPrefs() {
	if (!state.connected) return;
	const result = await command('music_device_settings_get').catch(() => null);
	if (!result?.entries) return;

	const mtimes = readMtimes();
	const behind = [];
	for (const entry of result.entries) {
		const key = String(entry?.key || '');
		if (!SYNCED_KEYS.includes(key)) continue;
		const theirs = Number(entry.updated_ms) || 0;
		const ours = Number(mtimes[key]) || 0;
		if (theirs > ours) {
			applyValue(key, entry.value);
			mtimes[key] = theirs;
		} else if (ours > theirs) {
			behind.push(key);
		}
	}
	persist(MTIMES, mtimes);

	for (const key of behind) dirty.add(key);
	if (dirty.size) await pushPrefs();
}

/** Flush immediately, for the moment the app goes away. */
export function flushPrefs() {
	if (dirty.size) pushPrefs().catch(() => {});
}
