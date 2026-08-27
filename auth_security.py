import hashlib
import math
from datetime import datetime, timedelta, timezone


def now_utc():
    return datetime.now(timezone.utc)


def attempt_key(username, client_address):
    value = f"{str(username or '').strip().lower()}\0{str(client_address or '').strip()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            attempt_key TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL,
            first_failed_at TEXT NOT NULL,
            locked_until TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def retry_after(conn, key, window_seconds, current_time=None):
    ensure_schema(conn)
    current_time = current_time or now_utc()
    row = conn.execute(
        "SELECT first_failed_at, locked_until FROM login_attempts WHERE attempt_key = ?",
        (key,),
    ).fetchone()
    if not row:
        return 0
    locked_until = _parse(row["locked_until"])
    if locked_until and locked_until > current_time:
        return max(1, math.ceil((locked_until - current_time).total_seconds()))
    first_failed_at = _parse(row["first_failed_at"])
    if not first_failed_at or current_time - first_failed_at >= timedelta(seconds=window_seconds):
        conn.execute("DELETE FROM login_attempts WHERE attempt_key = ?", (key,))
    return 0


def record_failure(conn, key, max_failures, window_seconds, lock_seconds, current_time=None):
    ensure_schema(conn)
    current_time = current_time or now_utc()
    row = conn.execute(
        "SELECT failed_count, first_failed_at FROM login_attempts WHERE attempt_key = ?",
        (key,),
    ).fetchone()
    first_failed_at = _parse(row["first_failed_at"]) if row else None
    if not row or not first_failed_at or current_time - first_failed_at >= timedelta(seconds=window_seconds):
        failed_count = 1
        first_failed_at = current_time
    else:
        failed_count = int(row["failed_count"]) + 1
    locked_until = current_time + timedelta(seconds=lock_seconds) if failed_count >= max_failures else None
    conn.execute(
        """
        INSERT INTO login_attempts(attempt_key, failed_count, first_failed_at, locked_until, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(attempt_key) DO UPDATE SET
            failed_count = excluded.failed_count,
            first_failed_at = excluded.first_failed_at,
            locked_until = excluded.locked_until,
            updated_at = excluded.updated_at
        """,
        (
            key,
            failed_count,
            first_failed_at.isoformat(),
            locked_until.isoformat() if locked_until else None,
            current_time.isoformat(),
        ),
    )
    return lock_seconds if locked_until else 0


def clear_failures(conn, key):
    ensure_schema(conn)
    conn.execute("DELETE FROM login_attempts WHERE attempt_key = ?", (key,))
