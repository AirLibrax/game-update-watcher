"""去重存储：基于 SQLite，记录已发布的版本条目。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class PublishStore:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS published (
                dedup_key TEXT PRIMARY KEY,
                game TEXT NOT NULL,
                version_num TEXT,
                version_name TEXT,
                published_at TEXT NOT NULL,
                payload TEXT
            )
            """
        )
        self._conn.commit()

    def is_published(self, dedup_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM published WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        return row is not None

    def mark_published(self, game: str, version_num: str | None, version_name: str, dedup_key: str, payload: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO published (dedup_key, game, version_num, version_name, published_at, payload) VALUES (?, ?, ?, ?, datetime('now','localtime'), ?)",
            (dedup_key, game, version_num or "", version_name, payload),
        )
        self._conn.commit()

    def recent(self, limit: int = 20) -> list[tuple]:
        return self._conn.execute(
            "SELECT game, version_num, version_name, published_at FROM published ORDER BY published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
