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
	return THEME_IDS.includes(t) ? t : 'mono';
}
function currentAccent() { const a = lsGet(ACCENT_KEY); return a === 'teal' || a === 'purple' ? a : 'default'; }

function applyTheme(theme) {
	if (!THEME_IDS.includes(theme)) theme = 'mono';
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
	const card = settingsCard('Appearance', 'Theme and accent color — synced live with the player window.');

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
		card.appendChild(settingsRow('Miniplayer window', switchControl(lsGet(MINIPLAYER_ENABLED_KEY) !== '0', on => {
			lsSet(MINIPLAYER_ENABLED_KEY, on ? '1' : '0');
		}), 'Restart Rainette Music to apply. The docked bar at the bottom of this window always shows playback controls; this only controls whether a separate floating player window also pops out, synced with it.'));
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

export function renderSettings(host) {
	const body = host.querySelector('#rwMusicBody');
	if (!body) return;
	body.innerHTML = '';
	const wrap = el('div', 'rw-settings-wrap');
	wrap.append(renderAppearance(), renderEqualizer(), renderPlaybackDefaults(), renderBehavior());
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
