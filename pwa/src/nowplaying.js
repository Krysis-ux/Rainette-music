/* The now-playing card: what the mini bar expands into.
 *
 * Same contents in the same order as the desktop's Now Playing view, so the two
 * devices are not two layouts. Phone-specific: the artwork tints the sheet, and
 * the scrubber is a real range input, because 2px is a fine cursor target and a
 * useless thumb one.
 */

import { $, el, icon, iconButton, tap, toast, motionOff } from './dom.js';
import { state, artworkUrl, artistName, formatTime, REPEAT_LABEL } from './state.js';
import { openSheet } from './sheets.js';
import { openQueueSheet } from './queue.js';
import { openAddToPlaylist, openLyrics, openSleepTimer, openOutputPicker, openTrackMenu, sleepLabel } from './extras.js';
import {
	currentTrack, isPlaying, isLoading, currentTime, duration, isLinked,
	toggle, skip, seekTo, setVolume, cycleRepeat, toggleShuffle, subscribe,
} from './player.js';

let openHandle = null;

export function isNowPlayingOpen() {
	return !!openHandle?.root?.isConnected;
}

export function openNowPlaying() {
	if (!currentTrack()) return;
	if (isNowPlayingOpen()) return;

	openHandle = openSheet({
		title: 'Now playing',
		className: 'sheet-now',
		full: true,
		build: handle => {
			const body = handle.body;

			const top = el('div', 'now-top sheet-drag');
			top.append(el('span', 'now-eyebrow', isLinked() ? `Playing on ${state.computerName || 'your computer'}` : 'Playing from your computer'));
			top.append(iconButton('chevronDown', {
				label: 'Close now playing',
				className: 'now-close',
				onClick: () => handle.close(),
			}));

			const artShell = el('div', 'now-art-shell');
			const art = document.createElement('img');
			art.className = 'now-art';
			art.alt = '';
			art.referrerPolicy = 'no-referrer';
			art.decoding = 'async';
			artShell.append(art);

			const meta = el('div', 'now-meta');
			const title = el('h2', 'now-title');
			const artist = el('p', 'now-artist');
			meta.append(title, artist);

			// ── Scrubber ────────────────────────────────────────────────────
			const seekWrap = el('div', 'now-seek');
			const seekTrack = el('div', 'now-seek-track');
			seekTrack.innerHTML = '<span></span>';
			const seek = document.createElement('input');
			seek.type = 'range';
			seek.className = 'now-seek-input';
			seek.min = '0';
			seek.max = '0';
			seek.step = '0.1';
			seek.value = '0';
			seek.setAttribute('aria-label', 'Seek');
			seekWrap.append(seekTrack, seek);

			const times = el('div', 'now-times');
			const elapsed = el('span', '', '0:00');
			const total = el('span', '', '0:00');
			times.append(elapsed, total);

			// Dragging must not fight the incoming timeupdate stream, so while a
			// scrub is in progress the display follows the thumb, not the audio.
			let scrubbing = false;
			seek.addEventListener('pointerdown', () => { scrubbing = true; });
			seek.addEventListener('input', () => {
				elapsed.textContent = formatTime(Number(seek.value));
				paintSeek(seekTrack, Number(seek.value), Number(seek.max));
			});
			const commitSeek = () => {
				if (!scrubbing) return;
				scrubbing = false;
				seekTo(Number(seek.value));
			};
			seek.addEventListener('change', commitSeek);
			seek.addEventListener('pointerup', commitSeek);
			seek.addEventListener('pointercancel', () => { scrubbing = false; });

			// ── Transport ───────────────────────────────────────────────────
			const transport = el('div', 'now-transport');
			const shuffleBtn = iconButton('shuffle', {
				label: 'Shuffle queue',
				className: 'now-btn',
				onClick: () => { const on = toggleShuffle(); tap(); toast(on ? 'Queue shuffled' : 'Original order restored', { icon: 'shuffle' }); render(); },
			});
			const prevBtn = iconButton('prev', { label: 'Previous track', className: 'now-btn', size: 26, onClick: () => skip(-1).catch(() => {}) });
			const playBtn = el('button', 'now-play');
			playBtn.type = 'button';
			const nextBtn = iconButton('next', { label: 'Next track', className: 'now-btn', size: 26, onClick: () => skip(1).catch(() => {}) });
			const repeatBtn = iconButton('loop', {
				label: REPEAT_LABEL.off,
				className: 'now-btn',
				onClick: () => { const mode = cycleRepeat(); tap(); toast(REPEAT_LABEL[mode], { icon: mode === 'one' ? 'loopOne' : 'loop' }); render(); },
			});
			playBtn.addEventListener('click', () => { toggle(); tap(); });
			transport.append(shuffleBtn, prevBtn, playBtn, nextBtn, repeatBtn);

			// ── Volume ──────────────────────────────────────────────────────
			const volumeRow = el('div', 'now-volume');
			const volumeIcon = el('span', 'now-volume-icon');
			const volume = document.createElement('input');
			volume.type = 'range';
			volume.min = '0';
			volume.max = '100';
			volume.step = '1';
			volume.value = String(Math.round(state.volume * 100));
			volume.setAttribute('aria-label', 'Volume');
			volume.addEventListener('input', () => {
				setVolume(Number(volume.value) / 100);
				volumeIcon.innerHTML = icon(Number(volume.value) ? 'volume' : 'volumeOff', 18);
				volume.setAttribute('aria-valuetext', `${volume.value}%`);
			});
			volumeIcon.innerHTML = icon(state.volume ? 'volume' : 'volumeOff', 18);
			volumeRow.append(volumeIcon, volume);

			// ── Actions ─────────────────────────────────────────────────────
			const actions = el('div', 'now-actions');
			const sleepAction = labelledAction('moon', sleepLabel(), () => openSleepTimer().then(render));
			actions.append(
				labelledAction('queue', 'Queue', openQueueSheet),
				// "Add to playlist" is the desktop's wording; five of those across a
				// phone's width truncates every one of them, so the label is the
				// noun and the full phrase stays as the accessible name.
				labelledAction('listAdd', 'Playlist', () => { const track = currentTrack(); if (track) openAddToPlaylist(track); }, 'Add to playlist'),
				labelledAction('mic', 'Lyrics', () => { const track = currentTrack(); if (track) openLyrics(track); }),
				sleepAction,
				labelledAction('more', 'More', () => { const track = currentTrack(); if (track) openTrackMenu(track); }),
			);

			// "Play on" sits with the artwork rather than in the action row: it
			// answers "where is this coming out", which belongs beside what is
			// playing, not among the things you can do to it.
			const outputRow = el('div', 'now-output');
			const outputBtn = el('button', 'now-output-btn');
			outputBtn.type = 'button';
			outputBtn.addEventListener('click', () => openOutputPicker().then(render));
			outputRow.append(outputBtn);

			body.append(top, artShell, meta, seekWrap, times, transport, volumeRow, outputRow, actions);

			function render() {
				const track = currentTrack();
				if (!track) { handle.close(); return; }

				if (art.src !== artworkUrl(track)) {
					art.src = artworkUrl(track);
					tintFromArtwork(art, handle.root);
				}
				title.textContent = track.title || 'Untitled';
				artist.textContent = artistName(track) || 'Unknown artist';

				const playing = isPlaying();
				const loading = isLoading();
				playBtn.innerHTML = loading
					? '<span class="now-play-wait" aria-hidden="true"></span>'
					: icon(playing ? 'pause' : 'play', 30);
				playBtn.setAttribute('aria-label', loading ? 'Loading' : (playing ? 'Pause' : 'Play'));
				playBtn.classList.toggle('is-loading', loading);

				shuffleBtn.classList.toggle('on', state.shuffled);
				repeatBtn.innerHTML = icon(state.repeat === 'one' ? 'loopOne' : 'loop', 20);
				repeatBtn.classList.toggle('on', state.repeat !== 'off');
				repeatBtn.setAttribute('aria-label', REPEAT_LABEL[state.repeat]);
				repeatBtn.title = REPEAT_LABEL[state.repeat];

				sleepAction.querySelector('.now-action-label').textContent = sleepLabel();

				const outputName = isLinked() ? (state.computerName || 'Your computer') : 'This phone';
				outputBtn.innerHTML = `${icon(isLinked() ? 'laptop' : 'phone', 16)}<span></span>`;
				outputBtn.querySelector('span').textContent = outputName;
				outputBtn.setAttribute('aria-label', `Play on, currently ${outputName}`);

				if (!scrubbing) {
					const length = duration();
					seek.max = String(length || 0);
					seek.value = String(Math.min(currentTime(), length || 0));
					elapsed.textContent = formatTime(currentTime());
					total.textContent = length ? formatTime(length) : '--:--';
					paintSeek(seekTrack, currentTime(), length);
				}
			}

			render();
			const stop = subscribe(render);
			handle.onRefresh = render;
			new MutationObserver((_records, observer) => {
				if (handle.root.isConnected) return;
				observer.disconnect();
				stop();
				openHandle = null;
			}).observe(document.body, { childList: true });
		},
	});
}

function labelledAction(glyph, label, onClick, accessibleName = label) {
	const button = el('button', 'now-action');
	button.type = 'button';
	button.innerHTML = `<span class="now-action-icon">${icon(glyph, 19)}</span>`;
	button.append(el('span', 'now-action-label', label));
	button.setAttribute('aria-label', accessibleName);
	button.title = accessibleName;
	button.addEventListener('click', onClick);
	return button;
}

function paintSeek(track, value, max) {
	const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
	track.style.setProperty('--played', String(ratio));
}

/* Average the cover down to one colour and hand it to the stylesheet.
 *
 * Cheap on purpose: a 1x1 downscale is a single GPU operation and gives a good
 * enough average for a background wash. Artwork is cross-origin and the canvas
 * would taint, so this only runs when the image opts in via CORS; when it does
 * not, the card keeps the default ground and nothing looks broken. */
function tintFromArtwork(image, root) {
	if (motionOff()) return;
	const source = new Image();
	source.crossOrigin = 'anonymous';
	source.referrerPolicy = 'no-referrer';
	source.onload = () => {
		try {
			const canvas = document.createElement('canvas');
			canvas.width = canvas.height = 1;
			const context = canvas.getContext('2d', { willReadFrequently: false });
			context.drawImage(source, 0, 0, 1, 1);
			const [r, g, b] = context.getImageData(0, 0, 1, 1).data;
			root.style.setProperty('--tint', `${r} ${g} ${b}`);
		} catch { /* tainted canvas: keep the default ground */ }
	};
	source.src = image.src;
}

export function closeNowPlaying() {
	openHandle?.close();
}

/** Wire the mini bar so tapping it opens the card. */
export function wireMiniBar() {
	const trigger = $('#playerOpen');
	trigger?.addEventListener('click', openNowPlaying);
}
