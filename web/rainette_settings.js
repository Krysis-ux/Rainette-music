/**
 * Rainette Music — Settings tab.
 *
 * Appearance (theme + accent) applies directly to this document and persists
 * to localStorage, broadcasting to the detached player window so both stay in
 * sync while open. The equalizer panel doesn't own any audio state — the Web
 * Audio graph lives in the player window (miniplayer.js); this panel only
 * sends remote-control commands and mirrors whatever `music_eq_state`
 * broadcasts back (see miniplayer.js's wireRemote()/_broadcastEq()).
 */

import { sendHelper, el } from './music_shell.js';
import { createSelect } from './rainette_select.js';
import { customDialog, confirmDialog } from './rainette_modal.js';

const QUEUE_SUPPORTED = typeof window !== 'undefined' && !!window.RW_REMOTE;

const THEME_KEY = 'rainette.theme';
const ACCENT_KEY = 'rainette.accent';
const VOLUME_KEY = 'rw.mp.volume';
const REDUCED_MOTION_KEY = 'rainette.reducedMotion';
const AUTO_OPEN_QUEUE_KEY = 'rainette.autoOpenQueue';
const DEFAULT_TAB_KEY = 'rainette.defaultTab';
const MINIPLAYER_ENABLED_KEY = 'rainette.miniplayerEnabled';
const FADE_KEY = 'rainette.fadePlayPause';
const AUTOPLAY_SIMILAR_KEY = 'rainette.autoplaySimilar';

const EQ_BANDS = [
	{ label: 'Bass', short: '60' },
	{ label: 'Low', short: '250' },
	{ label: 'Mid', short: '1k' },
	{ label: 'High', short: '4k' },
	{ label: 'Treble', short: '12k' },
];
const EQ_MIN = -12, EQ_MAX = 12;
const EQ_PRESETS = ['Flat', 'Bass Boost', 'Vocal', 'Treble'];

function lsGet(k, fallback = null) { try { const v = localStorage.getItem(k); return v == null ? fallback : v; } catch { return fallback; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* best effort */ } }

const eqState = { on: false, gains: EQ_BANDS.map(() => 0) };
let eqEls = null;
let _listenerBound = false;

// ── Appearance ────────────────────────────────────────────────────────────

// Widened from the old binary 'dark'|else. 'dark' keeps mapping to
// rw-theme-dark exactly as before (no migration needed for existing users);
// index.html/miniplayer.html's boot-flash-prevention scripts and
// miniplayer.js's theme-relay handler mirror this same mapping.
const THEME_CLASS = { light: 'rw-theme-light', dark: 'rw-theme-dark', mono: 'rw-theme-mono', midnight: 'rw-theme-midnight' };
const THEME_IDS = Object.keys(THEME_CLASS);

function currentTheme() {
	const t = lsGet(THEME_KEY);
	return THEME_IDS.includes(t) ? t : 'light';
}
function currentAccent() { const a = lsGet(ACCENT_KEY); return a === 'teal' || a === 'purple' ? a : 'default'; }

function applyTheme(theme) {
	if (!THEME_IDS.includes(theme)) theme = 'light';
	lsSet(THEME_KEY, theme);
	document.documentElement.classList.remove(...Object.values(THEME_CLASS));
	document.documentElement.classList.add(THEME_CLASS[theme]);
	sendHelper({ type: 'music_theme_set', theme });
}

function applyAccent(accent) {
	lsSet(ACCENT_KEY, accent);
	document.documentElement.classList.remove('rw-accent-teal', 'rw-accent-purple');
	if (accent === 'teal' || accent === 'purple') document.documentElement.classList.add('rw-accent-' + accent);
	sendHelper({ type: 'music_accent_set', accent });
}

// ── Small building blocks ────────────────────────────────────────────────

function switchControl(checked, onChange) {
	const wrap = el('label', 'rw-switch');
	const input = document.createElement('input');
	input.type = 'checkbox';
	input.checked = checked;
	input.addEventListener('change', () => onChange(input.checked));
	wrap.append(input, el('span', 'rw-track'));
	return wrap;
}

function settingsRow(label, control, hint) {
	const row = el('div', 'rw-settings-row');
	const text = el('div', 'rw-settings-row-text');
	text.appendChild(el('div', 'rw-settings-row-label', label));
	if (hint) text.appendChild(el('div', 'rw-settings-row-hint', hint));
	row.append(text, control);
	return row;
}

function settingsCard(title, sub) {
	const card = el('div', 'rw-bubble rw-bubble-pad rw-settings-card');
	card.appendChild(el('h3', 'rw-settings-card-title', title));
	if (sub) card.appendChild(el('p', 'rw-settings-card-sub', sub));
	return card;
}

// ── Sections ──────────────────────────────────────────────────────────────

function renderAppearance() {
	const card = settingsCard('Appearance', 'Theme and accent color, synced live with the player window.');

	const themeRow = el('div', 'rw-settings-row');
	const themeText = el('div', 'rw-settings-row-text');
	themeText.appendChild(el('div', 'rw-settings-row-label', 'Theme'));
	themeRow.appendChild(themeText);
	const themePicker = el('div', 'rw-theme-picker');
	const themeOptions = [['light', 'Light'], ['dark', 'Dark'], ['mono', 'Monochrome'], ['midnight', 'Midnight']];
	const activeTheme = currentTheme();
	for (const [id, name] of themeOptions) {
		const swatch = document.createElement('button');
		swatch.type = 'button';
		swatch.className = 'rw-theme-swatch' + (activeTheme === id ? ' on' : '');
		swatch.title = name;
		swatch.setAttribute('aria-label', name);
		// The theme class lives on the small preview span, not the button
		// itself, so its --rw-* custom properties resolve to that theme's
		// real values for an accurate live preview - without also recoloring
		// the label text next to it, which needs to stay legible against the
		// *actual* current theme, not whichever one this swatch is previewing.
		swatch.appendChild(el('span', 'rw-theme-swatch-preview ' + THEME_CLASS[id]));
		swatch.appendChild(el('span', 'rw-theme-swatch-label', name));
		swatch.addEventListener('click', () => {
			applyTheme(id);
			themePicker.querySelectorAll('.rw-theme-swatch').forEach(s => s.classList.remove('on'));
			swatch.classList.add('on');
		});
		themePicker.appendChild(swatch);
	}
	themeRow.appendChild(themePicker);
	card.appendChild(themeRow);

	const accentRow = el('div', 'rw-settings-row');
	const accentText = el('div', 'rw-settings-row-text');
	accentText.appendChild(el('div', 'rw-settings-row-label', 'Accent color'));
	accentRow.appendChild(accentText);
	const picker = el('div', 'rw-accent-picker');
	const options = [['default', 'Default'], ['teal', 'Teal'], ['purple', 'Purple']];
	const active = currentAccent();
	for (const [id, name] of options) {
		const swatch = document.createElement('button');
		swatch.type = 'button';
		swatch.className = 'rw-accent-swatch rw-accent-swatch-' + id + (active === id ? ' on' : '');
		swatch.title = name;
		swatch.setAttribute('aria-label', name);
		swatch.addEventListener('click', () => {
			applyAccent(id);
			picker.querySelectorAll('.rw-accent-swatch').forEach(s => s.classList.remove('on'));
			swatch.classList.add('on');
		});
		picker.appendChild(swatch);
	}
	accentRow.appendChild(picker);
	card.appendChild(accentRow);
	return card;
}

function eqControl(payload) {
	sendHelper({ type: 'music_remote_control', ...payload });
}

function syncEqUi() {
	if (!eqEls) return;
	if (eqEls.onInput) eqEls.onInput.checked = eqState.on;
	eqEls.sliders.forEach((s, i) => { if (document.activeElement !== s) s.value = String(eqState.gains[i] || 0); });
}

function renderEqualizer() {
	const card = settingsCard('Equalizer', 'Moved here from the mini player — applies live to the desktop player.');
	if (!QUEUE_SUPPORTED) {
		card.appendChild(el('p', 'rw-status-line', 'The equalizer requires the desktop app (native player window).'));
		eqEls = null;
		return card;
	}

	const onSwitch = switchControl(eqState.on, on => eqControl({ action: 'eq_set_on', on }));
	card.appendChild(settingsRow('Enable equalizer', onSwitch));

	const presetsWrap = el('div', 'rw-eq-presets');
	for (const name of EQ_PRESETS) {
		const b = document.createElement('button');
		b.type = 'button';
		b.className = 'rw-chip';
		b.textContent = name;
		b.addEventListener('click', () => eqControl({ action: 'eq_apply_preset', preset: name }));
		presetsWrap.appendChild(b);
	}
	card.appendChild(presetsWrap);

	const bandsWrap = el('div', 'rw-eq-bands');
	const sliders = [];
	EQ_BANDS.forEach((band, i) => {
		const col = el('div', 'rw-eq-band');
		const slider = document.createElement('input');
		slider.type = 'range';
		slider.className = 'rw-eq-slider';
		slider.min = String(EQ_MIN);
		slider.max = String(EQ_MAX);
		slider.step = '1';
		slider.value = String(eqState.gains[i] || 0);
		slider.setAttribute('orient', 'vertical');
		slider.addEventListener('input', () => eqControl({ action: 'eq_set_band', index: i, gain: Number(slider.value) }));
		sliders.push(slider);
		col.append(slider, el('span', 'rw-eq-band-label', band.label), el('span', 'rw-eq-band-hz', band.short));
		bandsWrap.appendChild(col);
	});
	card.appendChild(bandsWrap);

	eqEls = { onInput: onSwitch.querySelector('input'), sliders };
	syncEqUi();
	return card;
}

function renderPlaybackDefaults() {
	const card = settingsCard('Playback', 'How the desktop player starts, fades, and keeps the music going.');
	if (!QUEUE_SUPPORTED) {
		card.appendChild(el('p', 'rw-status-line', 'Playback defaults require the desktop app.'));
		return card;
	}

	// Both toggles are read live by the player engine (miniplayer.js) from the
	// shared same-origin localStorage - no relay message or restart needed.
	card.appendChild(settingsRow('Fade on play and pause', switchControl(lsGet(FADE_KEY) !== '0', on => {
		lsSet(FADE_KEY, on ? '1' : '0');
	}), 'A soft volume ramp instead of a hard cut when playback starts or stops.'));

	card.appendChild(settingsRow('Autoplay similar when queue ends', switchControl(lsGet(AUTOPLAY_SIMILAR_KEY) === '1', on => {
		lsSet(AUTOPLAY_SIMILAR_KEY, on ? '1' : '0');
	}), 'When Up Next runs out, keep going with a mix seeded from the last track.'));
	const initial = Math.min(1.5, Math.max(0, Number(lsGet(VOLUME_KEY, '1')) || 1));
	// The slider's own value *is* the percentage (100 = unity/full volume),
	// matching how every other surface in the app displays volume
	// (Math.round(state.volume * 100), e.g. miniplayer.js) - no separate
	// ceiling-relative conversion, so there's no dead zone at the top end.
	const pct = Math.round(initial * 100);

	const row = el('div', 'rw-settings-row');
	const text = el('div', 'rw-settings-row-text');
	text.appendChild(el('div', 'rw-settings-row-label', 'Default volume'));
	const valueHint = el('div', 'rw-settings-row-hint', pct + '%');
	text.appendChild(valueHint);
	row.appendChild(text);

	const slider = document.createElement('input');
	slider.type = 'range';
	slider.className = 'rw-settings-slider';
	slider.min = '0';
	slider.max = '150';
	slider.step = '1';
	slider.value = String(pct);
	slider.addEventListener('input', () => {
		lsSet(VOLUME_KEY, String(Number(slider.value) / 100));
		valueHint.textContent = slider.value + '%';
	});
	row.appendChild(slider);
	card.appendChild(row);
	return card;
}

function renderBehavior() {
	const card = settingsCard('Behavior');
	card.appendChild(settingsRow('Reduce motion', switchControl(lsGet(REDUCED_MOTION_KEY) === '1', on => {
		lsSet(REDUCED_MOTION_KEY, on ? '1' : '0');
		document.documentElement.classList.toggle('rw-reduced-motion', on);
	}), 'Turns off page and card entrance animations.'));

	if (window.RW_REMOTE) {
		card.appendChild(settingsRow('Auto-open mini player', switchControl(lsGet(MINIPLAYER_ENABLED_KEY) === '1', on => {
			lsSet(MINIPLAYER_ENABLED_KEY, on ? '1' : '0');
			window.RW_MINIPLAYER_ENABLED = on;
		}), 'Off by default. The bottom player bar always controls playback. Use Open mini player there whenever you want the separate window.'));
	}

	if (QUEUE_SUPPORTED) {
		card.appendChild(settingsRow('Auto-open queue on play', switchControl(lsGet(AUTO_OPEN_QUEUE_KEY) === '1', on => {
			lsSet(AUTO_OPEN_QUEUE_KEY, on ? '1' : '0');
		}), 'Opens the queue drawer whenever a new track starts.'));
	}

	const tabRow = el('div', 'rw-settings-row');
	const text = el('div', 'rw-settings-row-text');
	text.appendChild(el('div', 'rw-settings-row-label', 'Default tab on launch'));
	tabRow.appendChild(text);
	const tabs = [['home', 'Home'], ['search', 'Search'], ['songs', 'Songs'], ['following', 'Following'], ['recent', 'Recents'], ['playlists', 'Playlists'], ['insights', 'Insights']];
	if (QUEUE_SUPPORTED) tabs.push(['queue', 'Queue']);
	const select = createSelect({
		options: tabs,
		value: defaultLandingTab(),
		ariaLabel: 'Default tab on launch',
		onChange: v => lsSet(DEFAULT_TAB_KEY, v),
	});
	tabRow.appendChild(select);
	card.appendChild(tabRow);
	return card;
}

// ── Danger zone ───────────────────────────────────────────────────────────

// Each entry is a checkbox in the clear-data picker. `client: true` means it is
// cleared here in the browser (localStorage); the rest are erased server-side by
// the music_clear_data command, keyed by these ids (see state.clear_user_data).
// -- App updates ------------------------------------------------------------

const UPDATE_REPOSITORY_URL = 'https://github.com/Krysis-ux/Rainette-music';

function updateCopy(state = {}) {
	const result = state.result || {};
	const current = result.current || '';
	const latest = result.latest || result.version || '';
	if (!state.nativeAvailable && state.phase !== 'checking') {
		return ['Updates are available in the Windows app', 'Open the installed desktop app to check and install verified releases.'];
	}
	if (state.phase === 'checking') {
		return ['Checking for updates...', 'Looking for an eligible signed Windows release on GitHub.'];
	}
	if (state.phase === 'downloading') {
		const pct = Math.max(0, Math.min(100, Math.round((Number(state.progress) || 0) * 100)));
		return [latest ? `Downloading ${latest}... ${pct}%` : `Downloading update... ${pct}%`,
			'The installer is verified against Rainette\'s release signature before it runs.'];
	}
	if (state.phase === 'verifying') {
		return ['Verifying the update...', 'Checking the download\'s signature before it runs.'];
	}
	if (state.phase === 'installing') {
		return [latest ? `Installing ${latest}...` : 'Installing update...', 'Rainette will close only after the installer has been verified and started.'];
	}
	if (state.phase === 'stale') {
		return ['The available update changed', state.message || 'Check again before installing.'];
	}
	if (state.phase === 'error') {
		return ['The update could not be installed', state.message || 'Try the installation again, or check for a refreshed release.'];
	}
	if (result.status === 'update' && state.candidateId) {
		return [latest ? `${latest} is ready` : 'An update is ready', state.message || 'The signed Windows installer will be verified before it opens.'];
	}
	if (result.status === 'current') {
		return ['Rainette is up to date', current ? `You have version ${current}. No eligible Windows update is available.` : 'No eligible Windows update is available.'];
	}
	if (result.status === 'unavailable' || state.phase === 'unsupported') {
		return ['Updates are unavailable here', state.message || 'Update checks are available in the installed Windows app.'];
	}
	if (result.status === 'check_failed') {
		return ['Could not check for updates', state.message || 'Check your connection and try again.'];
	}
	return ['Check for updates', 'Rainette checks GitHub for eligible Windows releases.'];
}

export function syncUpdateSettings(host, state = {}) {
	const card = host?.querySelector('#rwUpdateSettings');
	if (!card) return;
	const [label, hint] = updateCopy(state);
	const status = card.querySelector('.rw-update-settings-status');
	const detail = card.querySelector('.rw-update-settings-detail');
	const check = card.querySelector('.rw-update-settings-check');
	const install = card.querySelector('.rw-update-settings-install');
	const progressTrack = card.querySelector('.rw-update-settings-progress');
	const progressFill = card.querySelector('.rw-update-settings-progress-fill');
	if (status) status.textContent = label;
	if (detail) detail.textContent = hint;
	const installing = state.phase === 'downloading' || state.phase === 'verifying' || state.phase === 'installing';
	const busy = state.phase === 'checking' || installing;
	if (check) {
		check.disabled = busy || !state.nativeAvailable;
		check.textContent = state.phase === 'checking' ? 'Checking...' : (state.result ? 'Check again' : 'Check now');
	}
	if (install) {
		install.hidden = !(state.result?.status === 'update' && state.candidateId);
		install.disabled = busy;
		install.textContent = installing
			? (state.phase === 'downloading' ? 'Downloading...' : 'Installing...')
			: (state.phase === 'error' ? 'Try installation again' : 'Download and install');
	}
	if (progressTrack) progressTrack.hidden = !installing;
	if (progressFill) {
		progressFill.style.width = state.phase === 'downloading'
			? Math.max(0, Math.min(100, Math.round((Number(state.progress) || 0) * 100))) + '%'
			: (installing ? '100%' : '0%');
	}
	card.classList.toggle('is-busy', busy);
}

function renderAppUpdates(updater = {}) {
	const card = settingsCard('App updates', 'Check the official Rainette repository for a newer Windows release.');
	card.id = 'rwUpdateSettings';

	const row = el('div', 'rw-settings-row rw-update-settings-row');
	const text = el('div', 'rw-settings-row-text');
	const status = el('div', 'rw-settings-row-label rw-update-settings-status');
	status.setAttribute('role', 'status');
	status.setAttribute('aria-live', 'polite');
	text.append(status, el('div', 'rw-settings-row-hint rw-update-settings-detail'));

	const actions = el('div', 'rw-update-settings-actions');
	const check = document.createElement('button');
	check.type = 'button';
	check.className = 'rw-btn rw-btn-ghost rw-update-settings-check';
	check.addEventListener('click', () => updater.check?.({ manual: true }));
	const install = document.createElement('button');
	install.type = 'button';
	install.className = 'rw-btn rw-btn-primary rw-update-settings-install';
	install.addEventListener('click', () => updater.install?.());
	actions.append(check, install);
	row.append(text, actions);
	card.appendChild(row);

	// Byte-progress bar for the download; hidden except while installing.
	const progress = el('div', 'rw-update-settings-progress');
	progress.hidden = true;
	progress.appendChild(el('div', 'rw-update-settings-progress-fill'));
	card.appendChild(progress);

	const source = el('p', 'rw-update-settings-source');
	source.append('Release source: ');
	const link = document.createElement('a');
	link.href = UPDATE_REPOSITORY_URL;
	link.target = '_blank';
	link.rel = 'noopener noreferrer';
	link.textContent = 'Krysis-ux/Rainette-music';
	source.appendChild(link);
	card.appendChild(source);
	syncUpdateSettings({ querySelector: selector => selector === '#rwUpdateSettings' ? card : null }, updater.snapshot?.() || {});
	return card;
}

const CLEAR_CATEGORIES = [
	{ id: 'recents', label: 'Recently played & history', hint: 'Your play history, Recents, and Insights.' },
	{ id: 'following', label: 'Followed artists', hint: 'Every artist you follow.' },
	{ id: 'playlists', label: 'Playlists & folders', hint: 'Playlists you made, their folders, and saved artwork.' },
	{ id: 'queues', label: 'Saved queues', hint: 'Saved queues and the restored last session.' },
	{ id: 'preferences', label: 'Appearance & preferences', hint: 'Theme, accent, equalizer, and other local settings.', client: true },
];

function dangerButton(label, onClick) {
	const button = document.createElement('button');
	button.type = 'button';
	button.className = 'rw-btn rw-btn-danger';
	button.textContent = label;
	if (onClick) button.addEventListener('click', onClick);
	return button;
}

// Both app namespaces cover every current and future preference key, including
// the rainette.musicFilters.* / rainette.folderClosed.* prefix families, so
// clearing by namespace is more robust than enumerating individual keys.
function clearLocalPreferences() {
	try {
		for (const key of Object.keys(localStorage)) {
			if (key.startsWith('rainette.') || key.startsWith('rw.mp.')) localStorage.removeItem(key);
		}
	} catch { /* best effort */ }
}

async function performClear(selected) {
	if (selected.length === CLEAR_CATEGORIES.length) {
		const ok = await confirmDialog({
			title: 'Erase everything?',
			message: 'This removes your entire Rainette library on this computer — recents, follows, playlists, saved queues, and preferences. It cannot be undone.',
			confirmLabel: 'Erase everything',
			danger: true,
		});
		if (!ok) return;
	}
	const serverCategories = selected.filter(id => id !== 'preferences');
	if (serverCategories.length) sendHelper({ type: 'music_clear_data', categories: serverCategories });
	if (selected.includes('preferences')) {
		clearLocalPreferences();
		// Reload so appearance and defaults fall back cleanly, after a short beat
		// that lets the clear command flush over the socket first.
		setTimeout(() => location.reload(), 250);
	}
}

function openClearDataDialog() {
	const body = el('div', 'rw-clear-data');
	body.appendChild(el('p', 'rw-modal-message', 'Choose what to remove from this computer. This cannot be undone.'));

	const boxes = [];
	const syncFromMaster = master => boxes.forEach(box => { box.checked = master.checked; });
	const list = el('div', 'rw-clear-list');

	const allRow = el('label', 'rw-clear-row rw-clear-row-all');
	const allBox = document.createElement('input');
	allBox.type = 'checkbox';
	allRow.append(allBox, el('span', 'rw-clear-row-label', 'Select everything'));
	list.appendChild(allRow);

	for (const category of CLEAR_CATEGORIES) {
		const row = el('label', 'rw-clear-row');
		const box = document.createElement('input');
		box.type = 'checkbox';
		box.value = category.id;
		const text = el('div', 'rw-clear-row-text');
		text.append(el('span', 'rw-clear-row-label', category.label), el('span', 'rw-clear-row-hint', category.hint));
		row.append(box, text);
		list.appendChild(row);
		boxes.push(box);
	}
	body.appendChild(list);

	return customDialog({
		title: 'Clear local data',
		bodyNode: body,
		className: 'rw-clear-data-modal',
		wire: close => {
			const clearBtn = dangerButton('Clear selected', async () => {
				const selected = boxes.filter(box => box.checked).map(box => box.value);
				if (!selected.length) return;
				close(true);
				await performClear(selected);
			});
			const refresh = () => {
				allBox.checked = boxes.every(box => box.checked);
				clearBtn.disabled = !boxes.some(box => box.checked);
			};
			allBox.addEventListener('change', () => { syncFromMaster(allBox); refresh(); });
			boxes.forEach(box => box.addEventListener('change', refresh));
			refresh();
			const cancel = document.createElement('button');
			cancel.type = 'button';
			cancel.className = 'rw-btn rw-btn-ghost';
			cancel.textContent = 'Cancel';
			cancel.addEventListener('click', () => close(null));
			return [cancel, clearBtn];
		},
	});
}

function renderDangerZone() {
	const card = settingsCard('Danger zone', 'Permanently erase data stored on this computer.');
	card.classList.add('rw-danger-zone');
	const control = dangerButton('Clear local data…', openClearDataDialog);
	card.appendChild(settingsRow('Clear local data', control,
		'Pick exactly what to remove — recents, follows, playlists, queues, or preferences.'));
	return card;
}

// ── Mount ─────────────────────────────────────────────────────────────────

function bindEqListener() {
	if (_listenerBound) return;
	_listenerBound = true;
	document.addEventListener('rainette:helper-message', e => {
		const msg = e.detail;
		if (msg?.type !== 'music_eq_state') return;
		eqState.on = !!msg.on;
		if (Array.isArray(msg.gains)) eqState.gains = msg.gains.slice();
		syncEqUi();
	});
}

export function renderSettings(host, updater = {}) {
	const body = host.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	const wrap = el('div', 'rw-settings-wrap');
	wrap.append(renderAppearance(), renderEqualizer(), renderPlaybackDefaults(), renderBehavior(), renderAppUpdates(updater), renderDangerZone());
	body.appendChild(wrap);
	bindEqListener();
	if (QUEUE_SUPPORTED) eqControl({ action: 'eq_request_state' });
}

export function defaultLandingTab() {
	const saved = lsGet(DEFAULT_TAB_KEY, 'home');
	if (saved === 'artists') return 'following';
	if (saved === 'albums') return 'recent';
	return saved;
}

export function shouldAutoOpenQueue() {
	return lsGet(AUTO_OPEN_QUEUE_KEY) === '1';
}
