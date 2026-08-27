"""Domain implementation loaded into the shared Flask application namespace."""

def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)


def get_cipher():
    ensure_data_dir()
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
        os.chmod(KEY_PATH, 0o600)
    return Fernet(KEY_PATH.read_bytes())


def encrypt(value):
    if value is None or value == "":
        return ""
    return get_cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value):
    if not value:
        return ""
    return get_cipher().decrypt(value.encode("utf-8")).decode("utf-8")


def db():
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    os.chmod(DB_PATH, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        channel_catalog.ensure_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT NOT NULL CHECK(platform IN ('new_api', 'sub2api')),
                base_url TEXT NOT NULL,
                username TEXT NOT NULL,
                password_enc TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                balance REAL,
                raw_balance TEXT,
                used_balance REAL,
                raw_used_balance TEXT,
                request_count INTEGER,
                currency TEXT NOT NULL DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'unknown',
                message TEXT,
                raw_response TEXT,
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        auth_security.ensure_schema(conn)
        ensure_column(conn, "channels", "credential_enc", "TEXT")
        ensure_column(conn, "channels", "cny_rate", "REAL")
        ensure_column(conn, "channels", "alert_cny", "REAL")
        ensure_column(conn, "channels", "boss_recharge_required", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "channels", "refresh_failures", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "channels", "next_refresh_at", "TEXT")
        conn.execute(
            """
            UPDATE channels
            SET cny_rate = ?
            WHERE cny_rate IS NULL
            """,
            (as_float(DEFAULT_CNY_RATE),),
        )
        conn.execute(
            """
            UPDATE channels
            SET alert_cny = ?
            WHERE alert_cny IS NULL
            """,
            (as_float(LOW_BALANCE_ALERT_CNY),),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                balance REAL,
                used_balance REAL,
                currency TEXT NOT NULL DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'unknown',
                message TEXT,
                checked_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_balance_history_channel_time
            ON balance_history(channel_id, checked_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS balance_history_hourly (
                channel_id INTEGER NOT NULL,
                hour TEXT NOT NULL,
                balance REAL,
                used_balance REAL,
                status TEXT NOT NULL DEFAULT 'unknown',
                sample_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                PRIMARY KEY(channel_id, hour),
                FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_balance_history_hourly_time ON balance_history_hourly(hour)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recharge_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                before_balance REAL,
                after_balance REAL NOT NULL,
                amount_usd REAL NOT NULL,
                amount_cny REAL NOT NULL,
                cny_rate REAL NOT NULL,
                detected_at TEXT NOT NULL,
                source_ref TEXT,
                source_status TEXT,
                source_type TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )
            """
        )
        ensure_column(conn, "recharge_logs", "source_ref", "TEXT")
        ensure_column(conn, "recharge_logs", "source_status", "TEXT")
        ensure_column(conn, "recharge_logs", "source_type", "TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recharge_logs_channel_time
            ON recharge_logs(channel_id, detected_at)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recharge_logs_channel_source
            ON recharge_logs(channel_id, source_ref)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_discovery (
                base_url TEXT PRIMARY KEY,
                source_names TEXT NOT NULL DEFAULT '',
                platform TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                message TEXT,
                channel_id INTEGER,
                checked_at TEXT NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_exclusions (
                base_url TEXT PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '手动移除',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO channel_exclusions(base_url, reason, created_at, updated_at)
            SELECT base_url, '已按要求关闭', checked_at, checked_at
            FROM channel_discovery
            WHERE state = 'excluded'
            ON CONFLICT(base_url) DO NOTHING
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                totp_secret TEXT,
                totp_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # OpenCode monitoring was retired. Purge its encrypted credentials and
        # alert state on upgrade instead of leaving unused secrets behind.
        conn.execute("DROP TABLE IF EXISTS opencode_accounts")
        conn.execute("DELETE FROM settings WHERE key = 'opencode_alert_state'")

    (DATA_DIR / "opencode-import.json").unlink(missing_ok=True)


def ensure_column(conn, table, column, column_type):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if column in {row["name"] for row in rows}:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def setting_get(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def setting_delete(key):
    with db() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def public_settings():
    webhook_enc = setting_get("wecom_webhook_enc")
    feishu_webhook_enc = setting_get("feishu_webhook_enc")
    email_recipients = setting_get("low_balance_email_recipients", os.getenv("LOW_BALANCE_EMAIL_RECIPIENTS", ""))
    return {
        "wecom_configured": bool(webhook_enc),
        "feishu_configured": bool(feishu_webhook_enc),
        "email_configured": low_balance_email_configured(email_recipients),
        "low_balance_email_recipients": email_recipients,
        "notify_enabled": setting_get("notify_enabled", "1") == "1",
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "notify_interval_seconds": NOTIFY_INTERVAL_SECONDS,
        "history_retention_hours": HISTORY_RETENTION_HOURS,
        "default_cny_rate": as_float(DEFAULT_CNY_RATE),
        "recharge_rounding_unit": as_float(RECHARGE_ROUNDING_UNIT),
        "low_balance_alert_cny": as_float(LOW_BALANCE_ALERT_CNY),
        "low_balance_alert_cooldown_seconds": LOW_BALANCE_ALERT_COOLDOWN_SECONDS,
        "channel_error_alert_cooldown_seconds": CHANNEL_ERROR_ALERT_COOLDOWN_SECONDS,
    }
