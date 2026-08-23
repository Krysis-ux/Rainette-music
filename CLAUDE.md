# Rainette Music — working rules

## The two things that must never break

**1. The music plays.**
**2. The tunnel connects.**

Everything else in this app is a feature. These two are the product. A build
that cannot play a song, or a phone that cannot reach its computer, is not a
degraded Rainette — it is a broken one, and no other improvement is worth
shipping at their expense.

This is written down because it has been learned the expensive way. Every
serious outage so far has been one of these two, and each was introduced by a
change that looked safe and had a green test suite behind it:

| what shipped | what it cost |
| --- | --- |
| A yt-dlp player client pinned to `android` | YouTube later cut that client to a muxed 360p **video**, so every phone was served `video/mp4` into an `<audio>` element |
| A `Range: bytes=0-0` probe in the phone's error path | Reported every dead stream as *"your phone could not play this format"*, pointing four days of debugging at codecs |
| Media Session `play`/`pause` wired to `toggle()` | CarPlay's absolute verbs inverted the state — a second of music, a second of silence, all song |
| A tunnel helper not owned by the launch that started it | A leaked `cloudflared` per abandoned attempt, still holding tunnels after the app quit |

Note what these have in common: **none of them failed a test, and none of them
announced themselves.** They were all found by a user, in the car or on the
sofa, saying "it doesn't work any more".

## Before shipping anything that touches playback or the tunnel

Run the preflight. It exercises both invariants against the real world:

```bash
python scripts/preflight.py
```

It must be run from an ordinary residential connection. YouTube answers
datacenter IPs with a bot challenge, so this cannot be delegated to CI — see
`.github/workflows/stream-canary.yml`.

## Rules that follow from the two invariants

- **Assert outcomes, not mechanisms.** A test that pinned the player *client*
  stayed green while playback died. Test that the resolved stream is audio;
  never that a particular client was asked.
- **A feature may not be paid for with an invariant** — but check whether the
  bill is still due. The iPhone volume slider was removed because the only
  volume iOS honours is a Web Audio GainNode, and WebKit suspended that graph
  whenever the page was hidden, costing background playback. Background
  playback won, correctly. Then WebKit fixed the suspension
  ([bug 261554](https://bugs.webkit.org/show_bug.cgi?id=261554), iOS 17.5): a
  graph survives backgrounding under a declared `playback` audio session. The
  slider is back, and the gate is now *"can this engine hold a playback
  session"* rather than *"is this iOS"*. Re-examine trades like this; the
  platform moves.
- **Gate on capability, never on platform.** "iOS means no graph" and "pin the
  `android` player client" are the same bug: each was true when written, each
  quietly stopped being true, and neither would ever have announced it.
- **Never guess at a platform you cannot test.** Anything iOS-specific
  (audio session category, background behaviour, CarPlay) can only be reasoned
  about here — Chromium has no `navigator.audioSession`. Make such changes the
  smallest possible, scope them to the one context that needs them, and make
  sure they cannot prevent playback from *starting*.
- **The stub cannot see playback.** `tests/test_pwa_client_behaviour.py`
  replaces `window.Audio` with a fake. For anything about real playback, drive a
  real element — see the harness note in `pwa/README.md`.
- **A guardrail that cries wolf is worse than none.** Distinguish "could not
  measure" from "measured something wrong", or people stop reading it.

## Repo shape

`pwa/` is mirrored byte-for-byte to the private `music-pwa-web` repo, which is
what Vercel deploys and what the phone actually runs. **Any `pwa/` change must be
copied there and `pwa/sw.js`'s `CACHE` bumped**, or installed phones keep serving
the old JS. `tests/test_output_and_phone_sync.py` fails if the digest and the
cache name disagree.

Releases fire on `v*` tags, must match `version.APP_VERSION`, and are gated on
the `release-signing` environment. **That approval is the maintainer's — never
give it on their behalf.**
