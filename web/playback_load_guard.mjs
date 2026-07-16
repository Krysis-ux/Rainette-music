/** Guards asynchronous stream resolution and limits recovery to one retry. */
export class PlaybackLoadGuard {
	constructor() {
		this.generation = 0;
		this.currentKey = '';
		this.currentAttempt = 0;
		this.retriedGeneration = -1;
	}

	begin(trackKey) {
		this.generation += 1;
		this.currentKey = String(trackKey || '');
		this.currentAttempt = 0;
		return Object.freeze({ generation: this.generation, attempt: 0, trackKey: this.currentKey });
	}

	isCurrent(token, trackKey) {
		return !!token
			&& token.generation === this.generation
			&& token.attempt === this.currentAttempt
			&& token.trackKey === this.currentKey
			&& String(trackKey || '') === this.currentKey;
	}

	advance(token, trackKey) {
		if (!this.isCurrent(token, trackKey)) return null;
		this.currentAttempt += 1;
		return Object.freeze({
			generation: this.generation,
			attempt: this.currentAttempt,
			trackKey: this.currentKey,
		});
	}

	claimRetry(token, trackKey) {
		if (!this.isCurrent(token, trackKey) || this.retriedGeneration === token.generation) return false;
		this.retriedGeneration = token.generation;
		return true;
	}
}

/** Settle a promise into an explicit result before a bounded deadline.
 *
 * MediaElement.play() is allowed to remain pending while the user agent waits
 * for media.  Turning that into a value (instead of throwing from a race) keeps
 * late rejection handlers attached and prevents unhandled-promise noise after
 * the timeout has already moved playback into its guarded retry path.
 */
export function settleWithin(value, timeoutMs) {
	return new Promise(resolve => {
		let finished = false;
		const finish = result => {
			if (finished) return;
			finished = true;
			clearTimeout(timer);
			resolve(result);
		};
		const timer = setTimeout(() => finish({ status: 'timeout' }), Math.max(0, Number(timeoutMs) || 0));
		Promise.resolve(value).then(
			resolved => finish({ status: 'fulfilled', value: resolved }),
			error => finish({ status: 'rejected', error }),
		);
	});
}

export function mediaEventIsCurrent(
	guard,
	token,
	trackKey,
	expectedSource = '',
	currentSource = '',
	assignedAt = 0,
	eventTime = Number.POSITIVE_INFINITY,
) {
	if (!guard?.isCurrent(token, trackKey)) return false;
	if (Number.isFinite(eventTime) && eventTime < Number(assignedAt || 0)) return false;
	return !expectedSource || !currentSource || String(expectedSource) === String(currentSource);
}

export class MediaEventGate {
	constructor(guard) {
		this.guard = guard;
		this.current = null;
		this.serial = 0;
	}

	bind(token, trackKey, source, owner = null) {
		const binding = {
			id: ++this.serial,
			token,
			trackKey,
			source: String(source || ''),
			owner,
			armed: false,
		};
		this.current = binding;
		return binding;
	}

	invalidate() {
		this.current = null;
	}

	arm(binding, trackKey, currentSource, owner = null) {
		if (!this._matches(binding, trackKey, currentSource, owner)) return false;
		binding.armed = true;
		return true;
	}

	accepts(binding, trackKey, currentSource, owner = null) {
		return !!binding?.armed && this._matches(binding, trackKey, currentSource, owner);
	}

	_matches(binding, trackKey, currentSource, owner) {
		return this.current === binding
			&& binding?.owner === owner
			&& mediaEventIsCurrent(this.guard, binding?.token, trackKey, binding?.source, currentSource);
	}
}
