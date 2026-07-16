/* Native boundary shared by the Capacitor build and ordinary desktop pages.
 * Browsers never receive a Capacitor global, so this file is deliberately a
 * no-op outside the installed mobile application. */
(function () {
	const plugins = window.Capacitor?.Plugins;
	if (!plugins) return;
	document.documentElement.classList.add('rw-native-mobile');
	const player = plugins.RainettePlayer;
	const companion = plugins.RainetteCompanion;
	if (!player && !companion) return;

	function publish(message) {
		window.dispatchEvent(new CustomEvent('rainette:native-message', { detail: message }));
	}

	window.RainetteNativeTransport = {
		isNative: true,
		async request(payload) {
			if (payload.type?.startsWith('music_') && companion) return companion.request({ payload });
			return { id: payload.id, ok: false, msg: 'Rainette companion is not connected' };
		},
		async playback(action, payload = {}) {
			if (!player) return { ok: false, msg: 'Native player is unavailable' };
			return player.command({ action, payload });
		},
		async connectionStatus() {
			if (!companion?.connectionStatus) return { ok: false, paired: false, status: 'unpaired' };
			return companion.connectionStatus();
		},
		async pair(uri) {
			if (!companion?.pair) return { ok: false, status: 'failed', msg: 'Pairing is unavailable' };
			return companion.pair({ uri });
		}
	};

	player?.addListener('rainettePlaybackState', event => publish({
		...(event || {}),
		type: 'rainette_playback_state',
	}));
	companion?.addListener('rainetteCompanionMessage', event => publish({
		...(event || {}),
		type: event?.type || 'rainette_companion_pairing',
	}));
	companion?.addListener('rainetteCompanionSync', event => {
		if (event?.reset_required) publish({ type: 'rainette_companion_refresh', revision: event.revision });
		for (const item of event?.events || []) {
			if (item?.message) publish({ ...item.message, companion_revision: item.revision });
		}
		publish({
			...(event || {}),
			type: 'rainette_companion_sync',
			status: event?.ok === false ? (event?.status || 'reconnecting') : 'connected',
		});
	});
	// Native long-polling is deliberately started here instead of being tied to
	// one page, so Search, Library, and the player all receive live updates.
	companion?.startSync?.().catch(() => {});
})();
