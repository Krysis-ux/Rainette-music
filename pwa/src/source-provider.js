/* Where a track's audio comes from, and which engine plays it.
 *
 * Three sources, decided per track:
 *   - a file on this phone (imported or downloaded): an IndexedDB blob, played
 *     by the <audio> element and never by anything that would upload it;
 *   - a streamed song: resolved on this phone when the native app is present,
 *     otherwise through the paired computer's relay;
 *   - something only the computer has: always through the computer.
 *
 * And one engine per queue: the native app's media service when it can stream
 * every track in the queue itself, the <audio> element otherwise. Never both —
 * player.js silences one before the other starts. */

import { isLocalTrack } from './local.js';
import { nativeAvailable } from './native-engine.js';

/* What a YouTube video id looks like, which is what the computer's catalog uses
 * as `source_id` too, so a song keeps one identity whichever engine plays it. */
const CATALOG_ID = /^[A-Za-z0-9_-]{11}$/;

/** A song the phone can stream by itself, given the native app. */
export function isCatalogTrack(track) {
	return !isLocalTrack(track) && CATALOG_ID.test(String(track?.source_id || ''));
}

/** 'native' when the native service can stream every track in `queue`. */
export function engineFor(queue) {
	if (!nativeAvailable()) return 'element';
	return Array.isArray(queue) && queue.length > 0 && queue.every(isCatalogTrack) ? 'native' : 'element';
}

/** What this copy of Rainette can do without a computer. */
export function onDeviceCatalog() {
	return nativeAvailable();
}

/* What a failure means to the person listening. Keyed by the codes the native
 * side sends (StreamResolver / PlaybackService), so one place words them. */
const MESSAGES = {
	offline: 'No internet connection. Rainette keeps trying for a little while.',
	unavailable: 'This song is no longer available.',
	region: 'This song isn’t available in your country.',
	restricted: 'This song needs a signed-in account, so Rainette can’t play it.',
	rate_limited: 'The music source asked this phone to slow down. Try again in a minute.',
	unsupported: 'This phone can’t play this song’s format.',
	source_changed: 'This song couldn’t be read from the source. If every song fails, Rainette needs an update.',
	search_failed: 'Search didn’t answer. Check your connection and try again.',
	expired: 'The stream expired and couldn’t be renewed. Try playing it again.',
	invalid: 'That can’t be played here.',
	version_mismatch: 'This copy of Rainette is incomplete. Reinstall it.',
	bridge_unavailable: 'The phone player isn’t available.',
	service_failed: 'Playback stopped unexpectedly. Try again.',
};

export function describeError(error) {
	return MESSAGES[error?.code] || error?.message || MESSAGES.service_failed;
}

/** A request something newer replaced: never worth telling anyone about. */
export function isCanceled(error) {
	return error?.code === 'canceled';
}
