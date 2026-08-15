/* Watching the music video, rather than only hearing it.
 *
 * The main transport stays an <audio> element on purpose: this app's
 * keep-playing-with-the-screen-off behaviour depends on it, and a <video>
 * element is stopped by the system when it is backgrounded. So video gets its
 * own element in its own sheet, and the audio transport is paused while it
 * plays rather than replaced by it. Close the sheet and the music picks up
 * where the video reached.
 */

import { el, toast } from './dom.js';
import { openSheet } from './sheets.js';
import { command, commandError, mediaUrl } from './bridge.js';
import { artistName } from './state.js';
import { currentTrack, isPlaying, pauseLocal, seekTo, toggle } from './player.js';

/** Whether a track is a music video rather than a song. The computer marks the
 *  kind on anything it got from the catalog; an artist's Videos shelf is the
 *  other reliable signal. */
export function looksLikeVideo(track) {
	const kind = String(track?.metadata?.result_type || '').toLowerCase();
	return kind === 'video' || kind === 'music_video' || kind.endsWith('_video');
}

export function openVideo(track, { startAt = 0 } = {}) {
	if (!track?.source_id) return;

	openSheet({
		title: track.title || 'Video',
		className: 'sheet-video',
		full: true,
		build: async handle => {
			const { body } = handle;
			const head = el('div', 'video-head sheet-drag');
			head.append(
				el('h2', 'sheet-title', track.title || 'Video'),
				el('p', 'lyrics-artist', artistName(track) || ''),
			);
			const stage = el('div', 'video-stage');
			stage.append(el('p', 'empty', 'Asking your computer for the video…'));
			body.append(head, stage);

			// The song and the video are the same performance; hearing both at
			// once is the one thing that must not happen.
			const wasPlaying = isPlaying();
			pauseLocal();

			let payload;
			try {
				payload = await command('music_stream_url', {
					source_id: track.source_id,
					track,
					want_video: true,
				}, 50000);
			} catch (error) {
				stage.replaceChildren(el('p', 'empty', commandError(error, 'Your computer could not find a video for this.')));
				return;
			}

			if (!payload?.url) {
				stage.replaceChildren(el('p', 'empty', 'No video came back for this track.'));
				return;
			}
			if (payload.is_video === false) {
				stage.replaceChildren(el('p', 'empty',
					'This one has no music video — your computer only found audio for it.'));
				return;
			}

			const video = document.createElement('video');
			video.className = 'video-player';
			video.controls = true;
			// Without this iOS takes the video full-screen the moment it plays,
			// which throws away the sheet the user is looking at.
			video.playsInline = true;
			video.setAttribute('playsinline', '');
			video.preload = 'metadata';
			video.src = mediaUrl(payload.url);
			if (startAt > 0) {
				video.addEventListener('loadedmetadata', () => {
					if (Number.isFinite(video.duration)) video.currentTime = Math.min(startAt, video.duration - 1);
				}, { once: true });
			}
			video.addEventListener('error', () => {
				stage.replaceChildren(el('p', 'empty', 'That video could not be played on this phone.'));
			});

			stage.replaceChildren(video);
			video.play().catch(() => {
				// Autoplay refused: the controls are right there, so this is a
				// prompt rather than a failure.
				toast('Tap play to start the video', { icon: 'play' });
			});

			// Leaving the video hands the position back to the audio transport, so
			// closing the sheet resumes the song where the video got to.
			new MutationObserver((_records, observer) => {
				if (handle.root.isConnected) return;
				observer.disconnect();
				const reached = video.currentTime || 0;
				video.pause();
				video.removeAttribute('src');
				video.load();
				const playing = currentTrack();
				if (playing && playing.source_id === track.source_id && reached > 1) seekTo(reached);
				if (wasPlaying) toggle();
			}).observe(document.body, { childList: true });
		},
	});
}
