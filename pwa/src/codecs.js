/* What this phone can actually decode, told to the computer once per session. */

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

/** Tell the computer, so it can mark what this phone cannot play. */
export function reportCodecSupport() {
	return command('music_client_capabilities', { can_play: localCodecSupport() })
		.catch(() => { /* older desktop, or offline */ });
}
