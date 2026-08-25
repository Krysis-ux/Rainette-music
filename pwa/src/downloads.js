/* Keeping a track on this phone.
 *
 * A download is a fetch of the same relay URL playback already uses, held as a
 * blob instead of streamed into an <audio> element. The bytes are passed
 * through exactly as the computer sent them — no decode, no re-encode, no
 * transcode. That is a deliberate choice and worth being plain about:
 *
 * The catalog resolves to M4A (AAC) because that is what `music_bridge`'s
 * format ladder asks YouTube for. Turning that into MP3 would mean decoding to
 * PCM and encoding again, which costs a second generation of lossy damage, tens
 * of seconds per track on a phone, and a great deal of memory — for a container
 * change. Every device this app runs on plays M4A natively, `local.js` already
 * reads its iTunes-style tag atoms, and `local_library.py` already counts `.m4a`
 * as music. So the file is saved as it arrived.
 *
 * Metadata does *not* come from the file. It comes from the catalog row, which
 * knows the real artist, album and cover art — a YouTube M4A often carries none
 * of those, and a library full of "Unknown artist" is the failure this avoids.
 *
 * Direction matters: bytes travel computer to phone and stop here. Nothing in
 * this module uploads, which keeps `local.js`'s categorical claim about the
 * local library true.
 */

import { command, mediaUrl, commandError } from './bridge.js';
import { trackKey, artworkUrl, artistName, trackDuration } from './state.js';
import {
	isLocalTrack, saveDownloadedTrack, downloadedId, localTrackIds, removeLocalTrack, localRow,
} from './local.js';

/* How long to let the computer resolve one stream. The same 50s `player.js`
 * allows: it is a yt-dlp call on the far end, and a cold one is genuinely slow.
 */
const RESOLVE_TIMEOUT_MS = 50000;

/* Content types the relay actually returns, and what a file of each should be
 * called. Anything unrecognised keeps `.m4a`, which is what the format ladder
 * asks for and therefore the overwhelmingly likely answer. */
const EXTENSIONS = {
	'audio/mp4': 'm4a',
	'audio/x-m4a': 'm4a',
	'audio/aac': 'aac',
	'audio/mpeg': 'mp3',
	'audio/webm': 'weba',
	'audio/ogg': 'ogg',
	'audio/opus': 'opus',
	'audio/flac': 'flac',
	'audio/wav': 'wav',
};

/* ── Which tracks are already here ────────────────────────────────────────
 * Lists ask this once per row while drawing, so it has to be answerable without
 * a promise. The set is read once at boot and kept current by the writes below,
 * rather than re-queried per row — four hundred rows must not cost four hundred
 * IndexedDB round trips. */

let present = new Set();
let loaded = false;
const listeners = new Set();

/** Re-read which rows exist. Cheap: keys only, no blobs. */
export async function refreshDownloaded() {
	present = await localTrackIds();
	loaded = true;
	announce();
	return present;
}

function announce() {
	for (const listener of [...listeners]) {
		try { listener(); } catch { /* one bad listener must not strand the rest */ }
	}
}

/** Subscribe to "the set of downloaded tracks changed". Returns an unsubscribe. */
export function onDownloadsChanged(listener) {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

/** Is this catalog track already on the phone? Synchronous by design. */
export function isDownloaded(track) {
	if (!track) return false;
	// A file imported from this phone is already local in the stronger sense;
	// offering to download it would be nonsense.
	if (isLocalTrack(track)) return true;
	if (!loaded) return false;
	return present.has(downloadedId(trackKey(track)));
}

/* ── One track ────────────────────────────────────────────────────────────*/

/* Nothing is downloaded twice at once. Tapping the button on a row and then on
 * the same track's player card is one download, and both surfaces watch it. */
const inFlight = new Map();

/** Is a download for this track running right now? */
export function isDownloading(track) {
	return track ? inFlight.has(trackKey(track)) : false;
}

/**
 * Fetch one track onto this phone.
 *
 * `onProgress({ received, total, ratio })` fires as bytes arrive; `total` is 0
 * when the relay sent no Content-Length, in which case `ratio` stays 0 and the
 * caller should show an indeterminate state rather than a lying bar.
 *
 * Resolves to the stored row. Rejects with a message worth showing a person.
 */
export function downloadTrack(track, { onProgress, signal } = {}) {
	const key = trackKey(track);
	const running = inFlight.get(key);
	if (running) return running.promise;

	const promise = runDownload(track, { onProgress, signal })
		.finally(() => { inFlight.delete(key); announce(); });
	inFlight.set(key, { promise });
	announce();
	return promise;
}

async function runDownload(track, { onProgress, signal }) {
	if (isLocalTrack(track)) throw new Error('That track is already on this phone.');

	/* Say that something is happening before anything is measurable.
	 *
	 * `resolveUrl` is a yt-dlp call on the computer and is allowed 50 seconds;
	 * the fetch that follows has its own wait before the first byte. Until then
	 * there is no ratio to report, and reporting nothing left the bar sitting
	 * at a dead 0% -- which is what "stuck at 0% all day" actually looks like,
	 * whether or not anything is wrong. A phase costs nothing and is the
	 * difference between "working" and "frozen". */
	onProgress?.({ phase: 'preparing', received: 0, total: 0, ratio: 0 });
	const url = await resolveUrl(track);
	onProgress?.({ phase: 'connecting', received: 0, total: 0, ratio: 0 });
	const response = await fetch(url, { signal, cache: 'no-store' });
	if (!response.ok) {
		// The relay answers a failed upstream with a JSON error and a real
		// status, so there is something specific to say rather than "failed".
		throw new Error(await relayError(response));
	}

	const blob = await readWithProgress(response, onProgress, signal);
	if (!blob.size) throw new Error('That download arrived empty.');

	const type = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
	const extension = EXTENSIONS[type] || 'm4a';
	const artwork = await fetchArtwork(track, signal);

	const row = await saveDownloadedTrack({
		catalogKey: trackKey(track),
		blob,
		title: track.title || 'Untitled',
		artist: artistName(track),
		album: albumTitle(track),
		artwork,
		duration_s: trackDuration(track),
		file_name: `${safeName(track)}.${extension}`,
	});

	present.add(row.id);
	loaded = true;
	return row;
}

async function resolveUrl(track) {
	let result;
	try {
		result = await command('music_stream_url', {
			source_id: track.source_id,
			track,
			// A download is not a listen. Without this the computer logs a play
			// for every track downloaded, and "recently played" fills with songs
			// nobody heard — the same reason `player.js` marks its prefetches.
			prefetch: true,
		}, RESOLVE_TIMEOUT_MS);
	} catch (error) {
		throw new Error(commandError(error, 'Your computer could not find that track.'));
	}
	if (!result?.url) throw new Error('Your computer did not return an audio stream.');
	return mediaUrl(result.url);
}

async function relayError(response) {
	try {
		const body = await response.json();
		if (body?.error) return String(body.error);
	} catch { /* not JSON; fall through to the status */ }
	if (response.status === 404) return 'That link expired. Play the track once, then download it.';
	return `Your computer could not send that track (${response.status}).`;
}

/* Read the body in chunks so a 4 MB download can show movement. `response.blob()`
 * would be one line, but it reports nothing until it is finished, and a
 * progress bar that only ever shows 0% and then 100% is not a progress bar. */
async function readWithProgress(response, onProgress, signal) {
	const total = Number(response.headers.get('Content-Length')) || 0;
	if (!response.body?.getReader) return response.blob();   // no streams: still correct, just silent

	const reader = response.body.getReader();
	const chunks = [];
	let received = 0;
	try {
		for (;;) {
			const { done, value } = await reader.read();
			if (done) break;
			chunks.push(value);
			received += value.length;
			// `total` is 0 when the response carries no Content-Length. The
			// ratio is then meaningless, so it is reported as null rather than
			// as 0 -- a caller can tell "no progress yet" from "cannot know",
			// and show movement instead of a bar frozen at zero.
			onProgress?.({
				phase: 'downloading',
				received,
				total,
				ratio: total ? received / total : null,
			});
		}
	} catch (error) {
		try { await reader.cancel(); } catch { /* already gone */ }
		throw signal?.aborted ? new Error('Download stopped.') : error;
	}
	return new Blob(chunks, { type: response.headers.get('Content-Type') || 'audio/mp4' });
}

/* Cover art is fetched separately and stored as a blob, so the local library
 * shows the real artwork offline rather than a URL that needs the network it
 * was downloaded to survive without. A failure here is not a failed download —
 * a track with no cover is still a track. */
async function fetchArtwork(track, signal) {
	const url = artworkUrl(track);
	if (!url || url.startsWith('./')) return null;
	try {
		const response = await fetch(url, { signal, referrerPolicy: 'no-referrer' });
		if (!response.ok) return null;
		const blob = await response.blob();
		return blob.size ? blob : null;
	} catch {
		return null;
	}
}

function albumTitle(track) {
	return track?.album || track?.metadata?.album_name || track?.metadata?.album?.name || '';
}

/* A filename a file system will accept, on every platform this can land on.
 * Windows is the strictest of them and is what the character class is drawn
 * from; the length cap is there because a very long name is refused outright on
 * some of them. */
function safeName(track) {
	const artist = artistName(track);
	const title = track?.title || 'Untitled';
	const stem = artist ? `${artist} - ${title}` : title;
	return stem.replace(/[<>:"/\\|?*]/g, '').replace(/\s+/g, ' ').trim().slice(0, 120) || 'track';
}

/* ── Many tracks ──────────────────────────────────────────────────────────*/

/**
 * Download a whole list, one at a time.
 *
 * Sequential on purpose. Each track costs the computer a yt-dlp resolve, and
 * firing thirty of those at once is how a phone turns a playlist download into
 * a stalled computer and a fistful of timeouts.
 *
 * `onProgress({ done, total, track, ratio, failed })` fires per track and as
 * bytes arrive within one. Already-downloaded tracks are skipped, not refetched,
 * so running this twice on a playlist finishes the part that did not land rather
 * than paying for all of it again.
 *
 * Never rejects on one bad track: it returns a tally. A playlist where two songs
 * are unavailable should still put the other twenty-eight on the phone.
 */
export async function downloadTracks(tracks, { onProgress, signal } = {}) {
	const wanted = [...tracks].filter(track => track && !isDownloaded(track));
	const total = wanted.length;
	let done = 0;
	let failed = 0;
	const errors = [];

	for (const track of wanted) {
		if (signal?.aborted) return { total, done, failed, cancelled: true, errors };
		onProgress?.({ done, total, track, ratio: 0, failed, phase: 'preparing' });
		try {
			await downloadTrack(track, {
				signal,
				onProgress: ({ ratio, phase, received, total: bytes }) =>
					onProgress?.({ done, total, track, ratio, failed, phase, received, bytes }),
			});
			done += 1;
		} catch (error) {
			if (signal?.aborted) return { total, done, failed, cancelled: true, errors };
			failed += 1;
			errors.push({ track, message: String(error?.message || error) });
		}
		onProgress?.({ done, total, track, ratio: 1, failed, phase: 'done' });
	}
	await refreshDownloaded();
	return { total, done, failed, cancelled: false, errors };
}

/** Forget a downloaded track. The catalog row it came from is untouched — this
 *  removes the copy, not the song. */
export async function removeDownload(track) {
	const id = isLocalTrack(track) ? String(track.source_id) : downloadedId(trackKey(track));
	await removeLocalTrack(id);
	present.delete(id);
	announce();
}

/* ── Saving a copy off the phone ──────────────────────────────────────────*/

/** Can this browser ask where to put a file? Chromium desktop can; Safari and
 *  Android Chrome cannot, and get the ordinary download instead. */
export function canChooseLocation() {
	return typeof window.showSaveFilePicker === 'function';
}

/**
 * Hand a downloaded track to the phone's own file system.
 *
 * Where possible this opens a real save dialog, so the file goes where the
 * person says. Where it is not — iOS has no such API — it becomes an ordinary
 * browser download, which lands in Downloads and is then theirs to move. Both
 * paths are a copy: the track stays in the local library either way, because
 * that is what makes it play offline.
 *
 * Returns 'saved' | 'downloaded' | 'cancelled'.
 */
export async function saveCopy(track) {
	// A track, not a row: every caller has one of those and none of them should
	// have to know this module stores rows. Downloading it first when it is not
	// here yet is what makes "save a copy" work as a single action.
	const id = isLocalTrack(track) ? String(track.source_id) : downloadedId(trackKey(track));
	let row = await localRow(id);
	if (!row) row = await downloadTrack(track);
	const blob = row?.blob;
	if (!blob) throw new Error('That track is no longer on this phone.');
	const name = row.file_name || 'track.m4a';

	if (canChooseLocation()) {
		let handle;
		try {
			handle = await window.showSaveFilePicker({
				suggestedName: name,
				types: [{
					description: 'Audio',
					accept: { [blob.type || 'audio/mp4']: [`.${name.split('.').pop()}`] },
				}],
			});
		} catch (error) {
			// The picker rejects with AbortError when it is dismissed, which is
			// a choice rather than a failure and must not surface as an error.
			if (error?.name === 'AbortError') return 'cancelled';
			throw error;
		}
		const stream = await handle.createWritable();
		await stream.write(blob);
		await stream.close();
		return 'saved';
	}

	const url = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = url;
	link.download = name;
	document.body.append(link);
	link.click();
	link.remove();
	// Revoking immediately can cancel the download on some browsers — the same
	// hazard `backup.js` documents, and the same delay.
	setTimeout(() => URL.revokeObjectURL(url), 60000);
	return 'downloaded';
}
