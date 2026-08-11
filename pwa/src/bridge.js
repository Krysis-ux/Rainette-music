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

/** Turn a relay path from the computer into an absolute, fetchable URL. */
export function mediaUrl(value) {
	return resolveMedia(value);
}
