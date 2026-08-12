/* One long-poll loop, and what it does with each event. A transfer is
 * acknowledged only once the track loads, because the desktop waits on that
 * before pausing itself. `reset_required` means the desktop restarted. */

import { state } from './state.js';
import { command, events } from './bridge.js';
import { adoptTransfer, notifyPlayerChanged } from './player.js';

let onStatus = () => {};
let onLibrary = () => {};
let onDesktopReset = () => {};
let onAuthLost = () => {};

export function configureSync(options) {
	onStatus = options.onStatus || onStatus;
	onLibrary = options.onLibrary || onLibrary;
	onDesktopReset = options.onDesktopReset || onDesktopReset;
	onAuthLost = options.onAuthLost || onAuthLost;
}

export function startEventLoop() {
	const loopId = ++state.eventLoopId;
	(async () => {
		while (state.connected && loopId === state.eventLoopId) {
			try {
				const result = await events({
					after: state.eventRevision,
					wait: 25,
					follow: state.linked,
				});
				state.eventRevision = Number(result.revision || state.eventRevision);
				if (result.reset_required) {
					state.eventRevision = Number(result.revision) || 0;
					onDesktopReset();
				}
				for (const event of result.events || []) handleEvent(event.message || {});
			} catch (error) {
				if (!state.connected || loopId !== state.eventLoopId) return;
				if (error?.status === 401 || error?.status === 403) {
					onAuthLost(error);
					return;
				}
				onStatus('', 'Reconnecting…');
				await new Promise(resolve => setTimeout(resolve, 1800));
			}
		}
	})();
}

export function stopEventLoop() {
	state.eventLoopId += 1;
}

function handleEvent(message) {
	if (!message || typeof message !== 'object') return;

	switch (message.type) {
		case 'music_output_transfer':
			acceptTransfer(message);
			return;

		case 'music_now_playing':
			absorbRemoteState(message);
			return;

		case 'music_progress':
			if (!state.linked || !state.remote) return;
			state.remote = {
				...state.remote,
				current_time: Number(message.current_time) || 0,
				duration: Number(message.duration) || state.remote.duration || 0,
				playing: !!message.playing,
			};
			notifyPlayerChanged();
			return;

		/* The desktop broadcasts a command's result to every paired device as well
		 * as answering the caller over HTTP, so this arrives for work this phone
		 * asked for. Render what it carries; re-issuing the command here would
		 * produce another result, and the two would chase each other forever. */
		case 'music_library_index_result':
			if (message.ok !== false && Array.isArray(message.tracks || message.items)) {
				state.library = message.tracks || message.items;
				onLibrary(state.library);
			}
			return;

		case 'music_playlist_list_result':
			if (Array.isArray(message.playlists || message.items)) {
				state.playlists = message.playlists || message.items;
			}
			return;

		default:
	}
}

/* The desktop's session, as this phone sees it while linked.
 *
 * Playback the phone itself caused comes back through the same channel, so
 * state that says it came from a phone output is ignored: adopting it would
 * make the phone a mirror of its own echo. */
function absorbRemoteState(message) {
	if (!state.linked) return;
	if (String(message.output_device_id || 'desktop') !== 'desktop') return;
	state.remote = {
		track: message.track || null,
		playing: !!message.playing,
		current_time: Number(message.current_time) || 0,
		duration: Number(message.duration) || 0,
		queue: Array.isArray(message.queue) ? message.queue : (state.remote?.queue || []),
		index: Number.isFinite(Number(message.index)) ? Number(message.index) : (state.remote?.index ?? -1),
	};
	if (message.repeat) state.repeat = message.repeat;
	notifyPlayerChanged();
}

async function acceptTransfer(message) {
	// The desktop addresses a transfer to one device id. The broker already
	// routed it here, but an explicit check keeps a future broadcast from being
	// grabbed by every paired phone at once.
	const target = String(message.target_device_id || '');
	if (target && state.deviceId && target !== state.deviceId) return;

	const reply = (ok, failure = '') => command('music_output_transfer_result', {
		id: message.id,
		ok,
		target_device_id: state.deviceId || target,
		source_device_id: String(message.source_device_id || 'desktop'),
		current_time: Number(message.current_time) || 0,
		...(failure ? { msg: failure } : {}),
	}).catch(() => { /* the desktop falls back to its own timeout */ });

	onStatus('busy', 'Taking over playback…');
	try {
		await adoptTransfer(message);
	} catch (error) {
		// Answering with the failure lets the desktop keep playing rather than
		// pausing into silence on the strength of a handoff that did not happen.
		await reply(false, error?.message || 'The phone could not load this track.');
		onStatus('online', state.computerName || 'Connected');
		return;
	}
	await reply(true);
	onStatus('online', state.computerName || 'Connected');
}
