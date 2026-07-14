/**
 * Custom dropdown wired to the .rh-selectx CSS (rainette_pages.css) — that
 * CSS was fully built and styled but never connected to any JS, ported from
 * an old template. Native <select> popups can't be restyled to match the
 * app's theme (no way to reach the OS-drawn option list), which is why the
 * default-tab/sort/smart-playlist dropdowns looked out of place.
 *
 * createSelect() returns a root node with a live `.value` property and a
 * dispatched 'change' Event, so existing call sites built around a native
 * <select> (`el.value`, `el.addEventListener('change', ...)`) swap in with
 * minimal changes.
 */

let _closeOpenInstance = null;

export function createSelect({ options, value, onChange, ariaLabel, className = '' }) {
	const root = document.createElement('div');
	root.className = 'rh-selectx' + (className ? ' ' + className : '');
	root.setAttribute('role', 'combobox');
	root.setAttribute('aria-haspopup', 'listbox');
	if (ariaLabel) root.setAttribute('aria-label', ariaLabel);

	const button = document.createElement('button');
	button.type = 'button';
	button.className = 'rh-selectx-button';
	button.setAttribute('aria-expanded', 'false');

	const list = document.createElement('div');
	list.className = 'rh-selectx-list';
	list.setAttribute('role', 'listbox');

	root.append(button, list);

	let current = value;
	let items = [];

	function labelFor(v) {
		const found = options.find(([ov]) => ov === v);
		return found ? found[1] : '';
	}

	function renderItems() {
		list.innerHTML = '';
		items = options.map(([ov, label]) => {
			const item = document.createElement('button');
			item.type = 'button';
			item.className = 'rh-selectx-item' + (ov === current ? ' selected' : '');
			item.textContent = label;
			item.setAttribute('role', 'option');
			item.addEventListener('click', () => { commit(ov); closeSelf(); button.focus(); });
			list.appendChild(item);
			return item;
		});
	}

	function commit(v) {
		if (v === current) return;
		current = v;
		button.textContent = labelFor(current);
		renderItems();
		root.dispatchEvent(new Event('change', { bubbles: true }));
		onChange?.(current);
	}

	function onDocMouseDown(e) {
		if (!root.contains(e.target) && !list.contains(e.target)) closeSelf();
	}

	// A handful of call sites (e.g. the smart-playlist dialog) sit inside an
	// overflow-y:auto container. An element stays clipped by a scrolling
	// ancestor's box regardless of its own position value as long as it's
	// still a DOM descendant of it - position:fixed alone doesn't escape that.
	// So the list is reparented to document.body while open (a standard
	// "portal" pattern) and positioned with computed viewport coordinates.
	function positionList() {
		const r = button.getBoundingClientRect();
		const viewport = window.visualViewport;
		const safeTop = viewport?.offsetTop ?? 0;
		const viewportBottom = (viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight);
		const dock = document.querySelector('#rwDockedBar');
		const dockRect = dock && !dock.hidden ? dock.getBoundingClientRect() : null;
		// A fixed player bar is visually part of the viewport, but not a usable
		// region for a menu. Only reserve it when it actually reaches the bottom.
		const safeBottom = dockRect && dockRect.bottom >= viewportBottom - 1
			? Math.min(viewportBottom, dockRect.top)
			: viewportBottom;
		const gap = 6;
		const below = Math.max(0, safeBottom - r.bottom - gap);
		const above = Math.max(0, r.top - safeTop - gap);
		const naturalHeight = Math.min(280, list.scrollHeight || 280);
		const openAbove = below < naturalHeight && above > below;
		const available = openAbove ? above : below;
		const height = Math.max(72, Math.min(naturalHeight, available));
		list.style.left = r.left + 'px';
		list.style.top = openAbove ? (r.top - gap - height) + 'px' : (r.bottom + gap) + 'px';
		list.style.width = r.width + 'px';
		list.style.maxHeight = height + 'px';
		list.classList.toggle('opens-upward', openAbove);
	}

	function openSelf() {
		if (_closeOpenInstance && _closeOpenInstance !== closeSelf) _closeOpenInstance();
		document.body.appendChild(list);
		list.classList.add('open');
		positionList();
		root.classList.add('open');
		button.setAttribute('aria-expanded', 'true');
		_closeOpenInstance = closeSelf;
		document.addEventListener('mousedown', onDocMouseDown, true);
		document.addEventListener('scroll', positionList, true);
		window.addEventListener('resize', positionList);
		window.visualViewport?.addEventListener('resize', positionList);
		window.visualViewport?.addEventListener('scroll', positionList);
	}

	function closeSelf() {
		root.classList.remove('open');
		list.classList.remove('open');
		root.appendChild(list);
		button.setAttribute('aria-expanded', 'false');
		if (_closeOpenInstance === closeSelf) _closeOpenInstance = null;
		document.removeEventListener('mousedown', onDocMouseDown, true);
		document.removeEventListener('scroll', positionList, true);
		window.removeEventListener('resize', positionList);
		window.visualViewport?.removeEventListener('resize', positionList);
		window.visualViewport?.removeEventListener('scroll', positionList);
		list.style.removeProperty('max-height');
		list.classList.remove('opens-upward');
	}

	function highlightedIndex() {
		return items.findIndex(item => item.classList.contains('selected'));
	}

	// Arrow-key highlight is visual only (shares .selected with the actual
	// current value, matching what the existing CSS already supports) - it
	// doesn't commit until Enter, and Escape/blur discards it via renderItems().
	function moveHighlight(delta) {
		const from = highlightedIndex();
		const next = Math.max(0, Math.min(items.length - 1, (from < 0 ? 0 : from) + delta));
		items.forEach((item, i) => item.classList.toggle('selected', i === next));
		items[next]?.scrollIntoView({ block: 'nearest' });
	}

	button.addEventListener('click', () => { root.classList.contains('open') ? closeSelf() : openSelf(); });
	button.addEventListener('keydown', e => {
		const isOpen = root.classList.contains('open');
		if (e.key === 'Escape') {
			if (isOpen) { e.preventDefault(); renderItems(); closeSelf(); }
		} else if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			if (!isOpen) { openSelf(); renderItems(); return; }
			const i = highlightedIndex();
			if (i >= 0) commit(options[i][0]);
			closeSelf();
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			if (!isOpen) { openSelf(); renderItems(); } else moveHighlight(1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			if (!isOpen) { openSelf(); renderItems(); } else moveHighlight(-1);
		}
	});

	renderItems();
	button.textContent = labelFor(current);

	Object.defineProperty(root, 'value', {
		get: () => current,
		set(v) { current = v; button.textContent = labelFor(current); renderItems(); },
	});

	return root;
}
