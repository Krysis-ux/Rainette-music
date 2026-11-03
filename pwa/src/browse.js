/* Browse: YouTube Music's editors' playlists, by mood and by genre.
 * Shown on Search before anything is typed. */

import { el, tap, stagger } from './dom.js';
import { openSheet } from './sheets.js';
import { command } from './bridge.js';
import { openAlbum } from './catalog.js';
import { artworkFor } from './state.js';

let loaded = null;

/** Fill `host` with the mood and genre tiles. Quietly empty when unavailable. */
export async function renderBrowse(host) {
	if (host.childElementCount) return;
	loaded ||= command('music_moods', {}, 20000).catch(() => null);
	const result = await loaded;
	const sections = Array.isArray(result?.sections) ? result.sections : [];
	if (!sections.length) {
		loaded = null;   // try again next visit
		return;
	}
	host.replaceChildren(...sections.map(section => el('section', 'browse-section',
		el('h2', 'browse-title', section.title),
		el('div', 'browse-grid', ...section.items.map(tile)),
	)));
	stagger(host, '.browse-tile');
}

function tile(item) {
	const button = el('button', 'browse-tile', el('span', '', item.title));
	button.type = 'button';
	button.style.setProperty('--tile', item.color || '#636366');
	button.addEventListener('click', () => { tap(); openMood(item); });
	return button;
}

function openMood(item) {
	openSheet({
		title: item.title,
		className: 'sheet-catalog',
		full: true,
		build: async ({ body }) => {
			body.append(el('h2', 'sheet-title sheet-drag', item.title));
			const status = el('p', 'empty', 'Loading…');
			body.append(status);
			let page;
			try {
				page = await command('music_mood_playlists', { params: item.params }, 25000);
			} catch (error) {
				status.textContent = error?.message || 'That didn’t load. Try again.';
				return;
			}
			const shelves = Array.isArray(page?.shelves) ? page.shelves : [];
			status.remove();
			if (!shelves.length) {
				body.append(el('p', 'empty', 'Nothing here right now.'));
				return;
			}
			for (const shelf of shelves) {
				body.append(el('section', 'home-shelf',
					el('div', 'section-head', el('h2', '', shelf.title)),
					el('div', 'shelf-row', ...shelf.playlists.map(card)),
				));
			}
		},
	});
}

function card(playlist) {
	const art = document.createElement('img');
	art.className = 'shelf-art';
	art.src = playlist.thumbnail_url ? artworkFor(playlist.thumbnail_url, 148) : './icon.svg';
	art.alt = '';
	art.width = 148;
	art.height = 148;
	art.loading = 'lazy';
	art.decoding = 'async';
	art.referrerPolicy = 'no-referrer';
	const button = el('button', 'shelf-card', art, el('b', '', playlist.title), el('span', '', playlist.subtitle));
	button.type = 'button';
	button.addEventListener('click', () => {
		tap();
		openAlbum({ id: playlist.id, title: playlist.title, thumbnail_url: playlist.thumbnail_url });
	});
	return button;
}
