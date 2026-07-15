"""SQLite-backed music state: playlists, tracks, and play history.

A compact standalone SQLite state layer for Rainette Music. Its methods and SQL
support the music bridge directly.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json
import sqlite3
import threading
import uuid


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

        conn.executescript(
            """
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
            """
        )

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

        ``days`` bounds the window (0 = all time). Daily buckets use the local
        timezone so "today" matches the user's clock, and the chart series
        covers the most recent ``min(days or 30, 30)`` days ending today.
        """
        try:
            days = max(0, min(int(days or 0), 365))
        except Exception:
            days = 7
        now_local = datetime.now(timezone.utc).astimezone()
        cutoff = None
        if days:
            cutoff = (now_local - timedelta(days=days)).astimezone(timezone.utc)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT h.played_at AS played_at, t.*
                FROM music_play_history h
                JOIN music_tracks t ON t.id = h.track_id
                ORDER BY h.rowid DESC
                """
            ).fetchall()

        chart_days = min(days or 30, 30)
        day_keys = [(now_local - timedelta(days=offset)).strftime("%Y-%m-%d")
                    for offset in range(chart_days - 1, -1, -1)]
        daily = {key: 0 for key in day_keys}

        total_plays = 0
        total_seconds = 0.0
        track_plays: dict[str, dict[str, Any]] = {}
        artist_plays: dict[str, dict[str, Any]] = {}
        for row in rows:
            track = self._track_row(row)
            played_at = parse_utc(track.pop("played_at", None))
            if played_at is None:
                continue
            if cutoff is not None and played_at < cutoff:
                continue
            total_plays += 1
            duration = track.get("duration_s")
            if isinstance(duration, (int, float)) and duration > 0:
                total_seconds += float(duration)
            local_day = played_at.astimezone().strftime("%Y-%m-%d")
            if local_day in daily:
                daily[local_day] += 1

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
            "daily": [{"date": key, "count": daily[key]} for key in day_keys],
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

    def music_library_index(self, *, limit: int = 500) -> dict[str, Any]:
        tracks = self.list_music_tracks(limit=limit)
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
        }
