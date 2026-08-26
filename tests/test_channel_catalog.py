import sqlite3
import unittest

import channel_catalog


class ChannelCatalogTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        channel_catalog.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_backup_sync_updates_system_fields_and_preserves_local_notes(self):
        first = {
            "source": "newapi_bluegreen",
            "generated_at": "2026-08-25T10:00:00Z",
            "quota_per_unit": 500,
            "channels": [
                {"id": 12, "name": "A", "status": 1, "models": "m1,m2", "group": "default", "used_quota": 1000},
                {"id": 13, "name": "B", "status": 1, "models": "m3"},
            ],
        }
        self.assertEqual(channel_catalog.sync_payload(self.conn, first), 2)
        row = self.conn.execute("SELECT id FROM channel_catalog WHERE source_id = '12'").fetchone()
        channel_catalog.update_local_fields(self.conn, row["id"], {
            "alias": "主渠道", "owner": "小陈", "note": "合同到年底", "tags": "核心",
            "ledger_balance": 100, "alert_balance": 20,
        })

        second = {
            "source": "newapi_bluegreen",
            "generated_at": "2026-08-25T11:00:00Z",
            "quota_per_unit": 500,
            "channels": [{"id": 12, "name": "A2", "status": 3, "models": "m1,m4", "used_quota": 1750}],
        }
        channel_catalog.sync_payload(self.conn, second)
        data = channel_catalog.list_catalog(self.conn)
        current = next(item for item in data["items"] if item["source_id"] == "12")
        missing = next(item for item in data["items"] if item["source_id"] == "13")

        self.assertEqual(current["name"], "A2")
        self.assertEqual(current["models"], ["m1", "m4"])
        self.assertEqual(current["alias"], "主渠道")
        self.assertEqual(current["owner"], "小陈")
        self.assertEqual(current["note"], "合同到年底")
        self.assertEqual(current["spent_since_calibration"], 1.5)
        self.assertEqual(current["estimated_balance"], 98.5)
        self.assertFalse(missing["present_in_source"])
        self.assertEqual(data["summary"], {
            "total": 2, "synced": 1, "manual": 0, "disabled": 1, "missing": 1,
            "monitored": 1, "unmonitored": 1, "low_balance": 0, "estimated_total": 98.5,
        })

    def test_manual_channel_is_independent_from_backup_sync(self):
        manual_id = channel_catalog.create_manual(self.conn, {
            "name": "线下供应商", "base_url": "https://manual.example", "models": "m5",
            "owner": "小李", "note": "微信对接", "tags": "备用",
            "ledger_balance": 50, "alert_balance": 10,
        })
        channel_catalog.sync_payload(self.conn, {
            "source": "newapi_bluegreen",
            "generated_at": "2026-08-25T11:00:00Z",
            "channels": [],
        })
        data = channel_catalog.list_catalog(self.conn)
        item = next(item for item in data["items"] if item["id"] == manual_id)
        self.assertEqual(item["source_kind"], "manual")
        self.assertTrue(item["present_in_source"])
        self.assertEqual(item["owner"], "小李")
        self.assertEqual(item["estimated_balance"], 50)

        channel_catalog.delete_manual(self.conn, manual_id)
        self.assertEqual(channel_catalog.list_catalog(self.conn)["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
