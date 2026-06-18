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


class RechargeAndNotificationTest(unittest.TestCase):
    def test_session_cookie_uses_app_specific_name(self):
        self.assertEqual(app.app.config["SESSION_COOKIE_NAME"], "upstream_balance_session")

    def test_auth_token_required_for_protected_routes(self):
        ts = app.now_iso()
        username = f"token-user-{uuid.uuid4().hex}"
        with app.db() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (username, generate_password_hash("secret123"), ts, ts),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        token = app.issue_auth_token(user)
        with app.app.test_client() as client:
            self.assertEqual(client.get("/api/channels").status_code, 401)
            self.assertEqual(client.get("/api/channels", headers={"Cookie": "session=invalid"}).status_code, 401)
            self.assertEqual(client.get("/api/channels", headers={"X-UB-Auth": token}).status_code, 200)

    def test_alert_threshold_uses_channel_value_then_default(self):
        self.assertEqual(str(app.alert_threshold_from("15")), "15")
        self.assertEqual(str(app.alert_threshold_from(None, "25")), "25")
        self.assertEqual(str(app.alert_threshold_from(None, None)), str(app.LOW_BALANCE_ALERT_CNY))

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

    def test_register_api_aliases_exposes_ub_prefix(self):
        rules = [str(rule) for rule in app.app.url_map.iter_rules() if str(rule).startswith("/_ub_api/")]
        self.assertIn("/_ub_api/auth/bootstrap", rules)
        self.assertIn("/_ub_api/settings", rules)


if __name__ == "__main__":
    unittest.main()
