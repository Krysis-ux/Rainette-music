/* The phone's playback engine: the one <audio>, the queue, the transport. Every
 * state change is reported to the computer, and in linked mode this drives the
 * computer's audio instead. A stream that expired re-resolves once. */

import { state, STORAGE, persist, trackKey, artworkUrl, artistName, nextRepeat, rememberRecent, trackDuration } from './state.js';
import { command, mediaUrl } from './bridge.js';

const audio = new Audio();
audio.preload = 'metadata';

let reportError = () => {};

const listeners = new Set();

export function configurePlayer(options = {}) {
	reportError = options.onError || reportError;
	audio.volume = Math.min(1, state.volume);
}

/** Subscribe to every playback state change. Returns an unsubscribe function. */
export function subscribe(listener) {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

function emit() {
	for (const listener of listeners) {
		try { listener(); } catch { /* one broken view must not stop the others */ }
	}
}

/* ── What is playing, wherever it is playing ───────────────────────────────
 * In linked mode the answers come from the desktop's last broadcast; otherwise
 * from this element. Views call these and stay unaware of the distinction. */

export function isLinked() {
	return state.linked && !!state.remote;
}

export function currentTrack() {
	return isLinked() ? (state.remote.track || null) : state.currentTrack;
}

export function isPlaying() {
	return isLinked() ? !!state.remote.playing : !audio.paused;
}

export function isLoading() {
	return !isLinked() && state.loading;
}

/* AVFoundation reads some YouTube m4a streams as exactly twice their real
 * length: 261.04s for a file AudioToolbox reads as 130.52s. It renders the
 * frames the file actually has and then plays silence for the remainder, so the
 * clock stays honest but the total is wrong and `ended` arrives minutes late.
 * iOS decodes through it, so the computer's duration_s is the authority. */
function isStretched() {
	const known = trackDuration(state.currentTrack);
	const raw = Number.isFinite(audio.duration) ? audio.duration : 0;
	return known > 0 && raw > known + Math.max(1, known * 0.05);
}

export function currentTime() {
	if (isLinked()) return Number(state.remote.current_time) || 0;
	return audio.currentTime || 0;
}

export function duration() {
	if (isLinked()) return Number(state.remote.duration) || 0;
	return trackDuration(state.currentTrack) || (Number.isFinite(audio.duration) ? audio.duration : 0);
}

export function activeQueue() {
	if (isLinked() && Array.isArray(state.remote.queue)) return state.remote.queue;
	return state.queue;
}

export function activeIndex() {
	if (isLinked() && Array.isArray(state.remote.queue)) return Number(state.remote.index) ?? -1;
	return state.queueIndex;
}

/* ── Reporting to the computer ─────────────────────────────────────────────
 * One-way notifications: the HTTP layer acknowledges them immediately and the
 * broker routes them back to this device's own session (and to the desktop's
 * windows), so a failure here is a missing update, never a stuck request. */

function publishNowPlaying(playbackState) {
	if (isLinked()) return;   // the desktop owns the session; do not talk over it
	command('music_now_playing_set', {
		track: state.currentTrack,
		state: playbackState,
		playing: !audio.paused,
		repeat: state.repeat,
		loop: state.repeat !== 'off',
		current_time: currentTime(),
		duration: duration(),
		queue: state.queue,
		index: state.queueIndex,
		queue_count: state.queue.length,
		output_device_id: 'phone',
	}).catch(() => { /* a dropped status tick is not worth surfacing */ });
}

let lastProgressAt = 0;

function publishProgress() {
	if (isLinked()) return;
	// One tick a second is enough for a remote progress bar and keeps a phone on
	// mobile data from spending its battery on the tunnel.
	const now = Date.now();
	if (now - lastProgressAt < 1000) return;
	lastProgressAt = now;
	command('music_progress', {
		current_time: currentTime(),
		duration: duration(),
		playing: !audio.paused,
		source_id: state.currentTrack?.source_id || '',
	}).catch(() => {});
}

/** Drive the desktop's transport when this phone is linked to it. */
function remoteControl(action, payload = {}) {
	command('music_remote_control', { action, ...payload }).catch(() => {});
}

/* ── Playback ──────────────────────────────────────────────────────────────*/

export async function playTrack(track, queue = [track], index = 0, options = {}) {
	if (!track?.source_id) throw new Error('This track has no playable source.');

	// Playing something from the phone takes ownership of the session back from
	// the desktop, which is the only sane reading of "I pressed play here".
	state.remote = null;
	state.queue = queue.slice();
	state.queueIndex = Math.max(0, Math.min(index, state.queue.length - 1));
	state.currentTrack = track;
	state.loading = true;
	endGuardKey = '';
	state.streamRefreshAttempted = !!options.forceRefresh;
	emit();

	// `loading` drives a spinner in place of the play glyph. Any exit from here
	// has to clear it, or a failure — a refused autoplay, an expired stream, a
	// computer that went to sleep mid-resolve — leaves the transport spinning
	// forever with no way back.
	try {
		const stream = await command('music_stream_url', {
			source_id: track.source_id,
			track,
			force_refresh: !!options.forceRefresh,
		}, 50000);
		if (!stream.url) throw new Error('The computer did not return an audio stream.');

		// A newer play may have started while this one was resolving; adopting
		// its stream would swap the track out from under the user.
		if (trackKey(state.currentTrack) !== trackKey(track)) return;

		const resumeAt = Number(options.resumeAt || 0);
		audio.src = mediaUrl(stream.url);
		audio.load();
		if (resumeAt > 0) {
			audio.addEventListener('loadedmetadata', () => {
				if (Number.isFinite(audio.duration)) audio.currentTime = Math.min(resumeAt, Math.max(0, audio.duration - 1));
			}, { once: true });
		}
		if (options.startPaused) {
			publishNowPlaying('paused');
			return;
		}
		await audio.play();
		rememberRecent(track);
		publishMediaSession(track);
		publishNowPlaying('playing');
	} finally {
		state.loading = false;
		emit();
	}
}

export async function toggle() {
	if (isLinked()) {
		// The desktop's answer comes back over the event loop, which is a round
		// trip away. Showing the new state now keeps the button from reading as
		// dead; the next broadcast corrects it if the command did not land.
		state.remote = { ...state.remote, playing: !state.remote.playing };
		emit();
		remoteControl('toggle');
		return;
	}
	if (!state.currentTrack) return;
	// Mid-resolve there is no stream to start yet. Calling play() here rejects
	// and surfaces a failure for a track that is loading perfectly well.
	if (state.loading) return;
	try {
		if (audio.paused) await audio.play();
		else audio.pause();
	} catch (error) {
		reportError(error);
	}
}

export async function skip(offset) {
	if (isLinked()) { remoteControl(offset > 0 ? 'next' : 'prev'); return; }
	if (!state.queue.length) return;

	// Repeat-one applies to the track ending on its own, not to a deliberate
	// press of next — a user asking for the next track means it.
	const index = state.queueIndex + offset;
	if (index < 0) {
		// Pressing previous mid-track restarts it first, as every other player
		// does, and only steps back when already near the start.
		if (currentTime() > 3) { audio.currentTime = 0; emit(); return; }
		if (state.repeat === 'all') {
			await playTrack(state.queue[state.queue.length - 1], state.queue, state.queue.length - 1);
			return;
		}
		// Nothing before the first track, so restart it. Doing nothing at all
		// reads as a dead button.
		audio.currentTime = 0;
		emit();
		return;
	}
	if (index >= state.queue.length) {
		if (state.repeat === 'all') {
			await playTrack(state.queue[0], state.queue, 0);
			return;
		}
		// End of the queue: stop on the last track rather than leaving it
		// running, and let the transport show paused instead of looking stuck.
		audio.pause();
		audio.currentTime = 0;
		emit();
		return;
	}
	await playTrack(state.queue[index], state.queue, index);
}

export function seekTo(seconds) {
	if (isLinked()) {
		const total = duration();
		if (total > 0) remoteControl('seek', { ratio: Math.max(0, Math.min(1, seconds / total)) });
		return;
	}
	if (!Number.isFinite(audio.duration)) return;
	audio.currentTime = Math.max(0, Math.min(seconds, duration()));
}

export function setVolume(value) {
	const volume = Math.max(0, Math.min(1, Number(value) || 0));
	state.volume = volume;
	persist(STORAGE.volume, volume);
	if (isLinked()) { remoteControl('set_volume', { value: volume }); return; }
	audio.volume = volume;
	emit();
}

export function cycleRepeat() {
	state.repeat = nextRepeat(state.repeat);
	persist(STORAGE.repeat, state.repeat);
	if (isLinked()) remoteControl('set_repeat', { mode: state.repeat });
	else publishNowPlaying(audio.paused ? 'paused' : 'playing');
	emit();
	return state.repeat;
}

/* Shuffle reorders the queue rather than randomising playback order on the fly,
 * so the queue the user is looking at is the order they will actually hear. The
 * pre-shuffle order is kept so turning it off is a restore, not a second
 * shuffle. */
export function toggleShuffle() {
	if (isLinked()) { remoteControl('queue_shuffle'); return state.shuffled; }
	const playing = state.queue[state.queueIndex] || null;

	if (state.shuffled && state.queueUnshuffled) {
		state.queue = state.queueUnshuffled.slice();
		state.queueUnshuffled = null;
		state.shuffled = false;
	} else {
		state.queueUnshuffled = state.queue.slice();
		const rest = state.queue.filter(track => track !== playing);
		for (let i = rest.length - 1; i > 0; i -= 1) {
			const j = Math.floor(Math.random() * (i + 1));
			[rest[i], rest[j]] = [rest[j], rest[i]];
		}
		// The current track stays put; shuffling must never interrupt it.
		state.queue = playing ? [playing, ...rest] : rest;
		state.shuffled = true;
	}
	state.queueIndex = playing ? state.queue.indexOf(playing) : state.queueIndex;
	publishNowPlaying(audio.paused ? 'paused' : 'playing');
	emit();
	return state.shuffled;
}

/* ── Queue editing ─────────────────────────────────────────────────────────*/

export function queueAddNext(track) {
	if (isLinked()) { remoteControl('queue_add_next', { track }); return; }
	const at = state.queueIndex < 0 ? state.queue.length : state.queueIndex + 1;
	state.queue = [...state.queue.slice(0, at), track, ...state.queue.slice(at)];
	publishNowPlaying(audio.paused ? 'paused' : 'playing');
	emit();
}

export function queueAddEnd(track) {
	if (isLinked()) { remoteControl('queue_add_end', { track }); return; }
	state.queue = [...state.queue, track];
	publishNowPlaying(audio.paused ? 'paused' : 'playing');
	emit();
}

export function queueRemove(index) {
	if (isLinked()) { remoteControl('queue_remove', { index }); return; }
	if (index < 0 || index >= state.queue.length) return;
	state.queue = state.queue.filter((_track, position) => position !== index);
	if (index < state.queueIndex) state.queueIndex -= 1;
	else if (index === state.queueIndex) state.queueIndex = Math.min(state.queueIndex, state.queue.length - 1);
	publishNowPlaying(audio.paused ? 'paused' : 'playing');
	emit();
}

export function queueMove(from, to) {
	if (isLinked()) { remoteControl('queue_move', { from, to }); return; }
	if (from === to || from < 0 || to < 0 || from >= state.queue.length || to >= state.queue.length) return;
	const playing = state.queue[state.queueIndex] || null;
	const next = state.queue.slice();
	next.splice(to, 0, ...next.splice(from, 1));
	state.queue = next;
	// The index follows the track, not the position: a reorder must never
	// silently change what is playing.
	state.queueIndex = playing ? next.indexOf(playing) : state.queueIndex;
	publishNowPlaying(audio.paused ? 'paused' : 'playing');
	emit();
}

export async function queuePlayIndex(index) {
	if (isLinked()) { remoteControl('queue_play_index', { index }); return; }
	const track = state.queue[index];
	if (track) await playTrack(track, state.queue, index);
}

export function queueClearUpNext() {
	if (isLinked()) { remoteControl('queue_clear_up_next'); return; }
	state.queue = state.queue.slice(0, Math.max(0, state.queueIndex + 1));
	publishNowPlaying(audio.paused ? 'paused' : 'playing');
	emit();
}

/* ── Output transfer ───────────────────────────────────────────────────────
 * The desktop hands its session over and waits for an acknowledgement before it
 * pauses itself, so this must not answer until the audio has genuinely loaded.
 * Answering early is what turns a failed handoff into silence on both devices. */

export async function adoptTransfer(message) {
	const queue = Array.isArray(message.queue) ? message.queue : [];
	const index = Math.max(0, Math.min(Number(message.index) || 0, queue.length - 1));
	const track = queue[index];
	if (!track) throw new Error('The transfer carried no playable track.');

	state.linked = false;   // the phone is the output now, not a spectator
	state.remote = null;
	if (message.repeat) state.repeat = message.repeat;
	else if (typeof message.loop === 'boolean') state.repeat = message.loop ? 'all' : 'off';

	await playTrack(track, queue, index, {
		resumeAt: Number(message.current_time) || 0,
		// A transfer of paused playback must arrive paused, or the phone starts
		// playing in someone's pocket.
		startPaused: !message.playing,
	});
}

/* ── Media Session ─────────────────────────────────────────────────────────
 * The lock screen and the car stereo are the surfaces this app is used through
 * most, so they get the same metadata and the same controls as the in-app
 * transport. */

function publishMediaSession(track) {
	if (!('mediaSession' in navigator)) return;
	try {
		navigator.mediaSession.metadata = new MediaMetadata({
			title: track?.title || 'Rainette Music',
			artist: artistName(track),
			album: track?.metadata?.album_name || track?.album || '',
			artwork: [{ src: new URL(artworkUrl(track), location.href).toString(), sizes: '512x512' }],
		});
		navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
	} catch { /* older iOS builds reject some artwork */ }
}

function wireMediaSession() {
	if (!('mediaSession' in navigator)) return;
	const handlers = {
		play: () => toggle(),
		pause: () => toggle(),
		previoustrack: () => skip(-1).catch(reportError),
		nexttrack: () => skip(1).catch(reportError),
		seekto: details => { if (Number.isFinite(details.seekTime)) seekTo(details.seekTime); },
		seekbackward: details => seekTo(currentTime() - (details.seekOffset || 10)),
		seekforward: details => seekTo(currentTime() + (details.seekOffset || 10)),
	};
	for (const [action, handler] of Object.entries(handlers)) {
		try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* unsupported action */ }
	}
}

/* ── Element events ────────────────────────────────────────────────────────*/

for (const event of ['play', 'pause']) {
	audio.addEventListener(event, () => {
		if ('mediaSession' in navigator) navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
		publishNowPlaying(audio.paused ? 'paused' : 'playing');
		emit();
	});
}

audio.addEventListener('loadedmetadata', emit);
audio.addEventListener('timeupdate', () => { guardTrueEnd(); publishProgress(); emit(); });

function finishTrack() {
	if (state.repeat === 'one') {
		audio.currentTime = 0;
		audio.play().catch(reportError);
		return;
	}
	skip(1).catch(reportError);
}

/* Past the real end the element is still playing the silent tail, so `ended`
 * would not arrive for minutes. Finish on the real duration instead. */
let endGuardKey = '';

function guardTrueEnd() {
	if (isLinked() || audio.paused || !state.currentTrack || !isStretched()) return;
	if (currentTime() < duration() - 0.35) return;
	const key = trackKey(state.currentTrack);
	if (endGuardKey === key) return;
	endGuardKey = key;
	finishTrack();
}

audio.addEventListener('ended', finishTrack);

audio.addEventListener('error', async () => {
	// A stream URL the computer resolved earlier can expire mid-session. That is
	// expected, and recoverable exactly once per track before it counts as a
	// real failure worth telling the user about.
	if (!state.currentTrack || state.streamRefreshAttempted) {
		reportError(new Error('The audio stream expired or became unavailable.'));
		return;
	}
	state.streamRefreshAttempted = true;
	try {
		await playTrack(state.currentTrack, state.queue, state.queueIndex, {
			forceRefresh: true,
			resumeAt: currentTime(),
		});
	} catch {
		reportError(new Error('The audio stream expired or became unavailable.'));
	}
});

wireMediaSession();

/** Pause this phone's own audio without forgetting the queue it holds. */
export function pauseLocal() {
	if (!audio.paused) audio.pause();
}

/** This phone's session, shaped the way an output handoff wants it. */
export function localSession() {
	return {
		queue: state.queue,
		index: state.queueIndex,
		current_time: audio.currentTime || 0,
		playing: !audio.paused,
		repeat: state.repeat,
	};
}

/** Stop and forget everything, for disconnect. */
export function resetPlayback() {
	audio.pause();
	audio.removeAttribute('src');
	audio.load();
	state.queue = [];
	state.queueIndex = -1;
	state.currentTrack = null;
	state.remote = null;
	emit();
}

export { emit as notifyPlayerChanged };
