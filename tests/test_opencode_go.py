import json
import time
import unittest
import uuid
from datetime import datetime, timezone
from threading import Event, Thread
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app
from opencode_go import evaluate_alerts, parse_dashboard_usage


class OpenCodeGoTest(unittest.TestCase):
    def test_concurrent_refresh_is_rejected_without_occupying_another_request(self):
        started = Event()
        release = Event()

        def first_refresh():
            with app.opencode_refresh_lock:
                started.set()
                release.wait(timeout=1)

        worker = Thread(target=first_refresh)
        worker.start()
        self.assertTrue(started.wait(timeout=1))
        try:
            before = time.monotonic()
            with self.assertRaises(app.OpenCodeRefreshBusy):
                app.load_opencode_accounts(force=True, reject_if_busy=True)
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertEqual(app.app.test_client().get("/api/health").status_code, 200)
        finally:
            release.set()
            worker.join(timeout=1)

    def test_refresh_deadline_returns_partial_errors_without_blocking_health(self):
        state = {
            "id": 1,
            "quota_configured": True,
            "models_configured": True,
            "quota": None,
            "quota_error": None,
            "models": None,
            "models_error": None,
        }

        def slow_fetch(*_args, **_kwargs):
            time.sleep(0.15)
            return {"ok": True}

        started = time.monotonic()
        with (
            patch.object(app, "list_opencode_account_rows", return_value=[{"id": 1}]),
            patch.object(app, "public_opencode_account", return_value=state.copy()),
            patch.object(app, "fetch_opencode_account_part", side_effect=slow_fetch),
        ):
            result = app.load_opencode_accounts(force=True, deadline_seconds=0.02)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.1)
        self.assertIn("整体时限", result[0]["quota_error"])
        self.assertIn("整体时限", result[0]["models_error"])

    def test_parse_dashboard_usage_reads_all_windows(self):
        document = """
        rollingUsage: {usagePercent: 25, resetInSec: 300},
        weeklyUsage: {usagePercent: 50.5, resetInSec: 600},
        monthlyUsage: {usagePercent: 100, resetInSec: 900}
        """
        result = parse_dashboard_usage(document, datetime(2026, 7, 19, tzinfo=timezone.utc))

        self.assertEqual(result["rolling"]["remaining_percent"], 75)
        self.assertEqual(result["weekly"]["used_percent"], 50.5)
        self.assertEqual(result["monthly"]["remaining_percent"], 0)
        self.assertEqual(result["rolling"]["resets_at"], "2026-07-19T00:05:00+00:00")

    def test_alert_threshold_is_deduplicated_for_the_same_window(self):
        accounts = [{
            "account_key": "demo",
            "label": "Demo",
            "quota_error": None,
            "models_error": None,
            "models": {"upstream_state": "available"},
            "quota": {
                "windows": {
                    "rolling": {
                        "label": "5 小时",
                        "remaining_percent": 4,
                        "used_percent": 96,
                        "resets_at": "2026-07-19T02:00:00+00:00",
                    }
                }
            },
        }]
        first = evaluate_alerts(accounts, thresholds=[20, 5, 0])
        second = evaluate_alerts(accounts, previous_state=first["state"], thresholds=[20, 5, 0])

        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(first["events"][0]["threshold"], 5)
        self.assertEqual(second["events"], [])

    def test_opencode_routes_require_existing_session(self):
        with app.app.test_client() as client:
            self.assertEqual(client.get("/api/opencode/accounts").status_code, 401)
            self.assertEqual(client.get("/_ub_api/opencode/accounts").status_code, 401)

    def test_account_secrets_are_encrypted_and_masked(self):
        marker = uuid.uuid4().hex
        username = f"opencode-test-user-{marker}"
        label = f"OpenCode {marker}"
        auth_cookie = f"auth-cookie-{marker}"
        api_key = f"sk-{marker}"
        ts = app.now_iso()
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

        account_id = None
        try:
            with app.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["id"] = user["id"]
                    sess["username"] = user["username"]
                    sess["totp_enabled"] = False
                response = client.post("/api/opencode/accounts", json={
                    "label": label,
                    "workspace_id": "wrk_TEST123",
                    "auth_cookie": auth_cookie,
                    "api_key": api_key,
                })
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            payload = response.get_json()["data"]
            account_id = payload["id"]
            serialized = json.dumps(payload)
            self.assertNotIn(auth_cookie, serialized)
            self.assertNotIn(api_key, serialized)
            self.assertTrue(payload["has_auth_cookie"])
            self.assertTrue(payload["has_api_key"])

            with app.db() as conn:
                row = conn.execute(
                    "SELECT auth_cookie_enc, api_key_enc FROM opencode_accounts WHERE id = ?",
                    (account_id,),
                ).fetchone()
            self.assertNotIn(auth_cookie, row["auth_cookie_enc"])
            self.assertNotIn(api_key, row["api_key_enc"])
            self.assertEqual(app.decrypt(row["auth_cookie_enc"]), auth_cookie)
            self.assertEqual(app.decrypt(row["api_key_enc"]), api_key)
        finally:
            with app.db() as conn:
                if account_id:
                    conn.execute("DELETE FROM opencode_accounts WHERE id = ?", (account_id,))
                conn.execute("DELETE FROM users WHERE username = ?", (username,))
            if account_id:
                app.clear_opencode_cache(account_id)

    def test_method_tunnel_aliases_include_opencode_routes(self):
        rules = [str(rule) for rule in app.app.url_map.iter_rules()]
        self.assertIn("/_ub_api/opencode/accounts", rules)
        self.assertIn("/_ub_api/opencode/refresh", rules)
        self.assertIn("/_ub_api/opencode/alerts/test", rules)

    def test_plain_get_cannot_trigger_tunneled_write_route(self):
        with app.app.test_client() as client:
            response = client.get("/_ub_api/opencode/alerts/test")
        self.assertIn(response.status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()
