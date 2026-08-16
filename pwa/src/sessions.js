/* Computers this phone has been paired with, so reconnecting is a tap.
 *
 * A Cloudflare Quick Tunnel hands out a new hostname every time the computer
 * restarts, and until now the only way to hand the phone the new address was to
 * scan a fresh QR code. The credential never needed replacing — pairing is
 * `(device_id, device_token)` with no endpoint bound into it — so this is
 * purely an address-book problem, and it belongs on the phone.
 *
 * Everything here is localStorage and stays there. It is per-browser by
 * construction, so one user's list can never become another's, and none of it
 * is ever sent anywhere.
 */

import { STORAGE, readJson, persist } from './state.js';

/* Enough to cover a desk, a laptop and a friend's machine without turning the
 * list into a thing that itself needs managing. */
const MAX_SESSIONS = 8;

/* Tokens live in their own map rather than inside the session rows. The rows
 * get read, filtered, sorted, and would be the natural thing to dump while
 * debugging; keeping credentials out of that shape means doing so cannot leak
 * one. Rows carry only whether a token exists. */
function readTokens() {
	return readJson(STORAGE.tokens, {}) || {};
}

function writeTokens(map) {
	persist(STORAGE.tokens, map);
}

function readRows() {
	const rows = readJson(STORAGE.sessions, []);
	return Array.isArray(rows) ? rows : [];
}

/** Past sessions, most recently seen first, each tagged with whether we still
 *  hold a credential for it. */
export function recentSessions() {
	const tokens = readTokens();
	return readRows()
		.map(row => ({ ...row, token_present: Boolean(tokens[row.device_id]) }))
		.sort((a, b) => Number(b.last_seen || 0) - Number(a.last_seen || 0));
}

/** Record, or refresh, the computer this phone is talking to now. */
export function rememberSession({ computerName, endpoint, deviceId, token }) {
	if (!endpoint || !deviceId) return;
	const rows = readRows().filter(row => row.device_id !== deviceId);
	rows.unshift({
		computer_name: String(computerName || 'Your computer'),
		endpoint: String(endpoint),
		device_id: String(deviceId),
		last_seen: Date.now(),
		stale: false,
	});

	const kept = rows.slice(0, MAX_SESSIONS);
	persist(STORAGE.sessions, kept);

	const tokens = readTokens();
	if (token) tokens[deviceId] = token;
	// Evicting a row without evicting its token would leave a credential behind
	// with nothing pointing at it.
	const live = new Set(kept.map(row => row.device_id));
	for (const id of Object.keys(tokens)) if (!live.has(id)) delete tokens[id];
	writeTokens(tokens);
}

/** The stored credential for a past session, if we still hold one. */
export function sessionToken(deviceId) {
	return readTokens()[deviceId] || '';
}

/** Mark a session as one we could not reach just now.
 *
 *  Deliberately not a delete: a rotating tunnel hostname is the normal case,
 *  not a broken pairing, and throwing the credential away would turn a moved
 *  computer into a re-pair. */
export function markSessionStale(deviceId, stale = true) {
	const rows = readRows().map(row => (
		row.device_id === deviceId ? { ...row, stale: Boolean(stale) } : row
	));
	persist(STORAGE.sessions, rows);
}

/** Drop one session and its credential. */
export function forgetSession(deviceId) {
	persist(STORAGE.sessions, readRows().filter(row => row.device_id !== deviceId));
	const tokens = readTokens();
	delete tokens[deviceId];
	writeTokens(tokens);
}

/** Drop everything, for an explicit disconnect. */
export function forgetAllSessions() {
	persist(STORAGE.sessions, []);
	writeTokens({});
}
