"""A playlist from a pasted link: Spotify, Apple Music, Deezer or YouTube. No accounts, no keys."""

from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import urllib.request
from typing import Any, Callable

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
TIMEOUT_S = 15
PAGE = 100
MAX_TRACKS = 2000
MATCH_THRESHOLD = 0.55
# The Spotify web player's own playlist query. ponytail: Spotify can rotate this hash;
# then only the first 100 tracks load and the result says `partial`.
SPOTIFY_PAGE_QUERY = "19ff1327c29e99c208c86d7a9d8f1929cfdf3d3202a0ff4253c821f1901aa94d"
SPOTIFY_PATHFINDER = "https://api-partner.spotify.com/pathfinder/v2/query"

_SPOTIFY = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?|spotify:)(playlist|album)[/:]([A-Za-z0-9]{10,40})", re.I)
_APPLE = re.compile(r"music\.apple\.com/([a-z]{2})/(playlist|album)/(?:[^/?#\s]+/)?(pl\.[A-Za-z0-9-]+|\d+)", re.I)
_DEEZER = re.compile(r"deezer\.com/(?:[a-z]{2}/)?(playlist|album)/(\d+)", re.I)
_YOUTUBE = re.compile(r"(?:youtube\.com|youtu\.be)/\S*?[?&]list=([A-Za-z0-9_-]{10,80})", re.I)
# Share buttons hand out these; only they are ever opened as given.
_SHORT = re.compile(r"https://(?:spotify\.link|link\.deezer\.com|deezer\.page\.link)/[A-Za-z0-9/_-]+", re.I)

Fetch = Callable[..., tuple[str, str]]


class LinkError(ValueError):
    """Something a person can act on."""


def fetch(url: str, data: dict | None = None, headers: dict | None = None) -> tuple[str, str]:
    """GET, or POST `data` as JSON. Returns the final address and the body."""
    body = json.dumps(data).encode() if data is not None else None
    sent = {"User-Agent": UA, **({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=sent), timeout=TIMEOUT_S) as resp:
        return resp.geturl(), resp.read().decode("utf-8", "replace")


def looks_like_link(text: str) -> bool:
    """A link (any link: an unknown one should hear why), not a list of songs."""
    text = str(text or "")
    return bool(re.match(r"\s*(https?://|spotify:)", text, re.I) or _known(text) or _SHORT.search(text))


def read(text: str, *, get: Fetch = fetch) -> dict[str, Any]:
    """{name, source, total, partial, entries: [{title, artist, album}], youtube_playlist_id}."""
    found = _known(str(text or ""))
    if not found:
        short = _SHORT.search(str(text or ""))
        if not short:
            raise LinkError("Paste a Spotify, Apple Music, Deezer or YouTube playlist link.")
        final, body = get(short.group(0))
        found = _known(final) or _known(body)
        if not found:
            raise LinkError("That link did not lead to a playlist. Open it in a browser and copy the address.")
    source, match = found
    return _READERS[source](match, get)


def _known(text: str) -> tuple[str, re.Match] | None:
    for source, pattern in (("spotify", _SPOTIFY), ("apple", _APPLE), ("deezer", _DEEZER), ("youtube", _YOUTUBE)):
        match = pattern.search(text)
        if match:
            return source, match
    return None


def _result(source: str, name: str, entries: list, total: int = 0, **extra: Any) -> dict[str, Any]:
    entries = [e for e in entries if e["title"]][:MAX_TRACKS]
    return {"name": name, "source": source, "total": max(total, len(entries)), "partial": False,
            "entries": entries, "youtube_playlist_id": "", **extra}


def _entry(title: Any, artist: Any = "", album: Any = "") -> dict[str, str]:
    return {"title": str(title or "").strip(), "artist": str(artist or "").strip(), "album": str(album or "").strip()}


def _dig(value: Any, *path: Any) -> Any:
    for key in path:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            return None
    return value


def _script_json(page: str, opening: str) -> Any:
    start = page.find(opening)
    if start < 0:
        return None
    start += len(opening)
    try:
        return json.loads(page[start:page.find("</script>", start)])
    except ValueError:
        return None


# ── Spotify ────────────────────────────────────────────────────────────────

def _spotify(match: re.Match, get: Fetch) -> dict[str, Any]:
    kind, ident = match.group(1).lower(), match.group(2)
    _, page = get(f"https://open.spotify.com/embed/{kind}/{ident}")
    state = _dig(_script_json(page, '<script id="__NEXT_DATA__" type="application/json">'), "props", "pageProps", "state")
    entity = _dig(state, "data", "entity")
    if not isinstance(entity, dict) or not entity.get("trackList"):
        raise LinkError("Spotify would not share that playlist. Make it public, or export it with exportify.net.")
    entries = [_entry(t.get("title"), str(t.get("subtitle") or "").split(", ")[0],
                      entity.get("name") if kind == "album" else "")
               for t in entity["trackList"] if isinstance(t, dict)]
    total, partial = len(entries), False
    # The embed stops at 100; the web player's query, with the embed's own guest token, has the rest.
    if kind == "playlist" and len(entries) >= PAGE:
        total, partial = _spotify_rest(ident, _dig(state, "settings", "session", "accessToken"), entries, get)
    return _result("spotify", str(entity.get("name") or ""), entries, total, partial=partial)


def _spotify_rest(ident: str, token: Any, entries: list, get: Fetch) -> tuple[int, bool]:
    total = 0
    try:
        while len(entries) < MAX_TRACKS:
            _, body = get(SPOTIFY_PATHFINDER, data={
                "variables": {"uri": f"spotify:playlist:{ident}", "offset": len(entries), "limit": PAGE},
                "operationName": "fetchPlaylist",
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": SPOTIFY_PAGE_QUERY}},
            }, headers={"Authorization": f"Bearer {token}"})
            content = _dig(json.loads(body), "data", "playlistV2", "content")
            total = int(content["totalCount"])
            items = content["items"] or []
            for item in items:
                track = _dig(item, "itemV2", "data") or {}
                entries.append(_entry(track.get("name"), _dig(track, "artists", "items", 0, "profile", "name"),
                                      _dig(track, "albumOfTrack", "name")))
            if not items or len(entries) >= total:
                return total, False
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return max(total, len(entries)), True


# ── Apple Music ────────────────────────────────────────────────────────────

def _apple(match: re.Match, get: Fetch) -> dict[str, Any]:
    country, kind, ident = match.group(1).lower(), match.group(2).lower(), match.group(3)
    _, page = get(f"https://music.apple.com/{country}/{kind}/{ident}")
    data = _script_json(page, '<script type="application/json" id="serialized-server-data">')
    name, total, entries = "", 0, []
    for section in _dig(data, "data", 0, "data", "sections") or []:
        for item in section.get("items") or []:
            if section.get("itemKind") == "containerDetailHeaderLockup":
                name, total = str(item.get("title") or ""), int(item.get("trackCount") or 0)
            elif section.get("itemKind") == "trackLockup":
                entries.append(_entry(item.get("title"), item.get("artistName"),
                                      _dig(item, "tertiaryLinks", 0, "title") or (name if kind == "album" else "")))
    if not entries:
        raise LinkError("Apple Music would not share that playlist. Make it public first.")
    return _result("apple", name, entries, total, partial=total > len(entries))


# ── Deezer ─────────────────────────────────────────────────────────────────

def _deezer(match: re.Match, get: Fetch) -> dict[str, Any]:
    kind, ident = match.group(1).lower(), match.group(2)
    info = json.loads(get(f"https://api.deezer.com/{kind}/{ident}")[1])
    if not isinstance(info, dict) or info.get("error"):
        raise LinkError("Deezer would not share that playlist. Make it public first.")
    name = str(info.get("title") or "")
    rows = list(_dig(info, "tracks", "data") or [])
    total = int(info.get("nb_tracks") or len(rows))
    while kind == "playlist" and len(rows) < min(total, MAX_TRACKS):
        page = json.loads(get(f"https://api.deezer.com/playlist/{ident}/tracks?index={len(rows)}&limit={PAGE}")[1])
        more = page.get("data") or []
        if not more:
            break
        rows += more
    entries = [_entry(r.get("title"), _dig(r, "artist", "name"), _dig(r, "album", "title") or (name if kind == "album" else ""))
               for r in rows if isinstance(r, dict)]
    return _result("deezer", name, entries, total, partial=total > len(entries))


# ── YouTube ────────────────────────────────────────────────────────────────

def _youtube(match: re.Match, get: Fetch) -> dict[str, Any]:
    # Its songs are already YouTube's; the caller loads them by id, with nothing to match.
    return _result("youtube", "", [], youtube_playlist_id=match.group(1))


_READERS = {"spotify": _spotify, "apple": _apple, "deezer": _deezer, "youtube": _youtube}


# ── A pasted list or CSV ───────────────────────────────────────────────────

def parse_text(text: str) -> list[dict[str, str]]:
    """Exportify-style CSV, or one "Artist - Title" per line."""
    text = str(text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].lower()
    if "track name" in header or ("name" in header and "artist" in header):
        return _parse_csv(text)
    entries = []
    for line in lines:
        bare = re.sub(r"^\d+[.)]\s*", "", line).strip()
        parts = re.split(r"\s+[-–—]\s+", bare)
        entries.append(_entry(" - ".join(parts[1:]), parts[0]) if len(parts) >= 2 else _entry(bare))
    return [e for e in entries if e["title"]]


def _parse_csv(text: str) -> list[dict[str, str]]:
    rows = list(csv.reader(io.StringIO(text)))
    columns = [c.strip().lower() for c in rows[0]]

    def at(*names: str) -> int:
        return next((i for i, c in enumerate(columns) if any(n in c for n in names)), -1)

    title_at = next((i for i, c in enumerate(columns) if "track name" in c or c in ("name", "title")), -1)
    artist_at, album_at = at("artist"), at("album")
    if title_at < 0:
        return []
    cell = lambda row, i: row[i] if 0 <= i < len(row) else ""  # noqa: E731
    return [e for e in (_entry(cell(r, title_at), cell(r, artist_at).split(",")[0], cell(r, album_at)) for r in rows[1:])
            if e["title"]]


# ── Matching ───────────────────────────────────────────────────────────────

def _normalise(value: Any) -> str:
    text = str(value or "").lower()
    # "(feat. X)", "- Remastered 2011", "[Official Video]" are noise on one side only.
    text = re.sub(r"\((feat|ft|with)[^)]*\)", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\s+-\s+(remaster|remastered|radio edit|single version|official).*$", "", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def score(candidate: dict, wanted: dict) -> float:
    """How well a found song agrees with the one asked for, 0 to 1."""
    title, asked = _normalise(candidate.get("title")), _normalise(wanted.get("title"))
    if not title or not asked:
        return 0.0
    if title == asked:
        value = 0.6
    elif asked in title or title in asked:
        value = 0.4
    else:
        return 0.0  # a different song; the artist cannot rescue it
    artist, asked_artist = _normalise(candidate.get("artist")), _normalise(wanted.get("artist"))
    if not asked_artist:
        value += 0.2
    elif artist == asked_artist:
        value += 0.4
    elif artist and (asked_artist in artist or artist in asked_artist):
        value += 0.25
    return min(1.0, value)


def best_match(candidates: list[dict], wanted: dict) -> dict | None:
    scored = [(score(c, wanted), i) for i, c in enumerate(candidates) if c.get("source_id")]
    value, at = max(scored, key=lambda pair: pair[0], default=(0.0, -1))
    return candidates[at] if value >= MATCH_THRESHOLD else None
