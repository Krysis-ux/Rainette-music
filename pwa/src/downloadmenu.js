/* What downloading looks like. `downloads.js` moves the bytes; this is the two places that offer
 * to. */

import { el, toast, tap } from './dom.js';
import { openSheet } from './sheets.js';
import { artistName } from './state.js';
import { isLocalTrack, formatBytes } from './local.js';
import {
	downloadTrack, downloadTracks, removeDownload, saveCopy,
	isDownloaded, isDownloading,
} from './downloads.js';

/* ── One track, from a menu ───────────────────────────────────────────────*/

/** The download rows for a track's action menu. */
export function trackDownloadItems(track, { onChanged } = {}) {
	if (!track) return [];
	const here = isDownloaded(track);
	const busy = isDownloading(track);

	const items = [];

	if (busy) {
		items.push({
			id: 'downloading',
			label: 'Downloading...',
			icon: 'download',
			disabled: true,
		});
	} else if (!here) {
		items.push({
			id: 'download',
			label: 'Download',
			icon: 'download',
			run: () => runTrackDownload(track).then(onChanged),
		});
	}

	items.push({
		id: 'save-copy',
		label: 'Save a copy',
		icon: 'download',
		run: () => runSaveCopy(track).then(onChanged),
	});

	/* Anything on the phone, imported files included. */
	if (here) {
		const stored = isLocalTrack(track);
		items.push({
			id: 'remove-download',
			label: stored ? 'Remove from this phone' : 'Remove download',
			icon: 'trash',
			danger: true,
			run: async () => {
				try {
					await removeDownload(track);
					toast(stored ? 'Removed from this phone' : 'Download removed', { icon: 'trash' });
				} catch (error) {
					toast(String(error?.message || 'Could not remove that download.'), { icon: 'trash' });
				}
				onChanged?.();
			},
		});
	}

	return items;
}

/* What the sheet says while there is nothing to measure. */
const PHASE_LABEL = {
	preparing: 'Preparing',
	connecting: 'Connecting to',
	// The link dropped and the download is resuming from where it stopped.
	// Worth naming: silence here is what "stuck" used to mean.
	reconnecting: 'Reconnecting for',
};

/** Download one track, saying what happened. Never rejects. */
export async function runTrackDownload(track) {
	if (isDownloaded(track)) { toast('Already on this phone', { icon: 'downloaded' }); return false; }
	toast(`Downloading ${track.title || 'track'}...`, { icon: 'download' });
	try {
		await downloadTrack(track);
		tap(6);
		toast('Saved to Local files', { icon: 'downloaded' });
		return true;
	} catch (error) {
		toast(String(error?.message || 'That download did not finish.'), { icon: 'download' });
		return false;
	}
}

/** Put a copy in the phone's own storage. Downloads it first if it is not here. */
export async function runSaveCopy(track) {
	try {
		const result = await saveCopy(track);
		if (result === 'cancelled') return false;
		tap(6);
		toast(result === 'saved' ? 'Saved' : 'Sent to your downloads', { icon: 'downloaded' });
		return true;
	} catch (error) {
		toast(String(error?.message || 'Could not save that file.'), { icon: 'download' });
		return false;
	}
}

/* ── A whole list ─────────────────────────────────────────────────────────*/

/** Download a playlist, an album, or any list of tracks, behind a sheet that shows what is
 * happening and can stop it. */
export function runListDownload(tracks, { title = 'Download' } = {}) {
	const all = [...(tracks || [])].filter(Boolean);
	const pending = all.filter(track => !isDownloaded(track));

	if (!all.length) { toast('Nothing to download', { icon: 'download' }); return Promise.resolve(null); }
	if (!pending.length) { toast('Already on this phone', { icon: 'downloaded' }); return Promise.resolve(null); }

	const controller = new AbortController();
	let settled = false;

	return new Promise(resolve => {
		openSheet({
			title,
			className: 'sheet-download',
			build: handle => {
				const { body } = handle;
				body.append(el('h2', 'sheet-title sheet-drag', title));

				const count = el('p', 'download-count', `${pending.length} track${pending.length === 1 ? '' : 's'} to fetch`);
				const now = el('p', 'download-now', 'Preparing…');

				// The number carries the precision; the bar carries the feel.
				const percent = el('span', 'download-percent', '0%');
				const head = el('div', 'download-head');
				head.append(count, percent);

				const bar = el('div', 'download-bar');
				const fill = el('i', 'download-fill');
				bar.append(fill);
				bar.setAttribute('role', 'progressbar');
				bar.setAttribute('aria-valuemin', '0');
				bar.setAttribute('aria-valuemax', String(pending.length));

				const stop = el('button', 'download-stop', 'Stop');
				stop.type = 'button';
				stop.addEventListener('click', () => {
					controller.abort();
					stop.disabled = true;
					stop.textContent = 'Stopping...';
				});

				body.append(head, bar, now, stop);

				/* The sheet being dismissed is not a cancel: a download the person walked away from should
				 * still finish. */
				const closeSheet = handle.close.bind(handle);
				handle.close = (fromHistory = false) => {
					if (!settled) toast('Still downloading in the background', { icon: 'download' });
					closeSheet(fromHistory);
				};

				downloadTracks(pending, {
					signal: controller.signal,
					onProgress: ({ done, total, track, ratio, phase, received, bytes }) => {
						// `done + ratio` reads as fractional progress through the list rather than a bar that jumps a
						// whole track at a time.
						const share = ratio == null ? 0 : ratio;
						const at = Math.min(total, done + share);
						const pct = total ? (at / total) * 100 : 0;
						fill.style.width = `${pct}%`;
						// Unknown size for the current track: keep the bar honest
						// about the tracks already finished, and let the label
						// carry the movement instead of faking a percentage.
						bar.classList.toggle('is-indeterminate', ratio == null && phase === 'downloading');
						bar.setAttribute('aria-valuenow', String(Math.round(at)));
						percent.textContent = `${Math.round(pct)}%`;
						count.textContent = `${done} of ${total} downloaded`;
						const who = artistName(track);
						const name = who ? `${track.title || 'Track'} — ${who}` : (track.title || 'Track');
						now.textContent = PHASE_LABEL[phase]
							? `${PHASE_LABEL[phase]} ${name}`
							: (ratio == null && received ? `${name} · ${formatBytes(received)}` : name);
					},
				}).then(result => {
					settled = true;
					resolve(result);
					handle.close();
					report(result);
				});
			},
		});
	});
}

function report({ done, failed, cancelled }) {
	tap(6);
	if (cancelled) {
		toast(done ? `Stopped — ${done} saved` : 'Stopped', { icon: 'download' });
		return;
	}
	if (failed && done) { toast(`${done} saved, ${failed} could not be fetched`, { icon: 'downloaded' }); return; }
	if (failed) { toast(`Could not download ${failed} track${failed === 1 ? '' : 's'}`, { icon: 'download' }); return; }
	toast(`${done} track${done === 1 ? '' : 's'} saved to Local files`, { icon: 'downloaded' });
}
