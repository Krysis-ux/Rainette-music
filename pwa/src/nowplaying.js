/* The now-playing card the mini bar expands into. Same contents in the same
 * order as the desktop, so the two devices are not two layouts. The scrubber and
 * the volume control are both `createSlider` — a 4px rail inside a 48px row,
 * which is the one thing a native range input cannot express. */

import { $, el, icon, iconButton, tap, toast, motionOff } from './dom.js';
import { createSlider } from './slider.js';
import { state, artworkUrl, artistName, formatTime, REPEAT_LABEL } from './state.js';
import { openSheet } from './sheets.js';
import { openQueueSheet } from './queue.js';
import { openAddToPlaylist, openLyrics, openSleepTimer, openOutputPicker, openTrackMenu, sleepLabel } from './extras.js';
import { openArtist, openAlbum, trackArtist, trackAlbum } from './catalog.js';
import {
	setVolume, volume as currentVolume, boostAvailable, resumeContext, VOLUME_MAX, volumeIsAdjustable,
} from './audio.js';
import { isLocalTrack, localArtworkUrl } from './local.js';
import {
	currentTrack, isPlaying, isLoading, currentTime, duration, isLinked,
	toggle, skip, seekTo, cycleRepeat, toggleShuffle, subscribe,
} from './player.js';

let openHandle = null;

/* If the audio clock lands within this of where we asked, it has caught up and
 * the slider's settle window is released early rather than sat out. The window
 * itself belongs to the component. */
const SEEK_SETTLE_TOLERANCE_S = 1.5;

export function isNowPlayingOpen() {
	return !!openHandle?.root?.isConnected;
}

export function openNowPlaying() {
	if (!currentTrack()) return;
	if (isNowPlayingOpen()) return;

	// The mini bar floats over browse sheets; this one it must not, being the
	// expanded form of it.
	document.body.classList.add('now-playing-open');
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

			const artShell = el('div', 'now-art-shell sheet-drag');
			const art = document.createElement('img');
			art.className = 'now-art';
			art.draggable = false;
			art.alt = '';
			art.referrerPolicy = 'no-referrer';
			art.decoding = 'async';
			artShell.append(art);

			// The artist line is a button, not a label. Tapping the name of whoever
			// you are listening to is the most obvious route to their page, and the
			// card was the one place in the app that did not offer it.
			const meta = el('div', 'now-meta');
			const title = el('h2', 'now-title');
			const artist = el('button', 'now-artist');
			artist.type = 'button';
			artist.addEventListener('click', () => {
				const track = currentTrack();
				const who = track && trackArtist(track);
				if (who) openArtist(who);
			});
			// The album sits under it for the same reason, and only when the track
			// actually carries one to go to.
			const album = el('button', 'now-album');
			album.type = 'button';
			album.hidden = true;
			album.addEventListener('click', () => {
				const track = currentTrack();
				const release = track && trackAlbum(track);
				if (release) openAlbum(release);
			});
			meta.append(title, artist, album);

			// ── Scrubber ────────────────────────────────────────────────────
			const times = el('div', 'now-times');
			const elapsed = el('span', '', '0:00');
			const total = el('span', '', '0:00');
			times.append(elapsed, total);

			// The render-vs-drag guard lives inside the component now. A live
			// gesture always beats the timeupdate stream, and after a commit the
			// thumb holds its own value until the audio clock catches up with
			// where it was sent — seekTo() writes audio.currentTime, which fires
			// timeupdate synchronously, and that first tick still carries the
			// pre-seek position.
			//
			// Both of those used to be open-coded here, and the guard was released
			// by a `change` binding that several engines fire once per value step
			// during a drag — dropping it mid-gesture, with the finger still down.
			const seek = createSlider({
				min: 0,
				max: 0,
				step: 0.1,
				variant: 'seek',
				label: 'Seek',
				// Seconds, matching the desktop's own arrow-key step.
				keyStep: 5,
				// A screen reader has to say "1:34 of 3:20", not "94".
				format: (value, range) => `${formatTime(value)} of ${range.max ? formatTime(range.max) : 'unknown length'}`,
				settleTolerance: SEEK_SETTLE_TOLERANCE_S,
				onInput: value => { elapsed.textContent = formatTime(value); },
				onCommit: value => seekTo(value),
			});

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
			// Runs to 200%, because a phone at full volume through a car aux or a
			// quiet bluetooth speaker is often still too quiet. Past 100% the gain
			// node is doing the work, so the readout says so — and if the graph is
			// unavailable the slider stops honestly at 100 rather than pretending.
			const volumeRow = el('div', 'now-volume');
			const volumeIcon = el('span', 'now-volume-icon');
			const volumeValue = el('span', 'now-volume-value');

			const paintVolume = percent => {
				volumeIcon.innerHTML = icon(percent ? 'volume' : 'volumeOff', 18);
				volumeValue.textContent = `${percent}%`;
				volumeRow.classList.toggle('is-boosted', percent > 100);
			};

			const volume = createSlider({
				min: 0,
				max: Math.round(VOLUME_MAX * 100),
				step: 1,
				value: Math.round(currentVolume() * 100),
				variant: 'volume',
				label: 'Volume',
				keyStep: 5,
				boostAbove: 100,
				format: percent => `${percent}%`,
				// Safari starts every context suspended, and a suspended context is
				// silence with no error anywhere. A pointerdown on this control is a
				// valid gesture to resume it — and a native range's internal drag is
				// not reliably a gesture context, which is one more reason the
				// component owns the pointer.
				onGrab: resumeContext,
				onInput: percent => { paintVolume(percent); setVolume(percent / 100).catch(() => {}); },
				// The snap-back is on commit only. Reading boostAvailable() on every
				// frame of a drag raced the graph still being built and yanked the
				// thumb back mid-gesture; once, on release, it is an answer rather
				// than a coin toss.
				onCommit: async percent => {
					const applied = await setVolume(percent / 100);
					if (percent <= 100 || boostAvailable()) return;
					// A phone whose graph refused to build cannot exceed unity;
					// snapping the thumb back is the only honest way to say so.
					volume.setValue(Math.round(applied * 100), 'force');
					paintVolume(volume.value);
					// Deliberately not "update your computer": the usual cause is
					// this one track's audio link, and the next one often works.
					toast('Extra volume is not available for this track. Try the next one.', { icon: 'volume' });
				},
			});
			paintVolume(volume.value);
			/* On a platform where the only volume that works is a graph, and a
			 * graph is what stops the music when the screen locks, there is no
			 * honest slider to draw. Saying which buttons do work is worth more
			 * than a control that moves and changes nothing — which is what was
			 * there before, on every iPhone. */
			if (volumeIsAdjustable()) {
				volumeRow.append(volumeIcon, volume.root, volumeValue);
			} else {
				volumeRow.classList.add('is-external');
				volumeIcon.innerHTML = icon('volume', 18);
				volumeRow.append(volumeIcon, el('span', 'now-volume-note',
					'Use your phone’s volume buttons. An in-app slider would need an audio graph, and that stops playback when the screen locks.'));
			}

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

			body.append(top, artShell, meta, seek.root, times, transport, volumeRow, outputRow, actions);

			function render() {
				const track = currentTrack();
				if (!track) { handle.close(); return; }

				const cover = isLocalTrack(track) ? localArtworkUrl(track) : artworkUrl(track);
				if (art.src !== cover) {
					art.dataset.localArt = isLocalTrack(track) ? track.source_id : '';
					art.src = cover;
					tintFromArtwork(art, handle.root);
				}
				title.textContent = track.title || 'Untitled';

				// Only offer the tap when there is genuinely somewhere to land:
				// a row that looks like a link and goes nowhere is worse than a label.
				const who = trackArtist(track);
				artist.textContent = artistName(track) || 'Unknown artist';
				artist.disabled = !who;
				artist.classList.toggle('is-link', !!who);
				artist.setAttribute('aria-label', who ? `Go to ${who.name}` : (artistName(track) || 'Unknown artist'));

				const release = trackAlbum(track);
				album.hidden = !release;
				album.disabled = !release;
				if (release) {
					album.textContent = release.title;
					album.setAttribute('aria-label', `Go to ${release.title}`);
				}

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

				const length = duration();
				seek.setRange(0, length || 0);
				// Guarded: a drag in progress, or a seek still travelling, keeps the
				// thumb. `seek.value` is therefore always what the thumb shows, which
				// is what the elapsed readout has to agree with.
				seek.setValue(currentTime(), 'stream');
				elapsed.textContent = formatTime(seek.value);
				total.textContent = length ? formatTime(length) : '--:--';
			}

			render();
			const stop = subscribe(render);
			new MutationObserver((_records, observer) => {
				if (handle.root.isConnected) return;
				observer.disconnect();
				stop();
				openHandle = null;
				document.body.classList.remove('now-playing-open');
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

/** Wire the mini bar so tapping it opens the card. */
export function wireMiniBar() {
	const trigger = $('#playerOpen');
	trigger?.addEventListener('click', openNowPlaying);
}
