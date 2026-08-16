/* What this phone can actually decode, told to the computer once per session.
 *
 * iOS Safari plays FLAC in an <audio> element but not Ogg or Opus. When the
 * computer offers a file it cannot decode, the element fails with an error that
 * carries no reason, and the phone reported it as "the audio stream expired" —
 * which is not merely unhelpful, it points at the wrong subsystem entirely and
 * sends you looking at your network.
 *
 * The computer cannot guess this: the same browser engine answers differently
 * across OS versions, and Android and iOS disagree. So the phone measures it
 * and says so, and the library can mark what will not play here before you tap
 * it rather than after.
 */

import { command } from './bridge.js';

/* Asked in the form `canPlayType` expects. The keys are what the computer
 * stores against this device, so they are container-and-codec pairs rather than
 * file extensions — "ogg" alone is not a decodable claim. */
const PROBES = {
	mp3: 'audio/mpeg',
	aac: 'audio/mp4; codecs="mp4a.40.2"',
	alac: 'audio/mp4; codecs="alac"',
	flac: 'audio/flac',
	ogg_vorbis: 'audio/ogg; codecs="vorbis"',
	opus: 'audio/ogg; codecs="opus"',
	webm_opus: 'audio/webm; codecs="opus"',
	wav: 'audio/wav',
	aiff: 'audio/aiff',
};

let probed = null;

/** Measure once. The answer cannot change without a browser restart. */
export function localCodecSupport() {
	if (probed) return probed;
	const probe = document.createElement('audio');
	const support = {};
	for (const [name, type] of Object.entries(PROBES)) {
		// canPlayType answers "probably" | "maybe" | "". Treat "maybe" as yes:
		// it means the engine has the decoder but will not commit without the
		// bytes, and refusing on that would hide files that play perfectly.
		let answer = '';
		try { answer = probe.canPlayType(type) || ''; } catch { answer = ''; }
		support[name] = answer !== '';
	}
	probed = support;
	return support;
}

/** Tell the computer, so it can mark what this phone cannot play.
 *
 *  Fire-and-forget. An older computer does not know the command and refuses it,
 *  which costs nothing: without the marks the library simply behaves as it did
 *  before, offering everything and failing on the ones it should have greyed. */
export function reportCodecSupport() {
	return command('music_client_capabilities', { can_play: localCodecSupport() })
		.catch(() => { /* older desktop, or offline */ });
}
