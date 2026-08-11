/**
 * Shared three-state repeat: off -> loop the queue -> repeat this song.
 *
 * The two playback engines own separate queues but must agree on modes, cycle
 * order and labels, or the same button means different things on each surface.
 *
 * Broadcasts carry `repeat` plus a derived boolean `loop` for older consumers.
 * bool("off") is True, so `repeat` must never go through that boolean field.
 */

export const REPEAT_MODES = ['off', 'all', 'one'];

export const REPEAT_LABEL = {
	off: 'Loop off — click to loop the queue',
	all: 'Looping the queue — click to repeat this song',
	one: 'Repeating this song — click to stop looping',
};

export function normalizeRepeat(mode, fallback = 'off') {
	return REPEAT_MODES.includes(mode) ? mode : fallback;
}

export function nextRepeat(mode) {
	return REPEAT_MODES[(REPEAT_MODES.indexOf(normalizeRepeat(mode)) + 1) % REPEAT_MODES.length];
}

/** The boolean `loop` field kept on the wire for older consumers. */
export function loopFlagFor(mode) {
	return normalizeRepeat(mode) !== 'off';
}

/** Read a repeat mode off a broadcast that may only carry the legacy boolean. */
export function repeatFromMessage(message, fallback = 'off') {
	if (REPEAT_MODES.includes(message?.repeat)) return message.repeat;
	if (typeof message?.loop === 'boolean') return message.loop ? 'all' : 'off';
	return fallback;
}
