/* Newer versions of the app itself. A sideloaded app has no store to tell it.
 *
 * Android downloads the release APK and opens the system's own "update this
 * app?" screen; Android always asks. A sideloaded iPhone app cannot install
 * itself, so there the same call opens the download page.
 * (RainettePlayerPlugin.installUpdate, on each.)
 * Asked at most every few hours, and "Not now" holds until the next version. */

import { toast } from './dom.js';
import { confirmSheet } from './sheets.js';
import { STORAGE } from './state.js';
import { nativeAvailable, nativeInfo, nativeInstallUpdate } from './native-engine.js';
import { nativeRequest } from './ytmusic.js';

const LATEST = 'https://api.github.com/repos/Krysis-ux/Rainette-music/releases/latest';
const CHECK_EVERY_MS = 6 * 60 * 60 * 1000;
const ASSET = { android: 'rainette-music-android.apk', ios: 'rainette-music-ios.ipa' };
const RELEASE_VERSION = /^\d+\.\d+\.\d+$/;

/** True when `candidate` ("0.5.4") is a later release than `current`. */
export function isNewer(candidate, current) {
	if (!RELEASE_VERSION.test(candidate) || !RELEASE_VERSION.test(current)) return false;
	const [a, b] = [candidate, current].map(version => version.split('.').map(Number));
	for (let i = 0; i < 3; i += 1) if (a[i] !== b[i]) return a[i] > b[i];
	return false;
}

function remembered() {
	try { return JSON.parse(localStorage.getItem(STORAGE.update) || '{}') || {}; } catch { return {}; }
}

function remember(value) {
	try { localStorage.setItem(STORAGE.update, JSON.stringify(value)); } catch { /* quota */ }
}

export async function checkForUpdate() {
	if (!nativeAvailable()) return;
	const seen = remembered();
	if (Date.now() - (seen.at || 0) < CHECK_EVERY_MS) return;
	const info = await nativeInfo();
	// Debug and local builds carry no release version and are never offered one.
	if (!RELEASE_VERSION.test(String(info?.version || ''))) return;
	const answer = await nativeRequest({ url: LATEST, method: 'GET', headers: { Accept: 'application/vnd.github+json' } });
	remember({ ...seen, at: Date.now() });
	const release = typeof answer?.data === 'string' ? JSON.parse(answer.data) : answer?.data;
	const latest = String(release?.tag_name || '').replace(/^v/, '');
	if (answer?.status !== 200 || !isNewer(latest, info.version) || seen.skipped === latest) return;

	const android = globalThis.Capacitor?.getPlatform?.() === 'android';
	// The phone builds go up after the release itself; until then there is nothing to get.
	const asset = (release.assets || []).find(item => item?.name === ASSET[android ? 'android' : 'ios']);
	if (!asset) return;

	const go = await confirmSheet({ title: `Rainette ${latest}`, message: 'A new version is ready.', confirmLabel: 'Update', cancelLabel: 'Not now' });
	if (!go) { remember({ at: Date.now(), skipped: latest }); return; }
	if (android) toast('Downloading update…', { icon: 'download' });
	try {
		await nativeInstallUpdate(asset.browser_download_url);
	} catch (error) {
		toast(error?.message || 'The update didn’t download. Try again.', { icon: 'close' });
	}
}
