/**
 * Where Rainette's audio is actually coming out of.
 *
 * Two half-answers have to be joined to get a whole one, and neither is
 * sufficient alone:
 *
 *   The operating system (via the desktop bridge) knows the *names* — including
 *   the one the user gave their Bluetooth speaker — and which device is the
 *   system default. It cannot route this app's audio.
 *
 *   The browser (via enumerateDevices) knows the *sink ids* that setSinkId
 *   accepts, which is the only way to send one page's audio somewhere specific.
 *   But it withholds the labels until the page holds a media permission, so on
 *   its own it can offer nothing but "default" and a list of opaque hashes.
 *
 * So this module merges them: OS names for the list the user reads, browser ids
 * for the routing that list performs. When the two cannot be matched — no
 * labels, or an engine without setSinkId, which is every WKWebView — selecting a
 * device falls back to opening the system sound panel. That is a real answer
 * rather than a control that silently does nothing.
 */

/** setSinkId exists on Chromium/WebView2 and is absent on WKWebView. */
export function canRouteAudio(element) {
	return typeof element?.setSinkId === 'function';
}

/**
 * The browser's own output sinks. Labels are empty unless the page holds a
 * media permission, which Rainette deliberately does not request just to name a
 * speaker — an empty label here is expected, not a failure.
 */
export async function browserSinks() {
	if (!navigator.mediaDevices?.enumerateDevices) return [];
	try {
		const devices = await navigator.mediaDevices.enumerateDevices();
		return devices
			.filter(device => device.kind === 'audiooutput')
			.map(device => ({ deviceId: device.deviceId, label: device.label || '' }));
	} catch {
		return [];
	}
}

/* Endpoint names are decorated differently by each layer: Windows reports
 * "Headphones (WH-1000XM4 Stereo)" where CoreAudio reports "WH-1000XM4". Compare
 * on letters and digits only so the two still meet. */
function comparable(name) {
	return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function namesMatch(a, b) {
	const [x, y] = [comparable(a), comparable(b)];
	if (!x || !y) return false;
	return x === y || x.includes(y) || y.includes(x);
}

/**
 * Merge the OS device list with the browser's sinks.
 *
 * Every OS device is returned whether or not a sink could be matched, because
 * the name is worth showing even when this engine cannot route to it. A
 * `sinkId` of '' means "listed, but not routable from here".
 */
export function mergeOutputs(systemDevices, sinks) {
	const list = (Array.isArray(systemDevices) ? systemDevices : []).map(device => {
		const sink = sinks.find(candidate => namesMatch(candidate.label, device.name));
		return { ...device, sinkId: sink ? sink.deviceId : '' };
	});

	// A sink the OS list did not describe (a virtual device, or a name the two
	// layers spell too differently to match) is still selectable when it has a
	// usable label of its own.
	for (const sink of sinks) {
		if (sink.deviceId === 'default' || !sink.label) continue;
		if (list.some(device => device.sinkId === sink.deviceId)) continue;
		list.push({ id: `sink:${sink.deviceId}`, name: sink.label, kind: 'speaker', is_default: false, sinkId: sink.deviceId });
	}
	return list;
}

/** The icon name for a device's transport. */
export function outputIcon(kind) {
	switch (kind) {
		case 'bluetooth': return 'bluetooth';
		case 'headphones': return 'headphones';
		case 'builtin': return 'laptop';
		case 'hdmi': return 'hdmi';
		case 'airplay': return 'airplay';
		case 'phone': return 'phone';
		default: return 'speaker';
	}
}

/**
 * Point one media element at a sink.
 *
 * Resolves to false rather than throwing when the engine has no setSinkId or
 * the sink has gone away since it was listed — both are ordinary conditions
 * that the caller answers by pointing at the system sound panel instead.
 */
export async function routeElementTo(element, sinkId) {
	if (!canRouteAudio(element) || !sinkId) return false;
	try {
		await element.setSinkId(sinkId);
		return true;
	} catch {
		return false;
	}
}
