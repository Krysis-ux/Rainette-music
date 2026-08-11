/* The one way this app talks to the computer.
 *
 * `app.js` owns the transport — the endpoint, the credential, and the careful
 * diagnosis of what a bare "Failed to fetch" actually meant — and installs it
 * here at boot. Everything else imports `command` from this module.
 *
 * The indirection exists to keep the import graph acyclic: the UI modules need
 * to send commands, `app.js` needs to render UI, and without a leaf like this
 * one in between, those two facts form a cycle.
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
