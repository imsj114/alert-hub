from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from alert_hub.models import DeliveryJob, IngestOutcome, IngestResult, PreparedEvent, Severity
from alert_hub.time_utils import format_utc


class Database:
    def __init__(self, path: str) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body TEXT,
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    dedupe_key TEXT,
                    effective_dedupe_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    suppressed_by_event_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(sender_id, event_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_dedupe
                    ON events(sender_id, effective_dedupe_key, received_at);
                CREATE INDEX IF NOT EXISTS idx_events_status
                    ON events(status, received_at);

                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_db_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    target_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(event_db_id, target_id)
                );

                CREATE INDEX IF NOT EXISTS idx_deliveries_pending
                    ON deliveries(status, next_attempt_at);

                CREATE TABLE IF NOT EXISTS seen_signatures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    UNIQUE(sender_id, signature)
                );

                CREATE INDEX IF NOT EXISTS idx_seen_signatures_seen_at
                    ON seen_signatures(seen_at);
                """
            )
            self._migrate_additive_columns(conn)

    def _migrate_additive_columns(self, conn: sqlite3.Connection) -> None:
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "tags_json" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")

    def ping(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1").fetchone()

    def record_signature(self, sender_id: str, signature: str, seen_at: datetime, replay_window_seconds: int) -> bool:
        cutoff = format_utc(seen_at - timedelta(seconds=replay_window_seconds))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM seen_signatures WHERE seen_at < ?", (cutoff,))
                conn.execute(
                    "INSERT INTO seen_signatures (sender_id, signature, seen_at) VALUES (?, ?, ?)",
                    (sender_id, signature, format_utc(seen_at)),
                )
                conn.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return False
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def ingest_event(self, event: PreparedEvent, target_ids: Iterable[str], dedupe_window_seconds: int) -> IngestResult:
        cutoff = format_utc(event.received_at - timedelta(seconds=dedupe_window_seconds))
        target_ids = tuple(target_ids)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT id, payload_hash, status FROM events WHERE sender_id = ? AND event_id = ?",
                    (event.sender_id, event.event_id),
                ).fetchone()
                if existing:
                    conn.execute("COMMIT")
                    if existing["payload_hash"] == event.payload_hash:
                        return IngestResult(
                            outcome=IngestOutcome.DUPLICATE,
                            event_db_id=existing["id"],
                            message=f"event already recorded with status={existing['status']}",
                            existing_status=existing["status"],
                        )
                    return IngestResult(
                        outcome=IngestOutcome.CONFLICT,
                        event_db_id=existing["id"],
                        message="event_id was reused with a different payload",
                        existing_status=existing["status"],
                    )

                suppressed_by = conn.execute(
                    """
                    SELECT id
                    FROM events
                    WHERE sender_id = ?
                      AND effective_dedupe_key = ?
                      AND status = 'accepted'
                      AND received_at >= ?
                    ORDER BY received_at DESC
                    LIMIT 1
                    """,
                    (event.sender_id, event.effective_dedupe_key, cutoff),
                ).fetchone()

                status = IngestOutcome.SUPPRESSED.value if suppressed_by else IngestOutcome.ACCEPTED.value
                cursor = conn.execute(
                    """
                    INSERT INTO events (
                        sender_id,
                        event_id,
                        source,
                        event_type,
                        severity,
                        summary,
                        body,
                        occurred_at,
                        received_at,
                        dedupe_key,
                        effective_dedupe_key,
                        payload_json,
                        payload_hash,
                        metadata_json,
                        links_json,
                        tags_json,
                        status,
                        suppressed_by_event_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.sender_id,
                        event.event_id,
                        event.source,
                        event.event_type,
                        event.severity.value,
                        event.summary,
                        event.body,
                        format_utc(event.occurred_at),
                        format_utc(event.received_at),
                        event.payload.dedupe_key,
                        event.effective_dedupe_key,
                        event.payload_json,
                        event.payload_hash,
                        event.metadata_json,
                        event.links_json,
                        event.tags_json,
                        status,
                        suppressed_by["id"] if suppressed_by else None,
                        format_utc(event.received_at),
                    ),
                )
                event_db_id = int(cursor.lastrowid)

                if not suppressed_by:
                    for target_id in target_ids:
                        conn.execute(
                            """
                            INSERT INTO deliveries (
                                event_db_id,
                                target_id,
                                status,
                                attempts,
                                next_attempt_at,
                                created_at,
                                updated_at
                            ) VALUES (?, ?, 'pending', 0, ?, ?, ?)
                            """,
                            (
                                event_db_id,
                                target_id,
                                format_utc(event.received_at),
                                format_utc(event.received_at),
                                format_utc(event.received_at),
                            ),
                        )
                conn.execute("COMMIT")

                outcome = IngestOutcome.SUPPRESSED if suppressed_by else IngestOutcome.ACCEPTED
                message = "event suppressed by dedupe window" if suppressed_by else "event accepted"
                return IngestResult(
                    outcome=outcome,
                    event_db_id=event_db_id,
                    target_ids=() if suppressed_by else target_ids,
                    message=message,
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def fetch_due_deliveries(self, now: datetime, limit: int = 20) -> list[DeliveryJob]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.event_db_id,
                    d.target_id,
                    d.attempts,
                    e.sender_id,
                    e.event_id,
                    e.source,
                    e.event_type,
                    e.severity,
                    e.summary,
                    e.body,
                    e.links_json,
                    e.tags_json
                FROM deliveries d
                JOIN events e ON e.id = d.event_db_id
                WHERE d.status = 'pending' AND d.next_attempt_at <= ?
                ORDER BY d.next_attempt_at ASC, d.id ASC
                LIMIT ?
                """,
                (format_utc(now), limit),
            ).fetchall()

        jobs = []
        for row in rows:
            links = json.loads(row["links_json"])
            tags = tuple(dict.fromkeys(json.loads(row["tags_json"])))
            jobs.append(
                DeliveryJob(
                    delivery_id=row["delivery_id"],
                    event_db_id=row["event_db_id"],
                    target_id=row["target_id"],
                    attempts=row["attempts"],
                    sender_id=row["sender_id"],
                    event_id=row["event_id"],
                    source=row["source"],
                    event_type=row["event_type"],
                    severity=Severity(row["severity"]),
                    summary=row["summary"],
                    body=row["body"],
                    links=tuple(links),
                    tags=tags,
                )
            )
        return jobs

    def mark_delivery_delivered(self, delivery_id: int, attempted_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'delivered',
                    attempts = attempts + 1,
                    last_error = NULL,
                    delivered_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (format_utc(attempted_at), format_utc(attempted_at), delivery_id),
            )

    def reschedule_delivery(self, delivery_id: int, attempted_at: datetime, next_attempt_at: datetime, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET attempts = attempts + 1,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, format_utc(next_attempt_at), format_utc(attempted_at), delivery_id),
            )

    def mark_delivery_dead(self, delivery_id: int, attempted_at: datetime, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE deliveries
                SET status = 'dead',
                    attempts = attempts + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, format_utc(attempted_at), delivery_id),
            )
