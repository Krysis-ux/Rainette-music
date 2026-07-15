/** Rainette's shared Kagebana activity indicator.
 *
 * The seven petals open in place instead of spinning the entire app icon. That
 * keeps the motion quiet, recognisable, and useful beside real async work.
 */

function escapeHtml(value) {
	return String(value || '').replace(/[&<>"']/g, character => ({
		'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
	})[character]);
}

export function rainetteLoaderMarkup(label = 'Loading', { compact = false, state = 'active' } = {}) {
	const safeLabel = escapeHtml(label);
	const petals = Array.from({ length: 7 }, (_, index) =>
		`<i class="rw-kage-loader-petal" style="--rw-petal:${index}" aria-hidden="true"></i>`
	).join('');
	return `<div class="rw-kage-loader${compact ? ' is-compact' : ''}" data-state="${escapeHtml(state)}" role="status" aria-live="polite" aria-label="${safeLabel}">
		<span class="rw-kage-loader-seal" aria-hidden="true">${petals}<i class="rw-kage-loader-heart"></i><i class="rw-kage-loader-orbit"></i></span>
		<span class="rw-kage-loader-label">${safeLabel}</span>
	</div>`;
}

export function createRainetteLoader(label = 'Loading', options = {}) {
	const template = document.createElement('template');
	template.innerHTML = rainetteLoaderMarkup(label, options).trim();
	return template.content.firstElementChild;
}
