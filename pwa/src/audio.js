/* The one audio graph, and everything that shapes what comes out of it.
 *
 * Two features need Web Audio and neither worked without it:
 *
 *  - Volume. iOS makes `HTMLMediaElement.volume` read-only — assigning to it is
 *    silently ignored — so the phone's volume slider genuinely did nothing on
 *    every iPhone. A GainNode is the only volume an iOS browser will honour.
 *  - Boost past 100%, which an element cannot do at all: its volume is clamped
 *    to 1. A gain node is not, so the desktop's 150% ceiling is reachable here.
 *  - The equaliser, which was already written against Web Audio.
 *
 * The graph is `element → [5 biquads] → gain → destination`, built once and
 * shared, because `createMediaElementSource` may only be called once per
 * element and permanently re-routes it.
 *
 * The awkward part is the cross-origin rule: routing an element whose media came
 * from another origin without CORS does not throw, it outputs silence forever.
 * So the graph is only ever built after a real request has proved the companion
 * answers with CORS headers, and never speculatively.
 */

import { state, STORAGE, persist } from './state.js';
import { audioElement, reloadCurrent, remoteSetVolume, isLinked, notifyPlayerChanged } from './player.js';

/* Matches web/miniplayer.js EQ_BANDS exactly — a preset named "Bass Boost" has
 * to mean the same thing on both devices or it is a different feature wearing
 * the same name. */
export const EQ_BANDS = [
	{ f: 60, type: 'lowshelf', label: 'Bass', short: '60' },
	{ f: 250, type: 'peaking', label: 'Low', short: '250' },
	{ f: 1000, type: 'peaking', label: 'Mid', short: '1k' },
	{ f: 4000, type: 'peaking', label: 'High', short: '4k' },
	{ f: 12000, type: 'highshelf', label: 'Treble', short: '12k' },
];

export const EQ_MIN = -12;
export const EQ_MAX = 12;

export const EQ_PRESETS = {
	Flat: [0, 0, 0, 0, 0],
	'Bass Boost': [8, 4, 0, 0, 1],
	Vocal: [-2, 0, 4, 3, 1],
	Treble: [0, 0, 0, 4, 8],
	Podcast: [-4, -1, 5, 2, -1],
	'Late night': [-6, -2, 2, 1, -3],
};

/* Past unity the signal is being amplified, which clips. The compressor after
 * the gain is what makes 200% loud rather than distorted. */
export const VOLUME_MAX = 2;

const STORE = {
	on: 'rainette.pwa.eq.on',
	gains: 'rainette.pwa.eq.gains',
};

function clamp(value, min, max) {
	return Math.max(min, Math.min(max, value));
}

function readGains() {
	try {
		const stored = JSON.parse(localStorage.getItem(STORE.gains) || 'null');
		if (Array.isArray(stored) && stored.length === EQ_BANDS.length) {
			return stored.map(value => clamp(Number(value) || 0, EQ_MIN, EQ_MAX));
		}
	} catch { /* fall through to flat */ }
	return EQ_BANDS.map(() => 0);
}

const eq = {
	on: localStorage.getItem(STORE.on) === '1',
	gains: readGains(),
};

function persistEq() {
	try {
		localStorage.setItem(STORE.on, eq.on ? '1' : '0');
		localStorage.setItem(STORE.gains, JSON.stringify(eq.gains));
	} catch { /* storage quota */ }
}

/* ── The graph ────────────────────────────────────────────────────────────
 * 'idle'    → never attempted
 * 'ready'   → built and live
 * 'blocked' → this computer's audio cannot be routed through Web Audio here
 */

let context = null;
let source = null;
let filters = [];
let gainNode = null;
let limiter = null;
let graph = 'idle';
/* There are two ways to be blocked, and conflating them is what made one bad
 * moment disable boost and the equalizer for a whole session:
 *
 *  - This browser cannot build a graph at all. Permanent, so never retried.
 *  - This *track's* media refused a CORS read. Specific to that URL — an
 *    expired grant or a reconnect mid-probe looks exactly like this — so the
 *    next track deserves a fresh answer.
 */
let blockedPermanently = false;
let blockedFor = '';

export function graphState() {
	return graph;
}

/** True when volume above 100% is actually available right now. */
export function boostAvailable() {
	return graph === 'ready';
}

/* One byte is enough to learn whether the browser will let this page read the
 * response. A media element gives no such signal until it is already too late. */
async function corsAllows(url) {
	try {
		const response = await fetch(url, {
			method: 'GET',
			headers: { Range: 'bytes=0-0' },
			mode: 'cors',
			cache: 'no-store',
		});
		return response.ok || response.status === 206;
	} catch {
		return false;
	}
}

function buildGraph(audio) {
	const Context = window.AudioContext || window.webkitAudioContext;
	if (!Context) return false;
	try {
		context = context || new Context();
		source = context.createMediaElementSource(audio);

		let node = source;
		filters = EQ_BANDS.map(band => {
			const filter = context.createBiquadFilter();
			filter.type = band.type;
			filter.frequency.value = band.f;
			if (band.type === 'peaking') filter.Q.value = 1.0;
			filter.gain.value = 0;
			node.connect(filter);
			node = filter;
			return filter;
		});

		gainNode = context.createGain();
		gainNode.gain.value = 1;
		node.connect(gainNode);

		// Boost and a lifted EQ band both push past full scale, where samples
		// wrap and the result is audible distortion rather than volume. A gentle
		// limiter on the tail trades a little headroom for staying clean.
		limiter = context.createDynamicsCompressor();
		limiter.threshold.value = -2;
		limiter.knee.value = 6;
		limiter.ratio.value = 12;
		limiter.attack.value = 0.003;
		limiter.release.value = 0.25;
		gainNode.connect(limiter);
		limiter.connect(context.destination);
		return true;
	} catch {
		return false;
	}
}

/* Safari starts every context suspended until a gesture resumes it, and a
 * suspended context is silence with no error anywhere. */
export function resumeContext() {
	if (context?.state === 'suspended') context.resume().catch(() => {});
}

function applyGains() {
	if (graph !== 'ready') return;
	eq.gains.forEach((gain, index) => {
		const filter = filters[index];
		if (filter) filter.gain.value = eq.on ? clamp(gain, EQ_MIN, EQ_MAX) : 0;
	});
}

function applyVolume() {
	const wanted = clamp(Number(state.volume) || 0, 0, VOLUME_MAX);
	const audio = audioElement();
	if (graph === 'ready' && gainNode) {
		// The element stays at unity and the gain node carries the whole range,
		// so one control governs volume instead of two fighting over it.
		if (audio.volume !== 1) audio.volume = 1;
		gainNode.gain.value = wanted;
		return;
	}
	// No graph: the element is all there is, and it cannot exceed unity. On iOS
	// this assignment is ignored entirely, which is why the graph is worth having.
	try { audio.volume = Math.min(1, wanted); } catch { /* read-only on iOS */ }
}

/* Build the graph if this track's media allows it. Order matters: probe, then
 * flag, then build, then reload — so a phone that cannot support the graph never
 * gets one, and so never goes silent. */
async function ensureGraph() {
	if (graph === 'ready') return true;
	if (blockedPermanently) return false;

	const audio = audioElement();
	const url = audio.currentSrc || audio.src;
	// Nothing loaded yet, so nothing to probe and nothing to break. The caller's
	// setting stays on and this runs again when a track loads.
	if (!url) return false;

	// Same track, same refusal — no point probing it again. A different one is
	// a different question, and gets asked.
	if (graph === 'blocked' && blockedFor === url) return false;

	const alreadyCors = audio.crossOrigin === 'anonymous';

	if (!(await corsAllows(url))) {
		graph = 'blocked';
		blockedFor = url;
		if (alreadyCors) {
			// Playback was gambled on a CORS mode this computer turns out not to
			// grant. Give the attribute back and reload, or the music stays dead
			// for a feature the user is not even using.
			audio.removeAttribute('crossorigin');
			await reloadCurrent();
		}
		applyVolume();
		return false;
	}

	audio.crossOrigin = 'anonymous';
	if (!buildGraph(audio)) {
		// A graph this browser refuses to build is a property of the browser,
		// not of the track, so this one *does* stay blocked for good.
		graph = 'blocked';
		blockedPermanently = true;
		if (!alreadyCors) audio.removeAttribute('crossorigin');
		applyVolume();
		return false;
	}
	graph = 'ready';
	blockedFor = '';
	// Media fetched before the attribute was set is still the opaque copy, which
	// the graph would render as silence. Reloading refetches it readable.
	if (!alreadyCors) await reloadCurrent();
	applyGains();
	applyVolume();
	resumeContext();
	return true;
}

/* Built only when the user asked for something an element cannot do: the EQ, or
 * a non-unity volume. Deliberately not built merely because volume writes are
 * ignored — iOS makes them read-only, so that gave every iPhone a graph, and
 * iOS suspends a graph in the background, which stopped the music on lock. */
function graphIsWanted() {
	return eq.on || Number(state.volume) !== 1;
}

/* Restored settings that need the graph mean the very first track should be
 * fetched under CORS from the start. Without this every session would begin by
 * loading a track, discovering it cannot be routed, and reloading it — a restart
 * the user hears, once, for a setting they already made. */
/* Deliberately the cheap half of graphIsWanted(): probing whether volume writes
 * stick would touch the element while modules are still evaluating, and this
 * only needs to catch settings restored from a previous session. */
if (eq.on || Number(state.volume) !== 1) {
	try { audioElement().crossOrigin = 'anonymous'; } catch { /* element not ready */ }
}

/* A context suspended in the background stays suspended, and that is silence
 * with no error anywhere. */
document.addEventListener('visibilitychange', () => {
	if (!document.hidden) resumeContext();
});

/** Called by the player whenever a new track's media has been handed to the
 *  element, so a setting made with nothing playing takes effect on the first
 *  track that does. */
export async function onTrackLoaded() {
	if (graph === 'ready') { applyGains(); applyVolume(); resumeContext(); return; }
	// Deliberately not "blocked means give up": a new track is exactly the event
	// that deserves a second look, and ensureGraph knows which refusals are
	// worth retrying and which are final.
	if (blockedPermanently || !graphIsWanted()) { applyVolume(); return; }
	await ensureGraph();
}

/* ── Volume ───────────────────────────────────────────────────────────────*/

/** Set volume on a 0–2 scale, where 1 is unity. Values above 1 need the graph;
 *  asking for one builds it. Returns the volume actually in effect. */
export async function setVolume(value) {
	const wanted = clamp(Number(value) || 0, 0, VOLUME_MAX);
	state.volume = wanted;
	persist(STORAGE.volume, wanted);

	// Linked mode: the computer is making the sound, so this drives its volume
	// rather than a gain node that is not in the signal path.
	if (isLinked()) {
		remoteSetVolume(wanted);
		notifyPlayerChanged();
		return wanted;
	}

	// Not just "past unity" any more: on a platform that ignores the element's
	// own volume, the graph is the only thing that can carry the change at all.
	if (graph === 'idle' && graphIsWanted()) await ensureGraph();
	applyVolume();
	resumeContext();
	notifyPlayerChanged();
	return boostAvailable() ? wanted : Math.min(1, wanted);
}

export function volume() {
	return clamp(Number(state.volume) || 0, 0, VOLUME_MAX);
}

/* ── Equaliser ────────────────────────────────────────────────────────────*/

export function eqIsOn() {
	return eq.on;
}

export function eqGains() {
	return eq.gains.slice();
}

/** The name of the preset currently matched, or '' for a custom curve. */
export function eqPresetName() {
	for (const [name, gains] of Object.entries(EQ_PRESETS)) {
		if (gains.every((value, index) => value === eq.gains[index])) return name;
	}
	return '';
}

export function eqSummary() {
	if (!eq.on) return 'Off';
	return eqPresetName() || 'Custom';
}

export async function setEqOn(on) {
	eq.on = !!on;
	persistEq();
	if (!eq.on) { applyGains(); return true; }

	const ok = await ensureGraph();
	if (!ok && graph === 'blocked') {
		eq.on = false;
		persistEq();
		return false;
	}
	applyGains();
	return true;
}

export function setBandGain(index, value) {
	if (index < 0 || index >= EQ_BANDS.length) return;
	eq.gains = eq.gains.map((gain, position) => (
		position === index ? clamp(Number(value) || 0, EQ_MIN, EQ_MAX) : gain
	));
	persistEq();
	applyGains();
}

export function applyPreset(name) {
	const preset = EQ_PRESETS[name];
	if (!preset) return;
	eq.gains = preset.slice();
	persistEq();
	applyGains();
}

// Whatever was restored from storage applies to the element straight away, so a
// volume set last session is in effect before the first track even loads.
applyVolume();
