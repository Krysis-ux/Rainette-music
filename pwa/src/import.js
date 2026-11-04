/* Bringing a playlist in from somewhere else: a Spotify, Apple Music, Deezer or YouTube link, a CSV, a list. */

import { el, toast } from './dom.js';
import { openSheet } from './sheets.js';
import { command, commandError } from './bridge.js';
import { hasComputer } from './state.js';
import { createLocalPlaylist } from './playlists.js';
import { looksLikeLink } from './playlist-links.js';

/* ── Parsing what was pasted ──────────────────────────────────────────────*/

/* The words a converter's CSV uses for each column (Exportify, TuneMyMusic, Soundiiz...). */
const TITLE_WORDS = ['track name', 'title', 'song', 'name', 'track'];
const AUDIO_FILE = /\.(mp3|m4a|aac|flac|wav|ogg|opus|aiff?|wma|alac)$/i;

/* A converter's CSV, an M3U playlist file, or one "Artist - Title" per line. */
export function parseImport(text) {
	let lines = String(text || '')
		.split(/\r?\n/)
		.map(line => line.trim())
		.filter(Boolean);
	if (!lines.length) return [];

	const header = lines[0].toLowerCase();
	if (header.includes(',') && header.includes('artist') && TITLE_WORDS.some(word => header.includes(word))) return parseCsv(lines);

	const extinf = lines.filter(line => /^#EXTINF:/i.test(line));
	lines = extinf.length
		? extinf.map(line => line.slice(line.indexOf(',') + 1)) // "#EXTINF:215,Artist - Title"
		// A bare M3U is file paths; their names usually read "Artist - Title.mp3".
		: lines.filter(line => !line.startsWith('#')).map(line => (AUDIO_FILE.test(line) ? line.split(/[\\/]/).pop().replace(AUDIO_FILE, '') : line));

	// An unresolvable line is kept rather than dropped: a paste of bare Spotify
	// URIs would otherwise look like it imported nothing for no stated reason.
	return lines
		.map(parseLine)
		.filter(entry => entry && (entry.title || entry.unresolvable));
}

/* Earlier words win; a column naming an id, a link or another field is never the one
 * (Exportify's "Artist URI(s)" sits before "Artist Name(s)"). */
function columnFor(columns, words, avoid) {
	const skip = ['uri', 'url', 'id', 'isrc', 'playlist', ...avoid];
	for (const word of words) {
		const at = columns.findIndex(name => name.includes(word)
			&& !name.replace(/[()]/g, ' ').split(/\s+/).some(part => skip.includes(part)));
		if (at >= 0) return at;
	}
	return -1;
}

function parseCsv(lines) {
	const columns = splitCsvRow(lines[0]).map(name => name.trim().toLowerCase());
	const titleAt = columnFor(columns, TITLE_WORDS, ['artist', 'album']);
	const artistAt = columnFor(columns, ['artist'], ['album']);
	const albumAt = columnFor(columns, ['album'], ['artist']);
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
	// NFKC folds full-width forms; letters, marks and numbers of any script are kept
	// (Hindi vowel signs are marks, and 夜曲 has no a-z at all).
	return String(value || '')
		.normalize('NFKC')
		.toLowerCase()
		// "(feat. X)", "- Remastered 2011", "[Official Video]" are noise for a
		// title comparison and present on one side far more often than both.
		.replace(/\((feat|ft|with)[^)]*\)/g, '')
		.replace(/\[[^\]]*\]/g, '')
		.replace(/\s+-\s+(remaster|remastered|radio edit|single version|official).*$/i, '')
		.replace(/[^\p{L}\p{M}\p{N}]+/gu, ' ')
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
		// The phone looks songs up itself, and asks for songs alone: a catalog
		// search is three lookups, and a long playlist would be rate-limited.
		if (!hasComputer()) throw new Error('songs only');
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
			body.append(el('p', 'sheet-message', 'Paste a playlist link from Spotify, Apple Music, YouTube, SoundCloud, Tidal and more.'));

			const nameField = el('label', 'field');
			nameField.append(el('span', '', 'Name (optional)'));
			const nameInput = document.createElement('input');
			nameInput.type = 'text';
			nameInput.maxLength = 80;
			nameInput.placeholder = 'From the link';
			nameField.append(nameInput);

			const pasteField = el('label', 'field');
			pasteField.append(el('span', '', 'Playlist link, or songs one per line'));
			const paste = document.createElement('textarea');
			paste.rows = 5;
			paste.placeholder = 'https://open.spotify.com/playlist/…\n\nor: Drake - Nokia';
			pasteField.append(paste);

			const fileRow = el('div', 'sheet-buttons');
			const fileButton = el('button', 'ghost', 'Choose a CSV or playlist file');
			fileButton.type = 'button';
			fileButton.addEventListener('click', () => pickTextFile(text => {
				paste.value = text;
				status.textContent = `${parseImport(text).length} tracks read from that file.`;
			}));
			fileRow.append(fileButton);

			const status = el('p', 'sheet-message', '');
			const go = el('button', 'primary', 'Import');
			go.type = 'button';

			const setBusy = busy => { go.disabled = busy; fileButton.disabled = busy; };

			go.addEventListener('click', async () => {
				setBusy(true);
				try {
					// A link is read first: its songs (YouTube's are ready), and its name.
					const link = looksLikeLink(paste.value);
					if (link) status.textContent = 'Reading that playlist…';
					const read = link ? await command('music_playlist_link', { url: paste.value.trim() }, 90000) : null;
					const entries = read ? read.entries || [] : parseImport(paste.value);
					const ready = read?.tracks || [];
					if (!entries.length && !ready.length) {
						status.textContent = 'Nothing to import yet. Paste a link or some songs first.';
						setBusy(false);
						return;
					}
					if (entries.length) status.textContent = `Looking up ${entries.length} song${entries.length === 1 ? '' : 's'}…`;
					const { found: matched, missing } = await matchAll(entries, (done, total) => {
						status.textContent = `Looked up ${done} of ${total}…`;
					});
					const found = [...ready, ...matched];
					if (!found.length) {
						status.textContent = 'None of those songs could be found.';
						setBusy(false);
						return;
					}
					const name = nameInput.value.trim() || read?.name || 'Imported playlist';
					createLocalPlaylist(name, found);
					onImported?.();
					toast(`Imported ${found.length} track${found.length === 1 ? '' : 's'}`, { icon: 'check' });
					const readCount = (read?.entries?.length || 0) + ready.length;
					const skipped = !read?.partial ? ''
						: read.total > readCount ? ` Only the first ${readCount} of ${read.total} could be read.` : ` Only the first ${readCount} could be read.`;
					status.textContent = (missing.length
						? `Added ${found.length}. ${missing.length} not found: ${missing.slice(0, 3).map(entry => entry.title || 'a Spotify link').join(', ')}${missing.length > 3 ? '…' : ''}`
						: `Added all ${found.length} to “${name}”.`) + skipped;
				} catch (error) {
					status.textContent = commandError(error, 'That playlist could not be read.');
				}
				setBusy(false);
			});

			body.append(pasteField, nameField, fileRow, go, status);
			body.append(el('p', 'catalog-note', 'Private playlist? Make it public, or export a CSV at exportify.net.'));
		},
	});
}

function pickTextFile(onRead) {
	const input = document.createElement('input');
	input.type = 'file';
	input.accept = '.csv,.txt,.tsv,.m3u,.m3u8,text/csv,text/plain,audio/x-mpegurl';
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
