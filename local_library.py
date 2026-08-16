"""Scan folders of audio files on this computer into the shared music library.

Everything here is deliberately conservative about two things.

**What counts as music.** The extension set is transcribed from the phone's
``AUDIO_PATTERN`` (``pwa/src/local.js:149``) and the filename fallback is a port
of its ``parseFilename`` (``pwa/src/local.js:207``). The two ends have to agree,
or a library looks different depending on which screen you are looking at.

**What a scan is allowed to do.** It adds and it marks; it never deletes. See
:func:`scan_root`. The failure mode being designed against is an external drive
that is simply not plugged in, which must cost a greyed-out row rather than a
hole in every playlist that referenced it.

Tags come from ``mutagen`` rather than a Python port of the phone's ID3/MP4
reader. On the phone a hand-rolled parser is the right call because a dependency
there is not free; on a desktop it is free, and mutagen additionally understands
FLAC/Ogg/Opus/WAV/AIFF (which ``local.js`` accepts but cannot parse) and reports
real durations, which the phone hardcodes to ``0``. It is optional in exactly the
way ``yt_dlp`` is in ``music_bridge``: without it, every file falls back to its
filename rather than the scanner failing.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Iterator

# Optional, like yt-dlp in music_bridge: absent means worse metadata, not a
# broken scanner.
try:
    from mutagen import File as _MutagenFile  # type: ignore

    MUTAGEN_AVAILABLE = True
    MUTAGEN_ERROR = ""
except Exception as exc:  # pragma: no cover - only when dep missing
    _MutagenFile = None  # type: ignore
    MUTAGEN_AVAILABLE = False
    MUTAGEN_ERROR = str(exc)


# Transcribed from pwa/src/local.js:149 AUDIO_PATTERN
# (/\.(mp3|m4a|aac|flac|wav|ogg|oga|opus|weba|webm|aiff?|alac)$/i) — the phone
# and the computer must agree on what counts as music.
AUDIO_SUFFIXES = frozenset({
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".oga",
    ".opus", ".weba", ".webm", ".aif", ".aiff", ".alac",
})

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",  ".m4a": "audio/mp4",   ".aac": "audio/aac",
    ".alac": "audio/mp4",  ".flac": "audio/flac", ".wav": "audio/wav",
    ".ogg": "audio/ogg",   ".oga": "audio/ogg",   ".opus": "audio/ogg",
    ".webm": "audio/webm", ".weba": "audio/webm",
    ".aif": "audio/aiff",  ".aiff": "audio/aiff",
}

DEFAULT_CONTENT_TYPE = "application/octet-stream"

# A single file larger than this is not a song. The cap exists so a stray disk
# image with a .wav extension cannot be handed to a phone as a track.
LOCAL_SCAN_MAX_FILE_BYTES = 1_073_741_824

# How often a running scan reports in. Per-file progress on a 100k library is
# more messages than information.
LOCAL_SCAN_BATCH = 200

# Directories a music folder never means to include. Dot-directories are
# covered by the dotfile rule; these are the ones that are not hidden.
SKIP_DIR_NAMES = frozenset({"node_modules", "__pycache__"})


def is_audio_file(name: str) -> bool:
    """Whether a filename is one the phone would also accept."""
    return os.path.splitext(str(name or ""))[1].lower() in AUDIO_SUFFIXES


def content_type_for(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(str(name or ""))[1].lower(), DEFAULT_CONTENT_TYPE)


def parse_filename(name: str) -> dict[str, str]:
    """Artist and title from a filename, ported from ``pwa/src/local.js:207``.

    ``"01 - Artist - Title.mp3"`` and ``"Artist - Title.mp3"`` are the two shapes
    that actually turn up. Anything else keeps the whole name as the title,
    which is at least honest.
    """
    bare = re.sub(r"^\d+\s*[-._]\s*", "", re.sub(r"\.[^.]+$", "", str(name or "") or "Unknown"))
    parts = re.split(r"\s+-\s+", bare)
    if len(parts) >= 2:
        return {"artist": parts[0].strip(), "title": " - ".join(parts[1:]).strip()}
    return {"artist": "", "title": bare.strip() or "Untitled"}


def long_path(path: str) -> str:
    """Extended-length form of ``path``, for one Windows file syscall.

    Windows refuses any file operation on an absolute path once it is roughly
    260 characters long -- and a music library nested a few artist/album/disc
    folders deep routinely is -- unless the path is spelled ``\\\\?\\`` (or
    ``\\\\?\\UNC\\`` for a network share). ``CreateFileW`` honours that prefix
    on every version of Windows, which makes it a strictly better fix here
    than relying on the *unprefixed* long-path support Windows 10 can also
    grant: that one additionally needs a machine-wide registry policy the
    user has to opt into *and* a per-executable manifest declaring
    ``longPathAware`` that this app's PyInstaller build does not carry.

    Applied narrowly, at the syscall and not the path stored anywhere: a
    prefixed path is NT-native syntax, not a filename, so it must never reach
    the database, a comparison, or a phone. It is a no-op everywhere but
    Windows, for a relative path, and for a path already given in this form.
    """
    if os.name != "nt":
        return path
    text = str(path or "")
    if not text or text.startswith("\\\\?\\") or not os.path.isabs(text):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _within(real_root: str, path: str) -> bool:
    """Whether ``path`` really resolves inside ``real_root``.

    ``os.walk(followlinks=False)`` already declines to descend through a
    symlinked *directory*, but it still lists symlinked *files*. Without this
    check a folder of symlinks pointing at ``~/.ssh`` would become a file
    disclosure primitive the moment anything could ask for those bytes. Grants
    name a ``track_id`` rather than a path, so this is defence in depth — which
    is exactly the layer that has to hold when the one above it is wrong.

    Compared through :func:`os.path.normcase` on both sides: a no-op on POSIX,
    where ``Song.mp3`` and ``song.mp3`` are different files, but folded to one
    case on Windows, where they are not — and where a resolved path can come
    back cased differently than the root string it is still genuinely inside.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    # normcase() first, *then* strip a trailing separator: a root recorded
    # with the "wrong" slash style (a forward slash where os.sep is "\\") or
    # a doubled-up separator must still collapse to exactly one, or a stray
    # extra separator here means nothing this root ever contains compares
    # equal again.
    root = os.path.normcase(real_root).rstrip(os.sep)
    candidate = os.path.normcase(real)
    return candidate == root or candidate.startswith(root + os.sep)


def iter_audio_files(root: str) -> Iterator[str]:
    """Every playable file under ``root``, skipping anything that escapes it.

    Directories are walked through the same containment check as files, for a
    reason ``os.walk(followlinks=False)`` alone does not cover on Windows: an
    NTFS junction is a reparse point with the ``IO_REPARSE_TAG_MOUNT_POINT``
    tag, not ``IO_REPARSE_TAG_SYMLINK``, and CPython's ``os.path.islink()`` —
    which is exactly what ``followlinks=False`` relies on to decline recursion
    — only recognises the latter. A junction is invisible to that guard, so it
    would be walked like an ordinary folder. ``_within`` still keeps it from
    disclosing anything outside ``root`` (files are filtered the same way they
    always were), and the ``visited`` set below keeps a junction that loops
    back on a folder already walked — a "shortcut to itself" — from recursing
    without end, which containment alone would not catch: the loop target is
    still "within root".
    """
    real_root = os.path.realpath(str(root))
    visited = {os.path.normcase(real_root)}
    # normcase'd, and computed once per walk rather than hoisted to module
    # scope: a folder literally named "Node_Modules" is a different name than
    # "node_modules" on POSIX but the same one on a case-folding filesystem,
    # and precomputing this at import time would freeze in whatever
    # normcase() happened to mean before a test (or a future caller) swaps it.
    skip_names = {os.path.normcase(name) for name in SKIP_DIR_NAMES}
    for folder, dirnames, filenames in os.walk(real_root, followlinks=False):
        kept = []
        for name in dirnames:
            if name.startswith(".") or os.path.normcase(name) in skip_names:
                continue
            candidate = os.path.join(folder, name)
            if not _within(real_root, candidate):
                continue
            try:
                real_dir = os.path.realpath(candidate)
            except OSError:
                continue
            key = os.path.normcase(real_dir)
            if key in visited:
                continue
            visited.add(key)
            kept.append(name)
        dirnames[:] = sorted(kept)
        for name in sorted(filenames):
            if name.startswith(".") or not is_audio_file(name):
                continue
            path = os.path.join(folder, name)
            if _within(real_root, path):
                yield path


def _first_tag(tags: Any, key: str) -> str:
    try:
        value = tags.get(key)
    except Exception:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def read_tags(path: str) -> dict[str, Any]:
    """Title/artist/album/duration for one file, or ``{}``.

    Never raises: an unreadable tag block is a worse-looking row, not a failed
    scan. Mirrors ``pwa/src/local.js:165`` — one bad file must not abandon the
    rest of the import.
    """
    if not MUTAGEN_AVAILABLE:
        return {}
    try:
        audio = _MutagenFile(long_path(str(path)), easy=True)
    except Exception:
        return {}
    if audio is None:
        return {}
    found: dict[str, Any] = {}
    tags = getattr(audio, "tags", None)
    if tags is not None:
        found["title"] = _first_tag(tags, "title")
        found["artist"] = _first_tag(tags, "artist") or _first_tag(tags, "albumartist")
        found["album"] = _first_tag(tags, "album")
    length = getattr(getattr(audio, "info", None), "length", None)
    try:
        duration = round(float(length), 3) if length else None
    except (TypeError, ValueError):
        duration = None
    # A zero-length reading is mutagen saying "I don't know", not a zero-second
    # song; storing it would be worse than storing nothing.
    found["duration_s"] = duration if duration and duration > 0 else None
    return found


def describe_file(path: str) -> dict[str, Any] | None:
    """Everything the state layer needs about one file, or None to skip it.

    Raises ``OSError`` when the file cannot be stat'd, which the caller counts
    and moves past.
    """
    stat = os.stat(long_path(path))
    if stat.st_size <= 0 or stat.st_size > LOCAL_SCAN_MAX_FILE_BYTES:
        return None
    name = os.path.basename(str(path))
    tags = read_tags(path)
    fallback = parse_filename(name)
    return {
        "file_path": str(path),
        "file_size": int(stat.st_size),
        "file_mtime": float(stat.st_mtime),
        "content_type": content_type_for(name),
        "title": str(tags.get("title") or "").strip() or fallback["title"],
        "artist": str(tags.get("artist") or "").strip() or fallback["artist"],
        "album": str(tags.get("album") or "").strip(),
        "duration_s": tags.get("duration_s"),
    }


_COUNT_KEYS = ("scanned", "added", "updated", "moved", "unchanged", "skipped", "missing")


def _empty_counts(root: str) -> dict[str, Any]:
    return {"root": str(root), **{key: 0 for key in _COUNT_KEYS}, "error": ""}


def scan_root(state: Any, root: str, *,
              on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Bring one folder's contents into the library.

    Three properties this function exists to guarantee.

    **Nothing is deleted.** Files that were in the library and are no longer on
    disk get ``missing_since`` stamped by
    :meth:`MusicState.mark_local_tracks_missing`, never removed.

    **An unavailable folder marks nothing.** If the root itself cannot be read
    the function returns early, *before* the marking step. Otherwise unplugging
    a drive would grey out its entire library, which — to anybody looking at a
    playlist — is the same damage as deleting it.

    **Absence is decided before presence.** The walk happens in full first, then
    the marking, and only then the per-file work. That ordering is what makes
    move repair possible: a file moved *within* one root is discovered at its new
    path in the same pass that would have noticed its old one, and repair only
    considers rows a scan has already given up on. Marking afterwards instead
    would insert a second row for the new path and then mark the original
    missing — the library would double, and every playlist would be pointing at
    the dead half. The cheap half of the work (listing names) is the half that
    runs first, so this costs a set of paths rather than a second pass over the
    bytes.
    """
    counts = _empty_counts(root)
    real_root = os.path.realpath(str(root or ""))
    if not real_root or not os.path.isdir(real_root):
        counts["error"] = "that folder is not available right now"
        return counts

    found = list(iter_audio_files(real_root))
    # Everything walked counts as seen before anything can fail on it. A file
    # that is present but momentarily unreadable is not a missing file, and
    # marking it would punish a transient permission blip with a greyed row.
    marked = state.mark_local_tracks_missing(real_root, set(found))

    # Which files the library already holds unchanged. Rescanning is the common
    # operation — a folder is scanned once and re-scanned forever — and without
    # this every rescan re-reads every tag block and rewrites every row to
    # produce exactly the state it started in.
    stats: dict[str, tuple[int, float]] = {}
    for path in found:
        try:
            info = os.stat(long_path(path))
        except OSError:
            continue
        stats[path] = (int(info.st_size), float(info.st_mtime))
    unchanged = state.unchanged_local_paths(real_root, stats)

    for path in found:
        counts["scanned"] += 1
        if path in unchanged:
            counts["unchanged"] += 1
            continue
        try:
            record = describe_file(path)
        except OSError:
            record = None
        if record is None:
            counts["skipped"] += 1
            continue
        try:
            row = state.upsert_local_track(**record)
        except Exception:
            # One unreadable file must not abandon the rest of the scan —
            # the same rule as pwa/src/local.js:165.
            counts["skipped"] += 1
            continue
        action = str(row.get("local_action") or "updated")
        counts[action if action in counts else "updated"] += 1
        if on_progress is not None and counts["scanned"] % LOCAL_SCAN_BATCH == 0:
            on_progress({**counts, "missing": max(0, marked - counts["moved"]), "path": path})

    # A row marked in the reconcile step and then claimed by move repair was
    # never actually gone; reporting it as missing would make a tidy-up look
    # like a loss. Floored at zero because a repair may also claim a row an
    # *earlier* scan marked, which this run never counted.
    counts["missing"] = max(0, marked - counts["moved"])
    if on_progress is not None:
        on_progress({**counts, "path": ""})
    return counts


def scan(state: Any, roots: Any = None, *,
         on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Scan the registered roots, or the subset of them that was asked for.

    A requested root that is not registered is ignored rather than scanned.
    Folder choice is a decision made at the computer through a native picker;
    a phone may re-run a scan but may never name a path, so an arbitrary path
    arriving here is not an instruction, it is something to drop.
    """
    registered = [str(row.get("path") or "") for row in state.list_local_roots()]
    wanted = [str(path or "").strip() for path in (roots or []) if str(path or "").strip()]
    targets = [path for path in registered if path in wanted] if wanted else list(registered)

    results: list[dict[str, Any]] = []
    for path in targets:
        result = scan_root(state, path, on_progress=on_progress)
        results.append(result)
        try:
            state.record_local_root_scan(
                path,
                last_error=str(result.get("error") or ""),
                track_count=int(result.get("scanned") or 0) - int(result.get("skipped") or 0),
            )
        except Exception:
            pass

    totals = {key: sum(int(item.get(key) or 0) for item in results) for key in _COUNT_KEYS}
    return {
        **totals,
        # `scanned_roots`, not `roots`: `status()` already uses `roots` for the
        # registered folders, and a caller that merges the two payloads (the
        # scan result event does exactly that) would otherwise lose one of them
        # silently to the other.
        "scanned_roots": results,
        "ignored": sorted(set(wanted) - set(targets)),
        "mutagen_available": MUTAGEN_AVAILABLE,
    }


def status(state: Any) -> dict[str, Any]:
    return {
        **state.local_library_status(),
        "mutagen_available": MUTAGEN_AVAILABLE,
        "mutagen_error": MUTAGEN_ERROR,
    }
