/* Artists and albums — the half of the catalog this client used to have no way
 * to reach. Search returned a flat track list, so tapping an artist tried to
 * play them and failed on a row that has no source_id.
 *
 * Both pages are full sheets rather than panels: they stack, the back gesture
 * pops them, and the tab you came from is still underneath. An artist opened
 * from search, from the queue, or from the now-playing card is the same sheet
 * either way.
 */

import { el, icon, iconButton, toast, stagger } from './dom.js';
import { openSheet } from './sheets.js';
import { runListDownload } from './downloadmenu.js';
import { command, commandError } from './bridge.js';
import { renderTracks } from './tracks.js';
import { sortTracks, sortControl, RELEASE_SORTS, sortReleases } from './sorting.js';
import { playTrack, queueAddEnd } from './player.js';
import { rememberRecent, artistName } from './state.js';
import { artistRef, knownArtist, resolveArtistImages } from './artists.js';

/* What to do once a track starts from inside a browse sheet. `nowplaying.js`
 * already imports this module, so it is handed in rather than imported back. */
let onStartedPlaying = () => {};

export function configureCatalog(options = {}) {
	onStartedPlaying = options.onStartedPlaying || onStartedPlaying;
}

/* ── Shapes ───────────────────────────────────────────────────────────────
 * The computer speaks one album shape everywhere, but which key holds the id
 * depends on whether it came from a search, an artist catalog, or a track's
 * own metadata. Normalising once here keeps that out of every call site. */

export function albumRef(album) {
	return {
		id: String(album?.id || album?.browse_id || album?.album_id || ''),
		title: String(album?.title || album?.name || 'Unknown album'),
		artist: String(album?.artist || ''),
		artistId: String(album?.artist_id || ''),
		year: String(album?.year || ''),
		art: album?.thumbnail_url || '',
		releaseType: String(album?.release_type || '').toLowerCase(),
	};
}

export { artistRef };

/** The artist behind a track, when the computer supplied enough to open one.
 *  A name alone is enough — the catalog command resolves it. */
export function trackArtist(track) {
	const meta = track?.metadata || {};
	const primary = Array.isArray(meta.artists) && meta.artists.length ? meta.artists[0] : null;
	const name = String(primary?.name || track?.artist || track?.uploader || '').trim();
	if (!name) return null;
	return { id: String(primary?.id || meta.artist_id || ''), name, art: '', subscribers: '' };
}

/** The artist name, as a control when there is somewhere to go and a label when
 *  there is not.
 *
 *  A real `<button>` rather than a tappable `<span>`: a span gives no keyboard
 *  route and no accessible name, which is an app that looks like it has artist
 *  links while having none. Where there is no artist to open we return a plain
 *  span rather than a disabled button — a disabled button is still a tab stop
 *  in some engines and announces itself as one.
 */
export function artistLink(track, { className = '' } = {}) {
	const name = artistName(track) || 'Unknown artist';
	const who = trackArtist(track);
	if (!who) return el('span', className, name);

	const link = el('button', `link-inline ${className}`.trim(), name);
	link.type = 'button';
	link.setAttribute('aria-label', `Go to ${who.name}`);
	link.addEventListener('click', event => {
		// The row behind this is what plays the track. Without this the tap
		// would open the artist and start the song at the same time.
		event.stopPropagation();
		openArtist(who);
	});
	return link;
}

/** The album a track belongs to, when it carries one. */
export function trackAlbum(track) {
	const meta = track?.metadata || {};
	const album = meta.album && typeof meta.album === 'object' ? meta.album : {};
	const title = String(album.name || meta.album_name || track?.album || '').trim();
	const id = String(album.id || meta.album_id || '');
	if (!title && !id) return null;
	return albumRef({ id, title: title || 'Album', artist: track?.artist || '' });
}

function catalogError(error) {
	return commandError(error, 'Your computer could not answer that.');
}

/* ── Follow ───────────────────────────────────────────────────────────────*/

let followed = null;   // null until the computer has been asked once

export async function loadFollowed({ force = false } = {}) {
	if (followed && !force) return followed;
	try {
		const result = await command('music_followed_artists', {}, 12000);
		followed = Array.isArray(result?.followed_artists) ? result.followed_artists : [];
	} catch {
		followed = followed || [];
	}
	return followed;
}

function isFollowed(artist) {
	if (!followed) return false;
	return followed.some(entry => {
		const ref = artistRef(entry);
		return (ref.id && ref.id === artist.id) || ref.name.toLowerCase() === artist.name.toLowerCase();
	});
}

async function toggleFollow(artist, button) {
	const following = isFollowed(artist);
	button.disabled = true;
	try {
		const result = await command(following ? 'music_artist_unfollow' : 'music_artist_follow', {
			artist_id: artist.id,
			name: artist.name,
			thumbnail_url: artist.art,
		}, 12000);
		if (Array.isArray(result?.followed_artists)) followed = result.followed_artists;
		else await loadFollowed({ force: true });
		toast(following ? `Unfollowed ${artist.name}` : `Following ${artist.name}`, { icon: following ? 'close' : 'check' });
	} catch (error) {
		toast(catalogError(error), { icon: 'close' });
	} finally {
		button.disabled = false;
		paintFollow(button, artist);
	}
}

function paintFollow(button, artist) {
	const following = isFollowed(artist);
	button.classList.toggle('on', following);
	button.textContent = following ? 'Following' : 'Follow';
	button.setAttribute('aria-pressed', String(following));
}

/* ── Artist ───────────────────────────────────────────────────────────────*/

/* Sections in the order an artist page is actually read: what to play first,
 * then the releases, then the long tail. A section with nothing in it is not
 * rendered at all — an empty "Videos" tab is a dead end, not information. */
const ARTIST_SECTIONS = [
	{ id: 'songs', label: 'Songs' },
	{ id: 'albums', label: 'Albums' },
	{ id: 'eps', label: 'EPs' },
	{ id: 'singles', label: 'Singles' },
	{ id: 'videos', label: 'Videos' },
];

/* ytmusicapi files EPs inside the artist's album section and marks them with a
 * release type. Without that marker there is nothing to split on, so they stay
 * with the albums rather than being guessed at from the track count. */
function shelveReleases(albums, singles) {
	const all = albums.map(albumRef);
	return {
		albums: all.filter(album => album.releaseType !== 'ep'),
		eps: all.filter(album => album.releaseType === 'ep'),
		singles: singles.map(albumRef),
	};
}

export function openArtist(seed) {
	const artist = artistRef(seed);
	if (!artist.id && !artist.name) return;

	openSheet({
		title: artist.name,
		className: 'sheet-catalog',
		full: true,
		build: async handle => {
			const { body } = handle;
			const head = el('div', 'catalog-head sheet-drag');
			head.append(el('p', 'empty', `Loading ${artist.name}…`));
			body.append(head);

			let payload;
			try {
				[payload] = await Promise.all([
					command('music_artist_catalog', { artist_id: artist.id, name: artist.name }, 45000),
					loadFollowed(),
				]);
			} catch (error) {
				head.replaceChildren(el('p', 'empty', catalogError(error)));
				return;
			}

			const resolved = { ...artist, ...artistRef(payload?.artist || {}) };
			const songs = Array.isArray(payload?.songs) ? payload.songs : [];
			const videos = Array.isArray(payload?.videos) ? payload.videos : [];
			const shelves = shelveReleases(
				Array.isArray(payload?.albums) ? payload.albums : [],
				Array.isArray(payload?.singles) ? payload.singles : [],
			);

			const counts = {
				songs: songs.length,
				albums: shelves.albums.length,
				eps: shelves.eps.length,
				singles: shelves.singles.length,
				videos: videos.length,
			};
			const available = ARTIST_SECTIONS.filter(section => counts[section.id] > 0);

			head.replaceChildren(renderArtistHeader(resolved, songs, handle));
			if (payload?.msg) body.append(el('p', 'catalog-note', payload.msg));

			if (!available.length) {
				body.append(el('p', 'empty', `Nothing of ${resolved.name}'s came back. Try searching for a song instead.`));
				return;
			}

			const tabs = el('div', 'segmented catalog-tabs');
			tabs.setAttribute('role', 'tablist');
			tabs.setAttribute('aria-label', `${resolved.name} catalog`);
			const content = el('div', 'catalog-content');
			body.append(tabs, content);

			let active = available[0].id;
			const paint = () => {
				for (const button of tabs.querySelectorAll('button')) {
					const on = button.dataset.section === active;
					button.classList.toggle('active', on);
					button.setAttribute('aria-selected', String(on));
				}
				renderArtistSection(content, active, { songs, videos, shelves, artist: resolved });
			};

			for (const section of available) {
				const button = el('button', '', section.label);
				button.type = 'button';
				button.role = 'tab';
				button.dataset.section = section.id;
				button.append(el('i', 'catalog-count', String(counts[section.id])));
				button.addEventListener('click', () => {
					if (active === section.id) return;
					active = section.id;
					paint();
				});
				tabs.append(button);
			}
			paint();
		},
	});
}

function renderArtistHeader(artist, songs, handle) {
	const head = el('div', 'catalog-hero');

	const close = iconButton('chevronDown', {
		label: 'Close',
		className: 'catalog-close',
		onClick: () => handle.close(),
	});

	const art = document.createElement('img');
	art.className = 'catalog-art round';
	art.src = artist.art || './icon.svg';
	art.alt = '';
	art.width = 104;
	art.height = 104;
	art.referrerPolicy = 'no-referrer';
	art.decoding = 'async';

	const meta = el('div', 'catalog-meta');
	meta.append(el('h2', 'catalog-title', artist.name));
	if (artist.subscribers) meta.append(el('p', 'catalog-sub', `${artist.subscribers} subscribers`));

	const actions = el('div', 'catalog-actions');
	const play = el('button', 'primary compact', 'Play');
	play.type = 'button';
	play.disabled = !songs.length;
	play.addEventListener('click', () => startList(songs));

	const shuffle = el('button', 'ghost small', 'Shuffle');
	shuffle.type = 'button';
	shuffle.disabled = !songs.length;
	shuffle.addEventListener('click', () => startList(songs, { shuffle: true }));

	const follow = el('button', 'chip catalog-follow');
	follow.type = 'button';
	paintFollow(follow, artist);
	follow.addEventListener('click', () => toggleFollow(artist, follow));

	actions.append(play, shuffle, follow);
	head.append(close, art, meta, actions);
	return head;
}

function renderArtistSection(container, section, { songs, videos, shelves, artist }) {
	container.replaceChildren();

	if (section === 'songs' || section === 'videos') {
		const list = section === 'songs' ? songs : videos;
		const rows = el('div', 'track-list');
		let sorted = list;
		const control = sortControl({
			scope: section === 'songs' ? 'artist-songs' : 'artist-videos',
			onChange: mode => {
				sorted = sortTracks(list, mode);
				renderTracks(rows, sorted, {
					emptyMessage: 'Nothing here.',
					onPlay: (track, index) => startList(sorted, { index }),
				});
			},
		});
		container.append(control.node, rows);
		control.apply();
		return;
	}

	const releases = shelves[section] || [];
	const grid = el('div', 'release-grid');
	let sorted = releases;
	const control = sortControl({
		scope: 'releases',
		modes: RELEASE_SORTS,
		onChange: mode => {
			sorted = sortReleases(releases, mode);
			grid.replaceChildren(...sorted.map(album => releaseCard(album, artist)));
			stagger(grid, ':scope > .release-card');
		},
	});
	container.append(control.node, grid);
	control.apply();
}

function releaseCard(album, artist) {
	const card = el('button', 'release-card');
	card.type = 'button';

	const art = document.createElement('img');
	art.src = album.art || './icon.svg';
	art.alt = '';
	art.loading = 'lazy';
	art.decoding = 'async';
	art.referrerPolicy = 'no-referrer';

	const copy = el('span', 'release-copy');
	copy.append(el('b', '', album.title));
	const line = [album.year, releaseLabel(album.releaseType)].filter(Boolean).join(' · ');
	if (line) copy.append(el('span', '', line));

	card.append(art, copy);
	card.addEventListener('click', () => openAlbum({ ...album, artist: album.artist || artist?.name || '' }));
	return card;
}

/* The computer passes YouTube Music's own wording through, whose casing varies
 * by locale. Naive title-casing turns "EP" into "Ep", so the shapes we know
 * about are named here and anything unrecognised is shown as it arrived. */
const RELEASE_LABELS = { ep: 'EP', album: 'Album', single: 'Single' };

function releaseLabel(value, fallback = '') {
	const key = String(value || '').toLowerCase();
	if (!key) return fallback;
	return RELEASE_LABELS[key] || (key.charAt(0).toUpperCase() + key.slice(1));
}

/* ── Album ────────────────────────────────────────────────────────────────*/

export function openAlbum(seed) {
	const album = albumRef(seed);
	if (!album.id && !album.title) return;

	openSheet({
		title: album.title,
		className: 'sheet-catalog',
		full: true,
		build: async handle => {
			const { body } = handle;
			const head = el('div', 'catalog-head sheet-drag');
			head.append(el('p', 'empty', `Loading ${album.title}…`));
			body.append(head);

			let payload;
			try {
				payload = await command('music_album_tracks', {
					album_id: album.id,
					title: album.title,
					artist: album.artist,
				}, 45000);
			} catch (error) {
				head.replaceChildren(el('p', 'empty', catalogError(error)));
				return;
			}

			const resolved = { ...album, ...albumRef(payload?.album || {}) };
			const tracks = Array.isArray(payload?.tracks) ? payload.tracks : [];
			head.replaceChildren(renderAlbumHeader(resolved, tracks, handle));

			const rows = el('div', 'track-list');
			let sorted = tracks;
			const control = sortControl({
				scope: 'album',
				onChange: mode => {
					sorted = sortTracks(tracks, mode);
					renderTracks(rows, sorted, {
						emptyMessage: 'This release came back empty.',
						onPlay: (track, index) => startList(sorted, { index }),
					});
				},
			});
			body.append(control.node, rows);
			control.apply();
		},
	});
}

function renderAlbumHeader(album, tracks, handle) {
	const head = el('div', 'catalog-hero');

	head.append(iconButton('chevronDown', {
		label: 'Close',
		className: 'catalog-close',
		onClick: () => handle.close(),
	}));

	const art = document.createElement('img');
	art.className = 'catalog-art';
	art.src = album.art || './icon.svg';
	art.alt = '';
	art.width = 104;
	art.height = 104;
	art.referrerPolicy = 'no-referrer';
	art.decoding = 'async';

	const meta = el('div', 'catalog-meta');
	meta.append(el('h2', 'catalog-title', album.title));
	const line = [album.artist, album.year, tracks.length ? `${tracks.length} tracks` : ''].filter(Boolean).join(' · ');
	if (line) meta.append(el('p', 'catalog-sub', line));

	const actions = el('div', 'catalog-actions');
	const play = el('button', 'primary compact', 'Play');
	play.type = 'button';
	play.disabled = !tracks.length;
	play.addEventListener('click', () => startList(tracks));

	const shuffle = el('button', 'ghost small', 'Shuffle');
	shuffle.type = 'button';
	shuffle.disabled = !tracks.length;
	shuffle.addEventListener('click', () => startList(tracks, { shuffle: true }));

	const queue = el('button', 'chip', 'Add to queue');
	queue.type = 'button';
	queue.disabled = !tracks.length;
	queue.addEventListener('click', () => {
		for (const track of tracks) queueAddEnd(track);
		toast(`Queued ${tracks.length} tracks`, { icon: 'listAdd' });
	});

	// "All of it" is a question the head of a release can ask, exactly as a
	// playlist can. An EP opens this same sheet, so this covers both.
	const download = el('button', 'chip', 'Download');
	download.type = 'button';
	download.disabled = !tracks.length;
	download.addEventListener('click', () => runListDownload(tracks, {
		title: album.title || releaseLabel(album.releaseType, 'Album'),
	}));

	// An album opened from search names its artist but cannot be navigated to
	// one, so the link is only offered when there is genuinely somewhere to go.
	if (album.artistId || album.artist) {
		const link = el('button', 'chip', album.artist || 'Artist');
		link.type = 'button';
		link.addEventListener('click', () => openArtist({ id: album.artistId, name: album.artist }));
		actions.append(play, shuffle, queue, download, link);
	} else {
		actions.append(play, shuffle, queue, download);
	}

	head.append(art, meta, actions);
	return head;
}

/* ── Playing a catalog list ───────────────────────────────────────────────*/

function startList(list, { index = 0, shuffle = false } = {}) {
	const playable = list.filter(track => track?.source_id);
	if (!playable.length) {
		toast('Nothing here can be played yet.', { icon: 'close' });
		return;
	}
	let queue = playable;
	let at = Math.max(0, playable.indexOf(list[index] ?? playable[0]));
	if (shuffle) {
		queue = playable.slice();
		for (let i = queue.length - 1; i > 0; i -= 1) {
			const j = Math.floor(Math.random() * (i + 1));
			[queue[i], queue[j]] = [queue[j], queue[i]];
		}
		at = 0;
	}
	playTrack(queue[at], queue, at)
		.then(() => {
			rememberRecent(queue[at]);
			// The mini bar is hidden under a sheet, so without this a track
			// started from an artist page gives no sign of what is playing. The
			// card opens *over* the profile: closing it returns here, not home.
			onStartedPlaying();
		})
		.catch(error => toast(error?.message || 'Playback failed.', { icon: 'close' }));
}

/* ── Followed artists ─────────────────────────────────────────────────────*/

export function openFollowedArtists() {
	openSheet({
		title: 'Artists you follow',
		className: 'sheet-catalog',
		full: true,
		build: async ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', 'Artists you follow'));
			const list = el('div', 'catalog-content');
			list.append(el('p', 'empty', 'Loading…'));
			body.append(list);

			const artists = (await loadFollowed({ force: true })).map(artistRef);
			if (!artists.length) {
				list.replaceChildren(el('p', 'empty', 'Follow an artist from their page and they show up here — on this phone and on your computer.'));
				return;
			}
			list.replaceChildren(...artists.map(artist => artistRow(artist)));
			hydrateArtistArt(list);
			stagger(list, ':scope > .collection-row');
		},
	});
}

/** One artist, as a row. Used by search results, the library and the followed
 *  list. When the seed carries no picture the row is tagged with the name so
 *  `hydrateArtistArt` can fill it in once the computer answers. */
export function artistRow(seed) {
	const artist = artistRef(seed);
	const cached = artist.art ? null : knownArtist(artist.name);
	const art = document.createElement('img');
	art.className = 'collection-art round';
	art.src = artist.art || cached?.art || './icon.svg';
	art.alt = '';
	art.width = 44;
	art.height = 44;
	art.loading = 'lazy';
	art.decoding = 'async';
	art.referrerPolicy = 'no-referrer';
	if (!artist.art && !cached?.art) art.dataset.artistName = artist.name;

	const row = el('button', 'collection-row');
	row.type = 'button';

	const copy = el('span', 'collection-copy');
	const detail = artist.subscribers
		? `Artist · ${artist.subscribers} subscribers`
		: (seed?.count ? `Artist · ${seed.count} track${seed.count === 1 ? '' : 's'}` : 'Artist');
	copy.append(el('b', '', artist.name), el('span', '', detail));

	const chevron = el('span', 'collection-go');
	chevron.innerHTML = icon('chevronDown', 16);

	row.append(art, copy, chevron);
	row.addEventListener('click', () => openArtist({ ...artist, art: art.src }));
	return row;
}

/** Fill in the artwork of every artist row under `container` that is still
 *  showing the placeholder. One command for the whole screen, and each answer
 *  paints as it lands. */
export function hydrateArtistArt(container) {
	const pending = [...container.querySelectorAll('img[data-artist-name]')];
	if (!pending.length) return;

	const names = pending.map(image => image.dataset.artistName);
	resolveArtistImages(names, (resolvedKey, entry) => {
		for (const image of pending) {
			if (image.dataset.artistName?.trim().toLowerCase() !== resolvedKey) continue;
			if (!image.isConnected) continue;
			image.src = entry.art;
			delete image.dataset.artistName;
		}
	});
}

/** One album or single, as a row. */
export function albumRow(seed) {
	const album = albumRef(seed);
	const row = el('button', 'collection-row');
	row.type = 'button';

	const art = document.createElement('img');
	art.className = 'collection-art';
	art.src = album.art || './icon.svg';
	art.alt = '';
	art.width = 44;
	art.height = 44;
	art.loading = 'lazy';
	art.decoding = 'async';
	art.referrerPolicy = 'no-referrer';

	const copy = el('span', 'collection-copy');
	const kind = releaseLabel(album.releaseType, 'Album');
	copy.append(el('b', '', album.title), el('span', '', [kind, album.artist, album.year].filter(Boolean).join(' · ')));

	const chevron = el('span', 'collection-go');
	chevron.innerHTML = icon('chevronDown', 16);

	row.append(art, copy, chevron);
	row.addEventListener('click', () => openAlbum(album));
	return row;
}
