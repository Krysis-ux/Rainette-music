"""SQLite-backed music state: playlists, tracks, and play history.

A compact standalone SQLite state layer for Rainette Music. Its methods and SQL
support the music bridge directly.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json
import os
import time
import sqlite3
import threading
import uuid

# Source marker for tracks that are files on this computer rather than
# something to be resolved from the network.
LOCAL_SOURCE = "local"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def loads_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def loads_value(value: str | None, default: Any = None) -> Any:
    """Parse a stored JSON value of any shape.

    Settings are booleans, numbers and strings as often as objects, and
    loads_object flattens all of those to {} — which would turn "fade is off"
    into "fade is unset" on every read."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def loads_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _monday_of(day: date) -> date:
    """The Monday of ``day``'s week (weekday() has Monday == 0)."""
    return day - timedelta(days=day.weekday())


def _add_months(first_of_month: date, months: int) -> date:
    """First-of-month arithmetic that never overflows a day into the wrong month."""
    index = first_of_month.month - 1 + months
    return date(first_of_month.year + index // 12, index % 12 + 1, 1)


def plan_insight_buckets(today: date, days: int, earliest: date) -> tuple[str, list[dict[str, Any]]]:
    """Choose the Insights chart's bucket unit and spans for a range.

    Keeps the bar count small enough that every value label always renders:
    a 7-day range stays daily, a month rolls up to Monday-start weeks, and
    all-time (``days == 0``) rolls up to at most the last 12 monthly buckets.
    The UI only ever asks for 7 / 30 / 0, which map to day / week / month.

    Returns ``(unit, spans)`` where each span is ``{"start", "end", "key"}``:
    ``start``/``end`` are inclusive local dates and ``key`` is what a play's
    local date is matched against to fall into that bucket.
    """
    if days == 0:
        this_month = today.replace(day=1)
        start_month = max(earliest.replace(day=1), _add_months(this_month, -11))
        spans: list[dict[str, Any]] = []
        month = start_month
        while month <= this_month:
            spans.append({"start": month, "end": _add_months(month, 1) - timedelta(days=1),
                          "key": (month.year, month.month)})
            month = _add_months(month, 1)
        return "month", spans
    if days <= 7:
        spans = []
        day = today - timedelta(days=days - 1)
        while day <= today:
            spans.append({"start": day, "end": day, "key": day})
            day += timedelta(days=1)
        return "day", spans
    first_monday = _monday_of(today - timedelta(days=days - 1))
    spans = []
    week = first_monday
    while week <= today:
        spans.append({"start": week, "end": week + timedelta(days=6), "key": week})
        week += timedelta(days=7)
    return "week", spans


def _bucket_key(unit: str, local_day: date):
    """The bucket key a play's local date maps to for a given unit."""
    if unit == "day":
        return local_day
    if unit == "week":
        return _monday_of(local_day)
    return (local_day.year, local_day.month)


class MusicState:
    """Small SQLite state store owned by the local music server."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def connect(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA journal_mode = WAL")
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS music_playlists (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS music_tracks (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT 'youtube',
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    duration_s REAL,
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    added_at TEXT NOT NULL,
                    UNIQUE(source, source_id)
                );

                CREATE TABLE IF NOT EXISTS music_playlist_tracks (
                    playlist_id TEXT NOT NULL REFERENCES music_playlists(id) ON DELETE CASCADE,
                    track_id TEXT NOT NULL REFERENCES music_tracks(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (playlist_id, track_id)
                );
                CREATE INDEX IF NOT EXISTS idx_music_playlist_tracks_playlist
                    ON music_playlist_tracks(playlist_id, position);

                CREATE TABLE IF NOT EXISTS music_play_history (
                    id TEXT PRIMARY KEY,
                    track_id TEXT NOT NULL REFERENCES music_tracks(id) ON DELETE CASCADE,
                    played_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_music_play_history_track
                    ON music_play_history(track_id, played_at);
                """
            )
            self._migrate_db(conn)

    def _columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _add_column(self, conn: sqlite3.Connection, table: str, column: str, sql: str) -> None:
        if column not in self._columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {sql}")

    def _migrate_db(self, conn: sqlite3.Connection) -> None:
        self._add_column(conn, "music_playlists", "kind", "kind TEXT NOT NULL DEFAULT 'manual'")
        self._add_column(conn, "music_playlists", "folder_id", "folder_id TEXT NOT NULL DEFAULT ''")
        self._add_column(conn, "music_playlists", "pinned", "pinned INTEGER NOT NULL DEFAULT 0")
        self._add_column(conn, "music_playlists", "position", "position INTEGER NOT NULL DEFAULT 0")
        self._add_column(conn, "music_playlists", "rules_json", "rules_json TEXT NOT NULL DEFAULT '{}'")
        self._add_column(conn, "music_playlists", "artwork_key", "artwork_key TEXT NOT NULL DEFAULT ''")

        # Files that live on this computer. `file_path` is the only part of a
        # local track's identity that is allowed to move; `source_id` is opaque
        # and assigned once (see upsert_local_track), so reorganising a music
        # folder or retagging an album never re-creates the row and never drops
        # it out of a playlist.
        #
        # `missing_since` rather than a delete: a scanner marks, it never
        # removes. An external drive that is merely unplugged must cost a greyed
        # out row, not a hole in every playlist that referenced it.
        self._add_column(conn, "music_tracks", "file_path", "file_path TEXT NOT NULL DEFAULT ''")
        self._add_column(conn, "music_tracks", "file_size", "file_size INTEGER NOT NULL DEFAULT 0")
        self._add_column(conn, "music_tracks", "file_mtime", "file_mtime REAL NOT NULL DEFAULT 0")
        self._add_column(conn, "music_tracks", "content_type", "content_type TEXT NOT NULL DEFAULT ''")
        self._add_column(conn, "music_tracks", "missing_since", "missing_since TEXT NOT NULL DEFAULT ''")

        conn.executescript(
            """
            /* Partial: only local tracks carry a path, and they are a minority
               of a library dominated by network sources. */
            CREATE INDEX IF NOT EXISTS idx_music_tracks_file_path
                ON music_tracks(file_path) WHERE file_path <> '';

            CREATE TABLE IF NOT EXISTS music_local_roots (
                path         TEXT PRIMARY KEY,
                added_at     TEXT NOT NULL,
                last_scan_at TEXT NOT NULL DEFAULT '',
                last_error   TEXT NOT NULL DEFAULT '',
                track_count  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS music_playlist_folders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_music_playlist_folders_position
                ON music_playlist_folders(position, updated_at);

            CREATE TABLE IF NOT EXISTS music_queue_sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tracks_json TEXT NOT NULL DEFAULT '[]',
                position INTEGER NOT NULL DEFAULT 0,
                is_last INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_music_queue_sessions_last
                ON music_queue_sessions(is_last, updated_at);

            CREATE TABLE IF NOT EXISTS music_followed_artists (
                artist_key TEXT PRIMARY KEY,
                artist_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                thumbnail_url TEXT NOT NULL DEFAULT '',
                followed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_music_followed_artists_followed
                ON music_followed_artists(followed_at DESC);

            /* Small singleton records that outlive a process. The playback
               target lives here so "where is this playing" survives a restart
               instead of being re-guessed from whichever window spoke last. */
            CREATE TABLE IF NOT EXISTS music_kv (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL DEFAULT '{}',
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            /* Phones this computer knows. `follows_desktop` was memory-only,
               which is the only reason evicting an idle device log used to lose
               a phone's linked mode. */
            CREATE TABLE IF NOT EXISTS music_devices (
                device_id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'phone',
                follows_desktop INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_music_devices_last_seen
                ON music_devices(last_seen_at DESC);

            /* Per-phone settings, stamped per key rather than per blob: two
               devices editing different keys must both survive the merge. */
            CREATE TABLE IF NOT EXISTS music_device_settings (
                device_id TEXT NOT NULL REFERENCES music_devices(device_id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                value_json TEXT NOT NULL DEFAULT 'null',
                updated_ms INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL DEFAULT 'phone',
                PRIMARY KEY (device_id, key)
            );
            """
        )

        # After the script above, because that is where music_devices is
        # created and ALTER TABLE cannot precede it on a fresh database.
        #
        # What each paired phone reported it can decode, from its own
        # `canPlayType`. Kept on the device row rather than in
        # music_device_settings, because that table round-trips to the phone as
        # its own preferences and this is the computer's note *about* the phone.
        self._add_column(conn, "music_devices", "codec_support_json",
                         "codec_support_json TEXT NOT NULL DEFAULT '{}'")

    # ── Row helpers ──────────────────────────────────────────────────────────

    def _track_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = loads_object(item.pop("metadata_json", "{}"))
        item.pop("_last", None)   # internal ordering column, never exposed
        return item

    @staticmethod
    def _track_artist_key(track: dict[str, Any]) -> str:
        """Artist identity of a track: artist_id when present, else lowercased name.

        Matches music_library_index so the Artists tab, top artists, and
        artist-scoped deletes all agree on what counts as one artist. artist_id
        lives inside metadata_json, so this cannot be expressed in SQL.

        Distinct from _artist_key(), which builds the "id:"/"name:" primary key of
        the followed-artists table - a different key space, not interchangeable.
        """
        metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
        name = str(track.get("artist") or "").strip() or "Unknown artist"
        artist_id = str(metadata.get("artist_id") or "").strip()
        return (artist_id or name).lower()

    # ── Playlists ────────────────────────────────────────────────────────────

    def create_playlist(self, name: str, description: str = "", *, kind: str = "manual",
                        rules: dict[str, Any] | None = None) -> dict[str, Any]:
        cleaned = str(name or "").strip()[:120]
        if not cleaned:
            raise ValueError("playlist name is required")
        cleaned_kind = "smart" if kind == "smart" else "manual"
        playlist_id = "pl_" + uuid.uuid4().hex
        now = utc_now()
        with self.connect() as conn:
            pos_row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS next FROM music_playlists").fetchone()
            conn.execute(
                """
                INSERT INTO music_playlists
                    (id, name, description, created_at, updated_at, kind, position, rules_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (playlist_id, cleaned, str(description or "")[:400], now, now, cleaned_kind,
                 int(pos_row["next"]), dumps(self.sanitize_smart_rules(rules) if cleaned_kind == "smart" else {})),
            )
            row = conn.execute("SELECT * FROM music_playlists WHERE id = ?", (playlist_id,)).fetchone()
        return dict(row)

    def _playlist_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["pinned"] = bool(item.get("pinned"))
        item["position"] = int(item.get("position") or 0)
        item["folder_id"] = item.get("folder_id") or ""
        item["kind"] = item.get("kind") or "manual"
        item["rules"] = loads_object(item.pop("rules_json", "{}"))
        if item["kind"] == "smart":
            item["track_count"] = len(self._smart_playlist_tracks(conn, item["rules"], count_only=True))
        return item

    def list_playlists(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM music_playlist_tracks t WHERE t.playlist_id = p.id) AS track_count
                FROM music_playlists p
                ORDER BY p.pinned DESC, p.position ASC, p.updated_at DESC, p.created_at DESC
                """
            ).fetchall()
            return [self._playlist_from_row(conn, row) for row in rows]

    def get_playlist(self, playlist_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM music_playlist_tracks t WHERE t.playlist_id = p.id) AS track_count
                FROM music_playlists p WHERE p.id = ?
                """,
                (str(playlist_id or "").strip(),),
            ).fetchone()
            return self._playlist_from_row(conn, row) if row is not None else None

    def update_playlist_artwork(self, playlist_id: str, artwork_key: str) -> dict[str, Any]:
        playlist_id = str(playlist_id or "").strip()
        cleaned_key = str(artwork_key or "").strip()[:240]
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE music_playlists SET artwork_key = ?, updated_at = ? WHERE id = ?",
                (cleaned_key, utc_now(), playlist_id),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError("playlist not found")
            row = conn.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM music_playlist_tracks t WHERE t.playlist_id = p.id) AS track_count
                FROM music_playlists p WHERE p.id = ?
                """,
                (playlist_id,),
            ).fetchone()
            return self._playlist_from_row(conn, row)

    def list_playlist_folders(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.*, (SELECT COUNT(*) FROM music_playlists p WHERE p.folder_id = f.id) AS playlist_count
                FROM music_playlist_folders f
                ORDER BY f.position ASC, f.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_playlist_folder(self, name: str) -> dict[str, Any]:
        cleaned = str(name or "").strip()[:80]
        if not cleaned:
            raise ValueError("folder name is required")
        folder_id = "fld_" + uuid.uuid4().hex
        now = utc_now()
        with self.connect() as conn:
            pos_row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS next FROM music_playlist_folders").fetchone()
            conn.execute(
                "INSERT INTO music_playlist_folders (id, name, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (folder_id, cleaned, int(pos_row["next"]), now, now),
            )
            row = conn.execute("SELECT * FROM music_playlist_folders WHERE id = ?", (folder_id,)).fetchone()
        return dict(row)

    def rename_playlist_folder(self, folder_id: str, name: str) -> dict[str, Any]:
        cleaned = str(name or "").strip()[:80]
        if not cleaned:
            raise ValueError("folder name is required")
        folder_id = str(folder_id or "").strip()
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE music_playlist_folders SET name = ?, updated_at = ? WHERE id = ?",
                (cleaned, utc_now(), folder_id),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError("folder not found")
            row = conn.execute("SELECT * FROM music_playlist_folders WHERE id = ?", (folder_id,)).fetchone()
        return dict(row)

    def delete_playlist_folder(self, folder_id: str) -> bool:
        folder_id = str(folder_id or "").strip()
        with self.connect() as conn:
            now = utc_now()
            conn.execute("UPDATE music_playlists SET folder_id = '', updated_at = ? WHERE folder_id = ?", (now, folder_id))
            cur = conn.execute("DELETE FROM music_playlist_folders WHERE id = ?", (folder_id,))
            return bool(cur.rowcount)

    def move_playlist_folder(self, folder_id: str, position: int) -> dict[str, Any]:
        folder_id = str(folder_id or "").strip()
        position = max(0, int(position or 0))
        with self.connect() as conn:
            rows = conn.execute("SELECT id, updated_at FROM music_playlist_folders ORDER BY position ASC, updated_at DESC").fetchall()
            ids = [str(row["id"]) for row in rows]
            updated_at = {str(row["id"]): row["updated_at"] for row in rows}
            if folder_id not in ids:
                raise KeyError("folder not found")
            ids.remove(folder_id)
            ids.insert(min(position, len(ids)), folder_id)
            now = utc_now()
            for idx, current_id in enumerate(ids):
                conn.execute(
                    "UPDATE music_playlist_folders SET position = ?, updated_at = ? WHERE id = ?",
                    (idx, now if current_id == folder_id else updated_at.get(current_id, now), current_id),
                )
            row = conn.execute("SELECT * FROM music_playlist_folders WHERE id = ?", (folder_id,)).fetchone()
        return dict(row)

    def update_playlist_meta(self, playlist_id: str, *, folder_id: str | None = None,
                             pinned: bool | None = None, position: int | None = None) -> dict[str, Any]:
        playlist_id = str(playlist_id or "").strip()
        updates: list[str] = []
        values: list[Any] = []
        if folder_id is not None:
            updates.append("folder_id = ?")
            values.append(str(folder_id or "").strip())
        if pinned is not None:
            updates.append("pinned = ?")
            values.append(1 if pinned else 0)
        if position is not None:
            updates.append("position = ?")
            values.append(max(0, int(position or 0)))
        if not updates:
            raise ValueError("no playlist metadata supplied")
        updates.append("updated_at = ?")
        values.append(utc_now())
        values.append(playlist_id)
        with self.connect() as conn:
            cur = conn.execute(f"UPDATE music_playlists SET {', '.join(updates)} WHERE id = ?", values)
            if int(cur.rowcount or 0) == 0:
                raise KeyError("playlist not found")
            row = conn.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM music_playlist_tracks t WHERE t.playlist_id = p.id) AS track_count
                FROM music_playlists p WHERE p.id = ?
                """,
                (playlist_id,),
            ).fetchone()
            return self._playlist_from_row(conn, row)

    def rename_playlist(self, playlist_id: str, name: str) -> dict[str, Any]:
        cleaned = str(name or "").strip()[:120]
        if not cleaned:
            raise ValueError("playlist name is required")
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE music_playlists SET name = ?, updated_at = ? WHERE id = ?",
                (cleaned, utc_now(), str(playlist_id or "").strip()),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError("playlist not found")
            row = conn.execute("SELECT * FROM music_playlists WHERE id = ?", (playlist_id,)).fetchone()
        return dict(row)

    def delete_playlist(self, playlist_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM music_playlists WHERE id = ?", (str(playlist_id or "").strip(),))
            return bool(cur.rowcount)

    # ── Tracks ───────────────────────────────────────────────────────────────

    def upsert_track(
        self,
        *,
        source: str = "youtube",
        source_id: str,
        title: str,
        artist: str = "",
        duration_s: float | None = None,
        thumbnail_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cleaned_source = str(source or "youtube").strip()[:40] or "youtube"
        cleaned_source_id = str(source_id or "").strip()[:200]
        cleaned_title = str(title or "").strip()[:400]
        if not cleaned_source_id:
            raise ValueError("track source_id is required")
        if not cleaned_title:
            raise ValueError("track title is required")
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM music_tracks WHERE source = ? AND source_id = ?",
                (cleaned_source, cleaned_source_id),
            ).fetchone()
            if existing is None:
                track_id = "trk_" + uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO music_tracks (id, source, source_id, title, artist, duration_s, thumbnail_url, metadata_json, added_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (track_id, cleaned_source, cleaned_source_id, cleaned_title, str(artist or "")[:200],
                     duration_s, str(thumbnail_url or "")[:600], dumps(metadata or {}), now),
                )
            else:
                track_id = existing["id"]
                conn.execute(
                    """
                    UPDATE music_tracks SET title = ?, artist = ?, duration_s = ?, thumbnail_url = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (cleaned_title, str(artist or "")[:200], duration_s, str(thumbnail_url or "")[:600],
                     dumps(metadata or {}), track_id),
                )
            row = conn.execute("SELECT * FROM music_tracks WHERE id = ?", (track_id,)).fetchone()
        return self._track_row(row)

    def add_track_to_playlist(self, playlist_id: str, track_id: str) -> None:
        playlist_id = str(playlist_id or "").strip()
        track_id = str(track_id or "").strip()
        if not playlist_id or not track_id:
            raise ValueError("playlist_id and track_id are required")
        now = utc_now()
        with self.connect() as conn:
            pos_row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM music_playlist_tracks WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT OR IGNORE INTO music_playlist_tracks (playlist_id, track_id, position, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (playlist_id, track_id, int(pos_row["next"]), now),
            )
            conn.execute("UPDATE music_playlists SET updated_at = ? WHERE id = ?", (now, playlist_id))

    def remove_track_from_playlist(self, playlist_id: str, track_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM music_playlist_tracks WHERE playlist_id = ? AND track_id = ?",
                (str(playlist_id or "").strip(), str(track_id or "").strip()),
            )
            return bool(cur.rowcount)

    def list_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*, pt.position AS position
                FROM music_playlist_tracks pt
                JOIN music_tracks t ON t.id = pt.track_id
                WHERE pt.playlist_id = ?
                ORDER BY pt.position ASC
                """,
                (str(playlist_id or "").strip(),),
            ).fetchall()
        return [self._track_row(row) for row in rows]

    # Smart playlists -----------------------------------------------------

    def sanitize_smart_rules(self, value: dict[str, Any] | None) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        match = "any" if source.get("match") == "any" else "all"
        allowed_fields = {
            "title", "artist", "album", "played_days", "not_played_days",
            "added_days", "duration_min", "duration_max", "has_album",
        }
        allowed_ops = {"contains", "equals", "starts", "is"}
        rules = []
        for raw in source.get("rules") or []:
            if not isinstance(raw, dict):
                continue
            field = str(raw.get("field") or "").strip()
            if field not in allowed_fields:
                continue
            op = str(raw.get("op") or "contains").strip()
            if op not in allowed_ops:
                op = "contains"
            val = raw.get("value")
            if field in {"played_days", "not_played_days", "added_days", "duration_min", "duration_max"}:
                try:
                    val = max(0, float(val))
                except Exception:
                    val = 0
            elif field == "has_album":
                val = bool(val)
                op = "is"
            else:
                val = str(val or "").strip()[:160]
            rules.append({"field": field, "op": op, "value": val})
            if len(rules) >= 8:
                break
        sort = str(source.get("sort") or "recent").strip()
        if sort not in {"recent", "added", "title", "artist", "duration"}:
            sort = "recent"
        try:
            limit = max(1, min(int(source.get("limit") or 50), 200))
        except Exception:
            limit = 50
        return {"match": match, "rules": rules, "sort": sort, "limit": limit}

    def create_smart_playlist(self, name: str, rules: dict[str, Any] | None) -> dict[str, Any]:
        return self.create_playlist(name, kind="smart", rules=rules)

    def update_smart_playlist(self, playlist_id: str, *, name: str | None = None,
                              rules: dict[str, Any] | None = None) -> dict[str, Any]:
        playlist_id = str(playlist_id or "").strip()
        updates: list[str] = []
        values: list[Any] = []
        if name is not None:
            cleaned = str(name or "").strip()[:120]
            if not cleaned:
                raise ValueError("playlist name is required")
            updates.append("name = ?")
            values.append(cleaned)
        if rules is not None:
            updates.append("rules_json = ?")
            values.append(dumps(self.sanitize_smart_rules(rules)))
        if not updates:
            raise ValueError("no smart playlist update supplied")
        updates.append("updated_at = ?")
        values.append(utc_now())
        values.append(playlist_id)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE music_playlists SET {', '.join(updates)} WHERE id = ? AND kind = 'smart'",
                values,
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError("smart playlist not found")
            row = conn.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM music_playlist_tracks t WHERE t.playlist_id = p.id) AS track_count
                FROM music_playlists p WHERE p.id = ?
                """,
                (playlist_id,),
            ).fetchone()
            return self._playlist_from_row(conn, row)

    def smart_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        playlist_id = str(playlist_id or "").strip()
        with self.connect() as conn:
            row = conn.execute("SELECT rules_json FROM music_playlists WHERE id = ? AND kind = 'smart'", (playlist_id,)).fetchone()
            if row is None:
                raise KeyError("smart playlist not found")
            rules = self.sanitize_smart_rules(loads_object(row["rules_json"]))
            return self._smart_playlist_tracks(conn, rules)

    def _smart_playlist_tracks(self, conn: sqlite3.Connection, rules: dict[str, Any],
                               *, count_only: bool = False) -> list[dict[str, Any]]:
        rules = self.sanitize_smart_rules(rules)
        rows = conn.execute(
            """
            SELECT t.*, MAX(h.played_at) AS last_played_at, MAX(h.rowid) AS _last
            FROM music_tracks t
            LEFT JOIN music_play_history h ON h.track_id = t.id
            GROUP BY t.id
            """
        ).fetchall()
        tracks = [self._track_row(row) for row in rows]
        matched = [track for track in tracks if self._track_matches_rules(track, rules)]
        sort = rules.get("sort")
        if sort == "title":
            matched.sort(key=lambda t: str(t.get("title") or "").lower())
        elif sort == "artist":
            matched.sort(key=lambda t: (str(t.get("artist") or "").lower(), str(t.get("title") or "").lower()))
        elif sort == "duration":
            matched.sort(key=lambda t: float(t.get("duration_s") or 0), reverse=True)
        elif sort == "added":
            matched.sort(key=lambda t: str(t.get("added_at") or ""), reverse=True)
        else:
            matched.sort(key=lambda t: (str(t.get("last_played_at") or ""), str(t.get("added_at") or "")), reverse=True)
        limit = int(rules.get("limit") or 50)
        return matched[:limit] if not count_only else matched

    def _track_matches_rules(self, track: dict[str, Any], rules: dict[str, Any]) -> bool:
        tests = [self._track_matches_rule(track, rule) for rule in rules.get("rules") or []]
        if not tests:
            return True
        return any(tests) if rules.get("match") == "any" else all(tests)

    def _track_matches_rule(self, track: dict[str, Any], rule: dict[str, Any]) -> bool:
        field = rule.get("field")
        op = rule.get("op")
        value = rule.get("value")
        metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
        album_meta = metadata.get("album") if isinstance(metadata.get("album"), dict) else {}
        album = str(metadata.get("album_name") or album_meta.get("name") or "").strip()
        if field in {"title", "artist", "album"}:
            hay = str(track.get(field) if field != "album" else album).casefold()
            needle = str(value or "").casefold()
            if op == "equals":
                return hay == needle
            if op == "starts":
                return hay.startswith(needle)
            return needle in hay
        if field == "has_album":
            return bool(album) is bool(value)
        if field in {"played_days", "not_played_days", "added_days"}:
            days = float(value or 0)
            now = datetime.now(timezone.utc)
            if field == "added_days":
                added = parse_utc(track.get("added_at"))
                return bool(added and added >= now - timedelta(days=days))
            played = parse_utc(track.get("last_played_at"))
            if field == "played_days":
                return bool(played and played >= now - timedelta(days=days))
            return not played or played < now - timedelta(days=days)
        duration = float(track.get("duration_s") or 0)
        if field == "duration_min":
            return duration >= float(value or 0) * 60
        if field == "duration_max":
            return duration <= float(value or 0) * 60
        return False

    # ── Play history + library index ─────────────────────────────────────────

    def log_play(self, track_id: str) -> None:
        track_id = str(track_id or "").strip()
        if not track_id:
            return
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO music_play_history (id, track_id, played_at) VALUES (?, ?, ?)",
                ("play_" + uuid.uuid4().hex, track_id, utc_now()),
            )

    def list_recent_plays(self, *, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 30), 200))
        with self.connect() as conn:
            # Order by MAX(rowid): played_at is second-resolution, so rapid
            # plays collide — rowid is the reliable insertion-order tiebreaker.
            rows = conn.execute(
                """
                SELECT t.*, MAX(h.played_at) AS played_at, MAX(h.rowid) AS _last
                FROM music_play_history h
                JOIN music_tracks t ON t.id = h.track_id
                GROUP BY h.track_id
                ORDER BY _last DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._track_row(row) for row in rows]

    #: Categories the "Clear local data" panel can erase. Playlists cascade to
    #: their tracks and folders, so those are not separately selectable.
    CLEARABLE_CATEGORIES = ("recents", "following", "playlists", "queues")

    def clear_user_data(self, categories: Any) -> dict[str, Any]:
        """Erase whole categories of user data in one transaction.

        Returns the row counts removed plus the playlist artwork keys left
        orphaned; deleting those files is the caller's job, since artwork path
        policy lives in music_bridge alongside the other artwork handling.
        """
        wanted = [c for c in dict.fromkeys(str(c or "").strip() for c in (categories or []))
                  if c in self.CLEARABLE_CATEGORIES]
        if not wanted:
            return {"cleared": [], "counts": {}, "artwork_keys": []}
        counts: dict[str, int] = {}
        artwork_keys: list[str] = []
        with self.connect() as conn:
            if "playlists" in wanted:
                artwork_keys = [
                    str(row["artwork_key"])
                    for row in conn.execute(
                        "SELECT artwork_key FROM music_playlists WHERE artwork_key != ''"
                    ).fetchall()
                ]
            for category in wanted:
                if category == "recents":
                    counts[category] = conn.execute("DELETE FROM music_play_history").rowcount
                elif category == "following":
                    counts[category] = conn.execute("DELETE FROM music_followed_artists").rowcount
                elif category == "queues":
                    counts[category] = conn.execute("DELETE FROM music_queue_sessions").rowcount
                elif category == "playlists":
                    conn.execute("DELETE FROM music_playlist_tracks")
                    conn.execute("DELETE FROM music_playlist_folders")
                    counts[category] = conn.execute("DELETE FROM music_playlists").rowcount
            # music_tracks is a cache reachable only via playlists or history. Once
            # those references are gone the rows are invisible in the UI but still
            # record what was listened to, so prune them rather than leave a
            # "cleared" library quietly holding the user's listening behind it.
            counts["tracks_pruned"] = conn.execute(
                """
                DELETE FROM music_tracks
                WHERE id NOT IN (SELECT track_id FROM music_playlist_tracks)
                  AND id NOT IN (SELECT track_id FROM music_play_history)
                """
            ).rowcount
        return {"cleared": wanted, "counts": counts, "artwork_keys": artwork_keys}

    def delete_play_history(self, track_id: str) -> int:
        """Forget every play of one track.

        list_recent_plays groups history by track and returns the track row, so a
        Recents entry has no per-play id to delete - "remove this from Recents"
        can only mean forgetting that track's plays. That also drops it out of
        Insights and the top-artist tallies, which is what makes the surfaces agree.
        """
        track_id = str(track_id or "").strip()
        if not track_id:
            return 0
        with self.connect() as conn:
            return conn.execute("DELETE FROM music_play_history WHERE track_id = ?", (track_id,)).rowcount

    def clear_play_history(self) -> int:
        with self.connect() as conn:
            return conn.execute("DELETE FROM music_play_history").rowcount

    def delete_artist_play_history(self, artist_key: str) -> int:
        """Forget every play by one artist, keyed the same way as list_top_artists."""
        artist_key = str(artist_key or "").strip().lower()
        if not artist_key:
            return 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM music_play_history h
                JOIN music_tracks t ON t.id = h.track_id
                GROUP BY t.id
                """
            ).fetchall()
            targets = [
                track["id"] for track in (self._track_row(row) for row in rows)
                if self._track_artist_key(track) == artist_key
            ]
            if not targets:
                return 0
            placeholders = ",".join("?" * len(targets))
            return conn.execute(
                f"DELETE FROM music_play_history WHERE track_id IN ({placeholders})", targets
            ).rowcount

    def list_top_artists(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """Aggregates total plays per artist from music_play_history, using the
        same artist-identity resolution as music_library_index (artist_id or
        lowercased artist_name) so results agree with the Artists tab."""
        limit = max(1, min(int(limit or 8), 50))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*
                FROM music_play_history h
                JOIN music_tracks t ON t.id = h.track_id
                """
            ).fetchall()
        artists: dict[str, dict[str, Any]] = {}
        for row in rows:
            track = self._track_row(row)
            metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
            artist_name = str(track.get("artist") or "").strip() or "Unknown artist"
            artist_id = str(metadata.get("artist_id") or "").strip()
            artist_key = self._track_artist_key(track)
            artist = artists.setdefault(artist_key, {
                "id": artist_id,
                "name": artist_name,
                "artist_key": artist_key,
                "play_count": 0,
                "thumbnail_url": track.get("thumbnail_url") or "",
            })
            artist["play_count"] += 1
            if not artist.get("thumbnail_url") and track.get("thumbnail_url"):
                artist["thumbnail_url"] = track.get("thumbnail_url")
        ranked = sorted(artists.values(), key=lambda a: (-int(a.get("play_count") or 0), str(a.get("name") or "").lower()))
        return ranked[:limit]

    def listening_insights(self, *, days: int = 7) -> dict[str, Any]:
        """Aggregate play history into the Insights payload.

        ``days`` bounds the window (0 = all time). Buckets use the local
        timezone so "today" matches the user's clock, and the chart adapts its
        unit to the range (day / week / month) via plan_insight_buckets so a
        legible handful of bars always fits. For a bounded range the totals
        window is aligned to the first bucket's start, so the summary strip and
        the chart agree exactly (sum of bar counts == total_plays).
        """
        try:
            days = max(0, min(int(days or 0), 365))
        except Exception:
            days = 7
        now_local = datetime.now(timezone.utc).astimezone()
        today = now_local.date()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT h.played_at AS played_at, t.*
                FROM music_play_history h
                JOIN music_tracks t ON t.id = h.track_id
                ORDER BY h.rowid DESC
                """
            ).fetchall()

        # Pre-parse plays so the bucket planner can see the earliest play (which
        # bounds the all-time monthly chart) before aggregation runs.
        plays: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            track = self._track_row(row)
            played_at = parse_utc(track.pop("played_at", None))
            if played_at is None:
                continue
            plays.append((played_at, track))

        earliest_local = min((played_at.astimezone().date() for played_at, _ in plays), default=today)
        bucket_unit, bucket_spans = plan_insight_buckets(today, days, earliest_local)
        bucket_index = {span["key"]: i for i, span in enumerate(bucket_spans)}
        bucket_counts = [0] * len(bucket_spans)

        # A bounded range counts only plays inside the chart's span, so the
        # totals and the bars line up. All-time (cutoff None) counts every play
        # into the totals while the chart shows just the last 12 months, so the
        # strip can legitimately exceed the visible bars on long histories.
        cutoff = None
        if days and bucket_spans:
            first = bucket_spans[0]["start"]
            cutoff = datetime(first.year, first.month, first.day,
                              tzinfo=now_local.tzinfo).astimezone(timezone.utc)

        total_plays = 0
        total_seconds = 0.0
        track_plays: dict[str, dict[str, Any]] = {}
        artist_plays: dict[str, dict[str, Any]] = {}
        for played_at, track in plays:
            if cutoff is not None and played_at < cutoff:
                continue
            total_plays += 1
            duration = track.get("duration_s")
            if isinstance(duration, (int, float)) and duration > 0:
                total_seconds += float(duration)
            slot = bucket_index.get(_bucket_key(bucket_unit, played_at.astimezone().date()))
            if slot is not None:
                bucket_counts[slot] += 1

            track_key = f"{track.get('source') or 'youtube'}:{track.get('source_id') or ''}"
            entry = track_plays.setdefault(track_key, {**track, "play_count": 0,
                                                        "last_played_at": played_at.isoformat(timespec="seconds")})
            entry["play_count"] += 1

            metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
            artist_name = str(track.get("artist") or "").strip() or "Unknown artist"
            artist_id = str(metadata.get("artist_id") or "").strip()
            artist_key = self._track_artist_key(track)
            artist = artist_plays.setdefault(artist_key, {
                "id": artist_id,
                "name": artist_name,
                "artist_key": artist_key,
                "play_count": 0,
                "thumbnail_url": track.get("thumbnail_url") or "",
            })
            artist["play_count"] += 1
            if not artist.get("thumbnail_url") and track.get("thumbnail_url"):
                artist["thumbnail_url"] = track.get("thumbnail_url")

        top_tracks = sorted(track_plays.values(),
                            key=lambda t: (-int(t.get("play_count") or 0), str(t.get("title") or "").lower()))[:8]
        top_artists = sorted(artist_plays.values(),
                             key=lambda a: (-int(a.get("play_count") or 0), str(a.get("name") or "").lower()))[:8]
        return {
            "window_days": days,
            "total_plays": total_plays,
            "total_minutes": int(total_seconds // 60),
            "unique_tracks": len(track_plays),
            "unique_artists": len(artist_plays),
            "bucket_unit": bucket_unit,
            "buckets": [{"start": span["start"].isoformat(), "end": span["end"].isoformat(),
                         "count": bucket_counts[i]} for i, span in enumerate(bucket_spans)],
            "top_tracks": top_tracks,
            "top_artists": top_artists,
        }

    def list_music_tracks(self, *, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 500), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*, MAX(h.played_at) AS last_played_at, MAX(h.rowid) AS _last
                FROM music_tracks t
                LEFT JOIN music_play_history h ON h.track_id = t.id
                GROUP BY t.id
                ORDER BY t.added_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._track_row(row) for row in rows]

    def get_track(self, track_id: str) -> dict[str, Any] | None:
        """One track by primary key, or None.

        The audio route needs this: a local grant names a ``track_id`` and
        nothing else, so the path on disk is looked up here rather than carried
        in the capability the phone holds.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM music_tracks WHERE id = ?", (str(track_id or "").strip(),)
            ).fetchone()
        return self._track_row(row) if row is not None else None

    # Queue sessions ------------------------------------------------------

    def _queue_session_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["tracks"] = loads_list(item.pop("tracks_json", "[]"))
        item["index"] = int(item.pop("position", 0) or 0)
        item["is_last"] = bool(item.get("is_last"))
        item["track_count"] = len(item["tracks"])
        return item

    def save_queue_session(self, *, name: str, tracks: list[dict[str, Any]], index: int = 0,
                           is_last: bool = False, session_id: str | None = None) -> dict[str, Any]:
        cleaned = str(name or "").strip()[:120] or ("Last session" if is_last else "Saved queue")
        clean_tracks = [t for t in tracks if isinstance(t, dict) and str(t.get("source_id") or "").strip()]
        session_id = "qs_last" if is_last else (str(session_id or "").strip() or "qs_" + uuid.uuid4().hex)
        now = utc_now()
        index = max(0, min(int(index or 0), max(0, len(clean_tracks) - 1)))
        with self.connect() as conn:
            if is_last:
                conn.execute("UPDATE music_queue_sessions SET is_last = 0 WHERE is_last = 1 AND id <> ?", (session_id,))
            existing = conn.execute("SELECT created_at FROM music_queue_sessions WHERE id = ?", (session_id,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE music_queue_sessions
                    SET name = ?, tracks_json = ?, position = ?, is_last = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (cleaned, dumps(clean_tracks), index, 1 if is_last else 0, now, session_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO music_queue_sessions
                        (id, name, tracks_json, position, is_last, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, cleaned, dumps(clean_tracks), index, 1 if is_last else 0, now, now),
                )
            row = conn.execute("SELECT * FROM music_queue_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._queue_session_row(row)

    def list_queue_sessions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM music_queue_sessions
                ORDER BY is_last DESC, updated_at DESC
                """
            ).fetchall()
        return [self._queue_session_row(row) for row in rows]

    def delete_queue_session(self, session_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM music_queue_sessions WHERE id = ?", (str(session_id or "").strip(),))
            return bool(cur.rowcount)

    # Followed artists ---------------------------------------------------

    @staticmethod
    def _artist_key(artist_id: str = "", name: str = "") -> str:
        cleaned_id = str(artist_id or "").strip()
        if cleaned_id:
            return "id:" + cleaned_id.casefold()
        cleaned_name = " ".join(str(name or "").split())
        if cleaned_name:
            return "name:" + cleaned_name.casefold()
        raise ValueError("artist id or name is required")

    def follow_artist(self, *, artist_id: str = "", name: str = "", thumbnail_url: str = "") -> dict[str, Any]:
        cleaned_id = str(artist_id or "").strip()[:240]
        cleaned_name = " ".join(str(name or "").split())[:240]
        if not cleaned_name:
            cleaned_name = cleaned_id
        key = self._artist_key(cleaned_id, cleaned_name)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO music_followed_artists (artist_key, artist_id, name, thumbnail_url, followed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(artist_key) DO UPDATE SET
                    artist_id = excluded.artist_id,
                    name = excluded.name,
                    thumbnail_url = CASE WHEN excluded.thumbnail_url <> '' THEN excluded.thumbnail_url ELSE music_followed_artists.thumbnail_url END
                """,
                (key, cleaned_id, cleaned_name, str(thumbnail_url or "").strip()[:800], now),
            )
            row = conn.execute("SELECT * FROM music_followed_artists WHERE artist_key = ?", (key,)).fetchone()
        return dict(row)

    def unfollow_artist(self, *, artist_id: str = "", name: str = "") -> bool:
        key = self._artist_key(artist_id, name)
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM music_followed_artists WHERE artist_key = ?", (key,))
            return bool(cur.rowcount)

    def list_followed_artists(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM music_followed_artists ORDER BY followed_at DESC, artist_key ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    # ── Playback target, devices, per-device settings ────────────────────────
    #
    # Which surface owns the audio right now. Before this, nothing recorded it:
    # the desktop kept a write-only field no event ever updated, the phone kept
    # a boolean, and the wire carried a two-value string stamped "desktop" by
    # default. That is why pause did not cross devices and why every screen
    # claimed the computer was playing regardless of what was true.

    PLAYBACK_TARGET_KEY = "playback_target"

    _DEFAULT_TARGET = {
        "owner_kind": "desktop",
        "owner_device_id": "desktop",
        "owner_name": "",
        "sink_id": "",
        "sink_name": "",
        "since_ms": 0,
        "reason": "restore",
    }

    def get_playback_target(self) -> dict[str, Any]:
        """The current owner of playback, rehydrated across restarts."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_json, revision FROM music_kv WHERE key = ?",
                (self.PLAYBACK_TARGET_KEY,),
            ).fetchone()
        if row is None:
            return {**self._DEFAULT_TARGET, "revision": 0}
        target = {**self._DEFAULT_TARGET, **loads_object(row["value_json"])}
        target["revision"] = int(row["revision"] or 0)
        return target

    def set_playback_target(self, patch: dict[str, Any], *,
                            expected_revision: int | None = None) -> dict[str, Any]:
        """Move ownership. The single writer, so two phones claiming at once
        resolve to one winner rather than to a torn record.

        `expected_revision` lets a caller refuse to overwrite a decision it has
        not seen; a mismatch raises rather than silently clobbering."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value_json, revision FROM music_kv WHERE key = ?",
                (self.PLAYBACK_TARGET_KEY,),
            ).fetchone()
            current = {**self._DEFAULT_TARGET, **(loads_object(row["value_json"]) if row else {})}
            revision = int(row["revision"] or 0) if row else 0
            if expected_revision is not None and expected_revision != revision:
                raise ValueError("playback target changed since it was read")

            merged = {**current, **{k: v for k, v in patch.items() if k != "revision"}}
            owner_changed = (
                merged.get("owner_device_id") != current.get("owner_device_id")
                or merged.get("owner_kind") != current.get("owner_kind")
            )
            # Ownership start, not "last touched": changing which speaker the
            # desktop uses is not a handoff and must not reset it.
            if owner_changed or not current.get("since_ms"):
                merged["since_ms"] = int(time.time() * 1000)

            revision += 1
            conn.execute(
                "INSERT INTO music_kv (key, value_json, revision, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                "revision = excluded.revision, updated_at = excluded.updated_at",
                (self.PLAYBACK_TARGET_KEY, json.dumps(merged), revision, utc_now()),
            )
        return {**merged, "revision": revision}

    def upsert_device(self, device_id: str, *, name: str = "", kind: str = "phone") -> None:
        """Record a phone, and stamp that we just heard from it."""
        device_id = str(device_id or "").strip()
        if not device_id:
            return
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO music_devices (device_id, name, kind, last_seen_at, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
                "name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE music_devices.name END",
                (device_id, str(name or ""), str(kind or "phone"), now, now),
            )

    def set_device_follow(self, device_id: str, follows: bool) -> None:
        self.upsert_device(device_id)
        with self.connect() as conn:
            conn.execute(
                "UPDATE music_devices SET follows_desktop = ? WHERE device_id = ?",
                (1 if follows else 0, str(device_id)),
            )

    def list_devices(self) -> list[dict[str, Any]]:
        """Known phones, most recently seen first.

        Used to turn an owner's device id into the name a person recognises,
        so "playing on" can say "Lennon's iPhone" instead of a hex string."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT device_id, name, kind, follows_desktop, last_seen_at "
                "FROM music_devices ORDER BY last_seen_at DESC"
            ).fetchall()
        return [
            {"device_id": row["device_id"], "name": row["name"], "kind": row["kind"],
             "follows_desktop": bool(row["follows_desktop"]), "last_seen_at": row["last_seen_at"]}
            for row in rows
        ]

    def device_follow(self, device_id: str) -> bool:
        """Whether this phone asked to mirror the computer.

        Read back on log creation, which is what makes evicting an idle device
        log cost a resync rather than silently unlinking a phone."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT follows_desktop FROM music_devices WHERE device_id = ?",
                (str(device_id),),
            ).fetchone()
        return bool(row and row["follows_desktop"])

    def merge_device_settings(self, device_id: str, entries: list[dict[str, Any]], *,
                              origin: str = "phone") -> list[dict[str, Any]]:
        """Last-write-wins per key, never per blob.

        Prefs travel as one JSON object, so a blob revision would force
        discarding one side whenever two devices edited different keys. Per-key
        stamps make the merge commutative and both edits survive.

        This computer's clock arbitrates: a stamp that is absent or implausibly
        far in the future is replaced, so a phone with a wrong date cannot pin a
        key forever."""
        self.upsert_device(device_id)
        now_ms = int(time.time() * 1000)
        horizon = now_ms + 300_000
        with self.connect() as conn:
            for entry in entries or []:
                key = str(entry.get("key") or "").strip()
                if not key:
                    continue
                stamp = int(entry.get("updated_ms") or 0)
                if stamp <= 0 or stamp > horizon:
                    stamp = now_ms
                conn.execute(
                    "INSERT INTO music_device_settings (device_id, key, value_json, updated_ms, origin) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(device_id, key) DO UPDATE SET "
                    "value_json = excluded.value_json, updated_ms = excluded.updated_ms, "
                    "origin = excluded.origin "
                    "WHERE excluded.updated_ms >= music_device_settings.updated_ms",
                    (str(device_id), key, json.dumps(entry.get("value")), stamp, str(origin)),
                )
        return self.read_device_settings(device_id)

    def read_device_settings(self, device_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value_json, updated_ms FROM music_device_settings "
                "WHERE device_id = ? ORDER BY key",
                (str(device_id),),
            ).fetchall()
        return [
            {"key": row["key"], "value": loads_value(row["value_json"]),
             "updated_ms": int(row["updated_ms"] or 0)}
            for row in rows
        ]

    # ── Local file library ───────────────────────────────────────────────────

    @staticmethod
    def _norm_mtime(value: Any) -> float:
        """Modification times, rounded so two readings of one file agree.

        Move repair matches on ``(size, mtime, basename)``, which only works if
        the stored number is reproducible. Filesystems disagree about how many
        digits of sub-second precision they keep, so everything is pinned to
        milliseconds on the way in and on the way back out.
        """
        try:
            return round(float(value or 0.0), 3)
        except (TypeError, ValueError):
            return 0.0

    def get_local_track(self, *, track_id: str = "", source_id: str = "") -> dict[str, Any] | None:
        """A local row by primary key or by its opaque source id.

        Both lookups are index-backed — the PK and the existing
        ``UNIQUE(source, source_id)`` — so this stays constant time on a library
        of any size.
        """
        track_id = str(track_id or "").strip()
        source_id = str(source_id or "").strip()
        with self.connect() as conn:
            row = None
            if track_id:
                row = conn.execute(
                    "SELECT * FROM music_tracks WHERE id = ? AND source = ?",
                    (track_id, LOCAL_SOURCE),
                ).fetchone()
            if row is None and source_id:
                row = conn.execute(
                    "SELECT * FROM music_tracks WHERE source = ? AND source_id = ?",
                    (LOCAL_SOURCE, source_id),
                ).fetchone()
        return self._track_row(row) if row is not None else None

    def list_local_roots(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM music_local_roots ORDER BY path ASC").fetchall()
        return [dict(row) for row in rows]

    def add_local_root(self, path: str) -> dict[str, Any]:
        cleaned = str(path or "").strip()
        if not cleaned:
            raise ValueError("a folder is required")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO music_local_roots (path, added_at) VALUES (?, ?) "
                "ON CONFLICT(path) DO NOTHING",
                (cleaned, utc_now()),
            )
            row = conn.execute("SELECT * FROM music_local_roots WHERE path = ?", (cleaned,)).fetchone()
        return dict(row)

    def remove_local_root(self, path: str) -> bool:
        """Stop watching a folder.

        Tracks scanned out of it are deliberately left in place. Forgetting
        where music came from is not the same as deciding it never existed, and
        a playlist must not lose entries because somebody tidied a preferences
        list.
        """
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM music_local_roots WHERE path = ?", (str(path or "").strip(),)
            )
            return bool(cur.rowcount)

    def record_local_root_scan(self, path: str, *, last_error: str = "",
                               track_count: int = 0) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE music_local_roots SET last_scan_at = ?, last_error = ?, track_count = ? "
                "WHERE path = ?",
                (utc_now(), str(last_error or "")[:400], max(0, int(track_count or 0)),
                 str(path or "").strip()),
            )

    def upsert_local_track(
        self,
        *,
        file_path: str,
        file_size: int,
        file_mtime: float,
        content_type: str = "",
        title: str,
        artist: str = "",
        album: str = "",
        duration_s: float | None = None,
        thumbnail_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one file, reusing the row it already has wherever possible.

        ``source_id`` is a fresh uuid assigned the first time a file is seen and
        never derived from anything about the file. That is the whole point:
        a path-derived id duplicates the library the moment somebody
        reorganises a music folder, and a ``name|size|mtime`` id (which is what
        the phone uses, ``pwa/src/local.js:179``) duplicates it on every retag.
        Playlists reference ``music_tracks.id``, so either would quietly empty
        them.

        Matching therefore happens explicitly, in two steps:

        1. by ``file_path`` — the ordinary case, including a retag, which
           changes the tags and the mtime but not where the file is;
        2. failing that, by ``(file_size, file_mtime, basename)`` against rows
           already marked missing — a *move repair* that rewrites ``file_path``
           in place, so a reorganised folder costs nothing.

        Returns the row with an extra ``local_action`` of ``added`` / ``moved``
        / ``updated`` so a scan can report what it actually did.
        """
        cleaned_path = str(file_path or "").strip()
        if not cleaned_path:
            raise ValueError("file_path is required")
        cleaned_title = str(title or "").strip()[:400]
        if not cleaned_title:
            raise ValueError("track title is required")
        size = max(0, int(file_size or 0))
        mtime = self._norm_mtime(file_mtime)
        basename = os.path.basename(cleaned_path)
        payload = dict(metadata or {})
        if album:
            payload.setdefault("album_name", album)
            payload.setdefault("album", {"name": album})
        payload.setdefault("file_name", basename)
        now = utc_now()

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM music_tracks WHERE source = ? AND file_path = ?",
                (LOCAL_SOURCE, cleaned_path),
            ).fetchone()
            action = "updated"
            if existing is None:
                # Move repair. Restricted to rows a scan has already given up
                # on, so a plain copy of a file that is still where it was does
                # not steal the original's identity.
                candidates = conn.execute(
                    "SELECT * FROM music_tracks WHERE source = ? AND missing_since <> '' "
                    "AND file_size = ? AND file_mtime = ?",
                    (LOCAL_SOURCE, size, mtime),
                ).fetchall()
                existing = next(
                    (row for row in candidates
                     if os.path.normcase(os.path.basename(str(row["file_path"] or "")))
                     == os.path.normcase(basename)),
                    None,
                )
                if existing is not None:
                    action = "moved"

            if existing is None:
                action = "added"
                track_id = "trk_" + uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO music_tracks
                        (id, source, source_id, title, artist, duration_s, thumbnail_url,
                         metadata_json, added_at, file_path, file_size, file_mtime,
                         content_type, missing_since)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (track_id, LOCAL_SOURCE, uuid.uuid4().hex, cleaned_title,
                     str(artist or "")[:200], duration_s, str(thumbnail_url or "")[:600],
                     dumps(payload), now, cleaned_path, size, mtime, str(content_type or "")[:80]),
                )
            else:
                track_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE music_tracks SET title = ?, artist = ?, duration_s = ?,
                        thumbnail_url = ?, metadata_json = ?, file_path = ?, file_size = ?,
                        file_mtime = ?, content_type = ?, missing_since = ''
                    WHERE id = ?
                    """,
                    (cleaned_title, str(artist or "")[:200], duration_s,
                     str(thumbnail_url or "")[:600], dumps(payload), cleaned_path, size,
                     mtime, str(content_type or "")[:80], track_id),
                )
            row = conn.execute("SELECT * FROM music_tracks WHERE id = ?", (track_id,)).fetchone()
        item = self._track_row(row)
        item["local_action"] = action
        return item

    def unchanged_local_paths(self, root: str, stats: dict[str, tuple[int, float]]) -> set[str]:
        """Which of these files the library already has, byte-identical.

        One query for a whole root rather than one per file, and the comparison
        happens here because this is where the rounding rule for ``file_mtime``
        lives — a second copy of it somewhere else is a bug waiting for a
        filesystem with different precision.

        A row already marked missing is never reported as unchanged: its file
        has come back, and that has to travel through the upsert so the mark
        gets cleared.
        """
        # normcase'd throughout: a no-op on POSIX, but on Windows this is what
        # keeps a rescan from re-reading every file just because a path came
        # back cased differently than the one already stored for it (a mapped
        # drive, a reparse point, or simply Windows' own realpath). normcase()
        # runs *before* the trailing separator is stripped, not after: a root
        # recorded with a forward slash (os.sep is "\\" on Windows) would
        # otherwise dodge the rstrip, and normcase folding it to "\\" afterwards
        # leaves a doubled separator that no stored path can ever start with --
        # silently defeating this whole rescan optimisation for that root.
        prefix = os.path.normcase(str(root or "")).rstrip(os.sep) + os.sep
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT file_path, file_size, file_mtime FROM music_tracks "
                "WHERE source = ? AND missing_since = '' AND file_path <> ''",
                (LOCAL_SOURCE,),
            ).fetchall()
        known = {
            os.path.normcase(str(row["file_path"])):
                (int(row["file_size"] or 0), self._norm_mtime(row["file_mtime"]))
            for row in rows
            if os.path.normcase(str(row["file_path"])).startswith(prefix)
        }
        return {
            path for path, (size, mtime) in (stats or {}).items()
            if known.get(os.path.normcase(path)) == (int(size), self._norm_mtime(mtime))
        }

    def mark_local_tracks_missing(self, root: str, seen_paths: Any) -> int:
        """Stamp every local track under ``root`` this scan did not encounter.

        Deliberately a stamp and not a delete. The commonest reason a file
        stops appearing is that its drive is not plugged in, and losing a
        playlist to that would be an unforgivable trade for a tidier table.
        """
        cleaned_root = str(root or "").rstrip(os.sep)
        if not cleaned_root:
            return 0
        # normcase'd for the same reason as unchanged_local_paths above: on
        # Windows a path that is genuinely still there must not be mistaken
        # for missing merely because of a case difference. And, as there,
        # normcase() runs before the trailing separator is stripped and
        # re-added -- doing it in the other order leaves a doubled separator
        # whenever the stored root used "/" instead of "\\", and nothing
        # under that root would ever be marked missing again.
        prefix = os.path.normcase(str(root or "")).rstrip(os.sep) + os.sep
        seen = {os.path.normcase(str(path)) for path in (seen_paths or ())}
        stamp = utc_now()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, file_path FROM music_tracks "
                "WHERE source = ? AND missing_since = '' AND file_path <> ''",
                (LOCAL_SOURCE,),
            ).fetchall()
            gone = [
                (stamp, str(row["id"])) for row in rows
                if os.path.normcase(str(row["file_path"])).startswith(prefix)
                and os.path.normcase(str(row["file_path"])) not in seen
            ]
            if gone:
                conn.executemany(
                    "UPDATE music_tracks SET missing_since = ? WHERE id = ?", gone
                )
        return len(gone)

    def mark_track_missing(self, track_id: str) -> bool:
        """Note that a file was not there when somebody tried to play it.

        The audio route calls this on a 404 so the library stops claiming a
        track is available before the next scan gets round to noticing.
        """
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE music_tracks SET missing_since = ? "
                "WHERE id = ? AND source = ? AND missing_since = ''",
                (utc_now(), str(track_id or "").strip(), LOCAL_SOURCE),
            )
            return bool(cur.rowcount)

    def local_library_status(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN missing_since <> '' THEN 1 ELSE 0 END), 0) AS missing, "
                "COALESCE(SUM(file_size), 0) AS bytes "
                "FROM music_tracks WHERE source = ?",
                (LOCAL_SOURCE,),
            ).fetchone()
        return {
            "roots": self.list_local_roots(),
            "tracks": int(row["total"] or 0),
            "missing": int(row["missing"] or 0),
            "bytes": int(row["bytes"] or 0),
        }

    def set_device_codecs(self, device_id: str, support: Any) -> dict[str, bool]:
        """Remember what one phone said it can decode."""
        cleaned = {
            str(key).strip().lower()[:80]: bool(value)
            for key, value in (support or {}).items()
            if str(key or "").strip()
        }
        device_id = str(device_id or "").strip()
        if not device_id:
            return {}
        self.upsert_device(device_id)
        with self.connect() as conn:
            conn.execute(
                "UPDATE music_devices SET codec_support_json = ? WHERE device_id = ?",
                (dumps(cleaned), device_id),
            )
        return cleaned

    def device_codecs(self, device_id: str) -> dict[str, bool]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT codec_support_json FROM music_devices WHERE device_id = ?",
                (str(device_id or "").strip(),),
            ).fetchone()
        stored = loads_object(row["codec_support_json"]) if row is not None else {}
        return {str(key): bool(value) for key, value in stored.items()}

    def music_library_index(self, *, limit: int = 500, device_id: str = "") -> dict[str, Any]:
        tracks = self.list_music_tracks(limit=limit)
        # iOS Safari plays FLAC in an <audio> element but not Ogg or Opus, and a
        # library the computer can play perfectly is not the same library the
        # phone can. Marking is per requesting device, so the mark travels with
        # the id of the device it describes; a phone receiving another device's
        # fan-out can tell that these marks are not about it.
        codecs = self.device_codecs(device_id) if device_id else {}
        if codecs:
            for track in tracks:
                if str(track.get("source") or "") != LOCAL_SOURCE:
                    continue
                content_type = str(track.get("content_type") or "").strip().lower()
                if not content_type or codecs.get(content_type, True):
                    continue
                track["playable_on_device"] = False
                track["unplayable_reason"] = "This computer can play it; your phone can't."
        artists: dict[str, dict[str, Any]] = {}
        albums: dict[str, dict[str, Any]] = {}
        for track in tracks:
            metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
            artist_name = str(track.get("artist") or "").strip() or "Unknown artist"
            artist_id = str(metadata.get("artist_id") or "").strip()
            artist_key = (artist_id or artist_name).lower()
            artist = artists.setdefault(artist_key, {
                "id": artist_id,
                "name": artist_name,
                "track_count": 0,
                "thumbnail_url": track.get("thumbnail_url") or "",
                "added_at": track.get("added_at") or "",
                "last_played_at": track.get("last_played_at") or "",
            })
            artist["track_count"] += 1
            if not artist.get("thumbnail_url") and track.get("thumbnail_url"):
                artist["thumbnail_url"] = track.get("thumbnail_url")
            if str(track.get("added_at") or "") > str(artist.get("added_at") or ""):
                artist["added_at"] = track.get("added_at") or ""
            if str(track.get("last_played_at") or "") > str(artist.get("last_played_at") or ""):
                artist["last_played_at"] = track.get("last_played_at") or ""

            album_meta = metadata.get("album") if isinstance(metadata.get("album"), dict) else {}
            album_name = str(metadata.get("album_name") or album_meta.get("name") or "").strip()
            album_id = str(metadata.get("album_id") or album_meta.get("id") or "").strip()
            if album_name or album_id:
                album_key = (album_id or f"{artist_name}:{album_name}").lower()
                album = albums.setdefault(album_key, {
                    "id": album_id,
                    "title": album_name or "Unknown album",
                    "artist": artist_name,
                    "artist_id": artist_id,
                    "track_count": 0,
                    "thumbnail_url": track.get("thumbnail_url") or "",
                    "tracks": [],
                    "added_at": track.get("added_at") or "",
                    "last_played_at": track.get("last_played_at") or "",
                })
                album["track_count"] += 1
                if len(album["tracks"]) < 50:
                    album["tracks"].append(track)
                if not album.get("thumbnail_url") and track.get("thumbnail_url"):
                    album["thumbnail_url"] = track.get("thumbnail_url")
                if str(track.get("added_at") or "") > str(album.get("added_at") or ""):
                    album["added_at"] = track.get("added_at") or ""
                if str(track.get("last_played_at") or "") > str(album.get("last_played_at") or ""):
                    album["last_played_at"] = track.get("last_played_at") or ""

        return {
            "tracks": tracks,
            "artists": sorted(artists.values(), key=lambda a: (-int(a.get("track_count") or 0), str(a.get("name") or "").lower())),
            "albums": sorted(albums.values(), key=lambda a: (str(a.get("artist") or "").lower(), str(a.get("title") or "").lower())),
            "followed_artists": self.list_followed_artists(),
            # Whose codec support the playability marks above describe. Empty
            # when nothing was marked, so a client never has to guess.
            "capabilities_device_id": str(device_id or "") if codecs else "",
        }
