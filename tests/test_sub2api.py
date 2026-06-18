import unittest
from unittest.mock import patch

import requests

import app


class Sub2APIParsingTest(unittest.TestCase):
    def test_profile_payload_shapes_and_balance_fields(self):
        cases = [
            ({"code": 0, "data": {"balance": "12.5", "used_balance": "1.5"}}, "balance", "used_balance", "12.5", "1.5"),
            ({"data": {"user": {"quota": 8, "used_quota": 2}}}, "quota", "used_quota", "8", "2"),
            ({"data": {"profile": {"credit": "7.25", "quota_used": "3.25"}}}, "credit", "quota_used", "7.25", "3.25"),
            ({"data": {"account": {"credits": "6", "used": "2"}}}, "credits", "used", "6", "2"),
            ({"data": {"current_user": {"remaining_balance": "5", "total_used": "4"}}}, "remaining_balance", "total_used", "5", "4"),
            ({"data": [{"available_balance": "4.5"}]}, "available_balance", None, "4.5", None),
            ({"data": {"wallet_balance": "3.5"}}, "wallet_balance", None, "3.5", None),
            ({"data": {"account_balance": "2.5"}}, "account_balance", None, "2.5", None),
        ]

        for payload, balance_field, used_field, balance, used in cases:
            with self.subTest(balance_field=balance_field):
                result = app.build_sub2api_result(app.extract_sub2api_payload(payload))
                self.assertEqual(str(result["balance"]), balance)
                self.assertEqual(result["raw_response"]["balance_field"], balance_field)
                self.assertEqual(result["raw_response"]["used_balance_field"], used_field)
                if used is not None:
                    self.assertEqual(str(result["used_balance"]), used)

    def test_order_statuses_are_case_insensitive(self):
        for status in ("COMPLETED", "completed", "PAID", "paid", "SUCCESS", "success", "SUCCEEDED", "succeeded"):
            with self.subTest(status=status):
                self.assertTrue(app.successful_sub2api_order(status))

        for status in ("pending", "failed", None, ""):
            with self.subTest(status=status):
                self.assertFalse(app.successful_sub2api_order(status))

    def test_safe_recharge_logs_returns_empty_on_failure(self):
        with patch("app.sub2api_recharge_logs", side_effect=RuntimeError("orders failed")):
            self.assertEqual(app.safe_sub2api_recharge_logs("https://example.com/", "token"), [])

        with patch("app.sub2api_recharge_logs", side_effect=requests.RequestException("timeout")):
            self.assertEqual(app.safe_sub2api_recharge_logs("https://example.com/", "token"), [])

    def test_fetch_with_existing_token_keeps_balance_when_recharge_logs_fail(self):
        channel = {"id": 1, "base_url": "https://example.com/", "credential_enc": "", "password_enc": ""}
        credential = {"access_token": "token"}
        profile = {"data": {"quota": "42"}}

        with patch("app.load_credential", return_value=credential), \
                patch("app.sub2api_profile", return_value=app.extract_sub2api_payload(profile)), \
                patch("app.sub2api_recharge_logs", side_effect=RuntimeError("orders failed")):
            result = app.fetch_sub2api(channel)

        self.assertEqual(str(result["balance"]), "42")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["recharge_logs"], [])


if __name__ == "__main__":
    unittest.main()
