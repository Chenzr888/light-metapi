import sqlite3
import unittest
import uuid
from unittest.mock import Mock, patch

import requests
from werkzeug.security import generate_password_hash

import app


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class RouteSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        params = kwargs.get("params") or {}
        key = (url, params.get("type"))
        if key in self.routes:
            return self.routes[key]
        return self.routes[url]


class NewApiTwoFASession:
    def __init__(self):
        self.headers = {}
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/api/user/login"):
            return FakeResponse({"success": True, "data": {"require_2fa": True}})
        if url.endswith("/api/user/login/2fa"):
            return FakeResponse({"success": True, "data": {"id": 42, "username": "demo"}})
        return FakeResponse({"success": False, "message": "unexpected post"})


class NewApiBearerSession:
    def __init__(self):
        self.headers = {}
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse({"success": True, "data": {"version": "jwt-demo"}})

    def post(self, url, **kwargs):
        return FakeResponse({
            "success": True,
            "data": {
                "access_token": "login-token",
                "token_type": "Bearer",
                "user": {"id": 73, "username": "demo"},
            },
        })


class RechargeAndNotificationTest(unittest.TestCase):
    def test_detect_upstream_platform_uses_public_signatures(self):
        new_api = FakeResponse({"success": True, "data": {"quota_per_unit": 500000}})
        with patch("app.requests.get", return_value=new_api):
            platform, _ = app.detect_upstream_platform("https://new.example")
        self.assertEqual(platform, "new_api")

        not_new = FakeResponse({"message": "not found"}, status_code=404)
        sub2 = FakeResponse({"code": 401, "message": "unauthorized"}, status_code=401)
        with patch("app.requests.get", side_effect=[not_new, sub2]):
            platform, _ = app.detect_upstream_platform("https://sub.example")
        self.assertEqual(platform, "sub2api")

    def test_retired_opencode_state_is_purged(self):
        with app.db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS opencode_accounts(id INTEGER PRIMARY KEY)")
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('opencode_alert_state', '{}')")

        app.init_db()

        with app.db() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'opencode_accounts'"
            ).fetchone()
            state = conn.execute(
                "SELECT value FROM settings WHERE key = 'opencode_alert_state'"
            ).fetchone()
        self.assertIsNone(table)
        self.assertIsNone(state)

    def test_session_cookie_uses_app_specific_name(self):
        self.assertEqual(app.app.config["SESSION_COOKIE_NAME"], "ub_admin_session")

    def test_login_sets_scoped_http_only_session_cookie(self):
        ts = app.now_iso()
        username = f"cookie-user-{uuid.uuid4().hex}"
        with app.db() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
        old_path = app.app.config["SESSION_COOKIE_PATH"]
        app.app.config["SESSION_COOKIE_PATH"] = "/upstream-balance"
        try:
            with app.app.test_client() as client:
                response = client.post("/api/auth/login", json={"username": username, "password": "secret123"})
        finally:
            app.app.config["SESSION_COOKIE_PATH"] = old_path
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("ub_admin_session=", cookie)
        self.assertIn("Path=/upstream-balance", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_session_login_required_for_protected_routes(self):
        ts = app.now_iso()
        username = f"session-user-{uuid.uuid4().hex}"
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        with app.app.test_client() as client:
            self.assertEqual(client.get("/api/channels").status_code, 401)
            self.assertEqual(client.get("/api/channels", headers={"Cookie": "session=invalid"}).status_code, 401)
            self.assertEqual(client.get("/api/channels", headers={"X-UB-Auth": "legacy-token"}).status_code, 401)
            with client.session_transaction() as sess:
                sess["id"] = user["id"]
                sess["username"] = user["username"]
                sess["totp_enabled"] = False
            self.assertEqual(client.get("/api/channels").status_code, 200)

    def test_ub_api_method_override_supports_login_post(self):
        ts = app.now_iso()
        username = f"method-override-{uuid.uuid4().hex}"
        with app.db() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )

        payload = app.base64.urlsafe_b64encode(
            app.json.dumps({"username": username, "password": "secret123"}).encode("utf-8")
        ).decode("ascii")
        with app.app.test_client() as client:
            response = client.get("/_ub_api/auth/login", headers={
                "X-UB-Method": "POST",
                "X-UB-Payload": payload,
            })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["data"]["authenticated"])

    def test_login_is_temporarily_locked_after_repeated_failures(self):
        ts = app.now_iso()
        username = f"rate-limit-{uuid.uuid4().hex}"
        address = f"198.51.100.{int(uuid.uuid4().hex[:2], 16) or 1}"
        with app.db() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("correct-password"), ts, ts),
            )
        with app.app.test_client() as client:
            for _ in range(app.LOGIN_MAX_FAILURES - 1):
                response = client.post(
                    "/api/auth/login",
                    json={"username": username, "password": "wrong-password"},
                    headers={"X-Forwarded-For": address},
                )
                self.assertEqual(response.status_code, 401)
            locked = client.post(
                "/api/auth/login",
                json={"username": username, "password": "wrong-password"},
                headers={"X-Forwarded-For": address},
            )
            still_locked = client.post(
                "/api/auth/login",
                json={"username": username, "password": "correct-password"},
                headers={"X-Forwarded-For": address},
            )

        self.assertEqual(locked.status_code, 429)
        self.assertEqual(still_locked.status_code, 429)
        self.assertGreaterEqual(int(locked.headers["Retry-After"]), 1)

    def test_web_registration_is_disabled_by_default(self):
        with app.app.test_client() as client:
            response = client.post(
                "/api/auth/register",
                json={"username": "browser-admin", "password": "secret123"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "管理员账号由服务器预设")

    def test_alert_threshold_uses_channel_value_then_default(self):
        self.assertEqual(str(app.alert_threshold_from("15")), "15")
        self.assertEqual(str(app.alert_threshold_from(None, "25")), "25")
        self.assertEqual(str(app.alert_threshold_from(None, None)), str(app.LOW_BALANCE_ALERT_CNY))

    def test_channel_error_alert_repeats_after_cooldown_and_clears_on_recovery(self):
        channel = {
            "id": 9191,
            "name": "broken-upstream",
            "base_url": "https://broken.example/",
            "status": "error",
            "message": "login failed",
        }
        settings = {"notify_enabled": "1"}

        def get_setting(key, default=""):
            return settings.get(key, default)

        def set_setting(key, value):
            settings[key] = value

        def delete_setting(key):
            settings.pop(key, None)

        with patch("app.setting_get", side_effect=get_setting), \
                patch("app.setting_set", side_effect=set_setting), \
                patch("app.setting_delete", side_effect=delete_setting), \
                patch("app.notification_webhooks_configured", return_value=True), \
                patch("app.post_notification_text", return_value=["wecom"]) as notify, \
                patch("app.post_low_balance_email", return_value=False):
            self.assertEqual(app.send_channel_error_alerts([channel]), [channel])
            self.assertEqual(app.send_channel_error_alerts([channel]), [])
            recovered = {**channel, "status": "ok", "message": ""}
            self.assertEqual(app.send_channel_error_alerts([recovered]), [])
            self.assertEqual(app.send_channel_error_alerts([channel]), [channel])

        self.assertEqual(notify.call_count, 2)

    def test_channel_rows_expose_alert_threshold(self):
        with app.db() as conn:
            ts = app.now_iso()
            cur = conn.execute(
                """
                INSERT INTO channels(
                    name, platform, base_url, username, password_enc, credential_enc, cny_rate, alert_cny, enabled,
                    balance, raw_balance, used_balance, raw_used_balance, request_count, currency, status, message,
                    raw_response, last_checked_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, '', '', ?, ?, 1, ?, ?, NULL, NULL, NULL, 'USD', 'ok', '', '{}', ?, ?, ?)
                """,
                ("demo", "new_api", "https://example.com/", "u", 7.3, 88, 100, "100", None, ts, ts),
            )
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (cur.lastrowid,)).fetchone()
        item = app.row_to_channel(row)
        self.assertEqual(str(item["alert_cny"]), "88.0")
        self.assertAlmostEqual(item["cny_balance"], 100 / 7.3)
        self.assertFalse(item["boss_recharge_required"])

    def test_channel_rows_expose_boss_recharge_flag(self):
        with app.db() as conn:
            ts = app.now_iso()
            cur = conn.execute(
                """
                INSERT INTO channels(
                    name, platform, base_url, username, password_enc, credential_enc, cny_rate, alert_cny,
                    boss_recharge_required, enabled, balance, raw_balance, used_balance, raw_used_balance, request_count,
                    currency, status, message, raw_response, last_checked_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, '', '', ?, ?, 1, 1, ?, ?, NULL, NULL, NULL, 'USD', 'ok', '', '{}', ?, ?, ?)
                """,
                ("boss-demo", "new_api", "https://example.com/", "u", 7.3, 88, 100, "100", None, ts, ts),
            )
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (cur.lastrowid,)).fetchone()
        item = app.row_to_channel(row)
        self.assertTrue(item["boss_recharge_required"])

    def test_cny_value_uses_usd_per_cny_ratio(self):
        self.assertEqual(app.cny_value(3000, 10), 300.0)

    def test_recharge_urls_match_upstream_pages(self):
        self.assertEqual(app.recharge_url_for("new_api", "https://example.com/"), "https://example.com/console/topup")
        self.assertEqual(app.recharge_url_for("sub2api", "https://example.com/"), "https://example.com/purchase")

    def test_paginated_items_reads_common_page_shapes(self):
        self.assertEqual(app.paginated_items({"data": {"rows": [{"id": 1}]}}), [{"id": 1}])
        self.assertEqual(app.paginated_items({"data": {"records": [{"id": 2}]}}), [{"id": 2}])

    def test_new_api_recharge_logs_parse_successful_rows(self):
        session = FakeSession(FakeResponse({
            "success": True,
            "data": {
                "rows": [
                    {
                        "id": 11,
                        "trade_no": "paid-11",
                        "status": "success",
                        "money": "10.5",
                        "complete_time": 1710000000,
                        "payment_method": "alipay",
                    },
                    {"id": 12, "status": "pending", "amount": "9"},
                ]
            },
        }))

        logs = app.new_api_recharge_logs(
            "https://example.com/",
            {"access_token": "token", "user_id": 7},
            session=session,
        )

        self.assertEqual(len(logs), 1)
        self.assertEqual(str(logs[0]["amount_usd"]), "10.5")
        self.assertEqual(logs[0]["source_status"], "success")
        self.assertEqual(logs[0]["source_type"], "alipay")
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://example.com/api/user/topup/self")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token")
        self.assertEqual(kwargs["headers"]["New-Api-User"], "7")

    def test_new_api_login_supports_2fa_flow(self):
        session = NewApiTwoFASession()

        with patch("app.requests.Session", return_value=session):
            _, user = app.new_api_login("https://example.com/", "demo", "secret123", "123456")

        self.assertEqual(user["id"], 42)
        self.assertEqual(session.posts[0][0], "https://example.com/api/user/login")
        self.assertEqual(session.posts[0][1]["params"], {"turnstile": ""})
        self.assertEqual(session.posts[1][0], "https://example.com/api/user/login/2fa")
        self.assertEqual(session.posts[1][1]["json"], {"code": "123456"})

    def test_new_api_login_reuses_bearer_token_response(self):
        session = NewApiBearerSession()

        with patch("app.requests.Session", return_value=session):
            logged_session, user = app.new_api_login("https://example.com/", "demo", "secret123")
            credential = app.new_api_generate_token("https://example.com/", logged_session, user["id"])

        self.assertEqual(user["id"], 73)
        self.assertEqual(session.headers["Authorization"], "Bearer login-token")
        self.assertEqual(session.headers["New-Api-User"], "73")
        self.assertEqual(credential["access_token"], "login-token")
        self.assertEqual(len(session.gets), 1)

    def test_fetch_new_api_relogs_when_saved_token_expires(self):
        channel = {
            "id": 731,
            "base_url": "https://example.com/",
            "username": "demo",
            "password_enc": app.encrypt("secret123"),
            "credential_enc": "",
        }
        session = Mock()
        with patch("app.load_credential", return_value={"access_token": "expired", "user_id": 7}), \
                patch("app.new_api_self", side_effect=[RuntimeError("Unauthorized, invalid access token"), {"quota": 500000}]), \
                patch("app.new_api_login", return_value=(session, {"id": 7})) as login, \
                patch("app.new_api_generate_token", return_value={"access_token": "fresh", "user_id": 7}), \
                patch("app.get_new_api_quota_unit", return_value=app.Decimal("500000")), \
                patch("app.safe_new_api_recharge_logs", return_value=[]), \
                patch("app.save_channel_credential") as save:
            result = app.fetch_new_api(channel)

        self.assertEqual(str(result["balance"]), "1")
        login.assert_called_once_with("https://example.com/", "demo", "secret123")
        save.assert_called_once()

    def test_new_api_login_explains_turnstile_block(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = FakeResponse({
            "success": True,
            "data": {"turnstile_check": True},
        })

        with patch("app.requests.Session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "Turnstile"):
                app.new_api_login("https://example.com/", "demo", "secret123")

        session.post.assert_not_called()

    def test_new_api_login_recovers_user_id_from_session(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [
            FakeResponse({"success": True, "data": {
                "turnstile_check": False,
                "user_agreement_enabled": True,
                "privacy_policy_enabled": True,
            }}),
            FakeResponse({"success": True, "data": {"id": 42, "username": "demo"}}),
        ]
        session.post.return_value = FakeResponse({"success": True, "data": {}})

        with patch("app.requests.Session", return_value=session):
            _, user = app.new_api_login("https://example.com/", "demo", "secret123")

        self.assertEqual(user["id"], 42)
        self.assertEqual(session.post.call_args.kwargs["json"], {
            "username": "demo",
            "password": "secret123",
        })

    def test_new_api_login_requires_2fa_code_when_upstream_requests_it(self):
        session = NewApiTwoFASession()

        with patch("app.requests.Session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "2FA"):
                app.new_api_login("https://example.com/", "demo", "secret123")

    def test_new_api_recharge_logs_convert_quota_when_needed(self):
        session = FakeSession(FakeResponse({
            "success": True,
            "data": {
                "items": [
                    {"id": 21, "status": "success", "quota": "1000000", "complete_time": 1710000000},
                ]
            },
        }))

        with patch("app.get_new_api_quota_unit", return_value=app.Decimal("500000")):
            logs = app.new_api_recharge_logs(
                "https://example.com/",
                {"access_token": "token", "user_id": 7},
                session=session,
            )

        self.assertEqual(str(logs[0]["amount_usd"]), "2")

    def test_new_api_recharge_logs_parse_redeem_and_admin_balance_logs(self):
        base_url = "https://example.com/"
        session = RouteSession({
            "https://example.com/api/user/topup/self": FakeResponse({"success": True, "data": {"items": []}}),
            ("https://example.com/api/log/self", app.NEW_API_LOG_TYPE_TOPUP): FakeResponse({
                "success": True,
                "data": {
                    "items": [
                        {
                            "created_at": 1710000000,
                            "type": app.NEW_API_LOG_TYPE_TOPUP,
                            "content": "通过兑换码充值 ＄5.000000 额度，兑换码ID 12",
                        }
                    ]
                },
            }),
            ("https://example.com/api/log/self", app.NEW_API_LOG_TYPE_MANAGE): FakeResponse({
                "success": True,
                "data": {
                    "items": [
                        {
                            "created_at": 1710000010,
                            "type": app.NEW_API_LOG_TYPE_MANAGE,
                            "content": "管理员增加用户额度 ＄10.000000 额度",
                        },
                        {
                            "created_at": 1710000020,
                            "type": app.NEW_API_LOG_TYPE_MANAGE,
                            "content": "管理员覆盖用户额度从 ＄1.000000 额度 为 ＄3.500000 额度",
                        },
                        {
                            "created_at": 1710000030,
                            "type": app.NEW_API_LOG_TYPE_MANAGE,
                            "content": "管理员减少用户额度 ＄2.000000 额度",
                        },
                    ]
                },
            }),
        })

        logs = app.new_api_recharge_logs(
            base_url,
            {"access_token": "token", "user_id": 7},
            session=session,
        )

        self.assertEqual([log["source_type"] for log in logs], ["redemption", "admin_add_quota", "admin_set_quota"])
        self.assertEqual([str(log["amount_usd"]) for log in logs], ["5.000000", "10.000000", "2.500000"])
        self.assertEqual(session.calls[1][1]["params"]["type"], app.NEW_API_LOG_TYPE_TOPUP)
        self.assertEqual(session.calls[2][1]["params"]["type"], app.NEW_API_LOG_TYPE_MANAGE)

    def test_logged_quota_values_convert_tokens_to_usd(self):
        values = app.logged_quota_values("通过兑换码充值 1000000 点额度，兑换码ID 7", app.Decimal("500000"))
        self.assertEqual(str(values[0]), "2")

    def test_balance_delta_recharge_rounds_to_nearest_hundred(self):
        previous = {"balance": "832"}
        result = {"balance": app.Decimal("1323.2"), "recharge_logs": []}
        log = app.balance_delta_recharge_log(8, previous, result, "2026-06-19T00:00:00+00:00")

        self.assertEqual(str(log["amount_usd"]), "500")
        self.assertEqual(log["before_balance"], "832")
        self.assertEqual(log["after_balance"], app.Decimal("1323.2"))
        self.assertEqual(log["source_status"], "inferred")

    def test_balance_delta_recharge_skips_when_upstream_log_matches(self):
        previous = {"balance": "832"}
        result = {
            "balance": app.Decimal("1323.2"),
            "recharge_logs": [{"amount_usd": app.Decimal("500")}],
        }

        self.assertIsNone(app.balance_delta_recharge_log(8, previous, result, "2026-06-19T00:00:00+00:00"))

    def test_create_channel_persists_after_successful_probe(self):
        ts = app.now_iso()
        username = f"create-channel-{uuid.uuid4().hex}"
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

        result = {
            "balance": app.Decimal("12"),
            "raw_balance": "6000000",
            "used_balance": None,
            "raw_used_balance": None,
            "request_count": None,
            "currency": "USD",
            "status": "ok",
            "message": "",
            "raw_response": {},
            "recharge_logs": [],
        }
        with app.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["id"] = user["id"]
                sess["username"] = user["username"]
                sess["totp_enabled"] = False
            with patch("app.provision_channel", return_value=({"access_token": "token", "user_id": 7}, result)):
                response = client.post("/api/channels", json={
                    "name": "created-demo",
                    "platform": "new_api",
                    "base_url": "https://created.example/",
                    "username": "upstream-user",
                    "password": "upstream-pass",
                    "cny_rate": 7.3,
                    "alert_cny": 100,
                    "boss_recharge_required": True,
                })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertEqual(data["name"], "created-demo")
        self.assertTrue(data["boss_recharge_required"])
        with app.db() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (data["id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["base_url"], "https://created.example/")
        self.assertNotEqual(row["password_enc"], "upstream-pass")
        self.assertEqual(app.decrypt(row["password_enc"]), "upstream-pass")

    def test_create_channel_persists_when_recharge_sync_fails(self):
        ts = app.now_iso()
        username = f"create-sync-fail-{uuid.uuid4().hex}"
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

        result = {
            "balance": app.Decimal("12"),
            "raw_balance": "6000000",
            "used_balance": None,
            "raw_used_balance": None,
            "request_count": None,
            "currency": "USD",
            "status": "ok",
            "message": "",
            "raw_response": {},
            "recharge_logs": [{"amount_usd": app.Decimal("1")}],
        }
        with app.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["id"] = user["id"]
                sess["username"] = user["username"]
                sess["totp_enabled"] = False
            with patch("app.provision_channel", return_value=({"access_token": "token", "user_id": 7}, result)), \
                    patch("app.sync_recharge_logs", side_effect=sqlite3.OperationalError("bad recharge log")):
                response = client.post("/api/channels", json={
                    "name": "created-sync-fail",
                    "platform": "new_api",
                    "base_url": "https://created-sync-fail.example/",
                    "username": "upstream-user",
                    "password": "upstream-pass",
                    "cny_rate": 7.3,
                    "alert_cny": 100,
                })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        with app.db() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id = ?", (data["id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "created-sync-fail")

    def test_create_channel_empty_url_returns_json_error(self):
        ts = app.now_iso()
        username = f"create-empty-url-{uuid.uuid4().hex}"
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

        with app.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["id"] = user["id"]
                sess["username"] = user["username"]
                sess["totp_enabled"] = False
            response = client.post("/api/channels", json={
                "platform": "new_api",
                "base_url": "",
                "username": "upstream-user",
                "password": "upstream-pass",
            })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "URL 不能为空")

    def test_safe_new_api_recharge_logs_returns_empty_on_failure(self):
        with patch("app.new_api_recharge_logs", side_effect=RuntimeError("topup failed")):
            self.assertEqual(app.safe_new_api_recharge_logs("https://example.com/", {}), [])

        with patch("app.new_api_recharge_logs", side_effect=requests.RequestException("timeout")):
            self.assertEqual(app.safe_new_api_recharge_logs("https://example.com/", {}), [])

    def test_fetch_new_api_keeps_balance_when_recharge_logs_fail(self):
        channel = {"id": 1, "base_url": "https://example.com/", "credential_enc": "", "password_enc": ""}
        credential = {"access_token": "token", "user_id": 7}
        payload = {"quota": "5000000", "used_quota": "500000"}

        with patch("app.load_credential", return_value=credential), \
                patch("app.new_api_self", return_value=payload), \
                patch("app.get_new_api_quota_unit", return_value=app.Decimal("500000")), \
                patch("app.new_api_recharge_logs", side_effect=RuntimeError("topup failed")):
            result = app.fetch_new_api(channel)

        self.assertEqual(str(result["balance"]), "10")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["recharge_logs"], [])

    def test_post_feishu_text_payload(self):
        response = Mock(status_code=200, text='{"code":0}')
        response.json.return_value = {"code": 0}

        with patch("app.feishu_webhook", return_value="https://open.feishu.cn/open-apis/bot/v2/hook/token"), \
                patch("app.requests.post", return_value=response) as post:
            self.assertTrue(app.post_feishu_text("hello"))

        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"msg_type": "text", "content": {"text": "hello"}})

    def test_post_notification_text_ignores_failed_webhook(self):
        with patch("app.post_wecom_text", side_effect=requests.RequestException("timeout")), \
                patch("app.post_feishu_text", return_value=True):
            self.assertEqual(app.post_notification_text("hello"), ["feishu"])

    def test_low_balance_alert_sends_email_alongside_webhook(self):
        channel = {
            "id": 9001,
            "status": "ok",
            "name": "low-demo",
            "base_url": "https://low.example/",
            "balance": 1,
            "cny_balance": 1,
            "alert_cny": 100,
        }
        with patch("app.setting_get", side_effect=lambda key, default="": "1" if key == "notify_enabled" else default), \
                patch("app.setting_set") as setting_set, \
                patch("app.notification_webhooks_configured", return_value=True), \
                patch("app.post_notification_text", return_value=["wecom"]) as notify, \
                patch("app.post_low_balance_email", return_value=True) as email:
            sent = app.send_low_balance_alerts([channel])

        self.assertEqual(sent, [channel])
        notify.assert_called_once()
        email.assert_called_once()
        content = notify.call_args.args[0]
        self.assertEqual(content, "渠道名: low-demo\n余额: 1 CNY\n阈值: 100 CNY")
        self.assertEqual(email.call_args.args[1], content)
        setting_set.assert_called_once()

    def test_catalog_balance_alert_uses_ledger_estimate(self):
        channel = {
            "id": 9201,
            "name": "ledger-demo",
            "alias": "主渠道",
            "balance_configured": True,
            "present_in_source": True,
            "estimated_balance": 8.5,
            "alert_balance": 20,
            "spent_since_calibration": 11.5,
        }
        with patch("app.setting_get", side_effect=lambda key, default="": "1" if key == "notify_enabled" else default), \
                patch("app.setting_set") as setting_set, \
                patch("app.notification_webhooks_configured", return_value=True), \
                patch("app.post_notification_text", return_value=["wecom"]) as notify, \
                patch("app.post_low_balance_email", return_value=False):
            sent = app.send_catalog_balance_alerts({"items": [channel]})

        self.assertEqual(sent, [channel])
        self.assertEqual(
            notify.call_args.args[0],
            "渠道名: 主渠道\n账本余额: 8.5 USD\n告警线: 20 USD\n本期扣减: 11.5 USD\n来源: New API 小时消耗估算",
        )
        setting_set.assert_called_once()

    def test_catalog_balance_recovery_clears_alert_state(self):
        channel = {
            "id": 9202,
            "balance_configured": True,
            "present_in_source": True,
            "estimated_balance": 35,
            "alert_balance": 20,
        }
        with patch("app.setting_get", side_effect=lambda key, default="": "1" if key == "notify_enabled" else default), \
                patch("app.setting_delete") as setting_delete, \
                patch("app.post_notification_text") as notify:
            sent = app.send_catalog_balance_alerts({"items": [channel]})

        self.assertEqual(sent, [])
        setting_delete.assert_called_once_with(app.catalog_balance_alert_key(9202))
        notify.assert_not_called()

    def test_email_test_requires_login_and_sends_configured_email(self):
        ts = app.now_iso()
        username = f"email-test-{uuid.uuid4().hex}"
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

        with app.app.test_client() as client:
            self.assertEqual(client.post("/api/settings/test-email", json={}).status_code, 401)
            with client.session_transaction() as sess:
                sess["id"] = user["id"]
                sess["username"] = user["username"]
                sess["totp_enabled"] = False
            with patch("app.low_balance_email_configured", return_value=True), \
                    patch("app.post_low_balance_email", return_value=True) as email:
                response = client.post("/api/settings/test-email", json={})

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        email.assert_called_once()
        subject, content = email.call_args.args
        self.assertEqual(subject, "上游余额告警测试")
        expected = "\n".join([
            "渠道名: 邮件测试",
            "余额: 0 CNY",
            f"阈值: {app.format_money(app.as_float(app.LOW_BALANCE_ALERT_CNY))} CNY",
        ])
        self.assertEqual(content, expected)

    def test_email_test_skips_send_when_email_is_unconfigured(self):
        ts = app.now_iso()
        username = f"email-unconfigured-{uuid.uuid4().hex}"
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()

        with app.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["id"] = user["id"]
                sess["username"] = user["username"]
                sess["totp_enabled"] = False
            with patch("app.low_balance_email_configured", return_value=False), \
                    patch("app.post_low_balance_email") as email:
                response = client.post("/api/settings/test-email", json={})

        self.assertEqual(response.status_code, 400)
        email.assert_not_called()

    def test_register_api_aliases_exposes_ub_prefix(self):
        rules = [str(rule) for rule in app.app.url_map.iter_rules() if str(rule).startswith("/_ub_api/")]
        self.assertIn("/_ub_api/auth/bootstrap", rules)
        self.assertIn("/_ub_api/settings", rules)
        self.assertIn("/_ub_api/settings/test-email", rules)


if __name__ == "__main__":
    unittest.main()
