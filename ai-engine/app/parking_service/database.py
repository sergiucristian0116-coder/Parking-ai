from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('ADMIN', 'OPERATOR', 'VIEWER')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cameras (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER,
                    profile TEXT NOT NULL DEFAULT '',
                    username_enc BLOB,
                    password_enc BLOB,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    zones_json TEXT NOT NULL DEFAULT '[]',
                    parking_limit_seconds INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parking_sessions (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL REFERENCES cameras(id),
                    vehicle_id TEXT NOT NULL,
                    plate TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    UNIQUE(camera_id, vehicle_id)
                );
                CREATE TABLE IF NOT EXISTS vehicle_observations (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL REFERENCES cameras(id),
                    vehicle_id TEXT NOT NULL,
                    plate TEXT,
                    confidence REAL NOT NULL DEFAULT 0,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS observations_by_time
                    ON vehicle_observations(observed_at);
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL REFERENCES cameras(id),
                    path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    camera_id TEXT NOT NULL REFERENCES cameras(id),
                    vehicle_id TEXT NOT NULL,
                    plate TEXT,
                    timestamp TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    rule TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    evidence_id TEXT REFERENCES evidence(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    decided_by TEXT REFERENCES users(id),
                    decision_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS incidents_by_status_time
                    ON incidents(status, timestamp);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id TEXT PRIMARY KEY,
                    user_id TEXT REFERENCES users(id),
                    action TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, parameters).fetchall()]

    def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(sql, parameters).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> int:
        with self.connect() as db:
            cursor = db.execute(sql, parameters)
            return cursor.rowcount

    def audit(self, user_id: str | None, action: str, metadata: dict[str, Any]) -> None:
        import uuid

        self.execute(
            "INSERT INTO audit_log(id, user_id, action, timestamp, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, action, utc_now(), json.dumps(metadata, separators=(",", ":"))),
        )
