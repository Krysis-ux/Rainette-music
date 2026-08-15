/* Bringing a playlist in from somewhere else — Spotify, Apple Music, a text
 * file a friend sent.
 *
 * A word on what this deliberately does not do. Reading somebody's Spotify
 * playlist straight off a link needs a Spotify API key and an OAuth login, and
 * this app has neither: it is a static page with no server of its own, and
 * asking for a Spotify password to paste into a third-party page is exactly the
 * habit nobody should be taught. So the import works from what Spotify already
 * lets you take out for free — a copied selection, or a CSV from an exporter —
 * and matches each line against your own computer's catalog.
 *
 * The matching is the interesting part: a line is a title and an artist, and a
 * search returns candidates ranked by how well they agree with both.
 */

import { el, toast } from './dom.js';
import { openSheet } from './sheets.js';
import { command, commandError } from './bridge.js';
import { createLocalPlaylist } from './playlists.js';

/* ── Parsing what was pasted ──────────────────────────────────────────────*/

/* The shapes that actually turn up:
 *   Exportify CSV   "Track Name","Artist Name(s)","Album Name",...
 *   Spotify copy    spotify:track:4uLU6hMCjMI75M1A2tKUQC
 *   Plain text      Artist - Title      /      Title - Artist
 *   Numbered list   1. Artist - Title
 */
export function parseImport(text) {
	const lines = String(text || '')
		.split(/\r?\n/)
		.map(line => line.trim())
		.filter(Boolean);
	if (!lines.length) return [];

	const header = lines[0].toLowerCase();
	const isCsv = header.includes('track name') || (header.includes('name') && header.includes('artist'));
	if (isCsv) return parseCsv(lines);

	// An unresolvable line is kept rather than dropped: a paste of bare Spotify
	// URIs would otherwise look like it imported nothing for no stated reason.
	return lines
		.map(parseLine)
		.filter(entry => entry && (entry.title || entry.unresolvable));
}

function parseCsv(lines) {
	const columns = splitCsvRow(lines[0]).map(name => name.trim().toLowerCase());
	const titleAt = columns.findIndex(name => name.includes('track name') || name === 'name' || name === 'title');
	const artistAt = columns.findIndex(name => name.includes('artist'));
	const albumAt = columns.findIndex(name => name.includes('album'));
	if (titleAt < 0) return [];

	return lines.slice(1).map(line => {
		const cells = splitCsvRow(line);
		const title = (cells[titleAt] || '').trim();
		if (!title) return null;
		return {
			title,
			artist: (artistAt >= 0 ? cells[artistAt] || '' : '').split(',')[0].trim(),
			album: albumAt >= 0 ? (cells[albumAt] || '').trim() : '',
		};
	}).filter(Boolean);
}

/* A hand-rolled reader rather than a library: quoted cells containing commas are
 * the only complication a music export ever has. */
function splitCsvRow(line) {
	const cells = [];
	let cell = '';
	let quoted = false;
	for (let at = 0; at < line.length; at += 1) {
		const char = line[at];
		if (quoted) {
			if (char === '"' && line[at + 1] === '"') { cell += '"'; at += 1; }
			else if (char === '"') quoted = false;
			else cell += char;
		} else if (char === '"') quoted = true;
		else if (char === ',') { cells.push(cell); cell = ''; }
		else cell += char;
	}
	cells.push(cell);
	return cells;
}

function parseLine(line) {
	// A bare Spotify URI carries an id and nothing readable, so it is reported
	// rather than silently dropped — the user needs to know why it did not land.
	if (/^(spotify:track:|https?:\/\/open\.spotify\.com\/track\/)/i.test(line)) {
		return { title: '', artist: '', unresolvable: true };
	}
	const bare = line.replace(/^\s*\d+[.)]\s*/, '').trim();
	const parts = bare.split(/\s+[-–—]\s+/);
	if (parts.length >= 2) {
		// "Artist - Title" is the overwhelmingly common order.
		return { title: parts.slice(1).join(' - ').trim(), artist: parts[0].trim(), album: '' };
	}
	return { title: bare, artist: '', album: '' };
}

/* ── Matching against the computer ────────────────────────────────────────*/

function normalise(value) {
	return String(value || '')
		.toLowerCase()
		// "(feat. X)", "- Remastered 2011", "[Official Video]" are noise for a
		// title comparison and present on one side far more often than both.
		.replace(/\((feat|ft|with)[^)]*\)/g, '')
		.replace(/\[[^\]]*\]/g, '')
		.replace(/\s+-\s+(remaster|remastered|radio edit|single version|official).*$/i, '')
		.replace(/[^a-z0-9]+/g, ' ')
		.trim();
}

/** How well a candidate agrees with what was asked for, 0 to 1. */
function score(candidate, wanted) {
	const title = normalise(candidate.title);
	const askedTitle = normalise(wanted.title);
	if (!title || !askedTitle) return 0;

	let value = 0;
	if (title === askedTitle) value += 0.6;
	else if (title.includes(askedTitle) || askedTitle.includes(title)) value += 0.4;
	else return 0;   // a different song entirely; the artist cannot rescue it

	const artist = normalise(candidate.artist);
	const askedArtist = normalise(wanted.artist);
	if (!askedArtist) value += 0.2;
	else if (artist === askedArtist) value += 0.4;
	else if (artist.includes(askedArtist) || askedArtist.includes(artist)) value += 0.25;

	return Math.min(1, value);
}

const MATCH_THRESHOLD = 0.55;

async function matchOne(entry) {
	const query = [entry.artist, entry.title].filter(Boolean).join(' ');
	let songs = [];
	try {
		const result = await command('music_catalog_search', { query }, 30000);
		songs = Array.isArray(result?.songs) ? result.songs : [];
	} catch {
		try {
			const flat = await command('music_search', { query }, 30000);
			songs = flat.items || flat.tracks || [];
		} catch {
			return null;
		}
	}
	let best = null;
	let bestScore = 0;
	for (const song of songs) {
		if (!song?.source_id) continue;
		const value = score(song, entry);
		if (value > bestScore) { best = song; bestScore = value; }
	}
	return bestScore >= MATCH_THRESHOLD ? best : null;
}

/* Searches run a few at a time. One at a time is unusably slow for a 200-track
 * playlist, and all at once buries the computer's rate limiter. */
const CONCURRENCY = 3;

async function matchAll(entries, onProgress) {
	const found = [];
	const missing = [];
	let index = 0;
	let done = 0;

	async function worker() {
		while (index < entries.length) {
			const at = index;
			index += 1;
			const entry = entries[at];
			const match = entry.unresolvable ? null : await matchOne(entry);
			if (match) found.push(match); else missing.push(entry);
			done += 1;
			onProgress?.(done, entries.length);
		}
	}

	await Promise.all(Array.from({ length: Math.min(CONCURRENCY, entries.length) }, worker));
	return { found, missing };
}

/* ── The sheet ────────────────────────────────────────────────────────────*/

export function openImportPlaylist({ onImported } = {}) {
	openSheet({
		title: 'Import a playlist',
		className: 'sheet-catalog',
		full: true,
		build: ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Import a playlist'));
			body.append(el('p', 'sheet-message',
				'Paste a track list, or pick a CSV exported from Spotify. Rainette looks each one up on your computer and builds a playlist from what it finds. Nothing is sent to Spotify and no login is needed.'));

			const nameField = el('label', 'field');
			nameField.append(el('span', '', 'Playlist name'));
			const nameInput = document.createElement('input');
			nameInput.type = 'text';
			nameInput.maxLength = 80;
			nameInput.placeholder = 'Imported playlist';
			nameField.append(nameInput);

			const pasteField = el('label', 'field');
			pasteField.append(el('span', '', 'Tracks, one per line'));
			const paste = document.createElement('textarea');
			paste.rows = 7;
			paste.placeholder = 'Drake - Nokia\nNettspend - Drum Go Dum\n\n…or paste an Exportify CSV';
			pasteField.append(paste);

			const fileRow = el('div', 'sheet-buttons');
			const fileButton = el('button', 'ghost', 'Choose a CSV or text file');
			fileButton.type = 'button';
			fileButton.addEventListener('click', () => pickTextFile(text => {
				paste.value = text;
				status.textContent = `${parseImport(text).length} tracks read from that file.`;
			}));
			fileRow.append(fileButton);

			const status = el('p', 'sheet-message', '');
			const go = el('button', 'primary', 'Find these on my computer');
			go.type = 'button';

			go.addEventListener('click', async () => {
				const entries = parseImport(paste.value);
				if (!entries.length) {
					status.textContent = 'Nothing to import yet — paste some tracks first.';
					return;
				}
				go.disabled = true;
				fileButton.disabled = true;
				status.textContent = `Looking up ${entries.length} track${entries.length === 1 ? '' : 's'}…`;

				try {
					const { found, missing } = await matchAll(entries, (done, total) => {
						status.textContent = `Looked up ${done} of ${total}…`;
					});
					if (!found.length) {
						status.textContent = 'None of those could be found on your computer.';
						go.disabled = false;
						fileButton.disabled = false;
						return;
					}
					const name = nameInput.value.trim() || 'Imported playlist';
					createLocalPlaylist(name, found);
					onImported?.();
					toast(`Imported ${found.length} track${found.length === 1 ? '' : 's'}`, { icon: 'check' });
					status.textContent = missing.length
						? `Added ${found.length}. ${missing.length} could not be matched: ${missing.slice(0, 3).map(entry => entry.title || 'a Spotify link').join(', ')}${missing.length > 3 ? '…' : ''}`
						: `Added all ${found.length} to “${name}”.`;
					go.disabled = false;
					fileButton.disabled = false;
				} catch (error) {
					status.textContent = commandError(error, 'Your computer could not answer that.');
					go.disabled = false;
					fileButton.disabled = false;
				}
			});

			body.append(nameField, pasteField, fileRow, go, status);
			body.append(el('p', 'catalog-note',
				'To get a list out of Spotify: open the playlist on a computer, select every track, and copy. For a CSV, exportify.net reads your account and gives you a file — Rainette never sees your Spotify account either way.'));
		},
	});
}

function pickTextFile(onRead) {
	const input = document.createElement('input');
	input.type = 'file';
	input.accept = '.csv,.txt,.tsv,text/csv,text/plain';
	input.style.display = 'none';
	document.body.append(input);
	input.addEventListener('change', async () => {
		const file = input.files?.[0];
		input.remove();
		if (!file) return;
		try {
			onRead(await file.text());
		} catch {
			toast('That file could not be read.', { icon: 'close' });
		}
	});
	input.click();
}
