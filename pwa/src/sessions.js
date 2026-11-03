/* Computers this phone has been paired with, so reconnecting is a tap. */

import { STORAGE, readJson, persist } from './state.js';

/* Enough to cover a desk, a laptop and a friend's machine without turning the
 * list into a thing that itself needs managing. */
const MAX_SESSIONS = 8;

/* Tokens live in their own map rather than inside the session rows. */
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

/** Mark a session as one we could not reach just now. */
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
