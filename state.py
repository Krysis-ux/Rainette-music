"""SQLite-backed music state: playlists, tracks, and play history.

A trimmed, standalone extraction of the music slice of Rainette's ``RainetteState``.
The method signatures and SQL are identical to Rainette so ``music_bridge`` works
against it unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
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

    # ── Row helpers ──────────────────────────────────────────────────────────

    def _track_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = loads_object(item.pop("metadata_json", "{}"))
        item.pop("_last", None)   # internal ordering column, never exposed
        return item

    # ── Playlists ────────────────────────────────────────────────────────────

    def create_playlist(self, name: str, description: str = "") -> dict[str, Any]:
        cleaned = str(name or "").strip()[:120]
        if not cleaned:
            raise ValueError("playlist name is required")
        playlist_id = "pl_" + uuid.uuid4().hex
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO music_playlists (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (playlist_id, cleaned, str(description or "")[:400], now, now),
            )
            row = conn.execute("SELECT * FROM music_playlists WHERE id = ?", (playlist_id,)).fetchone()
        return dict(row)

    def list_playlists(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM music_playlist_tracks t WHERE t.playlist_id = p.id) AS track_count
                FROM music_playlists p
                ORDER BY p.updated_at DESC, p.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

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

    def list_music_tracks(self, *, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 500), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM music_tracks
                ORDER BY added_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._track_row(row) for row in rows]

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
            })
            artist["track_count"] += 1
            if not artist.get("thumbnail_url") and track.get("thumbnail_url"):
                artist["thumbnail_url"] = track.get("thumbnail_url")

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
                })
                album["track_count"] += 1
                if len(album["tracks"]) < 50:
                    album["tracks"].append(track)
                if not album.get("thumbnail_url") and track.get("thumbnail_url"):
                    album["thumbnail_url"] = track.get("thumbnail_url")

        return {
            "tracks": tracks,
            "artists": sorted(artists.values(), key=lambda a: (-int(a.get("track_count") or 0), str(a.get("name") or "").lower())),
            "albums": sorted(albums.values(), key=lambda a: (str(a.get("artist") or "").lower(), str(a.get("title") or "").lower())),
        }
