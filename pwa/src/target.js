/* Which device owns the audio, as this phone understands it.
 *
 * Every "playing on ..." string in the app used to be built from the computer's
 * hostname and a local boolean, which meant the phone announced the computer
 * even while playing through its own speaker. The computer is now authoritative
 * about ownership and says so in one event; this module is where that answer
 * lands and the only place the rest of the client should ask.
 */

import { state } from './state.js';

let onChange = () => {};

export function configureTarget(options = {}) {
	onChange = options.onChange || onChange;
}

/** Adopt a broadcast target, ignoring anything we have already moved past.
 *
 *  Ordering is not guaranteed: a reconnect drains a backlog, so an older record
 *  can arrive after a newer one. Without the revision gate the phone would
 *  settle on whichever happened to land last. */
export function absorbPlaybackTarget(message) {
	const revision = Number(message?.revision || 0);
	if (revision && revision <= Number(state.playbackTarget?.revision || 0)) return;
	state.playbackTarget = {
		owner_kind: String(message.owner_kind || 'desktop'),
		owner_device_id: String(message.owner_device_id || 'desktop'),
		owner_name: String(message.owner_name || ''),
		sink_name: String(message.sink_name || ''),
		revision,
	};
	onChange(state.playbackTarget);
}

/** True when this phone is the one making the sound. */
export function phoneOwnsPlayback() {
	const target = state.playbackTarget;
	// No answer yet means fall back to what this phone knows about itself,
	// rather than claiming a computer we have not heard from.
	if (!target) return !state.linked;
	return target.owner_kind === 'phone' && target.owner_device_id === state.deviceId;
}

/** The name of whatever is playing, for the one label everything renders. */
export function playbackOwnerName() {
	const target = state.playbackTarget;
	if (phoneOwnsPlayback()) return 'this phone';
	if (target?.owner_kind === 'phone') return target.owner_name || 'another phone';
	return target?.owner_name || state.computerName || 'your computer';
}

/** "Playing on X" — the single source of that sentence. */
export function playbackSourceLabel() {
	return `Playing on ${playbackOwnerName()}`;
}
