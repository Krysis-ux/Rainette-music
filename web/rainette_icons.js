/**
 * Shared inline-SVG icon set for Rainette Music.
 *
 * Consolidates the icon paths that used to be duplicated between
 * miniplayer.js and rainette_music_player.js, and adds the extra icons
 * needed to replace literal text-character button labels ('>', '+', 'x', ...)
 * across the Music page.
 */

const PATHS = {
	prev: '<path d="M6 5v14"/><path d="M18 5.5v13a.6.6 0 0 1-.94.49L8 12.5a.6.6 0 0 1 0-.98l9.06-6.5a.6.6 0 0 1 .94.48z" fill="currentColor" stroke="none"/>',
	next: '<path d="M18 5v14"/><path d="M6 5.5v13a.6.6 0 0 0 .94.49L16 12.5a.6.6 0 0 0 0-.98L6.94 5.02a.6.6 0 0 0-.94.48z" fill="currentColor" stroke="none"/>',
	play: '<path d="M8 5v14l11-7-11-7z" fill="currentColor" stroke="none"/>',
	pause: '<path d="M7 5h4v14H7z" fill="currentColor" stroke="none"/><path d="M13 5h4v14h-4z" fill="currentColor" stroke="none"/>',
	loop: '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
	chevronDown: '<path d="m6 9 6 6 6-6"/>',
	chevronUp: '<path d="m18 15-6-6-6 6"/>',
	chevronRight: '<path d="m9 18 6-6-6-6"/>',
	plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
	listPlay: '<path d="M4 7h10"/><path d="M4 12h6"/><path d="M4 17h6"/><path d="M14.5 9.35v5.3a.5.5 0 0 0 .77.42l4.24-2.65a.5.5 0 0 0 0-.84L15.27 8.93a.5.5 0 0 0-.77.42z" fill="currentColor" stroke="none"/>',
	listAdd: '<path d="M4 7h13"/><path d="M4 12h9"/><path d="M4 17h9"/><path d="M18 14v6"/><path d="M15 17h6"/>',
	trash: '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
	close: '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
	more: '<circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
	search: '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="7"/>',
	folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
	pin: '<path d="M12 17v5"/><path d="M5 17h14"/><path d="m7 10 5-7 5 7"/><path d="M8 10h8v7H8z"/>',
	edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
	save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/>',
	shuffle: '<path d="M16 3h5v5"/><path d="M4 20 21 3"/><path d="M21 16v5h-5"/><path d="m15 15 6 6"/><path d="m4 4 5 5"/>',
	clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
	volume: '<path d="M11 5 6 9H2v6h4l5 4z" fill="currentColor" stroke="none"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
	mic: '<path d="M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 11a7 7 0 0 1-14 0"/><path d="M12 18v4"/>',
	moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
	chart: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M8 17v-6"/><path d="M13 17V7"/><path d="M18 17v-3"/>',
	keyboard: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01"/><path d="M10 10h.01"/><path d="M14 10h.01"/><path d="M18 10h.01"/><path d="M7 14h10"/>',
};

export function iconMarkup(name, size = 16) {
	const inner = PATHS[name] || '';
	return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${inner}</svg>`;
}
