"""Domain implementation loaded into the shared Flask application namespace."""

def recharge_url_for(platform, base_url):
    path = "/purchase" if platform == "sub2api" else "/console/topup"
    return join_url_path(base_url, path)


def recharge_admin_url_for(platform, base_url):
    path = "/admin/orders" if platform == "sub2api" else "/console/log"
    return join_url_path(base_url, path)


def list_recharge_logs(channel_id=None, limit=80):
    args = []
    where = ""
    if channel_id:
        where = "WHERE r.channel_id = ?"
        args.append(channel_id)
    args.append(limit)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT r.id,
                   r.channel_id,
                   c.name AS channel_name,
                   c.base_url,
                   r.before_balance,
                   r.after_balance,
                   r.amount_usd,
                   r.amount_cny,
                   r.cny_rate,
                   r.detected_at,
                   r.source_status,
                   r.source_type,
                   r.created_at
            FROM recharge_logs r
            JOIN channels c ON c.id = r.channel_id
            {where}
            ORDER BY r.detected_at DESC, r.id DESC
            LIMIT ?
            """,
            args,
        ).fetchall()
    return [dict(row) for row in rows]


def source_hash(*parts):
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_recharge_log(conn, channel_id, log, cny_rate):
    amount_usd = decimal_from(log.get("amount_usd"))
    if amount_usd is None or amount_usd <= 0:
        return False
    rate = rate_from(cny_rate)
    before_balance = decimal_from(log.get("before_balance"))
    after_balance = decimal_from(log.get("after_balance"))
    if after_balance is None:
        after_balance = amount_usd
    detected_at = log.get("detected_at") or now_iso()
    source_ref = log.get("source_ref") or source_hash(channel_id, amount_usd, detected_at, log.get("source_status"))
    conn.execute(
        """
        INSERT INTO recharge_logs(
            channel_id, before_balance, after_balance, amount_usd, amount_cny, cny_rate,
            detected_at, source_ref, source_status, source_type, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, source_ref) DO UPDATE SET
            amount_usd = excluded.amount_usd,
            amount_cny = excluded.amount_cny,
            cny_rate = excluded.cny_rate,
            detected_at = excluded.detected_at,
            source_status = excluded.source_status,
            source_type = excluded.source_type
        """,
        (
            channel_id,
            as_float(before_balance),
            as_float(after_balance),
            as_float(amount_usd),
            as_float(amount_usd / rate),
            as_float(rate),
            detected_at,
            source_ref,
            log.get("source_status", ""),
            log.get("source_type", ""),
            detected_at,
        ),
    )
    return True


def inferred_recharge_amount(before_balance, after_balance):
    before = decimal_from(before_balance)
    after = decimal_from(after_balance)
    if before is None or after is None or after <= before:
        return None
    delta = after - before
    if RECHARGE_ROUNDING_UNIT <= 0:
        return delta
    rounded = (delta / RECHARGE_ROUNDING_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * RECHARGE_ROUNDING_UNIT
    if rounded <= 0:
        return None
    return rounded


def has_matching_recharge_log(logs, amount, tolerance=Decimal("0.01")):
    target = decimal_from(amount)
    if target is None:
        return False
    for log in logs or []:
        logged_amount = decimal_from(log.get("amount_usd"))
        if logged_amount is not None and abs(logged_amount - target) <= tolerance:
            return True
    return False


def balance_delta_recharge_log(channel_id, previous_row, result, checked_at):
    before_balance = channel_value(previous_row, "balance") if previous_row else None
    after_balance = result.get("balance")
    amount = inferred_recharge_amount(before_balance, after_balance)
    if amount is None or has_matching_recharge_log(result.get("recharge_logs") or [], amount):
        return None
    return {
        "before_balance": before_balance,
        "after_balance": after_balance,
        "amount_usd": amount,
        "detected_at": checked_at,
        "source_ref": source_hash(
            "balance_delta",
            channel_id,
            before_balance,
            after_balance,
            checked_at,
            amount,
        ),
        "source_status": "inferred",
        "source_type": f"balance_delta_nearest_{format_money(as_float(RECHARGE_ROUNDING_UNIT))}",
    }


def sync_recharge_logs(conn, channel, logs):
    if not logs:
        return 0
    count = 0
    for log in logs:
        if record_recharge_log(conn, channel["id"], log, channel["cny_rate"]):
            count += 1
    return count


def safe_sync_recharge_logs(conn, channel, logs):
    try:
        return sync_recharge_logs(conn, channel, logs)
    except Exception as exc:
        print(
            f"[recharge-sync] channel_id={channel['id']} failed: {exc}",
            flush=True,
        )
        return 0
