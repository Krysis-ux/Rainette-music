/**
 * Shared three-state repeat model: off -> loop the queue -> repeat this song.
 *
 * There are two playback engines and each owns its own queue: miniplayer.js in
 * the detached player window (the packaged app), and rainette_music_player.js's
 * in-page bubble (the browser / Edge fallback, which launches without ?remote=1).
 * They can't share state, but the modes, cycle order, labels, and the migration
 * off the old boolean key have to stay identical or the two surfaces disagree
 * about what the same button means.
 *
 * Wire format: broadcasts carry `repeat` (this string) plus a derived boolean
 * `loop` for consumers predating three-state repeat -- including the Python relay,
 * which coerces that field with bool(). Note bool("off") is True, so `repeat` must
 * never be sent through the old boolean field.
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
