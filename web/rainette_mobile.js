const APK_URL = 'https://github.com/Krysis-ux/Rainette-music/releases/latest/download/rainette-music-android.apk';
const APK_NAME = 'rainette-music-android.apk';
const RELEASE_UNKNOWN = 'Release status unavailable here. Use the official GitHub link to check for the Android app.';

let mountedHost = null;
let pollTimer = null;
let countdownTimer = null;
let invitation = null;
let nativeStarted = false;
let managementInFlight = false;
let mountGeneration = 0;
let pywebviewReadyHandler = null;

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

function setPublication(message, published, generation, host) {
	if (!isCurrentMount(generation, host)) return;
	const publication = host.querySelector('#rwAndroidPublication');
	if (!publication) return;
	publication.textContent = message;
	publication.dataset.published = published;
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
		showQr(host.querySelector('#rwPairingQr'), result.pairing_qr_data_url, 'Secure Rainette pairing QR code', generation, host);
		startCountdown(generation, host);
		setStatus('Scan this code in the Rainette Android app, then approve the phone below.', 'success', generation, host);
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
			pendingHost.appendChild(deviceRow(request.device_name, 'Waiting for desktop approval', [
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
			pairedHost.appendChild(deviceRow(device.name, 'Allowed on this desktop', [
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

async function loadDownloadInfo(generation, host) {
	try {
		const info = await nativeCall('android_download_info');
		if (!isCurrentMount(generation, host)) return;
		const anchor = host.querySelector('#rwAndroidDownload');
		if (info?.url && anchor) anchor.href = info.url;
		showQr(host.querySelector('#rwInstallQr'), info?.install_qr_data_url, 'QR code to download Rainette Music for Android', generation, host);
		const releaseStatus = info?.status || (info?.published ? 'published' : 'unavailable');
		setPublication(
			releaseStatus === 'published'
				? 'The signed Android release is ready to download.'
				: releaseStatus === 'unavailable'
					? 'The Android app is not published yet. This button will work after the first signed release.'
					: 'Rainette could not check GitHub (network or certificate error). Open the official GitHub link to retry.',
			releaseStatus === 'published' ? 'true' : releaseStatus === 'unavailable' ? 'false' : 'unknown',
			generation,
			host,
		);
	} catch (error) {
		if (isCurrentMount(generation, host)) {
			setPublication('Could not check the Android release. Use the official GitHub link to try again.', 'unknown', generation, host);
			setStatus(error?.message || 'Could not check the Android release.', 'error', generation, host);
		}
	}
}

function startNativeFeatures(generation, host) {
	if (!isCurrentMount(generation, host) || nativeStarted || !nativeApi()) return;
	nativeStarted = true;
	const button = host.querySelector('#rwNewPairingCode');
	if (button) button.disabled = false;
	const fallback = host.querySelector('#rwNativePairingFallback');
	if (fallback) fallback.hidden = true;
	setPublication('Checking whether the Android release is published…', 'checking', generation, host);
	loadDownloadInfo(generation, host);
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
			<section class="rw-mobile-card rw-bubble" data-mobile-step="download">
				<div class="rw-mobile-step-title">1. Download</div>
				<h2>Get Rainette for Android</h2>
				<p>Download the signed APK from the official Rainette GitHub Release.</p>
				<a id="rwAndroidDownload" class="rw-btn rw-mobile-download" href="${APK_URL}" download="${APK_NAME}" target="_blank" rel="noopener noreferrer">Download APK</a>
				<p id="rwAndroidPublication" class="rw-mobile-note" data-published="unknown">${RELEASE_UNKNOWN}</p>
			</section>
			<section class="rw-mobile-card rw-bubble" data-mobile-step="install">
				<div class="rw-mobile-step-title">2. Install</div>
				<h2>Install it on your phone</h2>
				<div id="rwInstallQr" class="rw-mobile-qr-slot"><div class="rw-mobile-qr-placeholder">Open this page in Rainette desktop to show the install QR.</div></div>
				<p>Scan the download QR, open the APK, and allow installs from your browser or GitHub if Android asks.</p>
			</section>
			<section class="rw-mobile-card rw-mobile-card-pair rw-bubble" data-mobile-step="pair">
				<div class="rw-mobile-step-title">3. Pair</div>
				<div class="rw-mobile-pair-head">
					<div><h2>Connect to this desktop</h2><p>Keep the phone and desktop on the same Wi-Fi. Pairing codes last five minutes.</p></div>
					<button id="rwNewPairingCode" class="rw-btn" type="button" disabled>New pairing code</button>
				</div>
				<p id="rwNativePairingFallback" class="rw-mobile-native-fallback">Pairing requires the installed Rainette desktop app.</p>
				<div class="rw-mobile-pair-layout">
					<div>
						<div id="rwPairingQr" class="rw-mobile-qr-slot"><div class="rw-mobile-qr-placeholder">Create a code when your phone is ready.</div></div>
						<p id="rwPairingExpiry" class="rw-mobile-expiry">No active pairing code.</p>
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
	renderManagement({ pending: [], devices: [] }, generation, host);
	pywebviewReadyHandler = () => startNativeFeatures(generation, host);
	document.addEventListener('pywebviewready', pywebviewReadyHandler);
	startNativeFeatures(generation, host);
}

export function unmountMobile() {
	mountGeneration += 1;
	if (pollTimer) clearTimeout(pollTimer);
	if (countdownTimer) clearInterval(countdownTimer);
	if (pywebviewReadyHandler) document.removeEventListener('pywebviewready', pywebviewReadyHandler);
	pollTimer = null;
	countdownTimer = null;
	invitation = null;
	nativeStarted = false;
	managementInFlight = false;
	pywebviewReadyHandler = null;
	mountedHost = null;
}
