"""Local state on the Pi: what we believe is on disk, and the last manifest version.

SQLite so the agent survives power loss mid-sync and can diff cheaply.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class LocalState:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                media_id   TEXT PRIMARY KEY,
                playlist   TEXT NOT NULL,
                filename   TEXT NOT NULL,
                sha256     TEXT,
                size_bytes INTEGER
            );
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """
        )

    # --- kv helpers -------------------------------------------------------
    def get(self, k: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k: str, v: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v),
            )

    @property
    def manifest_version(self) -> int:
        return int(self.get("manifest_version", "0") or 0)

    @manifest_version.setter
    def manifest_version(self, v: int) -> None:
        self.set("manifest_version", str(v))

    # --- file tracking ----------------------------------------------------
    def known(self) -> dict[str, tuple[str, str, str | None]]:
        """media_id -> (playlist, filename, sha256)"""
        return {
            r[0]: (r[1], r[2], r[3])
            for r in self.db.execute("SELECT media_id, playlist, filename, sha256 FROM files")
        }

    def record(self, media_id: str, playlist: str, filename: str, sha256: str | None, size: int | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                (media_id, playlist, filename, sha256, size),
            )

    def forget(self, media_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM files WHERE media_id=?", (media_id,))

    def totals(self) -> tuple[int, int]:
        n, b = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM files").fetchone()
        return int(n), int(b)
