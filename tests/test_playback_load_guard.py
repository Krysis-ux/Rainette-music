import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlaybackLoadGuardTests(unittest.TestCase):
    def run_node(self, source):
        result = subprocess.run(
            ["node", "--input-type=module", "-e", source],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_load_invalidates_stale_result_and_retry_is_single_use(self):
        self.run_node(
            """
            import assert from 'node:assert/strict';
            import { PlaybackLoadGuard } from './web/playback_load_guard.mjs';
            const guard = new PlaybackLoadGuard();
            const first = guard.begin('youtube:first');
            const second = guard.begin('youtube:second');
            assert.equal(guard.isCurrent(first, 'youtube:first'), false);
            assert.equal(guard.isCurrent(second, 'youtube:second'), true);
            assert.equal(guard.claimRetry(first, 'youtube:first'), false);
            assert.equal(guard.claimRetry(second, 'youtube:second'), true);
            assert.equal(guard.claimRetry(second, 'youtube:second'), false);
            """
        )

    def test_attempt_epochs_reject_old_media_callbacks_after_refresh(self):
        self.run_node(
            """
            import assert from 'node:assert/strict';
            import { PlaybackLoadGuard } from './web/playback_load_guard.mjs';
            const guard = new PlaybackLoadGuard();
            const initialResolver = guard.begin('youtube:track');
            const initialMedia = guard.advance(initialResolver, 'youtube:track');
            assert.equal(guard.claimRetry(initialMedia, 'youtube:track'), true);
            const retryResolver = guard.advance(initialMedia, 'youtube:track');
            const refreshedMedia = guard.advance(retryResolver, 'youtube:track');
            assert.equal(guard.isCurrent(initialMedia, 'youtube:track'), false);
            assert.equal(guard.isCurrent(retryResolver, 'youtube:track'), false);
            assert.equal(guard.isCurrent(refreshedMedia, 'youtube:track'), true);
            assert.equal(guard.claimRetry(refreshedMedia, 'youtube:track'), false);
            """
        )

    def test_queue_change_invalidates_pending_resolver(self):
        self.run_node(
            """
            import assert from 'node:assert/strict';
            import { PlaybackLoadGuard } from './web/playback_load_guard.mjs';
            const guard = new PlaybackLoadGuard();
            let finish;
            const pending = new Promise(resolve => { finish = resolve; });
            const oldResolver = guard.begin('youtube:old');
            const completion = pending.then(() => guard.isCurrent(oldResolver, 'youtube:old'));
            guard.begin('youtube:new');
            finish();
            assert.equal(await completion, false);
            """
        )

    def test_old_media_events_are_rejected_while_new_track_resolves(self):
        self.run_node(
            """
            import assert from 'node:assert/strict';
            import { PlaybackLoadGuard, mediaEventIsCurrent } from './web/playback_load_guard.mjs';
            const guard = new PlaybackLoadGuard();
            const oldResolver = guard.begin('youtube:old');
            const oldMedia = guard.advance(oldResolver, 'youtube:old');
            guard.begin('youtube:selected');
            assert.equal(mediaEventIsCurrent(guard, oldMedia, 'youtube:old', 'old-url', 'old-url'), false);
            const selectedResolver = guard.begin('youtube:selected');
            const selectedMedia = guard.advance(selectedResolver, 'youtube:selected');
            assert.equal(mediaEventIsCurrent(guard, selectedMedia, 'youtube:selected', 'new-url', 'new-url'), true);
            assert.equal(mediaEventIsCurrent(guard, selectedMedia, 'youtube:selected', 'new-url', 'old-url'), false);
            assert.equal(mediaEventIsCurrent(guard, selectedMedia, 'youtube:selected', 'new-url', 'new-url', 100, 99), false);
            assert.equal(mediaEventIsCurrent(guard, selectedMedia, 'youtube:selected', 'new-url', 'new-url', 100, 101), true);
            """
        )

    def test_media_event_gate_stays_disarmed_until_new_source_loadstarts(self):
        self.run_node(
            """
            import assert from 'node:assert/strict';
            import { PlaybackLoadGuard, MediaEventGate } from './web/playback_load_guard.mjs';
            const guard = new PlaybackLoadGuard();
            const gate = new MediaEventGate(guard);
            const resolver = guard.begin('youtube:new');
            const media = guard.advance(resolver, 'youtube:new');
            const element = {};
            const staleElement = {};
            const binding = gate.bind(media, 'youtube:new', 'new-url', element);
            assert.equal(gate.accepts(binding, 'youtube:new', 'new-url', element), false);
            assert.equal(gate.arm(binding, 'youtube:new', 'new-url', staleElement), false);
            assert.equal(gate.arm(binding, 'youtube:new', 'new-url', element), true);
            assert.equal(gate.accepts(binding, 'youtube:new', 'new-url', element), true);
            assert.equal(gate.accepts(binding, 'youtube:new', 'new-url', staleElement), false);
            assert.equal(gate.accepts(binding, 'youtube:new', 'old-url', element), false);
            guard.begin('youtube:other');
            assert.equal(gate.accepts(binding, 'youtube:new', 'new-url', element), false);
            """
        )

    def test_never_settling_play_attempt_reaches_a_deadline(self):
        self.run_node(
            """
            import assert from 'node:assert/strict';
            import { settleWithin } from './web/playback_load_guard.mjs';
            const never = new Promise(() => {});
            const started = Date.now();
            const result = await settleWithin(never, 20);
            assert.equal(result.status, 'timeout');
            assert.ok(Date.now() - started >= 15);
            assert.ok(Date.now() - started < 500);
            """
        )

    def test_miniplayer_replaces_media_element_for_each_selected_or_retried_load(self):
        source = (ROOT / "web" / "miniplayer.js").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("_freshAudioElement();"), 2)
        self.assertIn("owner === audio", source)
        self.assertIn("mediaEventGate.bind(mediaToken, trackKey(track), expectedSrc, audio)", source)

    def test_eq_reload_preserves_a_paused_transport(self):
        source = (ROOT / "web" / "miniplayer.js").read_text(encoding="utf-8")
        self.assertIn("pauseAfterReload", source)
        self.assertNotIn("audio.addEventListener('canplay', () => audio.pause()", source)

    def test_miniplayer_uses_guard_for_loads_and_media_retries(self):
        source = (ROOT / "web" / "miniplayer.js").read_text(encoding="utf-8")
        self.assertIn("PlaybackLoadGuard", source)
        self.assertIn("settleWithin", source)
        self.assertIn("claimRetry", source)
        self.assertIn("forceRefresh", source)


if __name__ == "__main__":
    unittest.main()
