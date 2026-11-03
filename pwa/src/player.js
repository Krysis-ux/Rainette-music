/* The phone's playback engine: the one <audio>, the queue, the transport. Every
 * state change is reported to the computer, and in linked mode this drives the
 * computer's audio instead. A stream that expired re-resolves once. */

import { state, STORAGE, persist, trackKey, artworkUrl, artistName, nextRepeat, rememberRecent, trackDuration, countPlay } from './state.js';
import { command, mediaUrl } from './bridge.js';
import { isLocalTrack, localStreamUrl } from './local.js';
import {
	nativeAvailable, nativeState, nativePosition, nativeLoad, nativeQueue, nativeStop, nativeResume,
	nativePause, nativeSeek, nativeSkip, nativeRepeat, nativeResolve, nativeRestore, configureNative,
} from './native-engine.js';
import { engineFor, isCatalogTrack, describeError, isCanceled } from './source-provider.js';

const audio = new Audio();
audio.preload = 'metadata';
/* In the document, not merely in memory. */
function attachAudio() {
	// Defensive to the point of paranoia, because this is an enhancement and the player is not:
	// nothing here may be able to stop the module evaluating.
	try {
		if (!(audio instanceof Node) || audio.isConnected) return;
		// iOS will not reliably keep a video-capable element playing inline
		// without this, and it costs nothing on an audio element anywhere else.
		audio.setAttribute('playsinline', '');
		(document.body || document.documentElement)?.append(audio);
	} catch { /* detached is how it worked before; it still plays */ }
}

/* Tell iOS this is music, not a sound effect. */
function declarePlaybackSession() {
	try {
		if (!('audioSession' in navigator)) return;
		// Declared everywhere, not only in a Home Screen app.
		navigator.audioSession.type = 'playback';
	} catch { /* not supported here; a tab plays regardless */ }
}

if (document.body) attachAudio();
else document.addEventListener('DOMContentLoaded', attachAudio, { once: true });
audio.addEventListener('playing', declarePlaybackSession, { once: true });

let reportError = () => {};
/* Fired once a track's media has been handed to the element. The equaliser
 * hangs off this rather than importing the player back, which would close a
 * cycle between the two modules. */
let onSourceLoaded = () => {};

const listeners = new Set();

export function configurePlayer(options = {}) {
	reportError = options.onError || reportError;
	onSourceLoaded = options.onSourceLoaded || onSourceLoaded;
	// Volume is applied by audio.js, which knows whether the gain node or the
	// element is the thing that actually carries it.
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

/* Who makes the sound when this phone does: the <audio> element, or the native app's media
 * service. */
let owner = 'element';

export function nativeOwns() {
	return owner === 'native';
}

export function currentTrack() {
	return isLinked() ? (state.remote.track || null) : state.currentTrack;
}

export function isPlaying() {
	if (isLinked()) return !!state.remote.playing;
	return nativeOwns() ? nativeState.playing : !audio.paused;
}

export function isLoading() {
	return !isLinked() && state.loading;
}

/* AVFoundation reads some YouTube m4a streams as exactly twice their real length: 261.04s for a
 * file AudioToolbox reads as 130.52s. */
function isStretched() {
	const known = trackDuration(state.currentTrack);
	const raw = Number.isFinite(audio.duration) ? audio.duration : 0;
	return known > 0 && raw > known + Math.max(1, known * 0.05);
}

export function currentTime() {
	if (isLinked()) return Number(state.remote.current_time) || 0;
	if (nativeOwns()) return nativePosition();
	return audio.currentTime || 0;
}

export function duration() {
	if (isLinked()) return Number(state.remote.duration) || 0;
	if (nativeOwns()) return trackDuration(state.currentTrack) || nativeState.duration;
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
		playing: isPlaying(),
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
	// mobile data from spending its battery on the tunnel. With the screen off
	// nobody is reading it at all, so it drops to a keep-alive.
	const now = Date.now();
	if (now - lastProgressAt < (document.hidden ? 5000 : 1000)) return;
	lastProgressAt = now;
	command('music_progress', {
		current_time: currentTime(),
		duration: duration(),
		playing: isPlaying(),
		source_id: state.currentTrack?.source_id || '',
	}).catch(() => {});
}

/** Drive the desktop's transport when this phone is linked to it. */
function remoteControl(action, payload = {}) {
	command('music_remote_control', { action, ...payload }).catch(() => {});
}

/* ── Stream URLs ───────────────────────────────────────────────────────────
 * Resolved URLs are kept so the next track can start without asking the computer first. */

const HALF_LIFE = 0.9;   // refresh before the grant expires, not at the edge
const PREFETCH_AHEAD = 2;

/* Retry budget for a track whose stream fails mid-play. Two, because the common
 * shape is one expired link plus one race against its replacement. */
const STREAM_RETRY_MAX = 2;
const STREAM_RETRY_BACKOFF_MS = [0, 1500];
/* Play cleanly for this long and the budget is refilled: a track that dies
 * three minutes in is a fresh problem, not a continuation of the last one. */
const STREAM_RETRY_RESET_AFTER_MS = 30000;
let cleanSince = 0;

const streams = new Map();
const prefetching = new Set();

function rememberStream(track, result) {
	// The grant is the thing that stops resolving, so the grant's life is what this window has to
	// track.
	const seconds = Number(result?.grant_expires_in_s)
		|| Math.min(Math.max(Number(result?.expires_hint_s) || 3600, 60), 21600);
	streams.set(trackKey(track), { url: result.url, expiresAt: Date.now() + seconds * 1000 * HALF_LIFE });
}

function readyStream(track) {
	const key = trackKey(track);
	const entry = streams.get(key);
	if (!entry) return '';
	if (entry.expiresAt <= Date.now()) { streams.delete(key); return ''; }
	return entry.url;
}

/* True for a `play()` rejection that means "something newer took over", which is
 * not a failure the user should ever be shown. Chrome, Safari and Firefox all
 * word the message differently, so the name is what is checked. */
function isSupersededPlay(error) {
	return error?.name === 'AbortError';
}

/* WebKit collapses every unusable source into one `NotSupportedError` reading
 * "The operation is not supported". The `error` event can tell them apart by
 * asking the URL; the `play()` rejection arrives first and used to bypass it. */
async function playFailure(error, url) {
	if (isGestureRequired(error) || error?.name !== 'NotSupportedError') return error;
	return new Error(await diagnoseStreamFailure(url));
}

/* The gesture-required rejection, which *is* worth saying — briefly. */
export function isGestureRequired(error) {
	return error?.name === 'NotAllowedError';
}

async function resolveStream(track, forceRefresh, { prefetch = false } = {}) {
	// A file on this phone needs no computer and no network: the blob is right
	// here, and asking the companion for it would fail on a source_id it has
	// never heard of.
	if (isLocalTrack(track)) return localStreamUrl(track);

	// In the native app a streamed song is resolved on this phone, even when the
	// element is the one playing it (a queue that mixes in phone files).
	if (nativeAvailable() && isCatalogTrack(track)) {
		const stream = await nativeResolve(track, forceRefresh);
		const lifeMs = Math.max(60000, Number(stream.expires_at_ms) - Date.now());
		streams.set(trackKey(track), { url: stream.url, expiresAt: Date.now() + lifeMs * HALF_LIFE });
		return stream.url;
	}

	const result = await command('music_stream_url', {
		source_id: track.source_id,
		track,
		force_refresh: !!forceRefresh,
		// Warming a URL is not listening to it. Without this the computer logs
		// a play for every track we look ahead at, so the history fills with
		// songs that were never heard and "recently played" stops being true.
		prefetch,
	}, 50000);
	if (!result.url) throw new Error('The computer did not return an audio stream.');
	rememberStream(track, result);
	return result.url;
}

/** Resolve what is coming up, so advancing needs no round trip. */
function prefetchUpcoming() {
	// The native service prepares its own next track; nothing to warm here.
	if (nativeOwns()) return;
	// Only what is still coming up. A queue edit can move or remove the tracks
	// a previous pass was warming, and paying a yt-dlp resolve for a track the
	// user has already skipped past is the waste this guard exists to stop.
	const wanted = new Set();
	for (let offset = 1; offset <= PREFETCH_AHEAD; offset += 1) {
		const next = state.queue[state.queueIndex + offset];
		if (!next?.source_id) continue;
		wanted.add(trackKey(next));
		if (readyStream(next)) continue;
		const key = trackKey(next);
		if (prefetching.has(key)) continue;
		prefetching.add(key);
		resolveStream(next, false, { prefetch: true })
			.catch(() => {})
			.finally(() => prefetching.delete(key));
	}
	// Anything in flight that is no longer up next is forgotten rather than
	// awaited: the request cannot be recalled, but its slot can be freed so a
	// track that comes back into range is not blocked behind it.
	for (const key of [...prefetching]) if (!wanted.has(key)) prefetching.delete(key);
}

/* ── The native engine ─────────────────────────────────────────────────────
 * In the Android app a queue of streamed songs is played by the phone's media
 * service. Ownership changes hands only in claimNative and releaseNative. */

/* Every play request, whichever engine takes it. A request that finishes
 * loading after a newer one began must not start anything: that is how a slow
 * answer for the song you skipped past would otherwise play over the new one. */
let playRequests = 0;

function claimNative() {
	if (owner === 'native') return;
	owner = 'native';
	// Silent before the service makes a sound: one owner, never two.
	audio.pause();
	if (audio.src) {
		audio.removeAttribute('src');
		audio.load();
	}
}

async function releaseNative() {
	if (owner !== 'native') return;
	owner = 'element';
	await nativeStop().catch(() => { /* already stopped, or the service is gone */ });
}

function nativeFailure(error) {
	const failure = new Error(describeError(error));
	failure.code = error?.code;
	return failure;
}

async function playNatively(track, options, request) {
	claimNative();
	state.loading = true;
	emit();
	try {
		const loaded = await nativeLoad(state.queue, state.queueIndex, {
			resumeAt: Number(options.resumeAt || 0),
			startPaused: Boolean(options.startPaused),
			repeat: state.repeat,
		});
		if (!loaded || request !== playRequests) return;
		if (options.startPaused) {
			publishNowPlaying('paused');
			return;
		}
		rememberRecent(track);
		countPlay(track);
		publishNowPlaying('playing');
	} catch (error) {
		if (isCanceled(error)) return;
		throw nativeFailure(error);
	} finally {
		state.loading = false;
		emit();
	}
}

/* The service moved on by itself -- a track ended, or the notification or a
 * headset skipped -- so the page follows it, not the other way round. */
configureNative({
	onState: snapshot => {
		if (!nativeOwns()) return;
		const track = state.queue[snapshot.index];
		if (track && trackKey(track) === snapshot.sourceId && snapshot.index !== state.queueIndex) {
			state.queueIndex = snapshot.index;
			state.currentTrack = track;
			rememberRecent(track);
			countPlay(track);
			publishNowPlaying(snapshot.playing ? 'playing' : 'paused');
		}
		publishProgress();
		emit();
	},
	onError: error => { if (nativeOwns()) reportError(nativeFailure(error)); },
});

/** Hand a queue edit to the service without interrupting the current track. */
function syncNativeQueue() {
	if (!nativeOwns()) return;
	nativeQueue(state.queue, state.queueIndex).then(applied => {
		if (applied) return;
		// The playing track left the queue: carry on from where the page points now.
		const index = Math.max(0, state.queueIndex);
		const next = state.queue[index];
		if (next) playTrack(next, state.queue, index, { startPaused: !nativeState.playing }).catch(reportError);
	}).catch(error => reportError(nativeFailure(error)));
}

/** The app reopened over a service that kept playing: show what it is playing,
 *  rather than "Nothing playing" over music the phone is making. */
export async function restoreNative() {
	const restored = await nativeRestore().catch(() => null);
	// Something was started while this asked; that is the truth now.
	if (!restored || state.currentTrack) return;
	const track = restored.queue[restored.index];
	if (!track) return;
	claimNative();
	state.queue = restored.queue;
	state.queueIndex = restored.index;
	state.currentTrack = track;
	emit();
}

/* In the app, a queue holds one kind of track: streamed songs (the native service) or files on
 * this phone (the element). */
function queueOfOneKind(track, queue, index) {
	if (!nativeAvailable() || !Array.isArray(queue)) return { queue, index };
	const streamed = isCatalogTrack(track);
	const kept = queue.filter(item => isCatalogTrack(item) === streamed);
	if (kept.length === queue.length) return { queue, index };
	const at = kept.indexOf(track);
	return at >= 0 ? { queue: kept, index: at } : { queue: [track], index: 0 };
}

/** A phone file cannot join a queue the service is streaming; say so, once. */
function refuseForNative(track) {
	if (!nativeOwns() || isCatalogTrack(track)) return false;
	reportError(new Error('Music stored on this phone can’t join a streamed queue yet. Play it on its own instead.'));
	return true;
}

/* ── Playback ──────────────────────────────────────────────────────────────*/

export async function playTrack(track, queue = [track], index = 0, options = {}) {
	if (!track?.source_id) throw new Error('This track has no playable source.');
	const request = ++playRequests;

	// Playing something from the phone takes ownership of the session back from
	// the desktop, which is the only sane reading of "I pressed play here".
	state.remote = null;
	// And say so, so every other surface stops claiming the computer is playing.
	if (!isLinked() && !options.forceRefresh) claimPlaybackHere();
	if (!options.forceRefresh) ({ queue, index } = queueOfOneKind(track, queue, index));
	state.queue = queue.slice();
	state.queueIndex = Math.max(0, Math.min(index, state.queue.length - 1));
	state.currentTrack = track;
	endGuardKey = '';
	// A retry re-enters here, so the budget must survive it; only a genuinely
	// new play resets it.
	if (!options.forceRefresh) state.streamRetries = 0;
	cleanSince = 0;

	// A queue the native service can stream end to end is its to play. A retry
	// (`forceRefresh`) only ever comes from the element's own error handler.
	if (!options.forceRefresh && engineFor(state.queue) === 'native') return playNatively(track, options, request);
	if (nativeOwns()) await releaseNative();

	// Only a track whose URL still has to be fetched is "loading"; one already
	// resolved starts now and never shows a spinner.
	const ready = options.forceRefresh ? '' : readyStream(track);
	state.loading = !ready;
	emit();

	// `loading` drives a spinner in place of the play glyph.
	try {
		const url = ready || await resolveStream(track, options.forceRefresh);

		// A newer play may have started while this one was resolving; adopting
		// its stream would swap the track out from under the user -- or, if the
		// newer one went to the native service, play a second copy over it.
		if (request !== playRequests || trackKey(state.currentTrack) !== trackKey(track)) return;

		const resumeAt = Number(options.resumeAt || 0);
		// A blob URL, or one this phone resolved itself, is already absolute, and there may be no
		// companion to resolve it against -- local files and on-device streams are meant to play with no
		// computer paired at all.
		audio.src = isLocalTrack(track) || /^(blob:|https?:)/.test(url) ? url : mediaUrl(url);
		audio.load();
		if (resumeAt > 0) {
			audio.addEventListener('loadedmetadata', () => {
				if (Number.isFinite(audio.duration)) audio.currentTime = Math.min(resumeAt, Math.max(0, audio.duration - 1));
			}, { once: true });
		}
		// Named on the lock screen before it starts, not after, so the controls
		// never show the track that just finished.
		publishMediaSession(track);
		// Deliberately not awaited: a listener may reload this same element, and
		// waiting on that here would deadlock this call against its own restart.
		try { onSourceLoaded(); } catch { /* a broken listener is not playback's problem */ }
		if (options.startPaused) {
			publishNowPlaying('paused');
			return;
		}
		const started = audio.play();
		prefetchUpcoming();
		try {
			await started;
		} catch (error) {
			// `play()` rejects for two reasons that are not failures, and reporting them is what made
			// switching tracks quickly throw a wall of red and then play the song anyway.
			if (!isSupersededPlay(error)) throw await playFailure(error, audio.currentSrc || audio.src);
		}
		rememberRecent(track);
		// The one place a track genuinely began, so the one place worth counting.
		// "Most popular" over a library the computer sends no view counts for is
		// otherwise nothing at all.
		countPlay(track);
		publishNowPlaying('playing');
	} finally {
		state.loading = false;
		emit();
	}
}

/* `play` and `pause` state an *intent*; `toggle` states a flip, and a flip is only correct if the
 * sender's idea of what is playing is current. */

/** Start playing. Repeating it while already playing does nothing. */
export async function play() {
	if (isLinked()) {
		// Optimistic in the direction asked for, never a flip: the next
		// broadcast corrects it if the command did not land. The desktop makes
		// its own idempotence decision -- it knows whether it is mid-resolve.
		state.remote = { ...state.remote, playing: true };
		emit();
		remoteControl('play');
		return;
	}
	if (!state.currentTrack) return;
	// Mid-resolve there is no stream to start yet. Calling play() here rejects
	// and surfaces a failure for a track that is loading perfectly well.
	if (state.loading) return;
	// The service's own `play` is just as absolute: repeating it never pauses.
	if (nativeOwns()) {
		await nativeResume().catch(error => reportError(nativeFailure(error)));
		return;
	}
	// No `if (!audio.paused) return` guard: `play()` on a playing element is already a no-op per spec,
	// so the guard bought nothing -- and it could strand playback outright.
	try {
		await audio.play();
	} catch (error) {
		// Same rule as playTrack: a play superseded by a newer one is not
		// something to shout about.
		if (!isSupersededPlay(error)) reportError(error);
	}
}

/** Pause. Repeating it while already paused does nothing. */
export function pause() {
	if (isLinked()) {
		state.remote = { ...state.remote, playing: false };
		emit();
		remoteControl('pause');
		return;
	}
	if (!state.currentTrack) return;
	if (nativeOwns()) {
		nativePause().catch(() => { /* nothing playing to pause */ });
		return;
	}
	// Unconditional for the same reason as `play()`: `pause()` on a paused element is a no-op.
	audio.pause();
}

/** Flip. For the in-app button, where a tap does mean "the other one". */
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
	if (state.loading) return;
	if (isPlaying()) pause();
	else await play();
}

export async function skip(offset) {
	if (isLinked()) { remoteControl(offset > 0 ? 'next' : 'prev'); return; }
	if (!state.queue.length) return;
	if (nativeOwns()) {
		// Same rules as below, applied by the service so the notification and a headset behave
		// identically: previous restarts a track past 3 s, repeat all wraps.
		const last = state.queueIndex >= state.queue.length - 1;
		if (offset > 0 && last && state.repeat !== 'all') {
			await nativePause().catch(() => {});
			await nativeSeek(0).catch(() => {});
			emit();
			return;
		}
		await nativeSkip(offset).catch(error => reportError(nativeFailure(error)));
		return;
	}

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
	if (nativeOwns()) {
		const total = duration();
		nativeSeek(Math.max(0, total > 0 ? Math.min(seconds, total) : seconds)).catch(() => {});
		return;
	}
	if (!Number.isFinite(audio.duration)) return;
	audio.currentTime = Math.max(0, Math.min(seconds, duration()));
}

/* Volume itself lives in audio.js, because on iOS an element's volume is read-only and the only
 * control that works is a gain node in the Web Audio graph — which is that module's business. */
export function remoteSetVolume(value) {
	remoteControl('set_volume', { value: Math.max(0, Math.min(2, Number(value) || 0)) });
}

export function cycleRepeat() {
	state.repeat = nextRepeat(state.repeat);
	persist(STORAGE.repeat, state.repeat);
	if (isLinked()) remoteControl('set_repeat', { mode: state.repeat });
	else {
		if (nativeOwns()) nativeRepeat(state.repeat).catch(() => {});
		publishNowPlaying(isPlaying() ? 'playing' : 'paused');
	}
	emit();
	return state.repeat;
}

/* Shuffle reorders the queue rather than randomising playback order on the fly, so the queue the
 * user is looking at is the order they will actually hear. */
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
	publishNowPlaying(isPlaying() ? 'playing' : 'paused');
	syncNativeQueue();
	emit();
	return state.shuffled;
}

/* ── Queue editing ─────────────────────────────────────────────────────────*/

export function queueAddNext(track) {
	if (isLinked()) { remoteControl('queue_add_next', { track }); return; }
	if (refuseForNative(track)) return;
	const at = state.queueIndex < 0 ? state.queue.length : state.queueIndex + 1;
	state.queue = [...state.queue.slice(0, at), track, ...state.queue.slice(at)];
	publishNowPlaying(isPlaying() ? 'playing' : 'paused');
	syncNativeQueue();
	prefetchUpcoming();
	emit();
}

export function queueAddEnd(track) {
	if (isLinked()) { remoteControl('queue_add_end', { track }); return; }
	if (refuseForNative(track)) return;
	state.queue = [...state.queue, track];
	publishNowPlaying(isPlaying() ? 'playing' : 'paused');
	syncNativeQueue();
	prefetchUpcoming();
	emit();
}

export function queueRemove(index) {
	if (isLinked()) { remoteControl('queue_remove', { index }); return; }
	if (index < 0 || index >= state.queue.length) return;
	state.queue = state.queue.filter((_track, position) => position !== index);
	if (index < state.queueIndex) state.queueIndex -= 1;
	else if (index === state.queueIndex) state.queueIndex = Math.min(state.queueIndex, state.queue.length - 1);
	publishNowPlaying(isPlaying() ? 'playing' : 'paused');
	syncNativeQueue();
	prefetchUpcoming();
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
	publishNowPlaying(isPlaying() ? 'playing' : 'paused');
	syncNativeQueue();
	prefetchUpcoming();
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
	publishNowPlaying(isPlaying() ? 'playing' : 'paused');
	syncNativeQueue();
	prefetchUpcoming();
	emit();
}

/* ── Output transfer ───────────────────────────────────────────────────────
 * The desktop hands its session over and waits for an acknowledgement before it pauses itself, so
 * this must not answer until the audio has genuinely loaded. */

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
 * The lock screen and the car stereo are the surfaces this app is used through most, so they get
 * the same metadata and the same controls as the in-app transport. */

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
	publishPosition(true);
}

/* Without this the lock screen shows a scrubber that does not move, and its
 * skip-forward gestures land in the wrong place. */
let lastPositionAt = 0;

function publishPosition(force = false) {
	if (!navigator.mediaSession?.setPositionState) return;
	const now = Date.now();
	if (!force && now - lastPositionAt < 900) return;
	lastPositionAt = now;
	const length = duration();
	if (!(length > 0)) return;
	try {
		navigator.mediaSession.setPositionState({
			duration: length,
			position: Math.max(0, Math.min(currentTime(), length)),
			playbackRate: audio.playbackRate || 1,
		});
	} catch { /* a position the element disagrees with is refused, harmlessly */ }
}

function wireMediaSession() {
	if (!('mediaSession' in navigator)) return;
	const handlers = {
		// Absolute verbs, not toggles -- see play()/pause() above. This is the
		// CarPlay stutter.
		play: () => play().catch(reportError),
		pause: () => pause(),
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
		publishPosition(true);
		publishNowPlaying(audio.paused ? 'paused' : 'playing');
		emit();
	});
}

audio.addEventListener('loadedmetadata', () => { publishPosition(true); emit(); });
audio.addEventListener('timeupdate', () => {
	refillRetryBudget();
	guardTrueEnd();
	publishProgress();
	publishPosition();
	emit();
});

/* Audio that has been flowing for a while proves the stream is healthy, so the
 * retries spent getting here are no longer owed against the next failure. */
function refillRetryBudget() {
	if (audio.paused || !state.streamRetries) { cleanSince = cleanSince || Date.now(); return; }
	if (!cleanSince) { cleanSince = Date.now(); return; }
	if (Date.now() - cleanSince >= STREAM_RETRY_RESET_AFTER_MS) {
		state.streamRetries = 0;
		cleanSince = Date.now();
	}
}

function finishTrack() {
	if (state.repeat === 'one') {
		audio.currentTime = 0;
		// Re-arm the true-end guard.
		endGuardKey = '';
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

/* A media `error` event carries nothing usable — no status, no reason. Asking
 * the grant directly is the only way to tell "this link died" from "your phone
 * cannot decode this", and those want opposite messages. */
async function diagnoseStreamFailure(url) {
	if (!url || url.startsWith('blob:')) return 'This file could not be played on this phone.';
	try {
		// `bytes=0-` and not `bytes=0-0`: the open-ended range is the one a media element actually opens
		// with, and it is the *only* one some upstreams refuse.
		const response = await fetch(url, { headers: { Range: 'bytes=0-' }, cache: 'no-store' });
		try { await response.body?.cancel(); } catch { /* already drained */ }
		if (response.status === 404) return 'That audio link expired. Reconnecting to your computer…';
		if (response.status === 502) return 'Your computer could not reach the audio source.';
		if (response.status === 403) return 'The audio source refused your computer. Skipping to the next track may help.';
		if (response.ok || response.status === 206) return 'Your phone could not play this format.';
		return `Your computer answered ${response.status}.`;
	} catch {
		// No readable answer.
		if (await computerAnswers()) return 'Your computer is connected but could not load this song.';
		return 'Cannot reach your computer. Open Rainette there, then rescan the code to reconnect.';
	}
}

async function computerAnswers() {
	try {
		await command('music_followed_artists', {}, 8000);
		return true;
	} catch {
		return false;
	}
}

audio.addEventListener('error', async () => {
	// A stream URL the computer resolved earlier can expire mid-session.
	if (!state.currentTrack || state.streamRetries >= STREAM_RETRY_MAX) {
		reportError(new Error(await diagnoseStreamFailure(audio.currentSrc || audio.src)));
		return;
	}
	const wait = STREAM_RETRY_BACKOFF_MS[Math.min(state.streamRetries, STREAM_RETRY_BACKOFF_MS.length - 1)];
	state.streamRetries += 1;
	if (wait) await new Promise(resolve => setTimeout(resolve, wait));
	// The native service took over while this waited: a retry now would start
	// the element over it.
	if (nativeOwns()) return;
	try {
		await playTrack(state.currentTrack, state.queue, state.queueIndex, {
			forceRefresh: true,
			resumeAt: currentTime(),
		});
	} catch {
		// Not "expired": that guess sent people to rescan a computer that was
		// fine. Say what actually failed.
		reportError(new Error(await diagnoseStreamFailure(audio.currentSrc || audio.src)));
	}
});

wireMediaSession();

/** Pause this phone's own audio without forgetting the queue it holds. */
export function pauseLocal() {
	if (nativeOwns()) { nativePause().catch(() => {}); return; }
	if (!audio.paused) audio.pause();
}

/* ── Audio graph access ────────────────────────────────────────────────────
 * The equaliser needs the element itself, because Web Audio routes an element rather than a
 * stream, and it needs a way to reload what is playing after it changes the element's CORS mode. */

export function audioElement() {
	return audio;
}

/** Reload the current track in place, keeping its position and play state. */
export async function reloadCurrent() {
	const track = state.currentTrack;
	// The service's audio never passes through the page's graph; nothing to reload.
	if (!track || nativeOwns()) return;
	await playTrack(track, state.queue, state.queueIndex, {
		resumeAt: audio.currentTime || 0,
		startPaused: audio.paused,
	});
}

/** This phone's session, shaped the way an output handoff wants it. */
export function localSession() {
	return {
		queue: state.queue,
		index: state.queueIndex,
		current_time: currentTime(),
		playing: isPlaying(),
		repeat: state.repeat,
	};
}

/** Stop and forget everything, for disconnect. */
export function resetPlayback() {
	releaseNative();
	playRequests += 1;
	audio.pause();
	audio.removeAttribute('src');
	audio.load();
	// The cached URLs are grants bound to the credential being discarded.
	streams.clear();
	prefetching.clear();
	state.queue = [];
	state.queueIndex = -1;
	state.currentTrack = null;
	state.remote = null;
	emit();
}

/* ── Transport arriving from the computer ──────────────────────────────────
 * Deliberately at the end of the file: a test slices player.js from `export async function
 * playTrack` to the next `export ` and asserts on what is inside, so a new export placed between
 * them silently truncates it. */
export async function applyRemoteVerb(message) {
	if (!message || typeof message !== 'object') return;

	// Addressed at this phone specifically. An absent target means the desktop
	// engine, which is never us.
	const target = String(message.target_device_id || 'desktop');
	if (!state.deviceId || target !== state.deviceId) return;

	// Our own command coming back around. Non-idempotent verbs double-apply:
	// `next` would skip two tracks.
	if (message.origin_device_id && message.origin_device_id === state.deviceId) return;

	// Following the computer means it owns the audio; a verb aimed at this
	// phone's own element while mirroring would fight the mirror.
	if (state.linked) return;

	switch (String(message.action || '')) {
		case 'play':
			if (nativeOwns()) await play();
			else if (audio.paused && state.currentTrack) await audio.play().catch(reportError);
			return;
		case 'pause':
			pauseLocal();
			return;
		case 'toggle':
			await toggle();
			return;
		case 'next':
			await skip(1).catch(reportError);
			return;
		case 'prev':
			await skip(-1).catch(reportError);
			return;
		case 'seek': {
			const length = duration();
			if (!length) return;
			const ratio = Number(message.ratio);
			const seconds = Number.isFinite(Number(message.position_s))
				? Number(message.position_s)
				: (Number.isFinite(ratio) ? Math.min(1, Math.max(0, ratio)) * length : NaN);
			if (Number.isFinite(seconds)) seekTo(seconds);
			return;
		}
		case 'set_repeat':
			if (message.mode) { state.repeat = message.mode; persist(STORAGE.repeat, state.repeat); emit(); }
			return;
		default:
	}
}

/* Tell the computer this phone is the one making the sound now. */
function claimPlaybackHere() {
	if (!state.deviceId) return;
	command('music_playback_target_set', { owner_kind: 'phone', reason: 'claim_by_play' })
		.catch(() => { /* older desktop, or offline; the local label still holds */ });
}

export { emit as notifyPlayerChanged };
