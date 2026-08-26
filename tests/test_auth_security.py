import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import auth_security


class AuthSecurityTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        auth_security.ensure_schema(self.conn)
        self.now = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
        self.key = auth_security.attempt_key("chenyan", "127.0.0.1")

    def tearDown(self):
        self.conn.close()

    def test_fifth_failure_locks_login_and_success_clears_state(self):
        for _ in range(4):
            self.assertEqual(
                auth_security.record_failure(self.conn, self.key, 5, 900, 900, self.now),
                0,
            )
        self.assertEqual(
            auth_security.record_failure(self.conn, self.key, 5, 900, 900, self.now),
            900,
        )
        self.assertEqual(auth_security.retry_after(self.conn, self.key, 900, self.now), 900)
        auth_security.clear_failures(self.conn, self.key)
        self.assertEqual(auth_security.retry_after(self.conn, self.key, 900, self.now), 0)

    def test_expired_window_resets_failure_count(self):
        auth_security.record_failure(self.conn, self.key, 5, 900, 900, self.now)
        later = self.now + timedelta(seconds=901)
        self.assertEqual(auth_security.retry_after(self.conn, self.key, 900, later), 0)
        auth_security.record_failure(self.conn, self.key, 5, 900, 900, later)
        count = self.conn.execute(
            "SELECT failed_count FROM login_attempts WHERE attempt_key = ?", (self.key,)
        ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
