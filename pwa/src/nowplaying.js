/* The now-playing card the mini bar expands into. Same contents in the same
 * order as the desktop, so the two devices are not two layouts. The scrubber and
 * the volume control are both `createSlider` — a 4px rail inside a 48px row,
 * which is the one thing a native range input cannot express. */

import { $, el, icon, iconButton, tap, toast } from './dom.js';
import { createSlider } from './slider.js';
import { dragGesture } from './gesture.js';
import { spring } from './motion.js';
import { state, artworkAt, artistName, formatTime, REPEAT_LABEL } from './state.js';
import { openSheet } from './sheets.js';
import { openQueueSheet } from './queue.js';
import { openAddToPlaylist, openLyrics, openSleepTimer, openOutputPicker, openTrackMenu, sleepLabel, phoneOnly } from './extras.js';
import { openArtist, openAlbum, trackArtist, trackAlbum } from './catalog.js';
import {
	setVolume, volume as currentVolume, boostAvailable, resumeContext, volumeMax, volumeIsAdjustable,
} from './audio.js';
import { isLocalTrack, localArtworkUrl } from './local.js';
import { nativeAvailable } from './native-engine.js';
import {
	currentTrack, isPlaying, isLoading, currentTime, duration, isLinked,
	toggle, skip, seekTo, cycleRepeat, toggleShuffle, subscribe,
} from './player.js';

let openHandle = null;

/* The cover is up to 340px across, on screens of 2-3x density. */
const NOW_ART_PX = 720;

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

			// "Play on" is the top line: it answers where the sound is coming out,
			// which the card used to say twice, once here in words and once as a
			// button further down that pushed the actions off a phone's screen.
			const top = el('div', 'now-top sheet-drag');
			const outputBtn = el('button', 'now-output-btn');
			outputBtn.type = 'button';
			outputBtn.addEventListener('click', () => openOutputPicker().then(render));
			top.append(iconButton('chevronDown', {
				label: 'Close now playing',
				className: 'now-close',
				onClick: () => handle.close(),
			}), outputBtn);

			const artShell = el('div', 'now-art-shell sheet-drag');
			const art = document.createElement('img');
			art.className = 'now-art';
			art.draggable = false;
			art.alt = '';
			art.referrerPolicy = 'no-referrer';
			art.decoding = 'async';
			artShell.append(art);
			// Sideways on the cover changes the song; down still closes the card,
			// because the sheet's own drag only ever engages vertically.
			wireSkipSwipe(artShell, art);

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
				max: Math.round(volumeMax() * 100),
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
					// `boostAvailable()` describes the *local* graph, which
					// linked mode never builds because the computer is making
					// the sound. Without this check, asking for more than 100%
					// while playing on the computer snapped the thumb back and
					// blamed the track, for a limit that does not apply.
					if (percent <= 100 || isLinked() || boostAvailable()) return;
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
			/* Where a slider cannot do anything, there is no slider and no
			 * explanation of why — a paragraph about audio graphs in the middle
			 * of a now-playing card is worse than the empty space it fills, and
			 * the hardware buttons are discoverable without being told. This is
			 * now rare: it needs an engine that both ignores element volume and
			 * cannot hold a playback session. */
			if (volumeIsAdjustable()) {
				volumeRow.append(volumeIcon, volume.root, volumeValue);
			} else {
				volumeRow.hidden = true;
			}

			// ── Actions ─────────────────────────────────────────────────────
			const actions = el('div', 'now-actions');
			const sleepAction = labelledAction('moon', sleepLabel(), () => openSleepTimer().then(render));
			actions.append(...[
				labelledAction('queue', 'Queue', openQueueSheet),
				// "Add to playlist" is the desktop's wording; five of those across a
				// phone's width truncates every one of them, so the label is the
				// noun and the full phrase stays as the accessible name.
				labelledAction('listAdd', 'Playlist', () => { const track = currentTrack(); if (track) openAddToPlaylist(track); }, 'Add to playlist'),
				// Lyrics come from the computer; with none paired the slot goes.
				phoneOnly() ? null : labelledAction('mic', 'Lyrics', () => { const track = currentTrack(); if (track) openLyrics(track); }),
				sleepAction,
				labelledAction('more', 'More', () => { const track = currentTrack(); if (track) openTrackMenu(track); }),
			].filter(Boolean));

			body.append(top, artShell, meta, seek.root, times, transport, volumeRow, actions);

			function render() {
				const track = currentTrack();
				if (!track) { handle.close(); return; }

				const cover = isLocalTrack(track) ? localArtworkUrl(track) : artworkAt(track, NOW_ART_PX);
				if (art.src !== cover) {
					art.dataset.localArt = isLocalTrack(track) ? track.source_id : '';
					art.src = cover;
					washFromArtwork(art, handle.root);
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
				artShell.classList.toggle('is-paused', !playing && !loading);

				shuffleBtn.classList.toggle('on', state.shuffled);
				repeatBtn.innerHTML = icon(state.repeat === 'one' ? 'loopOne' : 'loop', 20);
				repeatBtn.classList.toggle('on', state.repeat !== 'off');
				repeatBtn.setAttribute('aria-label', REPEAT_LABEL[state.repeat]);
				repeatBtn.title = REPEAT_LABEL[state.repeat];

				sleepAction.querySelector('.now-action-label').textContent = sleepLabel();

				const outputName = isLinked()
					? `Playing on ${state.computerName || 'your computer'}`
					: (nativeAvailable() ? 'Playing on this phone' : 'Playing from your computer');
				outputBtn.innerHTML = `${icon(isLinked() ? 'laptop' : 'phone', 15)}<span></span>`;
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

/* The card's colour, from the cover: one averaged colour for the wash, and the
 * cover itself shrunk and blurred for the backdrop.
 *
 * The backdrop used to be the full-size cover under a 56px CSS blur, half as
 * large again as the card. That is one very expensive paint, and on a
 * mid-range Android phone it held up the card's first frame, so the mini bar
 * vanished well before the card arrived. A 40px canvas blurred once per song
 * and stretched by the GPU looks the same and costs nothing.
 *
 * Artwork is cross-origin and the canvas would taint, so this only runs when
 * the image opts in via CORS (both YouTube image hosts do); when it does not,
 * the card keeps the default ground and nothing looks broken. */
const WASH_PX = 40;

function washFromArtwork(image, root) {
	// The previous song's backdrop stays until this one's is ready, rather
	// than blinking to plain grey between them.
	const source = new Image();
	source.onerror = () => { if (source.src === image.src) root.style.setProperty('--now-art', 'none'); };
	source.crossOrigin = 'anonymous';
	source.referrerPolicy = 'no-referrer';
	source.onload = () => {
		if (source.src !== image.src) return;
		try {
			const dot = document.createElement('canvas');
			dot.width = dot.height = 1;
			dot.getContext('2d').drawImage(source, 0, 0, 1, 1);
			const [r, g, b] = dot.getContext('2d').getImageData(0, 0, 1, 1).data;
			root.style.setProperty('--tint', `${r} ${g} ${b}`);

			// Down to a few pixels and back up is a blur on its own; the
			// filter, where the engine has one, smooths what is left.
			const small = document.createElement('canvas');
			small.width = small.height = 10;
			small.getContext('2d').drawImage(source, 0, 0, 10, 10);
			const wash = document.createElement('canvas');
			wash.width = wash.height = WASH_PX;
			const context = wash.getContext('2d');
			context.imageSmoothingQuality = 'high';
			context.filter = 'blur(2px)';
			context.drawImage(small, -4, -4, WASH_PX + 8, WASH_PX + 8);
			root.style.setProperty('--now-art', `url(${wash.toDataURL()})`);
		} catch {
			// Tainted canvas: the default ground, not the last song's colour.
			root.style.setProperty('--now-art', 'none');
		}
	};
	source.src = image.src;
}

/* Past this many pixels sideways, or thrown faster than this, a swipe on what
 * is playing changes the song. Below both it springs back, so a hesitant
 * swipe is a cancel rather than a surprise. */
const SKIP_COMMIT_PX = 64;
const SKIP_FLICK_PX_MS = 0.45;
/* A throw still has to go somewhere: a fast twitch of the thumb is not a skip,
 * and an accidental skip costs more than a swipe that has to be repeated. */
const SKIP_FLICK_MIN_PX = 36;
/* An upward pull on the mini bar this far, or this fast, opens the card. */
const OPEN_COMMIT_PX = 28;
const OPEN_FLICK_PX_MS = 0.3;

/**
 * Swipe left for the next song, right for the previous one. `moving` (one
 * element or several) follows the finger; a committed throw carries it off
 * that side and the new song slides in from the other, which is how every
 * phone player says "skipped". `onVertical` (the mini bar only) takes a
 * gesture that starts mostly up or down instead.
 */
function wireSkipSwipe(surface, moving, { onVertical = null } = {}) {
	let motion = null;
	let mode = null;
	let offset = 0;
	let swallowClick = false;

	const targets = [].concat(moving);
	// Whatever the stylesheet already applies (a paused cover is scaled down)
	// is kept under the swipe rather than replaced by it.
	let bases = targets.map(() => '');
	const paint = x => {
		offset = x;
		const fade = x ? String(Math.max(0, 1 - Math.abs(x) / ((surface.clientWidth || 320) * 0.7))) : '';
		targets.forEach((target, index) => {
			target.style.transform = x ? `translateX(${x}px)${bases[index]}` : '';
			target.style.opacity = fade;
		});
	};
	const settle = (velocity = 0) => {
		motion = spring(offset, 0, velocity, {
			stiffness: 380,
			damping: 34,
			onFrame: paint,
			onDone: () => { motion = null; surface.classList.remove('is-swiping'); },
		});
	};

	dragGesture(surface, {
		axis: onVertical ? 'free' : 'x',
		threshold: 8,
		canStart: event => !event.target?.closest?.('.link-inline'),
		onStart: () => {
			swallowClick = false;
			mode = null;
			// Caught mid-flight: take over where it is rather than snapping.
			const live = motion?.();
			motion = null;
			if (live) { paint(live.value); return; }
			bases = targets.map(target => {
				const current = getComputedStyle(target).transform;
				return current && current !== 'none' ? ` ${current}` : '';
			});
		},
		onMove: ({ dx, dy }) => {
			mode ||= Math.abs(dx) >= Math.abs(dy) ? 'skip' : 'vertical';
			swallowClick = true;
			if (mode === 'vertical') { onVertical?.move(dy); return; }
			surface.classList.add('is-swiping');
			paint(dx);
		},
		onEnd: ({ dx, dy, velocity, committed }) => {
			// A tap that caught a motion mid-flight still has to put it back.
			if (!committed) { if (offset) settle(); return; }
			if (mode === 'vertical') { onVertical?.end(dy, velocity.y); return; }
			const flung = Math.abs(velocity.x) > SKIP_FLICK_PX_MS && Math.sign(velocity.x) === Math.sign(dx)
				&& Math.abs(dx) > SKIP_FLICK_MIN_PX;
			if (!currentTrack() || (Math.abs(dx) < SKIP_COMMIT_PX && !flung)) { settle(velocity.x); return; }
			const step = dx < 0 ? 1 : -1;
			const width = surface.clientWidth || 320;
			tap(10);
			// Asked for now, not when the animation lands: the next song starts
			// loading during the throw, and catching the throw cannot lose it.
			skip(step).catch(() => {});
			motion = spring(offset, -step * width, Math.min(-1.2, -Math.abs(velocity.x)) * step, {
				stiffness: 520,
				damping: 44,
				onFrame: paint,
				onDone: finished => {
					if (!finished) return;
					paint(step * width * 0.6);
					settle();
				},
			});
		},
		onCancel: () => {
			if (mode === 'vertical') onVertical?.end(0, 0);
			else settle();
		},
	});

	// A swipe that ends over the surface must not also count as a tap on it.
	surface.addEventListener('click', event => {
		if (!swallowClick) return;
		swallowClick = false;
		event.stopImmediatePropagation();
		event.preventDefault();
	}, true);
}

/** Wire the mini bar: tap or pull it up to open the card, swipe it sideways to
 *  change the song. */
export function wireMiniBar() {
	const bar = $('#player');
	const surface = bar?.querySelector('.player-open');
	$('#playerOpen')?.addEventListener('click', openNowPlaying);
	if (!bar || !surface) return;

	const lift = dy => {
		bar.classList.add('is-lifting');
		// Upward follows the finger, damped; downward barely gives.
		bar.style.transform = `translateY(${dy < 0 ? dy * 0.45 : dy * 0.12}px)`;
	};
	const drop = () => {
		bar.classList.remove('is-lifting');
		bar.style.transform = '';
	};
	// The artwork and the words move; the bar and its buttons stay put.
	wireSkipSwipe(surface, [$('#playerArt'), surface.querySelector('.player-copy')], {
		onVertical: {
			move: lift,
			end: (dy, velocity) => {
				drop();
				if (dy < -OPEN_COMMIT_PX || velocity < -OPEN_FLICK_PX_MS) { tap(8); openNowPlaying(); }
			},
		},
	});
}
