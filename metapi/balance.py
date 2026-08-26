"""Domain implementation loaded into the shared Flask application namespace."""

def history_cutoff_iso():
    return (datetime.now(timezone.utc) - timedelta(hours=HISTORY_RETENTION_HOURS)).isoformat()


def list_balance_history(channel_id):
    cutoff = history_cutoff_iso()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT balance, used_balance, currency, status, message, checked_at
            FROM balance_history
            WHERE channel_id = ? AND checked_at >= ?
            ORDER BY checked_at ASC
            """,
            (channel_id, cutoff),
        ).fetchall()
    return [dict(row) for row in rows]


def record_balance_history(conn, channel_id, result, checked_at):
    conn.execute(
        """
        INSERT INTO balance_history(channel_id, balance, used_balance, currency, status, message, checked_at, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            channel_id,
            as_float(result.get("balance")),
            as_float(result.get("used_balance")),
            result.get("currency", "USD"),
            result.get("status", "ok"),
            result.get("message", ""),
            checked_at,
            checked_at,
        ),
    )
    aggregate_hourly_history(conn, channel_id, result, checked_at)


def record_failure_history(conn, channel_id, row, error, checked_at):
    conn.execute(
        """
        INSERT INTO balance_history(channel_id, balance, used_balance, currency, status, message, checked_at, created_at)
        VALUES(?, ?, ?, ?, 'error', ?, ?, ?)
        """,
        (
            channel_id,
            row["balance"] if row else None,
            row["used_balance"] if row else None,
            row["currency"] if row else "USD",
            str(error)[:1000],
            checked_at,
            checked_at,
        ),
    )
    aggregate_hourly_history(conn, channel_id, {
        "balance": row["balance"] if row else None,
        "used_balance": row["used_balance"] if row else None,
        "status": "error",
    }, checked_at)


def aggregate_hourly_history(conn, channel_id, result, checked_at):
    """Upsert one durable hourly sample while retaining the raw 72h stream."""
    try:
        hour = datetime.fromisoformat(checked_at).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    except ValueError:
        hour = checked_at[:13] + ":00:00+00:00"
    conn.execute(
        """
        INSERT INTO balance_history_hourly(channel_id, hour, balance, used_balance, status, sample_count, created_at)
        VALUES(?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(channel_id, hour) DO UPDATE SET
            balance=excluded.balance, used_balance=excluded.used_balance,
            status=excluded.status, sample_count=balance_history_hourly.sample_count + 1
        """,
        (channel_id, hour, as_float(result.get("balance")), as_float(result.get("used_balance")), result.get("status", "unknown"), checked_at),
    )


def prune_history(conn):
    conn.execute("DELETE FROM balance_history WHERE checked_at < ?", (history_cutoff_iso(),))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HOURLY_HISTORY_RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM balance_history_hourly WHERE hour < ?", (cutoff,))


def row_to_channel(row, include_secret=False):
    item = dict(row)
    item.pop("password_enc", None)
    item.pop("credential_enc", None)
    item["enabled"] = bool(item["enabled"])
    item["boss_recharge_required"] = bool(item.get("boss_recharge_required"))
    item["cny_rate"] = as_float(rate_from(item.get("cny_rate")))
    item["alert_cny"] = as_float(alert_threshold_from(item.get("alert_cny")))
    item["cny_balance"] = cny_value(item.get("balance"), item["cny_rate"])
    item["cny_used_balance"] = cny_value(item.get("used_balance"), item["cny_rate"])
    item["recharge_url"] = recharge_url_for(item["platform"], item["base_url"])
    item["recharge_admin_url"] = recharge_admin_url_for(item["platform"], item["base_url"])
    item["history"] = list_balance_history(item["id"])
    item["recharge_logs"] = list_recharge_logs(item["id"], 12)
    if item.get("raw_response"):
        try:
            item["raw_response"] = json.loads(item["raw_response"])
        except json.JSONDecodeError:
            pass
    if include_secret:
        item["password"] = ""
    return item


def list_channels():
    with db() as conn:
        rows = conn.execute("SELECT * FROM channels ORDER BY id DESC").fetchall()
    return [row_to_channel(row) for row in rows]


def get_channel(channel_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return row


def persist_result(channel_id, result):
    checked_at = now_iso()
    with db() as conn:
        previous_row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        logs = list(result.get("recharge_logs") or [])
        inferred_log = balance_delta_recharge_log(channel_id, previous_row, result, checked_at)
        if inferred_log:
            logs.append(inferred_log)
        conn.execute(
            """
            UPDATE channels
            SET balance = ?,
                raw_balance = ?,
                used_balance = ?,
                raw_used_balance = ?,
                request_count = ?,
                currency = ?,
                status = ?,
                message = ?,
                raw_response = ?,
                last_checked_at = ?,
                updated_at = ?,
                refresh_failures = 0,
                next_refresh_at = NULL
            WHERE id = ?
            """,
            (
                as_float(result.get("balance")),
                result.get("raw_balance"),
                as_float(result.get("used_balance")),
                result.get("raw_used_balance"),
                result.get("request_count"),
                result.get("currency", "USD"),
                result.get("status", "ok"),
                result.get("message", ""),
                json.dumps(result.get("raw_response") or {}, ensure_ascii=False),
                checked_at,
                checked_at,
                channel_id,
            ),
        )
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if row:
            sync_recharge_logs(conn, row, logs)
        record_balance_history(conn, channel_id, result, checked_at)
        prune_history(conn)


def persist_failure(channel_id, error):
    checked_at = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        conn.execute(
            """
            UPDATE channels
            SET status = 'error',
                message = ?,
                last_checked_at = ?,
                updated_at = ?,
                refresh_failures = COALESCE(refresh_failures, 0) + 1,
                next_refresh_at = CASE WHEN COALESCE(refresh_failures, 0) + 1 >= 3
                    THEN datetime('now', '+1 hour') ELSE NULL END
            WHERE id = ?
            """,
            (str(error)[:1000], checked_at, checked_at, channel_id),
        )
        record_failure_history(conn, channel_id, row, error, checked_at)
        prune_history(conn)


def refresh_one(channel_id):
    row = get_channel(channel_id)
    if not row:
        raise RuntimeError("渠道不存在")
    result = fetch_channel(row)
    persist_result(channel_id, result)
    return row_to_channel(get_channel(channel_id))


def refresh_all(send_notify=False):
    with refresh_lock:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM channels WHERE enabled = 1 AND (next_refresh_at IS NULL OR julianday(next_refresh_at) <= julianday('now')) ORDER BY id"
            ).fetchall()
        results = []
        def refresh_row(row):
            try:
                refreshed = refresh_one(row["id"])
                return refreshed
            except Exception as exc:
                persist_failure(row["id"], exc)
                return row_to_channel(get_channel(row["id"]))
        max_workers = min(8, max(1, len(rows)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(refresh_row, rows))
        # Include skipped channels in notification/status responses.
        with db() as conn:
            skipped = conn.execute(
                "SELECT * FROM channels WHERE enabled = 1 AND julianday(next_refresh_at) > julianday('now') ORDER BY id"
            ).fetchall()
        results.extend(row_to_channel(row) for row in skipped)
        send_low_balance_alerts(results)
        send_channel_error_alerts(results)
        if send_notify and setting_get("notify_enabled", "1") == "1":
            send_notification_summary(results)
        return results
