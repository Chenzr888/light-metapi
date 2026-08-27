#!/usr/bin/env python3
import argparse
import getpass
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash


def main():
    parser = argparse.ArgumentParser(description="Create or reset an upstream-balance administrator")
    parser.add_argument("--db", required=True, help="Path to upstreams.sqlite3")
    parser.add_argument("--username", required=True)
    parser.add_argument("--exclusive", action="store_true", help="Delete all other administrator accounts")
    args = parser.parse_args()

    username = args.username.strip()
    if len(username) < 3:
        raise SystemExit("Username must contain at least 3 characters")
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if len(password) < 8:
        raise SystemExit("Password must contain at least 8 characters")

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")
    timestamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if not table:
            raise SystemExit("The users table does not exist; start the application once first")
        conn.execute(
            """
            INSERT INTO users(username, password_hash, totp_secret, totp_enabled, created_at, updated_at)
            VALUES(?, ?, NULL, 0, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                totp_secret = NULL,
                totp_enabled = 0,
                updated_at = excluded.updated_at
            """,
            (username, generate_password_hash(password), timestamp, timestamp),
        )
        if args.exclusive:
            conn.execute("DELETE FROM users WHERE username <> ?", (username,))
        attempts_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'login_attempts'"
        ).fetchone()
        if attempts_table:
            conn.execute("DELETE FROM login_attempts")
    os.chmod(db_path, 0o600)
    print(f"admin_user={username}")
    print("password_storage=werkzeug_scrypt_hash")
    print(f"exclusive={str(args.exclusive).lower()}")


if __name__ == "__main__":
    main()
