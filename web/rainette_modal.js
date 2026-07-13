/**
 * Small custom modal helpers — replace the native confirm()/alert()/prompt()
 * dialogs with UI that matches the rest of the app. Each exported helper
 * returns a Promise that resolves on confirm/cancel/backdrop-click/Escape and
 * removes the modal from the DOM once it settles.
 */

function _button(label, className, onClick) {
	const b = document.createElement('button');
	b.type = 'button';
	b.className = className;
	b.textContent = label;
	b.addEventListener('click', onClick);
	return b;
}

// `wire(close)` builds/attaches any interactive body content that needs to
// call `close(value)` (e.g. picker items), and returns the action-row buttons.
function _openModal({ title, bodyNode, wire }) {
	return new Promise(resolve => {
		let settled = false;
		function close(value) {
			if (settled) return;
			settled = true;
			document.removeEventListener('keydown', onKey);
			backdrop.remove();
			resolve(value);
		}
		function onKey(e) {
			if (e.key === 'Escape') close(null);
		}

		const backdrop = document.createElement('div');
		backdrop.className = 'rw-modal-backdrop';
		backdrop.addEventListener('mousedown', e => { if (e.target === backdrop) close(null); });

		const modal = document.createElement('div');
		modal.className = 'rw-modal';
		modal.setAttribute('role', 'dialog');
		modal.setAttribute('aria-modal', 'true');
		modal.setAttribute('aria-label', title);

		const head = document.createElement('div');
		head.className = 'rw-modal-head';
		head.textContent = title;

		const body = document.createElement('div');
		body.className = 'rw-modal-body';
		body.appendChild(bodyNode);

		const actionsWrap = document.createElement('div');
		actionsWrap.className = 'rw-modal-actions';
		for (const btn of wire(close)) actionsWrap.appendChild(btn);

		modal.append(head, body, actionsWrap);
		backdrop.appendChild(modal);
		document.addEventListener('keydown', onKey);
		document.body.appendChild(backdrop);

		const firstInput = body.querySelector('input');
		if (firstInput) { firstInput.focus(); firstInput.select(); }
		else actionsWrap.querySelector('button')?.focus();
	});
}

export function infoDialog({ title = 'Notice', message = '', okLabel = 'OK' } = {}) {
	const body = document.createElement('p');
	body.className = 'rw-modal-message';
	body.textContent = message;
	return _openModal({
		title,
		bodyNode: body,
		wire: close => [_button(okLabel, 'rw-btn rw-btn-primary', () => close(true))],
	});
}

export function confirmDialog({ title = 'Are you sure?', message = '', confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = false } = {}) {
	const body = document.createElement('p');
	body.className = 'rw-modal-message';
	body.textContent = message;
	return _openModal({
		title,
		bodyNode: body,
		wire: close => [
			_button(cancelLabel, 'rw-btn rw-btn-ghost', () => close(false)),
			_button(confirmLabel, 'rw-btn ' + (danger ? 'rw-btn-danger' : 'rw-btn-primary'), () => close(true)),
		],
	});
}

export function textPrompt({ title = 'Enter a value', label = '', defaultValue = '', confirmLabel = 'Save', cancelLabel = 'Cancel' } = {}) {
	const wrap = document.createElement('div');
	if (label) {
		const l = document.createElement('label');
		l.className = 'rw-label';
		l.textContent = label;
		wrap.appendChild(l);
	}
	const input = document.createElement('input');
	input.type = 'text';
	input.className = 'rw-input';
	input.value = defaultValue;
	wrap.appendChild(input);

	return _openModal({
		title,
		bodyNode: wrap,
		wire: close => {
			const submit = () => close(input.value.trim() || null);
			input.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
			return [
				_button(cancelLabel, 'rw-btn rw-btn-ghost', () => close(null)),
				_button(confirmLabel, 'rw-btn rw-btn-primary', submit),
			];
		},
	});
}

// items: [{ id, label }]. Resolves with the chosen item's id, or null on cancel.
export function pickerDialog({ title = 'Choose one', items = [], cancelLabel = 'Cancel' } = {}) {
	const list = document.createElement('div');
	list.className = 'rw-modal-picker-list';
	if (!items.length) {
		const empty = document.createElement('p');
		empty.className = 'rw-modal-message';
		empty.textContent = 'Nothing to choose from.';
		list.appendChild(empty);
	}
	return _openModal({
		title,
		bodyNode: list,
		wire: close => {
			for (const item of items) {
				list.appendChild(_button(item.label, 'rw-modal-picker-item', () => close(item.id)));
			}
			return [_button(cancelLabel, 'rw-btn rw-btn-ghost', () => close(null))];
		},
	});
}

export function customDialog({ title = 'Dialog', bodyNode, wire, className = '' } = {}) {
	const body = bodyNode || document.createElement('div');
	if (className) body.classList.add(className);
	return _openModal({
		title,
		bodyNode: body,
		wire: typeof wire === 'function' ? wire : close => [_button('Close', 'rw-btn rw-btn-primary', () => close(true))],
	});
}

export function actionSheet({ title = 'Actions', items = [], cancelLabel = 'Cancel' } = {}) {
	const list = document.createElement('div');
	list.className = 'rw-modal-action-list';
	return _openModal({
		title,
		bodyNode: list,
		wire: close => {
			for (const item of items.filter(Boolean)) {
				const b = _button(item.label, 'rw-modal-action-item' + (item.danger ? ' danger' : ''), () => {
					close(item.id || item.label);
					if (typeof item.run === 'function') item.run();
				});
				if (item.hint) {
					const hint = document.createElement('span');
					hint.textContent = item.hint;
					b.appendChild(hint);
				}
				list.appendChild(b);
			}
			if (!items.length) {
				const empty = document.createElement('p');
				empty.className = 'rw-modal-message';
				empty.textContent = 'No actions available.';
				list.appendChild(empty);
			}
			return [_button(cancelLabel, 'rw-btn rw-btn-ghost', () => close(null))];
		},
	});
}
