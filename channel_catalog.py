import json
import secrets
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('backup', 'manual')),
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            name TEXT NOT NULL,
            alias TEXT NOT NULL DEFAULT '',
            channel_type INTEGER,
            status INTEGER,
            base_url TEXT NOT NULL DEFAULT '',
            models_json TEXT NOT NULL DEFAULT '[]',
            group_name TEXT NOT NULL DEFAULT '',
            priority INTEGER,
            weight INTEGER,
            balance REAL,
            response_time INTEGER,
            source_tag TEXT NOT NULL DEFAULT '',
            remark TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            local_tags TEXT NOT NULL DEFAULT '',
            present_in_source INTEGER NOT NULL DEFAULT 1,
            synced_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, source_id)
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(channel_catalog)").fetchall()}
    additions = {
        "used_quota": "INTEGER",
        "balance_updated_time": "INTEGER",
        "quota_per_unit": "REAL NOT NULL DEFAULT 500000",
        "ledger_balance": "REAL",
        "ledger_baseline_used_quota": "INTEGER",
        "ledger_calibrated_at": "TEXT",
        "alert_balance": "REAL NOT NULL DEFAULT 20",
        "balance_currency": "TEXT NOT NULL DEFAULT 'USD'",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE channel_catalog ADD COLUMN {name} {declaration}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_catalog_sync (
            source TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            item_count INTEGER NOT NULL
        )
        """
    )


def _models(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sync_payload(conn, payload):
    ensure_schema(conn)
    source = str(payload.get("source") or "newapi_bluegreen").strip()
    generated_at = str(payload.get("generated_at") or now_iso()).strip()
    channels = payload.get("channels")
    quota_per_unit = _as_float(payload.get("quota_per_unit")) or 500000.0
    if not source or not isinstance(channels, list):
        raise ValueError("渠道清单格式不正确")

    synced_at = now_iso()
    conn.execute(
        "UPDATE channel_catalog SET present_in_source = 0, updated_at = ? WHERE source_kind = 'backup' AND source = ?",
        (synced_at, source),
    )
    imported = 0
    for item in channels:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            continue
        source_id = str(item["id"])
        name = str(item.get("name") or f"渠道 {source_id}").strip()
        base_url = str(item.get("base_url") or "").strip()
        models_json = json.dumps(_models(item.get("models")), ensure_ascii=False)
        values = (
            source,
            source_id,
            name,
            _as_int(item.get("type")),
            _as_int(item.get("status")),
            base_url,
            models_json,
            str(item.get("group") or "").strip(),
            _as_int(item.get("priority")),
            _as_int(item.get("weight")),
            _as_float(item.get("balance")),
            _as_int(item.get("response_time")),
            str(item.get("tag") or "").strip(),
            str(item.get("remark") or "").strip(),
            _as_int(item.get("used_quota")) or 0,
            _as_int(item.get("balance_updated_time")) or 0,
            quota_per_unit,
            synced_at,
            synced_at,
            synced_at,
        )
        conn.execute(
            """
            INSERT INTO channel_catalog(
                source_kind, source, source_id, name, channel_type, status, base_url,
                models_json, group_name, priority, weight, balance, response_time,
                source_tag, remark, used_quota, balance_updated_time, quota_per_unit,
                present_in_source, synced_at, created_at, updated_at
            )
            VALUES('backup', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                name = excluded.name,
                channel_type = excluded.channel_type,
                status = excluded.status,
                base_url = excluded.base_url,
                models_json = excluded.models_json,
                group_name = excluded.group_name,
                priority = excluded.priority,
                weight = excluded.weight,
                balance = excluded.balance,
                response_time = excluded.response_time,
                source_tag = excluded.source_tag,
                remark = excluded.remark,
                used_quota = excluded.used_quota,
                balance_updated_time = excluded.balance_updated_time,
                quota_per_unit = excluded.quota_per_unit,
                present_in_source = 1,
                synced_at = excluded.synced_at,
                updated_at = excluded.updated_at
            """,
            values,
        )
        imported += 1

    conn.execute(
        """
        INSERT INTO channel_catalog_sync(source, generated_at, synced_at, item_count)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            generated_at = excluded.generated_at,
            synced_at = excluded.synced_at,
            item_count = excluded.item_count
        """,
        (source, generated_at, synced_at, imported),
    )
    return imported


def sync_file(conn, path):
    if not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = str(payload.get("source") or "newapi_bluegreen").strip()
    generated_at = str(payload.get("generated_at") or "").strip()
    ensure_schema(conn)
    current = conn.execute(
        "SELECT generated_at FROM channel_catalog_sync WHERE source = ?", (source,)
    ).fetchone()
    if current and generated_at and current["generated_at"] == generated_at:
        return False
    sync_payload(conn, payload)
    return True


def create_manual(conn, payload):
    ensure_schema(conn)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("渠道名不能为空")
    ts = now_iso()
    source_id = secrets.token_hex(8)
    cur = conn.execute(
        """
        INSERT INTO channel_catalog(
            source_kind, source, source_id, name, alias, channel_type, status,
            base_url, models_json, group_name, owner, note, local_tags,
            present_in_source, synced_at, created_at, updated_at
        ) VALUES('manual', 'manual', ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            source_id,
            name,
            _as_int(payload.get("type")),
            _as_int(payload.get("status")) if payload.get("status") is not None else 1,
            str(payload.get("base_url") or "").strip(),
            json.dumps(_models(payload.get("models")), ensure_ascii=False),
            str(payload.get("group") or "").strip(),
            str(payload.get("owner") or "").strip(),
            str(payload.get("note") or "").strip(),
            str(payload.get("tags") or "").strip(),
            ts,
            ts,
            ts,
        ),
    )
    if payload.get("ledger_balance") not in (None, "") or payload.get("alert_balance") not in (None, ""):
        update_local_fields(conn, cur.lastrowid, payload)
    return cur.lastrowid


def update_local_fields(conn, catalog_id, payload):
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM channel_catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        raise LookupError("渠道不存在")
    editable = {
        "alias": str(payload.get("alias", row["alias"] or "")).strip(),
        "owner": str(payload.get("owner", row["owner"] or "")).strip(),
        "note": str(payload.get("note", row["note"] or "")).strip(),
        "local_tags": str(payload.get("tags", row["local_tags"] or "")).strip(),
    }
    if "ledger_balance" in payload and payload.get("ledger_balance") not in (None, ""):
        balance = _as_float(payload.get("ledger_balance"))
        if balance is None or balance < 0:
            raise ValueError("当前余额需要填写大于等于 0 的数字")
        editable.update({
            "ledger_balance": balance,
            "ledger_baseline_used_quota": row["used_quota"] or 0,
            "ledger_calibrated_at": now_iso(),
        })
    if "alert_balance" in payload and payload.get("alert_balance") not in (None, ""):
        threshold = _as_float(payload.get("alert_balance"))
        if threshold is None or threshold < 0:
            raise ValueError("告警阈值需要填写大于等于 0 的数字")
        editable["alert_balance"] = threshold
    if row["source_kind"] == "manual":
        editable.update({
            "name": str(payload.get("name", row["name"])).strip() or row["name"],
            "base_url": str(payload.get("base_url", row["base_url"] or "")).strip(),
            "group_name": str(payload.get("group", row["group_name"] or "")).strip(),
            "models_json": json.dumps(_models(payload.get("models", json.loads(row["models_json"] or "[]"))), ensure_ascii=False),
            "status": _as_int(payload.get("status")) if payload.get("status") is not None else row["status"],
        })
    assignments = ", ".join(f"{key} = ?" for key in editable)
    conn.execute(
        f"UPDATE channel_catalog SET {assignments}, updated_at = ? WHERE id = ?",
        (*editable.values(), now_iso(), catalog_id),
    )


def delete_manual(conn, catalog_id):
    ensure_schema(conn)
    row = conn.execute("SELECT source_kind FROM channel_catalog WHERE id = ?", (catalog_id,)).fetchone()
    if not row:
        raise LookupError("渠道不存在")
    if row["source_kind"] != "manual":
        raise ValueError("同步渠道不能删除，可在 New API 中停用")
    conn.execute("DELETE FROM channel_catalog WHERE id = ?", (catalog_id,))


def _row(row):
    item = dict(row)
    try:
        item["models"] = json.loads(item.pop("models_json") or "[]")
    except json.JSONDecodeError:
        item["models"] = []
    item["present_in_source"] = bool(item["present_in_source"])
    item["balance_configured"] = item.get("ledger_balance") is not None and item.get("ledger_baseline_used_quota") is not None
    if item["balance_configured"]:
        quota_per_unit = float(item.get("quota_per_unit") or 500000)
        used_delta = max(0, int(item.get("used_quota") or 0) - int(item.get("ledger_baseline_used_quota") or 0))
        item["spent_since_calibration"] = used_delta / quota_per_unit
        item["estimated_balance"] = float(item["ledger_balance"]) - item["spent_since_calibration"]
    else:
        item["spent_since_calibration"] = 0.0
        item["estimated_balance"] = None
    return item


def list_catalog(conn):
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM channel_catalog
        ORDER BY present_in_source DESC, source_kind DESC, priority DESC, id DESC
        """
    ).fetchall()
    sync_rows = conn.execute(
        "SELECT source, generated_at, synced_at, item_count FROM channel_catalog_sync ORDER BY source"
    ).fetchall()
    items = [_row(row) for row in rows]
    monitored = [item for item in items if item["balance_configured"]]
    low = [item for item in monitored if item["estimated_balance"] <= float(item.get("alert_balance") or 0)]
    return {
        "items": items,
        "syncs": [dict(row) for row in sync_rows],
        "summary": {
            "total": len(items),
            "synced": sum(1 for item in items if item["source_kind"] == "backup" and item["present_in_source"]),
            "manual": sum(1 for item in items if item["source_kind"] == "manual"),
            "disabled": sum(1 for item in items if item["present_in_source"] and item["status"] != 1),
            "missing": sum(1 for item in items if item["source_kind"] == "backup" and not item["present_in_source"]),
            "monitored": len(monitored),
            "unmonitored": len(items) - len(monitored),
            "low_balance": len(low),
            "estimated_total": sum(item["estimated_balance"] for item in monitored),
        },
    }
