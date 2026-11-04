"""A playlist from a pasted link: Spotify, Apple Music, YouTube, SoundCloud, Tidal, Deezer, JioSaavn,
NetEase, or any page that publishes its track list (Qobuz, Bandcamp...). No accounts, no keys."""

from __future__ import annotations

import csv
import html
import io
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from typing import Any, Callable

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
TIMEOUT_S = 15
PAGE = 100
MAX_TRACKS = 2000
MATCH_THRESHOLD = 0.55
MAX_PAGE_BYTES = 8_000_000
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
# The Spotify web player's own playlist query. ponytail: Spotify can rotate this hash;
# then only the first 100 tracks load and the result says `partial`.
SPOTIFY_PAGE_QUERY = "19ff1327c29e99c208c86d7a9d8f1929cfdf3d3202a0ff4253c821f1901aa94d"
SPOTIFY_PATHFINDER = "https://api-partner.spotify.com/pathfinder/v2/query"

_SPOTIFY = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?|spotify:)(playlist|album)[/:]([A-Za-z0-9]{10,40})", re.I)
_APPLE = re.compile(r"music\.apple\.com/([a-z]{2})/(playlist|album)/(?:[^/?#\s]+/)?(pl\.[A-Za-z0-9-]+|\d+)", re.I)
_DEEZER = re.compile(r"deezer\.com/(?:[a-z]{2}/)?(playlist|album)/(\d+)", re.I)
_YOUTUBE = re.compile(r"(?:youtube\.com|youtu\.be)/\S*?[?&]list=([A-Za-z0-9_-]{10,80})", re.I)
_SOUNDCLOUD = re.compile(r"soundcloud\.com/([\w-]+)/sets/([\w-]+)(?:/(s-\w+))?", re.I)
_TIDAL = re.compile(r"tidal\.com/(?:browse/)?(playlist|album)/([0-9a-fA-F-]{36}|\d+)", re.I)
_JIOSAAVN = re.compile(r"jiosaavn\.com/(?:s/)?(featured|playlist|album)/(?:[^/?#\s]+/)*([\w-]+)", re.I)
_NETEASE = re.compile(r"music\.163\.com/\S*?playlist\?(?:\S*?&)?id=(\d+)", re.I)
# Share buttons hand out these; they are opened as given and must lead to one of the above.
_SHORT = re.compile(r"https://(?:spotify\.link|link\.deezer\.com|deezer\.page\.link|on\.soundcloud\.com|apple\.co)/[A-Za-z0-9/_-]+", re.I)
_LINK = re.compile(r"https://\S+", re.I)

Fetch = Callable[..., tuple[str, str]]


class LinkError(ValueError):
    """Something a person can act on."""


def _check_public(url: str) -> None:
    """A paired phone chooses what this computer opens: never anything on its own network.
    ponytail: checked before connecting, so DNS rebinding could slip past; pin the address if that matters."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise LinkError("Only https links can be read.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, 443)}
    except OSError as exc:
        raise LinkError("That site could not be found.") from exc
    if not all(ipaddress.ip_address(address.split("%")[0]).is_global for address in addresses):
        raise LinkError("That link points inside this network.")


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_PublicRedirects)


def fetch(url: str, data: dict | None = None, headers: dict | None = None) -> tuple[str, str]:
    """GET, or POST `data` as JSON. Returns the final address and the body."""
    _check_public(url)
    body = json.dumps(data).encode() if data is not None else None
    # A browser's Accept: Bandcamp answers anything else with a bot check.
    sent = {"User-Agent": UA, "Accept": BROWSER_ACCEPT, "Accept-Language": "en-US,en;q=0.9",
            **({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    with _OPENER.open(urllib.request.Request(url, data=body, headers=sent), timeout=TIMEOUT_S) as resp:
        return resp.geturl(), resp.read(MAX_PAGE_BYTES).decode("utf-8", "replace")


def looks_like_link(text: str) -> bool:
    """A link (any link: an unknown one should hear why), not a list of songs."""
    text = str(text or "")
    return bool(re.match(r"\s*(https?://|spotify:)", text, re.I) or _known(text) or _SHORT.search(text))


def read(text: str, *, get: Fetch | None = None) -> dict[str, Any]:
    """{name, source, total, partial, entries: [{title, artist, album}], youtube_playlist_id}."""
    get = get or fetch
    text = str(text or "")
    found = _known(text)
    if not found:
        short = _SHORT.search(text)
        if short:
            final, body = get(short.group(0))
            found = _known(final) or _known(body)
            if not found:
                raise LinkError("That link did not lead to a playlist. Open it in a browser and copy the address.")
        elif _LINK.search(text):
            return _web_page(_LINK.search(text).group(0).rstrip(").,>\"'"), get)
        else:
            raise LinkError("Paste a playlist link: Spotify, Apple Music, YouTube, SoundCloud, Tidal, Deezer and more.")
    source, match = found
    return _READERS[source](match, get)


def _known(text: str) -> tuple[str, re.Match] | None:
    for source, pattern in (("spotify", _SPOTIFY), ("apple", _APPLE), ("deezer", _DEEZER), ("youtube", _YOUTUBE),
                            ("soundcloud", _SOUNDCLOUD), ("tidal", _TIDAL), ("jiosaavn", _JIOSAAVN), ("netease", _NETEASE)):
        match = pattern.search(text)
        if match:
            return source, match
    return None


def _result(source: str, name: str, entries: list, total: int = 0, partial: bool = False, **extra: Any) -> dict[str, Any]:
    """Partial is decided here, after the ceiling: a reader cannot see what the cut drops."""
    titled = [e for e in entries if e["title"]]
    kept = titled[:MAX_TRACKS]
    whole = max(total, len(titled))
    return {"name": name, "source": source, "total": whole, "partial": partial or whole > len(kept),
            "entries": kept, "youtube_playlist_id": "", **extra}


def _entry(title: Any, artist: Any = "", album: Any = "") -> dict[str, str]:
    clean = lambda value: html.unescape(str(value or "")).strip()  # noqa: E731  (JioSaavn sends &quot;)
    return {"title": clean(title), "artist": clean(artist), "album": clean(album)}


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


# ── SoundCloud ─────────────────────────────────────────────────────────────

def _soundcloud(match: re.Match, get: Fetch) -> dict[str, Any]:
    user, slug, secret = match.group(1), match.group(2), match.group(3)
    _, page = get(f"https://soundcloud.com/{user}/sets/{slug}" + (f"/{secret}" if secret else ""))
    hydration = re.search(r"window\.__sc_hydration\s*=\s*(\[.*?\]);\s*</script>", page, re.S)
    playlist = next((x.get("data") for x in json.loads(hydration.group(1)) if x.get("hydratable") == "playlist"), None) \
        if hydration else None
    if not isinstance(playlist, dict):
        raise LinkError("SoundCloud would not share that playlist. Make it public first.")
    tracks = [t for t in playlist.get("tracks") or [] if isinstance(t, dict)]
    # The page carries the first few in full and only ids for the rest; SoundCloud's own web API has them.
    missing = [str(t.get("id")) for t in tracks if not t.get("title")]
    try:
        if missing:
            client_id = _soundcloud_client_id(page, get)
            found: dict[str, dict] = {}
            for at in range(0, len(missing), 50):
                body = get(f"https://api-v2.soundcloud.com/tracks?ids={','.join(missing[at:at + 50])}&client_id={client_id}")[1]
                found.update({str(t.get("id")): t for t in json.loads(body) if isinstance(t, dict)})
            tracks = [found.get(str(t.get("id")), t) for t in tracks]
    except (OSError, ValueError, KeyError, TypeError, LinkError):
        pass
    entries = [_entry(t.get("title"), _dig(t, "publisher_metadata", "artist") or _dig(t, "user", "username")) for t in tracks]
    total = int(playlist.get("track_count") or len(tracks))
    return _result("soundcloud", str(playlist.get("title") or ""), entries, total,
                   partial=sum(1 for e in entries if e["title"]) < total)


def _soundcloud_client_id(page: str, get: Fetch) -> str:
    for script in reversed(re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', page)):
        found = re.search(r'client_id\s*:\s*"(\w{32})"', get(script)[1])
        if found:
            return found.group(1)
    raise LinkError("SoundCloud did not answer.")


# ── Tidal ──────────────────────────────────────────────────────────────────

def _tidal(match: re.Match, get: Fetch) -> dict[str, Any]:
    kind, ident = match.group(1).lower(), match.group(2)
    _, page = get(f"https://embed.tidal.com/{kind}s/{ident}")
    opening = "window.tidalMediaSessionMetadata="
    start = page.find(opening)
    try:
        tracks = json.JSONDecoder().raw_decode(page, start + len(opening))[0] if start >= 0 else {}
    except ValueError:
        tracks = {}
    entries = [_entry(t.get("title"), str(t.get("artist") or "").split(", ")[0], t.get("album"))
               for t in tracks.values() if isinstance(t, dict)]
    if not entries:
        raise LinkError("Tidal would not share that playlist. Make it public first.")
    name, total = "", 0
    try:  # its name and size are on the share page
        share = get(f"https://tidal.com/browse/{kind}/{ident}")[1]
        name = html.unescape(re.search(r'og:title" content="([^"]*)', share).group(1))
        total = int(re.search(r'og:description" content="[^"]*?(\d+) (?:items|tracks)', share).group(1))
    except (OSError, AttributeError, ValueError):
        pass
    return _result("tidal", name, entries, total, partial=total > len(entries))


# ── JioSaavn ───────────────────────────────────────────────────────────────

def _jiosaavn(match: re.Match, get: Fetch) -> dict[str, Any]:
    kind = "album" if match.group(1).lower() == "album" else "playlist"
    token, name, total, entries = match.group(2), "", 0, []
    for page in range(1, MAX_TRACKS // 50 + 1):
        data = json.loads(get(f"https://www.jiosaavn.com/api.php?__call=webapi.get&token={token}&type={kind}"
                              f"&p={page}&n=50&includeMetaTags=0&ctx=web6dot0&api_version=4&_format=json&_marker=0")[1])
        if not isinstance(data, dict):
            break
        songs = data["list"] if isinstance(data.get("list"), list) else []  # "" when the token is unknown
        name, total = name or str(data.get("title") or ""), int(data.get("list_count") or 0)
        entries += [_entry(s.get("title"), _dig(s, "more_info", "artistMap", "primary_artists", 0, "name"),
                           _dig(s, "more_info", "album")) for s in songs if isinstance(s, dict)]
        if not songs or len(entries) >= total:
            break
    if not entries:
        raise LinkError("JioSaavn would not share that playlist.")
    return _result("jiosaavn", html.unescape(name), entries, total, partial=total > len(entries))


# ── NetEase Cloud Music ────────────────────────────────────────────────────

def _netease(match: re.Match, get: Fetch) -> dict[str, Any]:
    result = json.loads(get(f"https://music.163.com/api/playlist/detail?id={match.group(1)}")[1]).get("result") or {}
    entries = [_entry(t.get("name"), _dig(t, "artists", 0, "name"), _dig(t, "album", "name"))
               for t in result.get("tracks") or [] if isinstance(t, dict)]
    if not entries:
        raise LinkError("NetEase would not share that playlist.")
    total = int(result.get("trackCount") or 0)
    return _result("netease", str(result.get("name") or ""), entries, total, partial=total > len(entries))


# ── Any page that publishes its track list (schema.org): Qobuz, Bandcamp and others ──

def _web_page(url: str, get: Fetch) -> dict[str, Any]:
    _, page = get(url)
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            found = _schema_list(item)
            if found:
                return found
    raise LinkError("Rainette can't read a playlist from that page. Try a Spotify, Apple Music, YouTube, "
                    "SoundCloud, Tidal or Deezer link, or a CSV.")


def _schema_list(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict) or item.get("@type") not in ("MusicPlaylist", "MusicAlbum"):
        return None
    tracks = item.get("track") or []
    if isinstance(tracks, dict):
        tracks = tracks.get("itemListElement") or []
    name = str(item.get("name") or "")
    album_artist = _schema_name(item.get("byArtist"))
    entries = []
    for track in tracks if isinstance(tracks, list) else []:
        track = track.get("item", track) if isinstance(track, dict) else {}
        entries.append(_entry(track.get("name"), _schema_name(track.get("byArtist")) or album_artist,
                              _schema_name(track.get("inAlbum")) or (name if item["@type"] == "MusicAlbum" else "")))
    if not any(e["title"] for e in entries):
        return None
    total = int(item.get("numTracks") or 0)
    return _result("web", html.unescape(name), entries, total, partial=total > len(entries))


def _schema_name(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value.get("name") or "") if isinstance(value, dict) else str(value or "")


_READERS = {"spotify": _spotify, "apple": _apple, "deezer": _deezer, "youtube": _youtube,
            "soundcloud": _soundcloud, "tidal": _tidal, "jiosaavn": _jiosaavn, "netease": _netease}


# ── A pasted list or CSV ───────────────────────────────────────────────────

_AUDIO_FILE = re.compile(r"\.(mp3|m4a|aac|flac|wav|ogg|opus|aiff?|wma|alac)$", re.I)
# The words a converter's CSV uses for each column (Exportify, TuneMyMusic, Soundiiz...); ids and links skipped.
_TITLE_WORDS, _ARTIST_WORDS, _ALBUM_WORDS = ("track name", "title", "song", "name", "track"), ("artist",), ("album",)


def parse_text(text: str) -> list[dict[str, str]]:
    """A converter's CSV, an M3U playlist file, or one "Artist - Title" per line."""
    text = str(text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].lower()
    if "," in header and "artist" in header and any(word in header for word in _TITLE_WORDS):
        return _parse_csv(text)
    if any(line.upper().startswith("#EXTINF:") for line in lines):
        lines = [line.split(",", 1)[-1] for line in lines if line.upper().startswith("#EXTINF:")]  # "#EXTINF:215,Artist - Title"
    else:  # a bare M3U is file paths; their names usually read "Artist - Title.mp3"
        lines = [_AUDIO_FILE.sub("", re.split(r"[\\/]", line)[-1]) if _AUDIO_FILE.search(line) else line
                 for line in lines if not line.startswith("#")]
    entries = []
    for line in lines:
        bare = re.sub(r"^\d+[.)]\s*", "", line).strip()
        parts = re.split(r"\s+[-–—]\s+", bare)
        entries.append(_entry(" - ".join(parts[1:]), parts[0]) if len(parts) >= 2 else _entry(bare))
    return [e for e in entries if e["title"]]


def _parse_csv(text: str) -> list[dict[str, str]]:
    rows = list(csv.reader(io.StringIO(text)))
    columns = [c.strip().lower() for c in rows[0]]

    def at(words: tuple[str, ...], avoid: tuple[str, ...]) -> int:
        # Earlier words win; a column naming an id, a link or another field is never the one.
        skip = ("uri", "url", "id", "isrc", "playlist") + avoid
        return next((i for word in words for i, c in enumerate(columns)
                     if word in c and not any(a in c.split() or a in c.replace("(", " ").split() for a in skip)), -1)

    title_at = at(_TITLE_WORDS, ("artist", "album"))
    artist_at, album_at = at(_ARTIST_WORDS, ("album",)), at(_ALBUM_WORDS, ("artist",))
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
