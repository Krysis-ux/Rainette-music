/* Backing the phone up to the phone.
 *
 * Everything this app knows that the computer does not — the playlists made
 * here, the settings, the play counts, the recent list — is written to one JSON
 * file and handed to whatever the phone does with files. It is a download, not
 * an upload: nothing is sent anywhere, and a backup taken on a plane restores on
 * a plane.
 *
 * Audio itself is deliberately not included. A library of local files is
 * gigabytes and belongs in the phone's own backup, not in a JSON blob; the
 * playlists that reference them are what is worth keeping.
 */

import { toast } from './dom.js';
import { STORAGE } from './state.js';
import { exportPrefs, importPrefs } from './prefs.js';
import { exportLocalPlaylists, importLocalPlaylists } from './playlists.js';

const FORMAT = 'rainette-phone-backup';
const VERSION = 1;

function readJson(key, fallback) {
	try {
		const value = JSON.parse(localStorage.getItem(key) || 'null');
		return value ?? fallback;
	} catch {
		return fallback;
	}
}

export function buildBackup() {
	return {
		format: FORMAT,
		version: VERSION,
		taken_at: new Date().toISOString(),
		settings: exportPrefs(),
		playlists: exportLocalPlaylists(),
		recent: readJson(STORAGE.recent, []),
		plays: readJson(STORAGE.plays, {}),
		artist_art: readJson(STORAGE.artistArt, {}),
		// Deliberately absent: the pairing endpoint and device token. A backup
		// that carries a credential is a credential that travels through every
		// place the file goes.
	};
}

export function downloadBackup() {
	const payload = JSON.stringify(buildBackup(), null, '\t');
	const blob = new Blob([payload], { type: 'application/json' });
	const url = URL.createObjectURL(blob);
	const stamp = new Date().toISOString().slice(0, 10);

	const link = document.createElement('a');
	link.href = url;
	link.download = `rainette-backup-${stamp}.json`;
	document.body.append(link);
	link.click();
	link.remove();
	// Revoking immediately can cancel the download on some browsers; a moment
	// later it is safely past.
	setTimeout(() => URL.revokeObjectURL(url), 30_000);
	toast('Backup saved to this phone', { icon: 'check' });
}

/** Read a backup the user picked and apply it. Returns a short summary. */
export async function restoreBackup(file, { replace = false } = {}) {
	let payload;
	try {
		payload = JSON.parse(await file.text());
	} catch {
		throw new Error('That file is not a Rainette backup.');
	}
	if (payload?.format !== FORMAT) throw new Error('That file is not a Rainette backup.');
	if (Number(payload.version) > VERSION) {
		throw new Error('That backup was made by a newer Rainette. Update this app first.');
	}

	importPrefs(payload.settings);
	const playlists = importLocalPlaylists(payload.playlists, { replace });

	if (Array.isArray(payload.recent)) writeJson(STORAGE.recent, payload.recent);
	if (payload.plays && typeof payload.plays === 'object') writeJson(STORAGE.plays, payload.plays);
	if (payload.artist_art && typeof payload.artist_art === 'object') {
		writeJson(STORAGE.artistArt, payload.artist_art);
	}

	return { playlists, taken: payload.taken_at || '' };
}

function writeJson(key, value) {
	try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* quota */ }
}

/** Ask for a backup file. Resolves null if the picker was dismissed. */
export function pickBackup() {
	return new Promise(resolve => {
		const input = document.createElement('input');
		input.type = 'file';
		input.accept = 'application/json,.json';
		input.style.display = 'none';
		document.body.append(input);

		let settled = false;
		const finish = value => {
			if (settled) return;
			settled = true;
			input.remove();
			resolve(value);
		};

		input.addEventListener('change', () => finish(input.files?.[0] || null));
		window.addEventListener('focus', () => setTimeout(() => finish(null), 600), { once: true });
		input.click();
	});
}
