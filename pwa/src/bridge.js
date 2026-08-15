/* The one way this app talks to the computer.
 *
 * `app.js` owns the transport and installs it here at boot. The indirection
 * keeps the import graph acyclic: UI modules send commands, app.js renders UI.
 */

let sendCommand = async () => { throw new Error('Rainette is not connected to a computer.'); };
let resolveMedia = value => value;
let readEvents = async () => { throw new Error('Rainette is not connected to a computer.'); };

export function configureBridge(options) {
	sendCommand = options.command;
	resolveMedia = options.mediaUrl;
	readEvents = options.events;
}

/** Long-poll this device's event log. `follow` re-asserts linked mode. */
export function events({ after, wait, follow }) {
	return readEvents({ after, wait, follow });
}

/** Invoke one allow-listed music command on the computer. */
export function command(type, payload = {}, timeoutMs) {
	return sendCommand(type, payload, timeoutMs);
}

/* This page updates itself the moment it is deployed; the computer it talks to
 * updates only when somebody installs a new Rainette there. So a phone can be
 * ahead of its computer, and asking for something the computer's allow-list
 * predates comes back as a flat 400 reading "command type is not allowed".
 *
 * Shown raw that is a protocol string blaming the wrong thing. Every caller of
 * a command added after the first release routes its failures through here. */
export function commandError(error, fallback = 'Your computer could not answer that.') {
	const message = String(error?.message || '');
	if (error?.status === 400 && /not allowed/i.test(message)) {
		return 'Your computer is running an older Rainette that does not offer this yet. Update it there, then try again.';
	}
	return message || fallback;
}

/** True when the computer refused a command because its build predates it. */
export function isUnsupportedCommand(error) {
	return error?.status === 400 && /not allowed/i.test(String(error?.message || ''));
}

/** Turn a relay path from the computer into an absolute, fetchable URL. */
export function mediaUrl(value) {
	return resolveMedia(value);
}
