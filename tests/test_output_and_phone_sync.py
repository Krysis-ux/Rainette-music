"""Output routing, and the phone/desktop link that makes it work.

These cover four defects that were each individually silent — nothing logged,
nothing raised, just a control that did nothing — so each test states the
symptom it prevents rather than only the mechanism.
"""

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


if __name__ == "__main__":
    unittest.main()
