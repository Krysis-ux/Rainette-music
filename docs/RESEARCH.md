# Rainette Music — Premium Feature Research

Competitive analysis of what Apple Music, Spotify, YouTube Music, and Tidal do
that Rainette doesn't yet, prioritized for impact vs. effort against Rainette's
actual architecture (local SQLite library + yt-dlp streaming, two-window
pywebview shell, existing queue / playlists / smart-playlists / EQ).

**This is a recommendations document only.** Nothing here is committed or built —
it's a menu to pick from later. Each item notes roughly how it fits Rainette's
current code so estimates are grounded, not aspirational.

---

## Tier 1 — High impact, fits the existing architecture

### 1. Lyrics (time-synced where possible)
Every premium competitor has this; it's the single most-requested "why doesn't
my player have this" feature. Static lyrics are cheap: fetch from a free
provider (e.g. LRCLIB, which needs no API key) keyed on title+artist+duration,
cache in SQLite alongside the track. Time-synced (LRC format) highlighting can
layer on later using the `timeupdate` events the player already emits. Natural
home: a panel in the new Now Playing view built in Phase 2e.

### 2. Gapless / crossfade playback
Apple Music and Spotify both crossfade between tracks; it's a large part of why
they feel "premium" during continuous listening. Rainette already prefetches
upcoming stream URLs (`_preResolveUpcoming`, PREFETCH_AHEAD=3), so the hard part
— having the next track's audio ready — is done. Crossfade needs a second
`<audio>` element (or a second Web Audio source node) faded via the existing
gain node in `miniplayer.js`. Add a Settings slider for crossfade duration
(0–12s), matching Spotify's control exactly.

### 3. "Up Next" reordering polish + queue history
The queue is already strong (drag-reorder now animated, sessions, dedupe). Two
gaps vs. competitors: (a) a visible split between "Now Playing → Next in queue →
Later from playlist" (Spotify's three-zone model), and (b) a "recently played
from queue" back-stack so `prev()` past the first track has somewhere to go.
Both are pure `state.py` / queue-state changes — no new streaming work.

### 4. Scrobbling / richer listening stats
Rainette already logs play history (`log_play`, the Recent tab). Competitors
turn that into "your top artists this month", "listening minutes", year-in-review
style summaries. This is entirely local: aggregate the existing play-history
table into a new "Insights" surface. Zero new external dependencies, high
perceived value, showcases data Rainette already collects.

### 5. Sleep timer
Small but genuinely premium-feeling and present in every competitor. "Stop
after this track" or "stop in N minutes." Trivial: a `setTimeout` that calls
`pause()` in the active engine, plus a control in the Now Playing "•••" menu.

---

## Tier 2 — High impact, more effort

### 6. Smarter radio / autoplay ("keep playing similar")
Rainette has `startMixFromSeed` already. Competitors extend this into infinite
radio: when the queue runs dry, auto-append similar tracks so playback never
stops. Rainette can approximate this with the ytmusicapi "related tracks"
endpoint (already a dependency for catalog browse) seeded off the last track.
Gate behind a Settings "Autoplay similar when queue ends" toggle.

### 7. Offline / download for local playback
The defining feature of paid tiers. Rainette resolves real audio stream URLs, so
it *could* cache the audio bytes to disk (via the existing `/audio` proxy) for
starred tracks and play them back offline. This is the biggest effort item here
(storage management, cache eviction, a "Downloaded" library filter) and has the
most legal/ToS nuance given the yt-dlp source — flagged as a real decision, not
a quick win.

### 8. Cross-fade-aware "Enhance"/normalization (ReplayGain)
Loudness normalization so tracks don't jump in volume between sources — Spotify's
"Normalize volume", Apple's "Sound Check". Analyze perceived loudness on first
play (or read stream metadata), store a per-track gain offset in SQLite, apply
via the existing Web Audio gain node. Pairs naturally with the EQ that already
exists.

### 9. Multi-select + bulk queue/playlist actions
Competitors let you shift-click a range of tracks and queue/add-to-playlist all
at once. Rainette's track lists are per-row today. A selection mode over the
existing `trackCard` rows + a bulk-action bar would meaningfully speed up
library curation.

---

## Tier 3 — Polish and delight

### 10. Dynamic color / "ambient" backgrounds from cover art
Apple Music's animated cover-art-derived gradients are pure atmosphere. Extract
a dominant color from the current `thumbnail_url` (a tiny canvas sample) and tint
the Now Playing view's background with it. Cheap, and it makes the Phase 2e view
feel alive.

### 11. Keyboard shortcuts + a shortcuts cheatsheet
The command palette (now fixed) is a great foundation. Add global media-key-style
shortcuts (space = play/pause, arrows = seek/skip) and a "?" overlay listing
them — a hallmark of pro-feeling desktop apps (Linear, Arc, Spotify desktop).

### 12. Mini "friends / activity" is **not** recommended
Spotify's social feed doesn't fit Rainette's local, single-user, ad-free
positioning (see PRODUCT.md's "focused listening session" framing). Called out
explicitly so it doesn't get cargo-culted in.

### 13. Richer artist/album pages
Rainette already has artist/album detail views. Competitors add: discography
grouping (albums / singles / appears-on — partially present), "fans also like",
and top-tracks-by-play-count (which Rainette could compute locally from play
history, unlike competitors who need a backend).

---

## Status update — July 2026 build pass

Now built (see git history for details): **#4 Insights** (new tab: plays /
minutes / unique tracks+artists, plays-per-day chart, heavy rotation, top
artists, 7/30/all windows), **#5 Sleep timer** (Now Playing action: 15-60 min
or stop-after-track), **#6 Autoplay similar** (Settings toggle; the player
engine appends a mix seeded from the last track when the queue runs dry),
**#10 Ambient cover-art color** (Now Playing panel tint sampled from art),
**#11 Keyboard shortcuts + "?" cheatsheet**, plus a soft **fade on play/pause**
(Settings toggle) as a first slice of #2's crossfade feel. Still open: full
beat-crossfade between tracks (#2), queue three-zone/back-stack (#3), offline
(#7), normalization (#8), multi-select (#9), richer artist pages (#13).

## Suggested sequencing if any of this is pursued

1. **Sleep timer** (#5) and **Insights/stats** (#4) first — near-zero risk, use
   only data/primitives Rainette already has, immediate "it feels complete" wins.
2. **Lyrics** (#1) — highest requested, moderate effort, one new cached data source.
3. **Crossfade** (#2) — leverages the prefetch work already in place.
4. Everything else as appetite allows.

Deliberately excluded from any near-term list: **offline downloads** (#7) — real
storage + ToS decisions to make first — and **social** (#12) — off-brand.

---

## 2026 addendum (fresh web research)

A follow-up pass validates the list above and surfaces one new idea, rather than
replacing this document's existing analysis.

- **AutoMix / crossfade** — Apple Music's beat-matched, time-stretched auto-DJ
  transitions ("AutoMix") reinforce Tier 1 item #2 above: crossfade reads as a
  defining "premium" signal to users right now. No new idea here, just market
  validation that this is worth the effort when it's picked up.
- **Synced lyrics engagement** — reported as a major engagement driver for both
  Spotify and Apple Music ("users go absolutely crazy for" synced lyrics).
  Validates prioritizing time-synced lyrics, which is exactly what this round of
  work built (see the main Rainette Music implementation log / git history for
  the highlight + auto-scroll feature).
- **New idea, not previously listed: prompt/mood-based playlist generation.**
  Spotify's "Prompted Playlists" and Apple Music's "Playlist Playground" both let
  a user describe a vibe in plain language and get a playlist built from it.
  Against Rainette's architecture this is feasible two ways: (a) a cheap
  heuristic pass over the local library's existing genre/title/artist metadata
  (no new dependency, weaker matching), or (b) a real natural-language pass via
  an LLM call (better matching, adds an external API dependency and cost this
  app doesn't currently have). Flagging as a **Tier 2 candidate** for a future
  round — not built in this pass.
- **Explicitly not recommended**, mirroring this document's existing rejection of
  social features: collaborative playlists (needs multi-user infrastructure,
  off-brand per `PRODUCT.md`'s single-user, local-first positioning) and a fully
  conversational, voice-narrated AI DJ (needs LLM + text-to-speech
  infrastructure, disproportionate effort for what this app is trying to be).
  Lock Screen / Dynamic Island lyrics are mobile-OS-specific features and don't
  apply to a Windows desktop app.
