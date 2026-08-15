/* Music that lives on the phone.
 *
 * Files picked here never leave the device. They are held in IndexedDB on this
 * phone, played from a blob URL, and no part of this module talks to the
 * companion, to Vercel, or to anything else on a network. That is the whole
 * point of the feature and it is worth being categorical about: there is no
 * upload path in this file.
 *
 * Tags are read out of the file itself — ID3v2 for MP3, the iTunes-style atoms
 * for M4A — so a track arrives with its title, artist, album and cover art
 * rather than as a filename. Files with no tags at all fall back to the name,
 * which is usually "Artist - Title" and is parsed as such.
 */

import { toast } from './dom.js';

const DB_NAME = 'rainette-local';
const DB_VERSION = 1;
const STORE = 'tracks';

/* A local track is a normal track everywhere else in the app, so it carries the
 * same fields. `source: 'local'` is what tells the player to skip the computer
 * and read the blob instead. */
export const LOCAL_SOURCE = 'local';

export function isLocalTrack(track) {
	return track?.source === LOCAL_SOURCE;
}

/* ── The store ────────────────────────────────────────────────────────────*/

let dbPromise = null;

function openDatabase() {
	if (dbPromise) return dbPromise;
	dbPromise = new Promise((resolve, reject) => {
		if (!window.indexedDB) { reject(new Error('This browser cannot store files offline.')); return; }
		const request = indexedDB.open(DB_NAME, DB_VERSION);
		request.onupgradeneeded = () => {
			const db = request.result;
			if (!db.objectStoreNames.contains(STORE)) {
				db.createObjectStore(STORE, { keyPath: 'id' });
			}
		};
		request.onsuccess = () => resolve(request.result);
		request.onerror = () => reject(request.error || new Error('Could not open local storage.'));
	});
	return dbPromise;
}

function transact(mode, run) {
	return openDatabase().then(db => new Promise((resolve, reject) => {
		const tx = db.transaction(STORE, mode);
		const store = tx.objectStore(STORE);
		let result;
		try { result = run(store); } catch (error) { reject(error); return; }
		tx.oncomplete = () => resolve(result?.result !== undefined ? result.result : result);
		tx.onerror = () => reject(tx.error || new Error('Local storage failed.'));
		tx.onabort = () => reject(tx.error || new Error('Local storage was interrupted.'));
	}));
}

/* ── Reading and writing ──────────────────────────────────────────────────*/

/** Every local track, newest import first. Blobs are not loaded here — only the
 *  metadata rows, so a library of hundreds does not pull hundreds of megabytes
 *  into memory to draw a list. */
export async function listLocalTracks() {
	try {
		const rows = await transact('readonly', store => store.getAll());
		return (rows || [])
			.map(toTrack)
			.sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
	} catch {
		return [];
	}
}

export async function localTrackCount() {
	try {
		return await transact('readonly', store => store.count());
	} catch {
		return 0;
	}
}

/** Total bytes held on the phone, for a settings row that can answer "how much
 *  of my storage is this using?" without guessing. */
export async function localBytes() {
	try {
		const rows = await transact('readonly', store => store.getAll());
		return (rows || []).reduce((sum, row) => sum + (Number(row.size) || 0), 0);
	} catch {
		return 0;
	}
}

export function formatBytes(bytes) {
	if (!bytes) return '0 MB';
	const mb = bytes / (1024 * 1024);
	if (mb < 1) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
	if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
	return `${(mb / 1024).toFixed(1)} GB`;
}

async function readRow(id) {
	return transact('readonly', store => store.get(id));
}

export async function removeLocalTrack(id) {
	await transact('readwrite', store => store.delete(id));
	releaseUrl(id);
}

export async function clearLocalTracks() {
	await transact('readwrite', store => store.clear());
	for (const id of [...urls.keys()]) releaseUrl(id);
}

/* ── Blob URLs ────────────────────────────────────────────────────────────
 * Minted on demand and kept, because revoking one while the element still holds
 * it stops playback mid-track. They cost nothing until played and are released
 * when the track is deleted. */

const urls = new Map();

function releaseUrl(id) {
	const url = urls.get(id);
	if (!url) return;
	URL.revokeObjectURL(url);
	urls.delete(id);
}

/** A playable URL for a local track. */
export async function localStreamUrl(track) {
	const id = String(track?.source_id || '');
	if (!id) throw new Error('This local track is missing its file.');
	const existing = urls.get(id);
	if (existing) return existing;
	const row = await readRow(id);
	if (!row?.blob) throw new Error('That file is no longer on this phone.');
	const url = URL.createObjectURL(row.blob);
	urls.set(id, url);
	return url;
}

/* ── Importing ────────────────────────────────────────────────────────────*/

const AUDIO_PATTERN = /\.(mp3|m4a|aac|flac|wav|ogg|oga|opus|weba|webm|aiff?|alac)$/i;

function looksPlayable(file) {
	return (file.type || '').startsWith('audio/') || AUDIO_PATTERN.test(file.name || '');
}

/** Add files the user picked. Returns how many landed. `onProgress(done, total)`
 *  is called as each one is read, because tagging a hundred files is slow enough
 *  to need a progress line. */
export async function importFiles(fileList, onProgress) {
	const files = [...fileList].filter(looksPlayable);
	if (!files.length) return { added: 0, skipped: 0 };

	let added = 0;
	let skipped = 0;
	for (const [index, file] of files.entries()) {
		try {
			const row = await buildRow(file);
			await transact('readwrite', store => store.put(row));
			added += 1;
		} catch {
			// One unreadable file must not abandon the rest of the import.
			skipped += 1;
		}
		onProgress?.(index + 1, files.length);
	}
	return { added, skipped };
}

/* An id that is stable for the same file picked twice, so re-importing a folder
 * updates rows instead of duplicating the library. Name, size and modified time
 * together are enough; hashing the bytes would mean reading every file twice. */
function fileId(file) {
	const stamp = `${file.name}|${file.size}|${file.lastModified || 0}`;
	let hash = 5381;
	for (let i = 0; i < stamp.length; i += 1) hash = ((hash * 33) ^ stamp.charCodeAt(i)) >>> 0;
	return `local-${hash.toString(36)}-${file.size.toString(36)}`;
}

async function buildRow(file) {
	const tags = await readTags(file).catch(() => ({}));
	const fallback = parseFilename(file.name);
	return {
		id: fileId(file),
		blob: file,
		size: file.size,
		added_at: Date.now(),
		title: tags.title || fallback.title,
		artist: tags.artist || fallback.artist,
		album: tags.album || '',
		duration_s: 0,
		artwork: tags.artwork || null,   // a Blob, stored as-is
		file_name: file.name,
	};
}

/* "01 - Artist - Title.mp3" and "Artist - Title.mp3" are the two shapes that
 * actually turn up. Anything else keeps the whole name as the title, which is
 * at least honest. */
function parseFilename(name) {
	const bare = String(name || 'Unknown').replace(/\.[^.]+$/, '').replace(/^\d+\s*[-._]\s*/, '');
	const parts = bare.split(/\s+-\s+/);
	if (parts.length >= 2) {
		return { artist: parts[0].trim(), title: parts.slice(1).join(' - ').trim() };
	}
	return { artist: '', title: bare.trim() || 'Untitled' };
}

/* ── Tags ─────────────────────────────────────────────────────────────────
 * Only the header is read, never the whole file: an ID3v2 tag declares its own
 * length in the first ten bytes, so a 12 MB track costs a few hundred kilobytes
 * to identify. */

const TAG_PROBE_BYTES = 1024 * 1024;

async function readTags(file) {
	const head = new Uint8Array(await file.slice(0, TAG_PROBE_BYTES).arrayBuffer());
	if (head[0] === 0x49 && head[1] === 0x44 && head[2] === 0x33) return readId3(head);
	// M4A/MP4 keeps its tags in a 'moov' atom that is often at the end of the
	// file, so the tail is worth a look before giving up.
	const tail = new Uint8Array(await file.slice(Math.max(0, file.size - TAG_PROBE_BYTES)).arrayBuffer());
	return readMp4(head) || readMp4(tail) || {};
}

const ID3_FRAMES = { TIT2: 'title', TPE1: 'artist', TALB: 'album' };

function readId3(bytes) {
	// Seven bits per byte: the high bit is reserved so a length can never
	// contain a byte that looks like the start of a frame sync.
	const size = ((bytes[6] & 0x7f) << 21) | ((bytes[7] & 0x7f) << 14) | ((bytes[8] & 0x7f) << 7) | (bytes[9] & 0x7f);
	const end = Math.min(bytes.length, 10 + size);
	const major = bytes[3];
	const headerSize = major >= 3 ? 10 : 6;
	const tags = {};

	let at = 10;
	while (at + headerSize <= end) {
		const id = String.fromCharCode(...bytes.slice(at, at + (major >= 3 ? 4 : 3)));
		if (!/^[A-Z0-9]{3,4}$/.test(id)) break;
		const frameSize = major >= 4
			? ((bytes[at + 4] & 0x7f) << 21) | ((bytes[at + 5] & 0x7f) << 14) | ((bytes[at + 6] & 0x7f) << 7) | (bytes[at + 7] & 0x7f)
			: major === 3
				? (bytes[at + 4] << 24) | (bytes[at + 5] << 16) | (bytes[at + 6] << 8) | bytes[at + 7]
				: (bytes[at + 3] << 16) | (bytes[at + 4] << 8) | bytes[at + 5];
		const body = bytes.slice(at + headerSize, at + headerSize + frameSize);
		if (frameSize <= 0 || body.length === 0) break;

		const field = ID3_FRAMES[id];
		if (field) tags[field] = decodeText(body);
		else if (id === 'APIC' || id === 'PIC') tags.artwork = decodePicture(body, id);

		at += headerSize + frameSize;
	}
	return tags;
}

/* The first byte of a text frame names its encoding, and the rest is the string
 * in it. UTF-16 carries a byte-order mark that TextDecoder handles for us. */
function decodeText(body) {
	const encoding = body[0];
	const raw = body.slice(1);
	const label = encoding === 1 ? 'utf-16' : encoding === 2 ? 'utf-16be' : encoding === 3 ? 'utf-8' : 'iso-8859-1';
	try {
		return new TextDecoder(label).decode(raw).replace(/\0+$/, '').trim();
	} catch {
		return '';
	}
}

function decodePicture(body, id) {
	const encoding = body[0];
	let at = 1;
	let mime = 'image/jpeg';
	if (id === 'APIC') {
		let end = at;
		while (end < body.length && body[end] !== 0) end += 1;
		mime = String.fromCharCode(...body.slice(at, end)) || mime;
		at = end + 1;
	} else {
		at += 3;   // v2.2 uses a three-character format code instead of a MIME type
	}
	at += 1;       // picture type
	// The description that follows is terminated the same way the text is
	// encoded, so a UTF-16 description ends on a double null.
	const wide = encoding === 1 || encoding === 2;
	while (at < body.length) {
		if (body[at] === 0 && (!wide || body[at + 1] === 0)) { at += wide ? 2 : 1; break; }
		at += wide ? 2 : 1;
	}
	const data = body.slice(at);
	return data.length > 64 ? new Blob([data], { type: mime }) : null;
}

/* MP4 atoms: a length, a four-character name, then either children or a payload.
 * Only the handful under 'ilst' are interesting here. */
const MP4_FIELDS = { '\xa9nam': 'title', '\xa9ART': 'artist', '\xa9alb': 'album' };

function readMp4(bytes) {
	const text = latin1(bytes);
	const listAt = text.indexOf('ilst');
	if (listAt < 0) return null;

	const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
	const tags = {};
	let at = listAt + 4;

	while (at + 8 <= bytes.length) {
		const size = view.getUint32(at);
		if (size < 8 || at + size > bytes.length) break;
		const name = text.slice(at + 4, at + 8);
		// Inside each entry is a 'data' atom: 8 bytes of header, 8 of type and
		// locale, then the value.
		const body = bytes.slice(at + 16 + 8, at + size);
		const field = MP4_FIELDS[name];
		if (field && body.length) {
			try { tags[field] = new TextDecoder('utf-8').decode(body).replace(/\0+$/, '').trim(); } catch { /* skip */ }
		} else if (name === 'covr' && body.length > 64) {
			tags.artwork = new Blob([body], { type: 'image/jpeg' });
		}
		at += size;
	}
	return Object.keys(tags).length ? tags : null;
}

function latin1(bytes) {
	let out = '';
	// Chunked, because spreading a megabyte into String.fromCharCode overflows
	// the argument limit on every engine.
	for (let at = 0; at < bytes.length; at += 8192) {
		out += String.fromCharCode(...bytes.subarray(at, at + 8192));
	}
	return out;
}

/* ── Artwork ──────────────────────────────────────────────────────────────
 * Cover art is stored as a blob and turned into a URL the first time a row
 * asks for it, on the same keep-it-alive terms as the audio. */

const artUrls = new Map();

export function localArtworkUrl(track) {
	const id = String(track?.source_id || '');
	if (!id) return './icon.svg';
	const known = artUrls.get(id);
	if (known !== undefined) return known || './icon.svg';
	// Nothing is known yet: claim the slot so a list of fifty rows does not
	// launch fifty identical reads, then fill it in when the row comes back.
	artUrls.set(id, '');
	readRow(id).then(row => {
		if (!row?.artwork) return;
		artUrls.set(id, URL.createObjectURL(row.artwork));
		// Rows already on screen were drawn with the placeholder, so they are
		// told directly rather than waiting for the next render.
		for (const image of document.querySelectorAll(`img[data-local-art="${CSS.escape(id)}"]`)) {
			image.src = artUrls.get(id);
		}
	}).catch(() => {});
	return './icon.svg';
}

/* ── Shape ────────────────────────────────────────────────────────────────*/

function toTrack(row) {
	return {
		source: LOCAL_SOURCE,
		source_id: row.id,
		title: row.title || 'Untitled',
		artist: row.artist || '',
		album: row.album || '',
		duration_s: row.duration_s || 0,
		thumbnail_url: '',
		added_at: row.added_at || 0,
		size: row.size || 0,
		file_name: row.file_name || '',
		metadata: {
			source_detail: 'local',
			local: true,
			artists: row.artist ? [{ name: row.artist, id: '' }] : [],
			album: row.album ? { name: row.album, id: '' } : {},
			album_name: row.album || '',
		},
	};
}

/** Ask for files and import them. Returns the same summary `importFiles` does. */
export function pickFiles({ directory = false } = {}) {
	return new Promise(resolve => {
		const input = document.createElement('input');
		input.type = 'file';
		input.accept = 'audio/*,.mp3,.m4a,.flac,.wav,.ogg,.opus,.aac';
		input.multiple = true;
		if (directory) {
			// Not in the spec, but every browser that supports folder picking
			// spells it this way. Ignored where it is not supported, which leaves
			// an ordinary multi-file picker.
			input.webkitdirectory = true;
		}
		input.style.display = 'none';
		document.body.append(input);

		let settled = false;
		const finish = value => {
			if (settled) return;
			settled = true;
			input.remove();
			resolve(value);
		};

		input.addEventListener('change', async () => {
			if (!input.files?.length) { finish({ added: 0, skipped: 0 }); return; }
			const total = input.files.length;
			toast(`Reading ${total} file${total === 1 ? '' : 's'}…`, { icon: 'listAdd' });
			finish(await importFiles(input.files));
		});
		// A cancelled picker fires nothing at all on most browsers, so the
		// promise would hang. Focus returning to the page is the only signal.
		window.addEventListener('focus', () => setTimeout(() => {
			if (!input.files?.length) finish({ added: 0, skipped: 0 });
		}, 600), { once: true });

		input.click();
	});
}
