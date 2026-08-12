/* The camera half of pairing. The phone's own Camera app opens the browser,
 * which on iOS has different storage from the Home Screen icon, so that copy
 * never got paired. Chrome has BarcodeDetector; ./qr.js covers Safari. */

import { el } from './dom.js';
import { openSheet } from './sheets.js';
import { decodeImage } from './qr.js';

/* Big enough that a code fills sensible detail, small enough that a decode
 * costs a few milliseconds rather than a stutter. */
const FRAME_WIDTH = 480;
const SCAN_INTERVAL_MS = 120;

function cameraProblem(error) {
	const name = error?.name || '';
	if (name === 'NotAllowedError' || name === 'SecurityError') {
		return 'Rainette needs camera access to scan. Allow it in your browser’s settings for this site, then try again.';
	}
	if (name === 'NotFoundError' || name === 'OverconstrainedError') {
		return 'No camera was found on this phone. Paste the pairing link instead.';
	}
	if (name === 'NotReadableError') {
		return 'Another app is using the camera. Close it and try again.';
	}
	return error?.message || 'The camera could not be started.';
}

async function makeDetector() {
	if (!('BarcodeDetector' in window)) return null;
	try {
		const formats = await window.BarcodeDetector.getSupportedFormats?.();
		if (formats && !formats.includes('qr_code')) return null;
		return new window.BarcodeDetector({ formats: ['qr_code'] });
	} catch {
		return null;
	}
}

/** Resolves with the decoded text, or null if dismissed. Never rejects: a
 *  camera that will not start is a message on the sheet. */
export function openScanner() {
	return new Promise(resolve => {
		let answer = null;
		let stream = null;
		let timer = 0;
		let stopped = false;

		const stop = () => {
			stopped = true;
			clearTimeout(timer);
			for (const track of stream?.getTracks() || []) track.stop();
			stream = null;
		};

		const handle = openSheet({
			title: 'Scan the pairing code',
			className: 'sheet-scan',
			full: true,
			build: async ({ body, close }) => {
				const head = el('div', 'scan-head sheet-drag');
				head.append(el('h2', 'sheet-title', 'Scan the pairing code'));
				head.append(el('p', 'scan-hint', 'Point this at the QR code on your computer.'));

				const stage = el('div', 'scan-stage');
				const video = document.createElement('video');
				video.setAttribute('playsinline', '');   // iOS otherwise goes fullscreen
				video.muted = true;
				video.autoplay = true;
				const reticle = el('div', 'scan-reticle');
				reticle.setAttribute('aria-hidden', 'true');
				stage.append(video, reticle);

				const status = el('p', 'scan-status', 'Starting the camera…');
				status.setAttribute('role', 'status');

				const cancel = el('button', 'ghost', 'Enter the link instead');
				cancel.type = 'button';
				cancel.addEventListener('click', () => close());

				body.append(head, stage, status, cancel);

				try {
					stream = await navigator.mediaDevices.getUserMedia({
						video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 } },
						audio: false,
					});
				} catch (error) {
					status.textContent = cameraProblem(error);
					status.classList.add('is-error');
					stage.hidden = true;
					return;
				}
				if (stopped) { stop(); return; }

				video.srcObject = stream;
				try {
					await video.play();
				} catch {
					// Autoplay refused despite the gesture; the frames still arrive.
				}
				status.textContent = 'Looking for a code…';

				const detector = await makeDetector();
				const canvas = document.createElement('canvas');
				const context = canvas.getContext('2d', { willReadFrequently: true });

				const found = text => {
					answer = text;
					stop();
					close();
				};

				const tick = async () => {
					if (stopped || !handle.root.isConnected) return;
					const width = video.videoWidth;
					const height = video.videoHeight;
					if (!width || !height) { timer = setTimeout(tick, SCAN_INTERVAL_MS); return; }

					if (detector) {
						try {
							const codes = await detector.detect(video);
							const code = codes.find(item => item.rawValue);
							if (code) { found(code.rawValue); return; }
						} catch {
							// A detector that throws mid-stream is not worth a retry
							// loop; the bundled reader below covers the same frames.
						}
					}

					const scale = Math.min(1, FRAME_WIDTH / width);
					canvas.width = Math.round(width * scale);
					canvas.height = Math.round(height * scale);
					context.drawImage(video, 0, 0, canvas.width, canvas.height);
					try {
						const text = decodeImage(context.getImageData(0, 0, canvas.width, canvas.height));
						if (text) { found(text); return; }
					} catch {
						// A frame that cannot be read is the normal case, not a fault.
					}
					timer = setTimeout(tick, SCAN_INTERVAL_MS);
				};

				tick();
			},
		});

		new MutationObserver((_records, observer) => {
			if (handle.root.isConnected) return;
			observer.disconnect();
			stop();
			resolve(answer);
		}).observe(document.body, { childList: true });
	});
}

/** True when this browser can offer a scanner at all. */
export function scanningIsPossible() {
	return !!navigator.mediaDevices?.getUserMedia && window.isSecureContext !== false;
}
