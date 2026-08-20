"""Output routing, and the phone/desktop link that makes it work.

These cover four defects that were each individually silent — nothing logged,
nothing raised, just a control that did nothing — so each test states the
symptom it prevents rather than only the mechanism.
"""

import hashlib
import re
import unittest
from pathlib import Path

import audio_outputs
import music_bridge
import server


ROOT = Path(__file__).resolve().parents[1]


class OutputTransferAddressingTests(unittest.TestCase):
    """"Play on → my phone" sat on "Connecting" until it timed out."""

    def test_transfer_targets_a_real_paired_device_id(self):
        # The broker routes a transfer by looking target_device_id up among the
        # paired device ids.  Addressing it to the literal string 'phone' found
        # no log at all, so no phone ever saw the request and the desktop waited
        # out its whole 35s timeout.
        music = (ROOT / "web" / "rainette_music.js").read_text(encoding="utf-8")
        self.assertIn("const targetId = String(phone?.device_id || '')", music)
        self.assertNotIn("target_device_id: 'phone'", music)

    def test_broker_delivers_only_to_the_addressed_device(self):
        broker = server.CompanionSyncBroker()
        broker.read_after("phone-a", 0, 0)
        broker.read_after("phone-b", 0, 0)

        broker.publish(
            {"type": "music_output_transfer", "target_device_id": "phone-b"}, ""
        )

        self.assertEqual(broker.read_after("phone-a", 0, 0)["events"], [])
        self.assertEqual(len(broker.read_after("phone-b", 0, 0)["events"]), 1)

    def test_phone_client_answers_a_transfer(self):
        # The desktop will not pause itself until the target acknowledges, so a
        # phone that never replies is indistinguishable from one that is off.
        sync = (ROOT / "pwa" / "src" / "sync.js").read_text(encoding="utf-8")
        self.assertIn("music_output_transfer", sync)
        self.assertIn("music_output_transfer_result", sync)
        # And a failed load must answer false rather than not at all, or the
        # desktop pauses into silence on both devices.
        self.assertIn("reply(false", sync)


class LinkedSessionTests(unittest.TestCase):
    """A phone can mirror the computer's session, but only when it asks to."""

    def setUp(self):
        self.broker = server.CompanionSyncBroker()
        self.broker.read_after("phone-a", 0, 0)
        self.broker.read_after("phone-b", 0, 0)

    def events_for(self, device_id):
        return [item["message"] for item in self.broker.read_after(device_id, 0, 0)["events"]]

    def test_desktop_playback_still_reaches_nobody_by_default(self):
        self.broker.publish({"type": "music_now_playing", "track": {"title": "A"}}, "")

        self.assertEqual(self.events_for("phone-a"), [])
        self.assertEqual(self.events_for("phone-b"), [])

    def test_a_linked_phone_receives_desktop_playback(self):
        self.broker.read_after("phone-a", 0, 0, True)

        self.broker.publish({"type": "music_now_playing", "track": {"title": "A"}}, "")

        self.assertEqual(len(self.events_for("phone-a")), 1)
        # Linking one phone must not conscript the others.
        self.assertEqual(self.events_for("phone-b"), [])

    def test_unlinking_stops_the_mirror(self):
        self.broker.read_after("phone-a", 0, 0, True)
        self.broker.read_after("phone-a", 0, 0, False)

        self.broker.publish({"type": "music_now_playing"}, "")

        self.assertEqual(self.events_for("phone-a"), [])

    def test_polling_without_the_parameter_keeps_the_current_mode(self):
        # The phone omits `follow` on some polls; absent must mean "unchanged",
        # never "off", or a linked phone would silently unlink itself.
        self.broker.read_after("phone-a", 0, 0, True)
        result = self.broker.read_after("phone-a", 0, 0)

        self.assertTrue(result["follows_desktop"])

    def test_a_phones_own_playback_is_never_mirrored_back_to_it(self):
        # Phone-originated state arrives tagged as a phone output.  Absorbing it
        # would make a linked phone a mirror of its own echo.
        sync = (ROOT / "pwa" / "src" / "sync.js").read_text(encoding="utf-8")
        self.assertIn("output_device_id", sync)
        self.assertRegex(sync, r"output_device_id \|\| 'desktop'\) !== 'desktop'\) return")


class DesktopToPhoneTransportTests(unittest.TestCase):
    """The computer's transport controls reaching a phone that owns the audio.

    This did not work in either direction of the problem: the broker returned no
    recipients for a desktop-originated transport command, and the phone had no
    handler for one even if it had arrived.
    """

    def setUp(self):
        self.broker = server.CompanionSyncBroker()
        for device in ("phone-a", "phone-b"):
            self.broker.read_after(device, 0, 0)

    def events_for(self, device_id):
        return [item["message"] for item in self.broker.read_after(device_id, 0, 0)["events"]]

    def test_the_desktop_can_pause_the_phone_that_owns_playback(self):
        self.broker.publish(
            {"type": "music_remote_control", "action": "pause", "target_device_id": "phone-a"}, "",
        )

        self.assertEqual([m["action"] for m in self.events_for("phone-a")], ["pause"])
        # And only that phone. Someone else's music does not stop.
        self.assertEqual(self.events_for("phone-b"), [])

    def test_a_device_never_receives_its_own_transport_back(self):
        """`next` is not idempotent; a returning echo would skip two tracks."""
        self.broker.publish(
            {"type": "music_remote_control", "action": "next", "target_device_id": "phone-a"},
            "phone-a",
        )

        self.assertEqual(self.events_for("phone-a"), [])

    def test_transport_aimed_at_the_desktop_does_not_disturb_phones(self):
        # The desktop acts on this over its own socket. Only a phone that asked
        # to mirror the computer has any use for a copy.
        self.broker.publish({"type": "music_remote_control", "action": "pause"}, "phone-a")

        self.assertEqual(self.events_for("phone-a"), [])
        self.assertEqual(self.events_for("phone-b"), [])

    def test_a_linked_phone_still_sees_desktop_transport(self):
        self.broker.read_after("phone-a", 0, 0, True)

        self.broker.publish({"type": "music_remote_control", "action": "pause"}, "")

        self.assertEqual(len(self.events_for("phone-a")), 1)
        self.assertEqual(self.events_for("phone-b"), [])

    def test_the_phone_client_acts_on_remote_transport(self):
        """Routing is delivery, not action. The client has to answer too."""
        sync = (ROOT / "pwa" / "src" / "sync.js").read_text(encoding="utf-8")
        self.assertIn("case 'music_remote_control':", sync)
        self.assertIn("applyRemoteVerb", sync)

        player = (ROOT / "pwa" / "src" / "player.js").read_text(encoding="utf-8")
        self.assertIn("export async function applyRemoteVerb", player)
        # Delivery is not authorisation: a verb must be addressed at this phone
        # and must not be its own echo.
        self.assertIn("target_device_id", player)
        self.assertIn("origin_device_id", player)

    def test_the_desktop_states_intent_rather_than_flipping(self):
        """`toggle` against a stale `playing` flag does the opposite of its icon."""
        shell = (ROOT / "web" / "music_shell.js").read_text(encoding="utf-8")
        self.assertRegex(shell, r"\? 'pause' : 'play'")
        # Derived from what the button is *showing*, not from `playing` alone:
        # a loading row shows a pause affordance while `playing` is still false.
        self.assertRegex(shell, r"state === 'loading'")

        mini = (ROOT / "web" / "miniplayer.js").read_text(encoding="utf-8")
        self.assertIn("a === 'play'", mini)
        self.assertIn("a === 'pause'", mini)

    def test_a_phone_cannot_claim_to_be_another_device(self):
        self.assertIn("music_remote_control", server._DEVICE_STAMPED_TYPES)


class AudioOutputEnumerationTests(unittest.TestCase):
    """A connected Bluetooth speaker should be nameable, not anonymous."""

    def test_enumeration_never_raises(self):
        # This runs while a picker is opening.  A failed shell-out is a missing
        # convenience, never a reason to break the control.
        self.assertIsInstance(audio_outputs.list_outputs(), list)
        self.assertIsInstance(audio_outputs.default_output_name(), str)

    def test_devices_carry_a_name_a_kind_and_a_default_flag(self):
        for device in audio_outputs.list_outputs():
            self.assertTrue(device["name"])
            self.assertIn("kind", device)
            self.assertIsInstance(device["is_default"], bool)

    def test_bluetooth_transport_is_recognised(self):
        # The whole point of the feature: the icon and the label both depend on
        # the transport being classified rather than lumped in with "speaker".
        self.assertEqual(
            audio_outputs._MACOS_TRANSPORTS["coreaudio_device_type_bluetooth"],
            "bluetooth",
        )

    def test_the_command_is_registered_and_reachable_from_a_phone(self):
        self.assertIn("music_output_devices", music_bridge.DISPATCH)
        self.assertIn("music_output_devices", server.COMPANION_COMMAND_TYPES)


class PhoneClientDefectTests(unittest.TestCase):
    """Two silent client bugs with no visible error to trace them by."""

    def test_a_never_set_volume_does_not_read_as_muted(self):
        # Number(null) is 0, not NaN, so the obvious Number() check treated
        # "never set" as "set to zero" and every fresh install started silent.
        state = (ROOT / "pwa" / "src" / "state.js").read_text(encoding="utf-8")
        self.assertIn("if (raw === null || raw === '') return fallback;", state)

    def test_a_failed_play_clears_the_loading_flag(self):
        # `loading` swaps the play glyph for a spinner.  Any exit that leaves it
        # set strands the transport spinning with no way back.
        player = (ROOT / "pwa" / "src" / "player.js").read_text(encoding="utf-8")
        body = player[player.index("export async function playTrack") :]
        body = body[: body.index("\nexport ", 1)]
        self.assertIn("} finally {", body)
        self.assertIn("state.loading = false;", body.split("} finally {")[1])

    def test_the_mini_bar_opens_the_now_playing_card(self):
        # The bar rendered but was inert: there was no way to reach a full
        # player at all from the phone.
        index = (ROOT / "pwa" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="playerOpen"', index)
        nowplaying = (ROOT / "pwa" / "src" / "nowplaying.js").read_text(encoding="utf-8")
        self.assertIn("#playerOpen", nowplaying)


class PhoneClientShellTests(unittest.TestCase):
    """The service worker answers cache-first, so the shell list has to be right."""

    def test_every_client_module_is_precached(self):
        worker = (ROOT / "pwa" / "sw.js").read_text(encoding="utf-8")
        shell = re.search(r"const SHELL = \[(.*?)\];", worker, re.S).group(1)
        for module in sorted((ROOT / "pwa" / "src").glob("*.js")):
            self.assertIn(f"./src/{module.name}", shell, f"{module.name} is not precached")

    def test_a_failed_precache_does_not_abandon_the_install(self):
        # addAll is all-or-nothing; without this the phone would end up with no
        # offline shell rather than a partial one.
        worker = (ROOT / "pwa" / "sw.js").read_text(encoding="utf-8")
        self.assertIn(".catch(() => {})", worker)

    def test_the_cache_name_matches_the_files_it_would_hold(self):
        """A changed client file must mean a changed cache name.

        This is the regression guard for a real and repeated failure. Three
        separate fixes to the sheet pull-down (#19, #22, #23) each shipped
        without touching CACHE. The worker answers stale-while-revalidate, so a
        missed bump is not permanent -- but it does cost one load running the
        *previous* JavaScript. That is enough to test a gesture fix on a phone,
        watch it not work, and conclude the fix was wrong when it was only
        late.

        Tying the cache name to a digest of the shell makes the two impossible
        to disagree about: change any client file and this fails until the name
        follows.
        """
        worker = (ROOT / "pwa" / "sw.js").read_text(encoding="utf-8")
        shell = re.search(r"const SHELL = \[(.*?)\];", worker, re.S).group(1)

        digest = hashlib.sha256()
        for relative in sorted(re.findall(r"'\./([^']*)'", shell)):
            path = ROOT / "pwa" / relative
            # './' names the directory, not a file; index.html covers it.
            if not relative or not path.is_file():
                continue
            digest.update(relative.encode())
            digest.update(path.read_bytes())
        expected = digest.hexdigest()[:8]

        name = re.search(r"const CACHE = '([^']+)'", worker).group(1)
        self.assertTrue(
            name.endswith("-" + expected),
            f"pwa/sw.js CACHE is {name!r}; the shell now digests to {expected!r}.\n"
            f"A client file changed without the cache name following it. Set it to "
            f"a bumped version plus that digest, e.g. 'rainette-pwa-v17-{expected}'.",
        )


class PlaybackTargetBroadcastTests(unittest.TestCase):
    """Every surface has to be able to name the same owner.

    This is the difference between an app that knows where the sound is and one
    that guesses: a phone showing "playing on the computer" must stop saying so
    the moment another device takes over, and it cannot learn that from its own
    session's events.
    """

    def setUp(self):
        self.broker = server.CompanionSyncBroker()
        for device in ("phone-a", "phone-b"):
            self.broker.read_after(device, 0, 0)

    def events_for(self, device_id):
        return [item["message"] for item in self.broker.read_after(device_id, 0, 0)["events"]]

    def test_the_target_reaches_every_paired_device(self):
        self.broker.publish({"type": "music_playback_target", "owner_kind": "phone",
                             "owner_device_id": "phone-a", "revision": 3}, "")

        for device in ("phone-a", "phone-b"):
            with self.subTest(device=device):
                self.assertEqual([m["type"] for m in self.events_for(device)],
                                 ["music_playback_target"])

    def test_it_fans_out_without_touching_the_recipient_rules(self):
        # It is in _SYNC_TYPES and in neither of the narrower sets, so it falls
        # through to the catch-all. Anything else would have meant special-casing
        # the routing that the session and transfer types depend on.
        self.assertIn("music_playback_target", server.CompanionSyncBroker._SYNC_TYPES)
        self.assertNotIn("music_playback_target", server.CompanionSyncBroker._SESSION_TYPES)
        self.assertNotIn("music_playback_target", server.CompanionSyncBroker._TARGETED_TYPES)

    def test_a_phone_cannot_claim_playback_for_another_device(self):
        self.assertIn("music_playback_target_set", server._DEVICE_STAMPED_TYPES)

    def test_the_claim_does_not_block_on_a_reply(self):
        # The authoritative answer arrives as the broadcast every device already
        # receives, so waiting on the response would only delay it.
        self.assertIn("music_playback_target_set", server.COMPANION_ONE_WAY_COMMAND_TYPES)

    def test_both_commands_are_reachable_from_a_phone(self):
        for command in ("music_playback_target_get", "music_playback_target_set"):
            with self.subTest(command=command):
                self.assertIn(command, server.COMPANION_COMMAND_TYPES)
                self.assertIn(command, music_bridge.DISPATCH)

    def test_the_clients_gate_on_the_revision(self):
        # A reconnect drains a backlog, so an older record can arrive after a
        # newer one and would otherwise win simply by arriving last.
        target = (ROOT / "pwa" / "src" / "target.js").read_text(encoding="utf-8")
        self.assertIn("revision <= Number(state.playbackTarget?.revision || 0)", target)

        desktop = (ROOT / "web" / "rainette_music.js").read_text(encoding="utf-8")
        self.assertIn("revision <= (pageState.output.revision || 0)", desktop)

    def test_starting_playback_is_itself_the_claim(self):
        # There is nothing to hand over, so it needs no handshake — which is why
        # pressing play on the phone works without a transfer first.
        player = (ROOT / "pwa" / "src" / "player.js").read_text(encoding="utf-8")
        self.assertIn("claim_by_play", player)

    def test_ownership_moves_only_once_the_target_has_the_track(self):
        # A failed handoff must leave the source playing rather than pausing
        # into silence on the strength of a transfer that did not happen.
        bridge = (ROOT / "music_bridge.py").read_text(encoding="utf-8")
        transfer_ack = bridge.index("def cmd_music_output_transfer_result")
        following = bridge[transfer_ack:transfer_ack + 2000]
        self.assertIn('reason="transfer_ack"', following)
        self.assertIn('if payload["ok"]', following)


class DeviceSettingsSyncTests(unittest.TestCase):
    """A phone's settings follow it back, and go nowhere else."""

    def setUp(self):
        self.broker = server.CompanionSyncBroker()
        for device in ("phone-a", "phone-b"):
            self.broker.read_after(device, 0, 0)

    def events_for(self, device_id):
        return [item["message"] for item in self.broker.read_after(device_id, 0, 0)["events"]]

    def test_one_phones_settings_never_reach_another(self):
        # The result is deliberately absent from _SYNC_TYPES, so it reaches the
        # HTTP caller and this computer's own windows and stops there. Anything
        # else would broadcast one person's theme to every paired device.
        self.assertNotIn("music_device_settings_result", server.CompanionSyncBroker._SYNC_TYPES)

        self.broker.publish({"type": "music_device_settings_result", "device_id": "phone-a",
                             "entries": [{"key": "theme", "value": "mono"}]}, "phone-a")
        self.assertEqual(self.events_for("phone-a"), [])
        self.assertEqual(self.events_for("phone-b"), [])

    def test_a_phone_cannot_read_or_write_as_another_device(self):
        for command in ("music_device_settings_get", "music_device_settings_put"):
            with self.subTest(command=command):
                self.assertIn(command, server._DEVICE_STAMPED_TYPES)
                self.assertIn(command, server.COMPANION_COMMAND_TYPES)
                self.assertIn(command, music_bridge.DISPATCH)

    def test_volume_and_linked_mode_are_left_out_on_purpose(self):
        sync = (ROOT / "pwa" / "src" / "prefsync.js").read_text(encoding="utf-8")
        # volume changes many times a minute; linked mode already has a single
        # authoritative home in music_devices, and a second writer for one fact
        # is the defect this whole area exists to remove.
        self.assertNotIn("STORAGE.volume", sync)
        self.assertNotIn("STORAGE.linked", sync)

    def test_the_merge_is_per_key_rather_than_per_blob(self):
        sync = (ROOT / "pwa" / "src" / "prefsync.js").read_text(encoding="utf-8")
        self.assertIn("updated_ms", sync)
        # And the computer's clock arbitrates, so a phone with a wrong date
        # cannot pin a key by claiming a time far in the future.
        self.assertIn("absorbServerMtimes", sync)

    def test_prefs_stays_a_leaf_module(self):
        # prefsync imports prefs, so prefs must not import back; a callback is
        # what keeps the pair acyclic.
        prefs = (ROOT / "pwa" / "src" / "prefs.js").read_text(encoding="utf-8")
        # An actual import, not the word in the comment that explains why there
        # isn't one.
        self.assertNotRegex(prefs, r"^\s*import\s.*prefsync\.js")
        self.assertIn("export function observePrefs", prefs)


class SessionSurvivalTests(unittest.TestCase):
    """A phone that slept had to be restarted by hand to come back.

    There was no visibilitychange, pageshow or freeze handler anywhere in the
    client, and the only reconnect was a flat retry with no backoff.
    """

    def setUp(self):
        self.connection = (ROOT / "pwa" / "src" / "connection.js").read_text(encoding="utf-8")
        self.sync = (ROOT / "pwa" / "src" / "sync.js").read_text(encoding="utf-8")
        self.app = (ROOT / "pwa" / "app.js").read_text(encoding="utf-8")

    def test_every_way_a_phone_wakes_up_is_handled(self):
        for event in ("visibilitychange", "pageshow", "freeze", "resume", "online"):
            with self.subTest(event=event):
                self.assertIn(event, self.connection)

    def test_a_restored_bfcache_page_is_treated_as_a_long_absence(self):
        # Nothing else fires on a back-forward restore, so the session looks
        # healthy while its poll is long dead.
        self.assertIn("event.persisted", self.connection)

    def test_reconnect_backs_off_with_jitter_and_a_cap(self):
        self.assertIn("export function backoffDelay", self.connection)
        self.assertIn("CAP_MS", self.connection)
        self.assertIn("Math.random()", self.connection)
        # The flat retry is gone.
        self.assertNotIn("1800", self.sync)
        self.assertIn("backoffDelay(attempt)", self.sync)

    def test_a_successful_poll_refills_the_backoff(self):
        # Otherwise one long outage leaves every later reconnect slow.
        self.assertIn("attempt = 0", self.sync)

    def test_the_watch_is_actually_installed(self):
        self.assertIn("startConnectionWatch()", self.app)
        self.assertIn("restartEventLoop", self.app)


class RecentSessionsTests(unittest.TestCase):
    """Reconnecting to a computer whose tunnel address rotated, without a rescan."""

    def setUp(self):
        self.sessions = (ROOT / "pwa" / "src" / "sessions.js").read_text(encoding="utf-8")
        self.app = (ROOT / "pwa" / "app.js").read_text(encoding="utf-8")
        self.state = (ROOT / "pwa" / "src" / "state.js").read_text(encoding="utf-8")

    def test_the_list_is_browser_local_only(self):
        # Per-browser by construction, so one user's list can never become
        # another's, and none of it is ever uploaded.
        self.assertIn("rainette.pwa.sessions", self.state)
        self.assertNotIn("fetch(", self.sessions)
        self.assertNotIn("command(", self.sessions)

    def test_credentials_are_kept_out_of_the_session_rows(self):
        # The rows get read, filtered and sorted; keeping tokens in a separate
        # map means handling them cannot leak one.
        self.assertIn("rainette.pwa.tokens", self.state)
        self.assertIn("token_present", self.sessions)

    def test_an_unreachable_computer_is_marked_not_deleted(self):
        # A rotating Quick Tunnel hostname is the normal case, not a broken
        # pairing; deleting the credential would turn a moved computer into a
        # full re-pair.
        self.assertIn("markSessionStale", self.sessions)

    def test_the_probe_can_re_test_the_address_it_already_has(self):
        # adoptEndpoint short-circuited when the endpoint was unchanged, which
        # is exactly the case when only the token has been swapped in.
        self.assertRegex(self.app, r"adoptEndpoint\(endpoint, \{ force = false \} = \{\}\)")
        self.assertIn("if (!force && endpoint === state.endpoint) return false;", self.app)

    def test_disconnect_clears_the_address_book(self):
        self.assertIn("forgetAllSessions()", self.app)


if __name__ == "__main__":
    unittest.main()
