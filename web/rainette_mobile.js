/* Settings → Mobile: pair a phone through the Rainette PWA. The desktop is the
 * authority — it mints a short-lived invitation and nothing is granted until
 * someone here approves. Each phone gets its own revocable credential. */

const DEFAULT_PWA_URL = 'https://music-pwa-web.vercel.app';

let mountedHost = null;
let pollTimer = null;
let countdownTimer = null;
let invitation = null;
let nativeStarted = false;
let managementInFlight = false;
let mountGeneration = 0;
let pywebviewReadyHandler = null;
let tunnelTimer = null;
let tunnelPhase = '';
let helperReady = false;
let helperRequired = true;
let providers = [];
let selectedProvider = '';
let providerConfig = {};
let setupStepInFlight = false;

// The first run downloads a helper binary, so the poll has to stay patient
// while the phase is "downloading" or "starting" and go quiet once it settles.
const TUNNEL_BUSY_POLL_MS = 1200;
const TUNNEL_IDLE_POLL_MS = 15000;
// A browser sign-in finishes on the person's schedule, not ours, and the only
// sign it is done is a file appearing. Keep watching rather than going idle, or
// the panel sits on "finish signing in" long after they have.
const TUNNEL_SETUP_POLL_MS = 2000;

// Every provider that needs something typed in, and what to call it on screen.
// A provider missing from here simply has no settings of its own.
//
// The Cloudflare fields are deliberately last and described as overrides: the
// guided steps fill both in, and somebody who has not pressed those buttons yet
// should read them as "you do not have to touch this", not as a form to
// complete before anything will work.
const PROVIDER_FIELDS = {
	'cloudflare-named': [
		{ key: 'hostname', label: 'Address (Rainette fills this in for you)', placeholder: 'music.example.com' },
		{ key: 'tunnel_name', label: 'Tunnel name (optional)', placeholder: 'rainette-my-computer' },
	],
	manual: [
		{ key: 'public_url', label: 'Your HTTPS address for this computer', placeholder: 'https://music-pc.example.com' },
	],
};

// The label for the *link* beside a step — the part of the job that is
// unavoidably the user's, done in a browser. The button that Rainette presses
// itself is labelled by the provider, through `setup_fix_label`.
const SETUP_ACTIONS = {
	install: 'Get it',
	signup: 'Create a free account',
	login: 'Create a free account',
	consent: 'Allow it',
	configure: 'Open the dashboard',
	provision: 'Open the dashboard',
};

function isCurrentMount(generation, host) {
	return generation === mountGeneration && !!host && mountedHost === host;
}

function nativeApi() {
	return window.pywebview?.api || null;
}

async function nativeCall(name, ...args) {
	const api = nativeApi();
	if (!api || typeof api[name] !== 'function') {
		throw new Error('Pairing requires the installed Rainette desktop app.');
	}
	return api[name](...args);
}

function setStatus(message, tone, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const status = host.querySelector('#rwMobileStatus');
	if (!status) return;
	status.textContent = message || '';
	status.dataset.tone = tone || '';
}

function showQr(slot, dataUrl, alt, generation, host) {
	if (!isCurrentMount(generation, host) || !slot) return;
	slot.innerHTML = '';
	if (!dataUrl) {
		const placeholder = document.createElement('div');
		placeholder.className = 'rw-mobile-qr-placeholder';
		placeholder.textContent = 'QR code unavailable';
		slot.appendChild(placeholder);
		return;
	}
	const image = document.createElement('img');
	image.className = 'rw-mobile-qr';
	image.alt = alt;
	image.src = dataUrl;
	image.addEventListener('error', () => {
		if (isCurrentMount(generation, host)) showQr(slot, '', alt, generation, host);
	}, { once: true });
	slot.appendChild(image);
}

function formatCountdown(generation, host) {
	if (!isCurrentMount(generation, host) || !invitation) return;
	const label = host.querySelector('#rwPairingExpiry');
	if (!label) return;
	const remaining = Math.max(0, Math.ceil(Number(invitation.expires_at) - Date.now() / 1000));
	label.dataset.remainingSeconds = String(remaining);
	if (remaining <= 0) {
		label.textContent = 'Pairing code expired. Create a new one.';
		label.dataset.expired = 'true';
		return;
	}
	const minutes = Math.floor(remaining / 60);
	const seconds = String(remaining % 60).padStart(2, '0');
	label.textContent = `Expires in ${minutes}:${seconds}`;
	label.dataset.expired = 'false';
}

function startCountdown(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	if (countdownTimer) clearInterval(countdownTimer);
	formatCountdown(generation, host);
	countdownTimer = setInterval(() => formatCountdown(generation, host), 1000);
}

async function createInvitation(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const button = host.querySelector('#rwNewPairingCode');
	if (button) button.disabled = true;
	setStatus('Starting the secure companion service…', '', generation, host);
	try {
		const result = await nativeCall('companion_create_invitation');
		if (!isCurrentMount(generation, host)) return;
		if (!result?.ok) throw new Error(result?.msg || 'Rainette could not create a pairing code.');
		invitation = result;
		showQr(host.querySelector('#rwPairingQr'), result.pairing_qr_data_url, 'Rainette pairing QR code', generation, host);
		startCountdown(generation, host);
		const link = host.querySelector('#rwPairingLink');
		if (link) {
			link.value = result.pairing_url || '';
			link.hidden = false;
		}
		const copyButton = host.querySelector('#rwCopyPairingLink');
		if (copyButton) copyButton.hidden = false;
		// A loopback endpoint means the phone would be told to call itself.
		// Saying so here is the difference between a message the user can act
		// on and the browser's bare "Failed to fetch" on the phone.
		setStatus(
			result.endpoint_is_local
				? 'This computer has no secure address yet, so this code only works in a browser on this computer. Generate an HTTPS tunnel in step 2, then create the code again.'
				: 'Scan this code with your phone camera, then approve it below.',
			result.endpoint_is_local ? 'error' : 'success',
			generation,
			host,
		);
	} catch (error) {
		if (isCurrentMount(generation, host)) {
			setStatus(error?.message || 'Rainette could not create a pairing code.', 'error', generation, host);
		}
	} finally {
		if (isCurrentMount(generation, host) && button) button.disabled = false;
	}
}

function deviceRow(name, detail, actions, generation, host) {
	const row = document.createElement('div');
	row.className = 'rw-mobile-device';
	const copy = document.createElement('div');
	copy.className = 'rw-mobile-device-copy';
	const title = document.createElement('strong');
	title.textContent = name || 'Unknown device';
	const meta = document.createElement('span');
	meta.textContent = detail;
	copy.append(title, meta);
	const controls = document.createElement('div');
	controls.className = 'rw-mobile-device-actions';
	for (const action of actions) {
		const button = document.createElement('button');
		button.type = 'button';
		button.className = action.primary ? 'rw-btn' : 'rw-btn rw-btn-ghost';
		button.textContent = action.label;
		button.addEventListener('click', () => manageDevice(button, action, generation, host));
		controls.appendChild(button);
	}
	row.append(copy, controls);
	return row;
}

async function manageDevice(button, action, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	button.disabled = true;
	setStatus(`${action.progress}…`, '', generation, host);
	try {
		const result = await nativeCall(action.method, action.id);
		if (!isCurrentMount(generation, host)) return;
		if (result === false || result?.ok === false) throw new Error(result?.msg || action.failure);
		setStatus(action.success, 'success', generation, host);
		await refreshManagementState(false, generation, host);
		if (!isCurrentMount(generation, host)) return;
	} catch (error) {
		if (isCurrentMount(generation, host)) setStatus(error?.message || action.failure, 'error', generation, host);
	} finally {
		if (isCurrentMount(generation, host)) button.disabled = false;
	}
}

function renderManagement(state, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const pendingHost = host.querySelector('#rwPendingDevices');
	const pairedHost = host.querySelector('#rwPairedDevices');
	if (!pendingHost || !pairedHost) return;
	pendingHost.innerHTML = '';
	pairedHost.innerHTML = '';
	const pending = Array.isArray(state?.pending) ? state.pending : [];
	const devices = Array.isArray(state?.devices) ? state.devices : [];
	if (!pending.length) {
		pendingHost.innerHTML = '<p class="rw-mobile-empty">No phones are waiting for approval.</p>';
	} else {
		for (const request of pending) {
			pendingHost.appendChild(deviceRow(request.device_name, 'Waiting for your approval', [
				{ label: 'Approve', primary: true, method: 'companion_approve_request', id: request.request_id, progress: 'Approving device', success: `${request.device_name || 'Device'} approved`, failure: 'Could not approve this device.' },
				{ label: 'Reject', method: 'companion_reject_request', id: request.request_id, progress: 'Rejecting request', success: 'Pairing request rejected', failure: 'Could not reject this request.' },
			], generation, host));
		}
	}
	const activeDevices = devices.filter(device => !device.revoked);
	if (!activeDevices.length) {
		pairedHost.innerHTML = '<p class="rw-mobile-empty">No phones are paired yet.</p>';
	} else {
		for (const device of activeDevices) {
			pairedHost.appendChild(deviceRow(device.name, 'Has its own listening session', [
				{ label: 'Revoke', method: 'companion_revoke_device', id: device.device_id, progress: 'Revoking device', success: `${device.name || 'Device'} revoked`, failure: 'Could not revoke this device.' },
			], generation, host));
		}
	}
}

function scheduleManagementPoll(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	if (pollTimer) clearTimeout(pollTimer);
	pollTimer = setTimeout(() => {
		pollTimer = null;
		refreshManagementState(true, generation, host);
	}, 2000);
}

async function refreshManagementState(schedule, generation, host) {
	if (!isCurrentMount(generation, host) || !nativeApi()) return;
	if (managementInFlight) {
		if (schedule) scheduleManagementPoll(generation, host);
		return;
	}
	managementInFlight = true;
	try {
		const state = await nativeCall('companion_management_state');
		if (!isCurrentMount(generation, host)) return;
		renderManagement(state, generation, host);
	} catch (error) {
		if (isCurrentMount(generation, host)) {
			setStatus(error?.message || 'Could not refresh companion devices.', 'error', generation, host);
		}
	} finally {
		if (isCurrentMount(generation, host)) {
			managementInFlight = false;
			if (schedule) scheduleManagementPoll(generation, host);
		}
	}
}

function setTunnelStatus(message, tone, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const status = host.querySelector('#rwTunnelStatus');
	if (!status) return;
	status.textContent = message || '';
	status.dataset.tone = tone || '';
	// An empty line still occupies its margins, which reads as a gap nobody put
	// there. This is what keeps the panel from growing a blank row whenever the
	// setup checklist above is already saying the same thing.
	status.hidden = !message;
}

function setHelperStatus(message, tone, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const status = host.querySelector('#rwHelperStatus');
	if (!status) return;
	status.textContent = message || '';
	status.dataset.tone = tone || '';
	// `.rw-mobile-status` reserves 19px so a one-line message does not make the
	// panel jump as it arrives. An empty one spends that height on nothing.
	status.hidden = !message;
}

function tunnelTone(phase) {
	if (phase === 'running') return 'success';
	if (phase === 'error') return 'error';
	return '';
}

// A provider that brings no binary of its own has nothing to download, and the
// panel must not ask for a step that does not exist.
function isHelperRequired(helper) {
	return helper?.required !== false;
}

function helperMessage(helper) {
	if (!isHelperRequired(helper)) return helper.message || 'This option needs no download.';
	if (helper.phase === 'ready') return 'The Cloudflare helper is ready on this computer.';
	if (helper.phase === 'error') return helper.message || 'The Cloudflare helper could not be downloaded.';
	if (helper.busy) return helper.message || 'Downloading the Cloudflare helper…';
	return 'The Cloudflare helper is not here yet — Rainette downloads it for you, once.';
}

function tunnelMessage(status) {
	const label = status.provider_label || 'This connection';
	const helper = status.helper;
	if (status.phase === 'setup') {
		// The checklist above is already showing this exact sentence next to the
		// button that acts on it. Repeating it here made the panel look broken —
		// two identical paragraphs, one of them detached from its own button.
		if (status.setup_message) return '';
		return `${label} needs a little setup on this computer first.`;
	}
	if (status.phase === 'running') return `${label} is live at ${status.url} — your phone can reach this computer.`;
	if (status.phase === 'error') return status.message || `${label} could not be started.`;
	if (status.busy) return status.message || 'Opening the connection…';
	if (isHelperRequired(helper) && !helper?.ready) return 'Download the Cloudflare helper first.';
	if (status.public_url && !status.public_url_is_managed) return `Using your own address: ${status.public_url}`;
	return `Ready to turn on ${label} for this computer.`;
}

// The honest version of the "Limited" / "High-quality" labels: what a person
// actually notices is whether they have to rescan the code after every restart.
function stabilityMessage(status) {
	if (!status.provider) return '';
	const address = status.stable_hostname
		? 'Address stays the same, so your phone only scans the code once.'
		: 'Address changes each time you restart Rainette, so your phone has to scan a new code.';
	return status.public === false
		? `${address} Reachable only from your own devices, never from the public internet.`
		: address;
}

function renderProviderDetail(status, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const description = host.querySelector('#rwProviderDescription');
	const stability = host.querySelector('#rwProviderStability');
	const chosen = providers.find(entry => entry.id === (status.provider || selectedProvider));
	if (description) description.textContent = chosen?.description || '';
	if (stability) stability.textContent = stabilityMessage(status);
}

// The setup step is rendered as a checklist with a button, never as an error:
// "install Tailscale" and "allow Funnel once" are things a person does, and a
// failure message is the one shape that makes them look impossible.
function renderSetupChecklist(status, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const block = host.querySelector('#rwTunnelSetup');
	const message = host.querySelector('#rwTunnelSetupMessage');
	const link = host.querySelector('#rwTunnelSetupLink');
	if (!block || !message || !link) return;
	const action = status.setup_action || '';
	const text = status.setup_message || '';
	block.hidden = !action && !text;
	message.textContent = text;
	message.hidden = !text;

	// The second line carries the "why" — what the step costs, what it needs —
	// so the primary message can stay one short sentence.
	const detail = host.querySelector('#rwTunnelSetupDetail');
	if (detail) {
		detail.textContent = status.setup_detail || '';
		detail.hidden = !status.setup_detail;
	}

	// The step Rainette can take itself is a button that acts. Everything the
	// user has to do in a browser stays a link beside it, so the two are never
	// confused for one another.
	const fix = host.querySelector('#rwTunnelSetupFix');
	if (fix) {
		const canFix = !!status.setup_can_fix && !setupStepInFlight;
		fix.hidden = !status.setup_can_fix;
		fix.disabled = !canFix;
		fix.textContent = setupStepInFlight
			? 'Working…'
			: status.setup_fix_label || 'Do this for me';
		fix.dataset.step = action;
	}

	// The URL can come from a helper's own output, so it is placed as an
	// attribute after a scheme check rather than interpolated into markup.
	const url = String(status.setup_url || '');
	const safe = url.startsWith('https://') ? url : '';
	link.hidden = !safe || !SETUP_ACTIONS[action];
	link.textContent = SETUP_ACTIONS[action] || '';
	if (safe) link.setAttribute('href', safe);
	else link.removeAttribute('href');
}

/* Carry out the one step the provider said Rainette could take.
 *
 * Signing in is the case that shapes this: `cloudflared tunnel login` does not
 * return until the person has finished in the browser, so the call is fired and
 * the panel goes back to polling rather than awaiting a result that is minutes
 * away. Every step therefore reports through `tunnel_status` like all the rest.
 */
async function runSetupStep(generation, host) {
	if (setupStepInFlight || !isCurrentMount(generation, host)) return;
	const button = host.querySelector('#rwTunnelSetupFix');
	const step = button?.dataset.step || '';
	if (!step) return;
	setupStepInFlight = true;
	if (button) {
		button.disabled = true;
		button.textContent = 'Working…';
	}
	try {
		const result = await nativeCall('tunnel_setup_step', step, {});
		if (!result?.ok) throw new Error(result?.msg || 'That step could not be finished.');
	} catch (error) {
		if (isCurrentMount(generation, host)) {
			setTunnelStatus(error?.message || 'That step could not be finished.', 'error', generation, host);
		}
	} finally {
		setupStepInFlight = false;
		if (isCurrentMount(generation, host)) refreshTunnel(generation, host);
	}
}

/* The step panels are drawn as filled, padded cards, so one whose contents have
 * all been hidden still paints a grey rounded box with nothing in it. Several
 * of them stack up on a provider that needs no download and has no settings —
 * a column of empty boxes under the picker. A container is only a container
 * when something is actually in it. */
function hideEmptyStepPanels(host) {
	for (const panel of host.querySelectorAll('.rw-mobile-tunnel-steps')) {
		const hasVisibleChild = [...panel.children].some(child => (
			!child.hidden && (child.textContent.trim() !== '' || child.tagName === 'INPUT' || child.querySelector('input'))
		));
		panel.hidden = !hasVisibleChild;
	}
}

function renderTunnel(status, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	tunnelPhase = status.phase || '';
	const helper = status.helper || { phase: 'missing', ready: false, busy: false };
	helperReady = !!helper.ready;
	helperRequired = isHelperRequired(helper);
	if (status.provider) selectedProvider = status.provider;

	const picker = host.querySelector('#rwTunnelProvider');
	if (picker && status.provider && picker.value !== status.provider) picker.value = status.provider;

	const download = host.querySelector('#rwDownloadHelper');
	if (download) {
		// When the wizard is already offering this exact step, its button is the
		// one to press. Two buttons that do the same thing is the confusion this
		// panel exists to remove, so the older one steps aside.
		const wizardOffersDownload = status.setup_action === 'install' && !!status.setup_can_fix;
		download.hidden = !helperRequired || wizardOffersDownload;
		download.disabled = !!helper.busy || helper.ready;
		download.textContent = helper.busy
			? 'Downloading…'
			: helper.ready
				? 'Helper installed'
				: helper.phase === 'error'
					? 'Retry the download'
					: 'Download the Cloudflare helper';
	}
	const helperLine = host.querySelector('#rwHelperStatus');
	if (helperLine) helperLine.hidden = !helperRequired;

	const toggle = host.querySelector('#rwTunnelToggle');
	if (toggle) {
		toggle.disabled = !!status.busy || (helperRequired && !helper.ready && !status.running);
		const startLabel = helperRequired ? 'Generate HTTPS tunnel' : 'Turn this connection on';
		toggle.textContent = status.running
			? (helperRequired ? 'Stop the HTTPS tunnel' : 'Turn this connection off')
			: status.busy
				? 'Working…'
				: startLabel;
	}

	// The generated address lands in the same field somebody would paste their
	// own address into, so what pairing will actually use is always on screen.
	const publicInput = host.querySelector('#rwPublicUrl');
	if (publicInput && document.activeElement !== publicInput) {
		publicInput.value = status.url || status.public_url || '';
	}

	renderProviderDetail(status, generation, host);
	renderSetupChecklist(status, generation, host);
	setHelperStatus(
		helperMessage(helper),
		helper.phase === 'error' ? 'error' : helper.ready ? 'success' : '',
		generation,
		host,
	);
	setTunnelStatus(tunnelMessage(status), tunnelTone(status.phase), generation, host);
	// Last, so it sees the final visibility of everything above it.
	hideEmptyStepPanels(host);
}

function renderProviderOptions(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const picker = host.querySelector('#rwTunnelProvider');
	if (!picker) return;
	picker.innerHTML = '';
	for (const entry of providers) {
		const option = document.createElement('option');
		option.value = entry.id;
		option.textContent = entry.recommended ? `${entry.label} (recommended)` : entry.label;
		picker.appendChild(option);
	}
	if (selectedProvider) picker.value = selectedProvider;
}

function renderProviderConfig(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const block = host.querySelector('#rwProviderConfig');
	if (!block) return;
	block.innerHTML = '';
	const fields = PROVIDER_FIELDS[selectedProvider] || [];
	block.hidden = !fields.length;
	if (!fields.length) return;
	for (const field of fields) {
		const label = document.createElement('label');
		label.className = 'rw-mobile-field';
		const caption = document.createElement('span');
		caption.textContent = field.label;
		const input = document.createElement('input');
		input.type = 'text';
		input.spellcheck = false;
		input.dataset.providerKey = field.key;
		input.placeholder = field.placeholder;
		input.value = String(providerConfig[field.key] || '');
		label.append(caption, input);
		block.appendChild(label);
	}
	const save = document.createElement('button');
	save.type = 'button';
	save.className = 'rw-btn rw-btn-ghost';
	save.textContent = 'Save these details';
	save.addEventListener('click', () => applyProvider(selectedProvider, generation, host));
	block.appendChild(save);
}

function readProviderConfig(host) {
	const settings = {};
	for (const input of host.querySelectorAll('#rwProviderConfig input[data-provider-key]')) {
		settings[input.dataset.providerKey] = input.value.trim();
	}
	return settings;
}

async function applyProvider(providerId, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	setTunnelStatus('Saving this choice…', '', generation, host);
	try {
		const settings = readProviderConfig(host);
		const result = await nativeCall('tunnel_set_provider', providerId, settings);
		if (!isCurrentMount(generation, host)) return;
		if (!result?.ok) throw new Error(result?.msg || 'Rainette could not switch to that option.');
		selectedProvider = result.provider || providerId;
		providerConfig = settings;
		renderProviderConfig(generation, host);
		// Ask what is still missing straight away, so the checklist appears
		// without the user having to press "turn it on" to discover it.
		await refreshPreflight(generation, host);
	} catch (error) {
		if (isCurrentMount(generation, host)) {
			setTunnelStatus(error?.message || 'Rainette could not switch to that option.', 'error', generation, host);
		}
	}
}

async function refreshPreflight(generation, host) {
	if (!isCurrentMount(generation, host) || !nativeApi()) return;
	try {
		await nativeCall('tunnel_preflight');
	} catch {
		/* Preflight is advisory; the status poll below still drives the panel. */
	}
	// Redraw from tunnel_status rather than the preflight reply: only the
	// status call carries the helper state the buttons are gated on.
	await refreshTunnel(generation, host);
}

async function loadProviders(generation, host) {
	if (!isCurrentMount(generation, host) || !nativeApi()) return;
	try {
		const result = await nativeCall('tunnel_providers');
		if (!isCurrentMount(generation, host) || !result?.ok) return;
		providers = Array.isArray(result.providers) ? result.providers : [];
		selectedProvider = result.selected || selectedProvider;
		providerConfig = result.config && typeof result.config === 'object' ? result.config : {};
		renderProviderOptions(generation, host);
		renderProviderConfig(generation, host);
	} catch {
		/* Without the desktop bridge the picker stays empty and inert. */
	}
}

function scheduleTunnelPoll(generation, host, delayMs) {
	if (!isCurrentMount(generation, host)) return;
	if (tunnelTimer) clearTimeout(tunnelTimer);
	tunnelTimer = setTimeout(() => {
		tunnelTimer = null;
		refreshTunnel(generation, host);
	}, delayMs);
}

async function refreshTunnel(generation, host) {
	if (!isCurrentMount(generation, host) || !nativeApi()) return;
	try {
		const status = await nativeCall('tunnel_status');
		if (!isCurrentMount(generation, host)) return;
		if (!status?.ok) throw new Error(status?.msg || 'The tunnel status is unavailable.');
		renderTunnel(status, generation, host);
		const working = status.busy || status.helper?.busy;
		// A pending setup step is watched more closely than an idle tunnel but
		// less anxiously than a download: the thing being waited on is a person.
		const awaitingSetup = !!status.setup_action && !working;
		scheduleTunnelPoll(
			generation,
			host,
			working ? TUNNEL_BUSY_POLL_MS : awaitingSetup ? TUNNEL_SETUP_POLL_MS : TUNNEL_IDLE_POLL_MS,
		);
	} catch (error) {
		if (!isCurrentMount(generation, host)) return;
		setTunnelStatus(error?.message || 'The tunnel status is unavailable.', 'error', generation, host);
		scheduleTunnelPoll(generation, host, TUNNEL_IDLE_POLL_MS);
	}
}

async function downloadHelper(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const button = host.querySelector('#rwDownloadHelper');
	if (button) button.disabled = true;
	setHelperStatus('Starting the download…', '', generation, host);
	try {
		const result = await nativeCall('tunnel_helper_download');
		if (!isCurrentMount(generation, host)) return;
		if (!result?.ok) throw new Error(result?.msg || 'cloudflared could not be downloaded.');
		// The download runs on the desktop side, so switch to the fast poll and
		// let its progress stream back instead of blocking this call on it.
		scheduleTunnelPoll(generation, host, TUNNEL_BUSY_POLL_MS);
	} catch (error) {
		if (!isCurrentMount(generation, host)) return;
		setHelperStatus(error?.message || 'cloudflared could not be downloaded.', 'error', generation, host);
		if (button) button.disabled = false;
	}
}

async function toggleTunnel(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const toggle = host.querySelector('#rwTunnelToggle');
	const stopping = tunnelPhase === 'running';
	if (!stopping && helperRequired && !helperReady) {
		setTunnelStatus('Download the Cloudflare helper first.', 'error', generation, host);
		return;
	}
	if (toggle) toggle.disabled = true;
	setTunnelStatus(stopping ? 'Closing the connection…' : 'Opening the connection…', '', generation, host);
	try {
		const result = await nativeCall(stopping ? 'tunnel_stop' : 'tunnel_start');
		if (!isCurrentMount(generation, host)) return;
		if (!result?.ok) throw new Error(result?.msg || 'The connection could not be started.');
		scheduleTunnelPoll(generation, host, TUNNEL_BUSY_POLL_MS);
	} catch (error) {
		if (!isCurrentMount(generation, host)) return;
		setTunnelStatus(error?.message || 'The connection could not be started.', 'error', generation, host);
		if (toggle) toggle.disabled = false;
	}
}

async function loadConfig(generation, host) {
	try {
		const config = await nativeCall('pwa_config_get');
		if (!isCurrentMount(generation, host) || !config?.ok) return;
		const pwaInput = host.querySelector('#rwPwaUrl');
		const publicInput = host.querySelector('#rwPublicUrl');
		if (pwaInput) pwaInput.value = config.pwa_url || DEFAULT_PWA_URL;
		if (publicInput) publicInput.value = config.public_url || '';
	} catch {
		/* The desktop bridge is unavailable in a plain browser; inputs stay blank. */
	}
}

async function saveConfig(generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const button = host.querySelector('#rwSavePwaConfig');
	if (button) button.disabled = true;
	setStatus('Saving addresses…', '', generation, host);
	try {
		const result = await nativeCall(
			'pwa_config_set',
			host.querySelector('#rwPwaUrl')?.value || '',
			host.querySelector('#rwPublicUrl')?.value || '',
		);
		if (!isCurrentMount(generation, host)) return;
		if (!result?.ok) throw new Error(result?.msg || 'Could not save these addresses.');
		setStatus('Addresses saved. Create a new pairing code to use them.', 'success', generation, host);
		refreshTunnel(generation, host);
	} catch (error) {
		if (isCurrentMount(generation, host)) {
			setStatus(error?.message || 'Could not save these addresses.', 'error', generation, host);
		}
	} finally {
		if (isCurrentMount(generation, host) && button) button.disabled = false;
	}
}

function startNativeFeatures(generation, host) {
	if (!isCurrentMount(generation, host) || nativeStarted || !nativeApi()) return;
	nativeStarted = true;
	const button = host.querySelector('#rwNewPairingCode');
	if (button) button.disabled = false;
	const fallback = host.querySelector('#rwNativePairingFallback');
	if (fallback) fallback.hidden = true;
	loadConfig(generation, host);
	// The picker has to be populated before the first status lands, or the
	// first render has no label to show for whichever provider is selected.
	loadProviders(generation, host).then(() => refreshTunnel(generation, host));
	refreshManagementState(true, generation, host);
}

export function renderMobile(host) {
	unmountMobile();
	if (!host) return;
	const generation = mountGeneration;
	mountedHost = host;
	managementInFlight = false;
	host.innerHTML = `
		<div class="rw-mobile-grid">
			<section class="rw-mobile-card rw-bubble" data-mobile-step="open">
				<div class="rw-mobile-step-title">1. Open</div>
				<h2>Rainette on your phone</h2>
				<p>Rainette runs on your phone as a web app — iPhone or Android, nothing to install from a store. Your music still plays from this computer.</p>
				<a id="rwPwaLink" class="rw-btn rw-mobile-download" href="${DEFAULT_PWA_URL}" target="_blank" rel="noopener noreferrer">Open the Rainette PWA</a>
				<p class="rw-mobile-note">On the phone, use your browser's <b>Share → Add to Home Screen</b> to keep it one tap away.</p>
			</section>
			<section class="rw-mobile-card rw-bubble" data-mobile-step="reach">
				<div class="rw-mobile-step-title">2. Make this computer reachable</div>
				<h2>Secure address</h2>
				<p>Your phone talks straight to this computer, so this computer needs an address on the internet that uses HTTPS. Rainette can make one for you, or use one you already have.</p>
					<label class="rw-mobile-field">
						<span>How your phone reaches this computer</span>
						<select id="rwTunnelProvider" style="min-width:0;border:1px solid var(--rw-border);border-radius:11px;padding:10px 12px;background:var(--rw-bg);color:var(--rw-text);font:inherit;font-size:13px;"></select>
					</label>
					<p id="rwProviderDescription" class="rw-mobile-note"></p>
					<p id="rwProviderStability" class="rw-mobile-note"></p>
					<div id="rwProviderConfig" class="rw-mobile-tunnel-steps" hidden></div>
					<div id="rwTunnelSetup" class="rw-mobile-tunnel-steps" hidden>
						<p id="rwTunnelSetupMessage" class="rw-mobile-status" role="status" aria-live="polite"></p>
						<p id="rwTunnelSetupDetail" class="rw-mobile-note" hidden></p>
						<button id="rwTunnelSetupFix" class="rw-btn" type="button" hidden></button>
						<a id="rwTunnelSetupLink" class="rw-btn rw-btn-ghost" target="_blank" rel="noopener noreferrer" hidden></a>
					</div>
				<div class="rw-mobile-tunnel-steps">
					<button id="rwDownloadHelper" class="rw-btn" type="button" disabled>Download cloudflared</button>
					<p id="rwHelperStatus" class="rw-mobile-status" role="status" aria-live="polite">Checking for cloudflared…</p>
					<button id="rwTunnelToggle" class="rw-btn" type="button" disabled>Generate HTTPS tunnel</button>
					<p id="rwTunnelStatus" class="rw-mobile-status" role="status" aria-live="polite">Checking the secure address…</p>
				</div>
				<label class="rw-mobile-field">
					<span>Public address for this computer</span>
					<input id="rwPublicUrl" type="url" inputmode="url" spellcheck="false" placeholder="https://music-pc.example.com">
				</label>
				<label class="rw-mobile-field">
					<span>Rainette PWA address</span>
					<input id="rwPwaUrl" type="url" inputmode="url" spellcheck="false" placeholder="${DEFAULT_PWA_URL}">
				</label>
				<button id="rwSavePwaConfig" class="rw-btn rw-btn-ghost" type="button">Save addresses</button>
				<p class="rw-mobile-note">Generating a tunnel fills the public address in for you and saves it. Paste your own here instead if you already run a named Cloudflare tunnel, Tailscale Funnel, or a reverse proxy. Never port-forward the companion port on your router.</p>
			</section>
			<section class="rw-mobile-card rw-mobile-card-pair rw-bubble" data-mobile-step="pair">
				<div class="rw-mobile-step-title">3. Pair</div>
				<div class="rw-mobile-pair-head">
					<div><h2>Connect a phone</h2><p>Each phone gets its own listening session, so two people never interrupt each other. Pairing codes last five minutes.</p></div>
					<button id="rwNewPairingCode" class="rw-btn" type="button" disabled>New pairing code</button>
				</div>
				<p id="rwNativePairingFallback" class="rw-mobile-native-fallback">Pairing requires the installed Rainette desktop app.</p>
				<div class="rw-mobile-pair-layout">
					<div>
						<div id="rwPairingQr" class="rw-mobile-qr-slot"><div class="rw-mobile-qr-placeholder">Create a code when your phone is ready.</div></div>
						<p id="rwPairingExpiry" class="rw-mobile-expiry">No active pairing code.</p>
						<input id="rwPairingLink" class="rw-mobile-link" type="text" readonly hidden aria-label="Pairing link">
						<button id="rwCopyPairingLink" class="rw-btn rw-btn-ghost" type="button" hidden>Copy pairing link</button>
						<p id="rwMobileStatus" class="rw-mobile-status" role="status" aria-live="polite"></p>
					</div>
					<div class="rw-mobile-management">
						<h3>Waiting for approval</h3><div id="rwPendingDevices"></div>
						<h3>Paired phones</h3><div id="rwPairedDevices"></div>
					</div>
				</div>
			</section>
		</div>`;
	host.querySelector('#rwNewPairingCode')?.addEventListener('click', () => createInvitation(generation, host));
	host.querySelector('#rwSavePwaConfig')?.addEventListener('click', () => saveConfig(generation, host));
	host.querySelector('#rwDownloadHelper')?.addEventListener('click', () => downloadHelper(generation, host));
	host.querySelector('#rwTunnelSetupFix')?.addEventListener('click', () => runSetupStep(generation, host));
	host.querySelector('#rwTunnelToggle')?.addEventListener('click', () => toggleTunnel(generation, host));
	host.querySelector('#rwTunnelProvider')?.addEventListener('change', event => {
		selectedProvider = event.target.value || '';
		// Redraw the settings first so the details typed for the *new* provider
		// are what gets saved, never the previous one's.
		renderProviderConfig(generation, host);
		applyProvider(selectedProvider, generation, host);
	});
	host.querySelector('#rwCopyPairingLink')?.addEventListener('click', async () => {
		const field = host.querySelector('#rwPairingLink');
		if (!field?.value) return;
		try {
			await navigator.clipboard.writeText(field.value);
			setStatus('Pairing link copied.', 'success', generation, host);
		} catch {
			// Clipboard access can be denied; the link is selectable either way.
			field.select();
			setStatus('Press Ctrl+C to copy the selected link.', '', generation, host);
		}
	});
	renderManagement({ pending: [], devices: [] }, generation, host);
	pywebviewReadyHandler = () => startNativeFeatures(generation, host);
	document.addEventListener('pywebviewready', pywebviewReadyHandler);
	startNativeFeatures(generation, host);
}

export function unmountMobile() {
	mountGeneration += 1;
	if (pollTimer) clearTimeout(pollTimer);
	if (countdownTimer) clearInterval(countdownTimer);
	if (tunnelTimer) clearTimeout(tunnelTimer);
	if (pywebviewReadyHandler) document.removeEventListener('pywebviewready', pywebviewReadyHandler);
	pollTimer = null;
	countdownTimer = null;
	tunnelTimer = null;
	tunnelPhase = '';
	helperReady = false;
	helperRequired = true;
	providers = [];
	selectedProvider = '';
	providerConfig = {};
	invitation = null;
	nativeStarted = false;
	managementInFlight = false;
	pywebviewReadyHandler = null;
	mountedHost = null;
}
