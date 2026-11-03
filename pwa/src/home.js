/* Home, the way the Mac app and other music apps lay it out: what is playing, your favourites at a
 * glance, then shelves of the artists, songs and albums you play most, and artists to try next. */

import { STORAGE, readHistory, readJson, persist, trackKey, artistName, artworkFor, artworkUrl } from './state.js';
import { el, icon, tap, stagger } from './dom.js';
import { artistRef, knownArtist } from './artists.js';
import { openArtist, openAlbum, trackArtist, trackAlbum, monogram, hydrateArtistArt, loadFollowed } from './catalog.js';
import { currentTrack, isPlaying, toggle } from './player.js';
import { openNowPlaying } from './nowplaying.js';
import { localPlaylists } from './playlists.js';
import { nativeAvailable } from './native-engine.js';
import { artistHighlights } from './ytmusic.js';

const QUICK_TILES = 6;
const SHELF_SIZE = 12;
/* A shelf of one or two looks like a mistake; below this it is left out. */
const MIN_SHELF = 3;
/* Whose pages the picks are drawn from: the artists played most. */
const PICK_SEEDS = 3;
const PICKS_TTL_MS = 6 * 60 * 60 * 1000;

let options = { onPlay: () => {}, onOpenPlaylist: () => {}, playlists: () => [] };
let picks = readJson(STORAGE.homePicks, null);
let picking = null;
let followed = [];
let shown = '';

export function configureHome(next) {
	options = { ...options, ...next };
}

/* ── Reading the history ──────────────────────────────────────────────────*/

const byPlays = (a, b) => b.plays - a.plays || b.last - a.last;
const nameKey = name => String(name || '').trim().toLowerCase();

/** The songs played most, most first. */
export function topSongs(history, limit = SHELF_SIZE) {
	return history.slice().sort(byPlays).slice(0, limit).map(entry => entry.track);
}

/* The phone's own song search names every artist on a song in one string,
 * "Daft Punk, Pharrell Williams & Nile Rodgers"; Home counts the first. Only
 * the comma splits, and not before "The" ("Tyler, The Creator").
 * ponytail: "Mumford & Sons"-style names stay whole, so "A & B" does too. */
function leadArtist(track) {
	const who = trackArtist(track);
	if (!who || track?.metadata?.artists?.length) return who;
	return { ...who, name: who.name.split(/,(?!\s*the\s)/i)[0].trim() || who.name };
}

/** The artists played most, each with its plays added up across songs. */
export function topArtists(history, limit = SHELF_SIZE) {
	const artists = new Map();
	for (const { track, plays, last } of history) {
		const who = leadArtist(track);
		if (!who) continue;
		const seen = artists.get(nameKey(who.name)) || { ...who, plays: 0, last: 0 };
		artists.set(nameKey(who.name), { ...seen, id: seen.id || who.id, plays: seen.plays + plays, last: Math.max(seen.last, last) });
	}
	return [...artists.values()].sort(byPlays).slice(0, limit);
}

/** The albums played most. Only songs that name their album count. */
export function topAlbums(history, limit = SHELF_SIZE) {
	const albums = new Map();
	for (const { track, plays, last } of history) {
		const album = trackAlbum(track);
		if (!album) continue;
		const key = album.id || `${nameKey(album.title)}|${nameKey(album.artist)}`;
		const seen = albums.get(key) || { ...album, art: artworkUrl(track), plays: 0, last: 0 };
		albums.set(key, { ...seen, plays: seen.plays + plays, last: Math.max(seen.last, last) });
	}
	return [...albums.values()].sort(byPlays).slice(0, limit);
}

const playsLabel = count => `${count} play${count === 1 ? '' : 's'}`;

/* ── Picks ────────────────────────────────────────────────────────────────
 * One artist page per seed: its "Fans might also like" for artists to try, and the top seed's own
 * songs you have not played yet. */

function seedsOf(artists) {
	return artists.slice(0, PICK_SEEDS).map(artist => artist.id || nameKey(artist.name)).join(',');
}

function picksAreFresh(seeds) {
	return picks?.seeds === seeds && Date.now() - (picks.at || 0) < PICKS_TTL_MS;
}

async function fetchPicks(artists, seeds, onReady) {
	const pages = await Promise.all(artists.slice(0, PICK_SEEDS).map((artist, at) =>
		artistHighlights({ artist_id: artist.id, name: artist.name }, { allSongs: at === 0 }).catch(() => null)));
	const found = pages.filter(Boolean);
	if (!found.length) return;
	// Round-robin across the seeds, so the first few are not all one artist's
	// fans; an artist two of them share is listed once.
	const related = new Map();
	const longest = Math.max(...found.map(page => page.related.length));
	for (let at = 0; at < longest; at += 1) {
		for (const page of found) {
			const artist = page.related[at];
			if (artist && !related.has(artist.id)) related.set(artist.id, artist);
		}
	}
	picks = {
		seeds,
		at: Date.now(),
		related: [...related.values()],
		more: { artist: found[0].artist, songs: found[0].songs },
	};
	persist(STORAGE.homePicks, picks);
	onReady();
}

function requestPicks(artists, onReady) {
	if (!nativeAvailable() || !artists.length) return;
	const seeds = seedsOf(artists);
	if (picksAreFresh(seeds) || picking) return;
	picking = fetchPicks(artists, seeds, onReady).catch(() => {}).finally(() => { picking = null; });
}

/* ── Building blocks ──────────────────────────────────────────────────────*/

/* Shelves load as they scroll in; the top of Home loads at once. */
function image(src, className, size, loading = 'lazy') {
	const art = document.createElement('img');
	art.className = className;
	art.src = src;
	art.alt = '';
	art.width = size;
	art.height = size;
	art.loading = loading;
	art.decoding = 'async';
	art.referrerPolicy = 'no-referrer';
	return art;
}

/** An artist's picture, or their initial until one is found. */
function artistImage(artist, className, size, loading) {
	const known = artist.art || knownArtist(artist.name)?.art || '';
	const art = image(known ? artworkFor(known, size) : monogram(artist.name), className, size, loading);
	if (!known) art.dataset.artistName = artist.name;
	return art;
}

function button(className) {
	const node = el('button', className);
	node.type = 'button';
	return node;
}

function shelf(id, title, cards) {
	const section = el('section', 'home-shelf');
	section.dataset.shelf = id;
	const head = el('div', 'section-head');
	head.append(el('h2', '', title));
	const row = el('div', 'shelf-row');
	row.append(...cards);
	section.append(head, row);
	return section;
}

function songCard(track, list, index) {
	const card = button('shelf-card');
	card.append(
		image(artworkFor(artworkUrl(track), 148), 'shelf-art', 148),
		el('b', '', track.title || 'Untitled'),
		el('span', '', artistName(track) || 'Unknown artist'),
	);
	card.addEventListener('click', () => { tap(); options.onPlay(list, index); });
	return card;
}

function artistCard(seed, detail = '') {
	const artist = artistRef(seed);
	const card = button('shelf-card artist');
	const art = artistImage(artist, 'shelf-art round', 148);
	card.append(art, el('b', '', artist.name));
	if (detail) card.append(el('span', '', detail));
	// The picture may have arrived since the card was drawn; a monogram is not one.
	card.addEventListener('click', () => openArtist({ ...artist, art: /^https:/.test(art.src) ? art.src : '' }));
	return card;
}

function albumCard(album) {
	const card = button('shelf-card');
	card.append(
		image(album.art ? artworkFor(album.art, 148) : './icon.svg', 'shelf-art', 148),
		el('b', '', album.title),
		el('span', '', album.artist || 'Album'),
	);
	card.addEventListener('click', () => openAlbum({ id: album.id, title: album.title, artist: album.artist, thumbnail_url: album.art }));
	return card;
}

function playlistCard(playlist) {
	const card = button('shelf-card');
	const first = playlist.tracks?.[0];
	const cover = playlist.cover || playlist.thumbnail_url || (first ? artworkUrl(first) : '');
	const count = Number(playlist.tracks?.length ?? playlist.track_count ?? 0);
	card.append(
		cover ? image(artworkFor(cover, 148), 'shelf-art', 148) : el('span', 'shelf-art shelf-mark', (playlist.name || '?').slice(0, 1).toUpperCase()),
		el('b', '', playlist.name || 'Untitled playlist'),
		el('span', '', count ? `${count} song${count === 1 ? '' : 's'}` : 'Playlist'),
	);
	card.addEventListener('click', () => options.onOpenPlaylist(playlist));
	return card;
}

/* The song in the player, big, over its own colours: the Mac app's Continue
 * card. With nothing loaded it offers the last song played instead. */
function heroCard(recent) {
	const now = currentTrack();
	const track = now || recent[0];
	if (!track) return null;
	const playing = Boolean(now) && isPlaying();
	const art = artworkUrl(track);

	const card = el('div', 'home-hero');
	const open = button('home-hero-open');
	const copy = el('span', 'home-hero-copy');
	copy.append(
		el('span', 'home-hero-eyebrow', now ? (playing ? 'Now playing' : 'Paused') : 'Jump back in'),
		el('b', '', track.title || 'Untitled'),
		el('span', 'home-hero-artist', artistName(track) || 'Unknown artist'),
	);
	open.append(image(artworkFor(art, 104), 'home-hero-art', 104, 'eager'), copy);
	open.setAttribute('aria-label', now ? 'Open the now playing screen' : `Play ${track.title || 'the last song'}`);

	const resume = () => (now ? toggle() : options.onPlay(recent, 0));
	open.addEventListener('click', () => { tap(); if (now) openNowPlaying(); else resume(); });

	const play = button('home-hero-play');
	play.innerHTML = icon(playing ? 'pause' : 'play', 22);
	play.setAttribute('aria-label', playing ? 'Pause' : 'Play');
	play.addEventListener('click', () => { tap(); resume(); });

	card.append(image(artworkFor(art, 120), 'home-hero-backdrop', 120, 'eager'), open, play);
	return card;
}

/* Your favourites at a glance, as Spotify opens: the artists and albums you
 * play most, then your most played songs, two to a row. */
function quickGrid(history) {
	const songs = topSongs(history, QUICK_TILES);
	const tiles = [
		...topArtists(history, 2).map(artist => {
			const tile = button('quick-tile');
			tile.append(artistImage(artistRef(artist), 'round', 56, 'eager'), el('b', '', artist.name));
			tile.addEventListener('click', () => openArtist(artist));
			return tile;
		}),
		...topAlbums(history, 2).map(album => {
			const tile = button('quick-tile');
			tile.append(image(artworkFor(album.art, 56), '', 56, 'eager'), el('b', '', album.title));
			tile.addEventListener('click', () => openAlbum({ id: album.id, title: album.title, artist: album.artist, thumbnail_url: album.art }));
			return tile;
		}),
		...songs.map((track, index) => {
			const tile = button('quick-tile');
			tile.append(image(artworkFor(artworkUrl(track), 56), '', 56, 'eager'), el('b', '', track.title || 'Untitled'));
			tile.addEventListener('click', () => { tap(); options.onPlay(songs, index); });
			return tile;
		}),
	].slice(0, QUICK_TILES);
	if (tiles.length < 4) return null;
	const grid = el('div', 'quick-grid');
	grid.append(...tiles.slice(0, tiles.length - (tiles.length % 2)));
	return grid;
}

/* Above the recent list: the favourites. Below it: picks and the rest. */
function shelves(history) {
	const lead = [];
	const sections = [];
	const artists = topArtists(history);
	const played = new Set(history.map(entry => trackKey(entry.track)));
	const known = new Set(artists.map(artist => nameKey(artist.name)));
	const shelfOf = into => (id, title, cards) => { if (cards.length >= MIN_SHELF) into.push(shelf(id, title, cards)); };
	const add = shelfOf(sections);

	shelfOf(lead)('top-artists', 'Your Top Artists', artists.map(artist => artistCard(artist, playsLabel(artist.plays))));
	const onRepeat = topSongs(history);
	shelfOf(lead)('on-repeat', 'On Repeat', onRepeat.map((track, index) => songCard(track, onRepeat, index)));

	if (picks) {
		const fresh = picks.related.filter(artist => !known.has(nameKey(artist.name))).slice(0, SHELF_SIZE);
		add('picks', 'Artists You Might Like', fresh.map(artist => artistCard(artist, artist.audience || 'Artist')));
		const more = (picks.more?.songs || []).filter(track => !played.has(trackKey(track))).slice(0, SHELF_SIZE);
		add('more-from', `More from ${picks.more?.artist?.name || 'your top artist'}`, more.map((track, index) => songCard(track, more, index)));
	}

	add('top-albums', 'Your Top Albums', topAlbums(history).map(albumCard));
	add('playlists', 'Your Playlists', [...localPlaylists(), ...options.playlists()].slice(0, SHELF_SIZE).map(playlistCard));
	add('followed', 'Artists You Follow', followed.map(artist => artistCard(artist)));
	return { lead, sections };
}

/* ── Painting ─────────────────────────────────────────────────────────────*/

/* What a playlist card shows, so a rename or an edit repaints it. */
function playlistShape(playlist) {
	const count = playlist.tracks?.length ?? playlist.track_count ?? 0;
	return [playlist.id, playlist.name, playlist.cover || playlist.thumbnail_url || trackKey(playlist.tracks?.[0]), count].join(':');
}

/* Shelves are scrolled sideways, and Home repaints when a song starts. Without
 * this every repaint threw each shelf back to its first card. */
function keepScroll(container, paint) {
	const scrolled = new Map([...container.querySelectorAll('.home-shelf')]
		.map(section => [section.dataset.shelf, section.querySelector('.shelf-row').scrollLeft]));
	paint();
	for (const section of container.querySelectorAll('.home-shelf')) {
		const left = scrolled.get(section.dataset.shelf);
		if (left) section.querySelector('.shelf-row').scrollLeft = left;
	}
}

/** Paint Home around the recent list. `recent` is the merged play history,
 *  newest first, as the recent list shows it. */
export function renderHome(recent) {
	const history = readHistory();
	const now = currentTrack();
	const signature = [
		trackKey(now), isPlaying(), trackKey(recent[0]),
		history.slice(0, 40).map(entry => `${trackKey(entry.track)}:${entry.plays}`).join(','),
		picks?.at || 0, followed.map(artist => artist?.id || artist?.name).join(','),
		[...localPlaylists(), ...options.playlists()].map(playlistShape).join(','),
	].join('|');
	if (signature === shown) return;
	const first = !shown;
	shown = signature;

	const hero = document.getElementById('homeHero');
	hero.replaceChildren(...[heroCard(recent)].filter(Boolean));
	const quick = document.getElementById('homeQuick');
	quick.replaceChildren(...[quickGrid(history)].filter(Boolean));
	const { lead, sections } = shelves(history);
	const panel = document.querySelector('[data-panel="home"]');
	keepScroll(panel, () => {
		document.getElementById('homeLead').replaceChildren(...lead);
		document.getElementById('homeShelves').replaceChildren(...sections);
	});
	hydrateArtistArt(panel);
	if (first) stagger(panel, '.home-shelf');

	requestPicks(topArtists(history), () => { shown = ''; renderHome(recent); });
}

/** Followed artists arrive from the computer or storage; Home asks once. */
export async function refreshFollowedOnHome(recent) {
	try {
		followed = (await loadFollowed()).slice(0, SHELF_SIZE);
	} catch {
		followed = [];
	}
	renderHome(recent);
}
