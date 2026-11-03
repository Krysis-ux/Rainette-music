/* The on-device player in Rainette's Android app. */

import { artistName, artworkAt, trackDuration } from './state.js';

const PLUGIN = 'RainettePlayer';
/* Must match RainettePlayerPlugin.PROTOCOL. A mismatch means an app whose page
 * and native half were built apart, which is refused rather than guessed at. */
const PROTOCOL = 1;

const cap = globalThis.Capacitor;
const present = (() => {
	try {
		return Boolean(cap?.isNativePlatform?.() && cap.isPluginAvailable?.(PLUGIN));
	} catch {
		return false;
	}
})();

export function nativeAvailable() {
	return present;
}

function failure(raw) {
	const error = new Error(String(raw?.message || 'Playback stopped unexpectedly.'));
	error.code = String(raw?.code || 'service_failed');
	return error;
}

function call(method, options = {}) {
	if (!present) return Promise.reject(failure({ code: 'bridge_unavailable', message: 'The phone player is not available.' }));
	return cap.nativePromise(PLUGIN, method, options).catch(error => { throw failure(error); });
}

/* What the service last said, for the current generation only. */
export const nativeState = { playing: false, loading: false, ended: false, index: -1, sourceId: '', position: 0, duration: 0, at: 0 };

let generation = 0;
let handlers = { onState: () => {}, onError: () => {} };
const routeListeners = new Set();

/** Told whenever the phone's audio output changes (headphones in, Bluetooth on). */
export function onNativeRoute(listener) {
	routeListeners.add(listener);
	listen();
	return () => routeListeners.delete(listener);
}

export function configureNative(options = {}) {
	handlers = { ...handlers, ...options };
}

function absorb(snapshot) {
	if (!snapshot || Number(snapshot.generation) !== generation) return false;
	nativeState.playing = Boolean(snapshot.playing);
	nativeState.loading = Boolean(snapshot.loading);
	nativeState.ended = Boolean(snapshot.ended);
	nativeState.index = Number.isInteger(snapshot.index) ? snapshot.index : -1;
	nativeState.sourceId = String(snapshot.source_id || '');
	nativeState.position = Number(snapshot.position_s) || 0;
	nativeState.duration = Number(snapshot.duration_s) || 0;
	nativeState.at = Date.now();
	return true;
}

function absorbAndTell(snapshot) {
	if (absorb(snapshot)) handlers.onState(nativeState);
	return snapshot;
}

/* Events arrive through the plugin's own `watch` channel, not Capacitor's addListener. */
const PAGE = Math.random().toString(36).slice(2);
let listening = false;

function listen() {
	if (listening || !present) return;
	listening = true;
	cap.nativeCallback(PLUGIN, 'watch', { page: PAGE }, (data, error) => {
		if (error || !data) return;
		if (data.event === 'error') {
			if (Number(data.generation) === generation) handlers.onError(failure(data));
			return;
		}
		if (data.event === 'route') {
			for (const listener of routeListeners) listener(data);
			return;
		}
		absorbAndTell(data);
	});
}

/** Where the track is now: the last report, carried forward while it plays. */
export function nativePosition() {
	const moving = nativeState.playing && !nativeState.loading;
	const position = moving ? nativeState.position + (Date.now() - nativeState.at) / 1000 : nativeState.position;
	return nativeState.duration > 0 ? Math.min(position, nativeState.duration) : position;
}

/* Only what the service needs to name a track on the lock screen. It derives
 * where the audio comes from itself; nothing here is a URL to play. */
function wire(track) {
	return {
		source_id: String(track?.source_id || ''),
		title: String(track?.title || ''),
		artist: artistName(track),
		// Large enough for a lock screen; a missing cover stays missing.
		thumbnail_url: track?.thumbnail_url ? artworkAt(track, LOCK_SCREEN_ART_PX) : '',
		duration_s: trackDuration(track),
	};
}

const LOCK_SCREEN_ART_PX = 544;

let checked = null;

/* Once per launch: refuse a native half that speaks another protocol. */
function checkProtocol() {
	checked ||= call('capabilities').then(caps => {
		if (Number(caps?.protocol) !== PROTOCOL) {
			throw failure({ code: 'version_mismatch', message: 'This copy of Rainette is incomplete. Reinstall it.' });
		}
	});
	return checked;
}

/** Replace the queue and start (or prepare) `index`. Resolves true once the track has loaded, false
 * if a newer request superseded this one meanwhile. */
export async function nativeLoad(queue, index, { resumeAt = 0, startPaused = false, repeat = 'off' } = {}) {
	listen();
	generation += 1;
	const mine = generation;
	Object.assign(nativeState, { playing: !startPaused, loading: true, ended: false, index, sourceId: String(queue[index]?.source_id || ''), position: resumeAt, duration: 0, at: Date.now() });
	await checkProtocol();
	await call('setRepeat', { mode: repeat });
	// Superseded while getting here: never ask the service for a song nobody wants now.
	if (mine !== generation) return false;
	const snapshot = await call('play', {
		queue: queue.map(wire),
		index,
		position_s: resumeAt,
		start_paused: startPaused,
		generation: mine,
	});
	if (mine !== generation) return false;
	absorbAndTell(snapshot);
	return true;
}

/** Swap the queue around the playing track. False if the service has moved on. */
export async function nativeQueue(queue, index) {
	try {
		absorbAndTell(await call('setQueue', { queue: queue.map(wire), index, generation }));
		return true;
	} catch (error) {
		if (error.code === 'mismatch') return false;
		throw error;
	}
}

/** Silence the service and forget its queue. Bumps the generation first, so
 *  nothing it reports on the way down is mistaken for current. */
export function nativeStop() {
	generation += 1;
	Object.assign(nativeState, { playing: false, loading: false, ended: false, index: -1, sourceId: '', position: 0, duration: 0 });
	return call('stop');
}

export const nativeResume = () => call('resume').then(absorbAndTell);
export const nativePause = () => call('pause').then(absorbAndTell);
export const nativeSeek = seconds => call('seek', { position_s: Math.max(0, Number(seconds) || 0) }).then(absorbAndTell);
export const nativeSkip = offset => call('skip', { offset: offset > 0 ? 1 : -1 }).then(absorbAndTell);
export const nativeRepeat = mode => call('setRepeat', { mode });
export const nativeSetVolume = volume => call('setVolume', { volume: Math.max(0, Math.min(1, Number(volume) || 0)) });
export const nativeSync = () => { listen(); return call('getState').then(absorbAndTell); };

/** What the service is already playing, for a page that has just opened over it (the app was swiped
 * away and reopened while music played). */
export async function nativeRestore() {
	if (!present) return null;
	listen();
	const snapshot = await call('getState', { with_queue: true });
	if (!Array.isArray(snapshot?.queue) || !snapshot.queue.length || snapshot.index < 0) return null;
	generation = Number(snapshot.generation) || 0;
	absorb(snapshot);
	return { queue: snapshot.queue, index: snapshot.index };
}

/** Song search on this phone. Results are shaped like the computer's. */
export async function nativeSearch(query) {
	const result = await call('search', { query });
	return Array.isArray(result?.songs) ? result.songs : [];
}

/** The app's own version, and whether it can install a newer one itself. */
export const nativeInfo = () => call('capabilities');

/** Speed, streaming quality and silence skipping for the phone's player. */
export const nativeConfigure = options => call('configure', options);

/** Where the phone's sound goes: `{ current, devices }`, Bluetooth included. */
export const nativeOutputs = () => { listen(); return call('outputs'); };

/** Send the sound to one of `nativeOutputs().devices` (Android). */
export const nativePickOutput = id => call('pickOutput', { id: String(id || '') });

/** The system's own device picker (iPhone), or Bluetooth settings (Android). */
export const nativeShowOutputs = () => call('showOutputPicker');

/** Download a release APK and open the system installer (Android). */
export const nativeInstallUpdate = url => call('installUpdate', { url });

/** Save a file where the person chooses, through the phone's own save screen
 *  (a page cannot, inside the app). Resolves true when it was saved. */
export async function nativeSaveFile(blob, name) {
	const data = await new Promise((resolve, reject) => {
		const reader = new FileReader();
		reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
		reader.onerror = () => reject(reader.error);
		reader.readAsDataURL(blob);
	});
	// The bytes go first and on their own: the call that opens the save
	// screen has to stay small (RainettePlayerPlugin.stageFile).
	await call('stageFile', { data });
	const result = await call('saveFile', { name, mime: blob.type || 'application/octet-stream' });
	return Boolean(result?.saved);
}

/** A stream URL for the <audio> element, for queues the service does not own. */
export function nativeResolve(track, forceRefresh = false) {
	return call('resolve', { source_id: String(track?.source_id || ''), force_refresh: Boolean(forceRefresh) });
}
