"""Job state in SQLite (data/jobs.db). One row per video."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = [
    "video_id",
    "url",
    "status",  # pending | running | failed | article_ready | draft_created | published
    "current_stage",
    "failed_stage",
    "error",
    "transcript_method",
    "ghost_post_id",
    "ghost_editor_url",
    "published_url",
    "created_at",
    "updated_at",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    video_id TEXT PRIMARY KEY,
    {", ".join(f"{c} TEXT" for c in COLUMNS[1:])}
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, video_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE video_id = ?", (video_id,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def upsert(self, video_id: str, **fields: str | None) -> None:
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise ValueError(f"Unknown job fields: {unknown}")
        fields["updated_at"] = _now()
        if self.get(video_id) is None:
            fields.setdefault("created_at", fields["updated_at"])
            cols = ["video_id", *fields]
            self._conn.execute(
                f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (video_id, *fields.values()),
            )
        else:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(
                f"UPDATE jobs SET {assignments} WHERE video_id = ?",
                (*fields.values(), video_id),
            )
        self._conn.commit()
