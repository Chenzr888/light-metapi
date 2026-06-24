import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from cryptography.fernet import Fernet
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "upstreams.sqlite3"
KEY_PATH = DATA_DIR / "secret.key"
SESSION_KEY_PATH = DATA_DIR / "session.secret"
STATIC_DIR = ROOT / "static"
REFRESH_INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", "30"))
NOTIFY_INTERVAL_SECONDS = int(os.getenv("NOTIFY_INTERVAL_SECONDS", "3600"))
REQUEST_TIMEOUT = int(os.getenv("UPSTREAM_REQUEST_TIMEOUT", "25"))
HISTORY_RETENTION_HOURS = int(os.getenv("HISTORY_RETENTION_HOURS", "72"))
DEFAULT_CNY_RATE = Decimal(os.getenv("DEFAULT_CNY_RATE", "7.3"))
RECHARGE_ROUNDING_UNIT = Decimal(os.getenv("RECHARGE_ROUNDING_UNIT", "100"))
LOW_BALANCE_ALERT_CNY = Decimal(os.getenv("LOW_BALANCE_ALERT_CNY", "100"))
LOW_BALANCE_ALERT_COOLDOWN_SECONDS = int(os.getenv("LOW_BALANCE_ALERT_COOLDOWN_SECONDS", "21600"))
LOW_BALANCE_EMAIL_ENABLED = os.getenv("LOW_BALANCE_EMAIL_ENABLED", "0") == "1"
LOW_BALANCE_EMAIL_FROM = os.getenv("LOW_BALANCE_EMAIL_FROM", "noreply@mail.sandboxai.top")
LOW_BALANCE_EMAIL_SMTP_SERVER = os.getenv("LOW_BALANCE_EMAIL_SMTP_SERVER", "smtp.resend.com")
LOW_BALANCE_EMAIL_SMTP_PORT = int(os.getenv("LOW_BALANCE_EMAIL_SMTP_PORT", "2587"))
LOW_BALANCE_EMAIL_SMTP_USER = os.getenv("LOW_BALANCE_EMAIL_SMTP_USER", "resend")
LOW_BALANCE_EMAIL_SMTP_TOKEN = os.getenv("LOW_BALANCE_EMAIL_SMTP_TOKEN", "")


def read_or_create_secret(path, size=32):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(secrets.token_bytes(size))
        os.chmod(path, 0o600)
    return path.read_bytes()


app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
app.config.update(
    SECRET_KEY=read_or_create_secret(SESSION_KEY_PATH),
    SESSION_COOKIE_NAME=os.getenv("SESSION_COOKIE_NAME", "ub_admin_session"),
    SESSION_COOKIE_PATH=os.getenv("SESSION_COOKIE_PATH", "/"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
refresh_lock = threading.Lock()


class UbMethodOverrideMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        override = environ.get("HTTP_X_UB_METHOD", "")
        if path.startswith("/_ub_api/") and override:
            environ["REQUEST_METHOD"] = override.upper()
        return self.app(environ, start_response)


app.wsgi_app = UbMethodOverrideMiddleware(app.wsgi_app)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
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
        ensure_column(conn, "channels", "credential_enc", "TEXT")
        ensure_column(conn, "channels", "cny_rate", "REAL")
        ensure_column(conn, "channels", "alert_cny", "REAL")
        ensure_column(conn, "channels", "boss_recharge_required", "INTEGER NOT NULL DEFAULT 0")
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
    }


def normalize_url(base_url):
    value = (base_url or "").strip()
    if not value:
        raise ValueError("URL 不能为空")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/") + "/"


def decimal_from(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def as_float(value):
    if value is None:
        return None
    return float(value)


def rate_from(value):
    rate = decimal_from(value)
    if rate is None or rate <= 0:
        return DEFAULT_CNY_RATE
    return rate


def cny_value(usd_value, cny_rate):
    usd = decimal_from(usd_value)
    if usd is None:
        return None
    return as_float(usd / rate_from(cny_rate))


def alert_threshold_from(value, default=None):
    threshold = decimal_from(value)
    if threshold is not None and threshold > 0:
        return threshold
    fallback = decimal_from(default)
    if fallback is not None and fallback > 0:
        return fallback
    return LOW_BALANCE_ALERT_CNY


def join_url_path(base_url, path):
    base = normalize_url(base_url)
    return urljoin(base, path.lstrip("/"))


def recharge_url_for(platform, base_url):
    path = "/purchase" if platform == "sub2api" else "/console/topup"
    return join_url_path(base_url, path)


def recharge_admin_url_for(platform, base_url):
    path = "/admin/orders" if platform == "sub2api" else "/console/log"
    return join_url_path(base_url, path)


def history_cutoff_iso():
    return (datetime.now(timezone.utc) - timedelta(hours=HISTORY_RETENTION_HOURS)).isoformat()


def response_error(message, status=400):
    return jsonify({"ok": False, "message": message}), status


def log_channel_create(platform, base_url, username, message):
    safe_platform = str(platform or "-")
    safe_base_url = str(base_url or "-")
    safe_username = str(username or "-")
    print(
        f"[channel-create] platform={safe_platform} base_url={safe_base_url} "
        f"username={safe_username} result={message}",
        flush=True,
    )


def request_payload(force=False):
    encoded = request.headers.get("X-UB-Payload")
    if encoded:
        try:
            raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
            return json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}
    return request.get_json(force=force, silent=True) or {}


def user_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user_by_username(username):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def setup_login(user):
    session.permanent = True
    session["id"] = user["id"]
    session["username"] = user["username"]
    session["totp_enabled"] = bool(user["totp_enabled"])


def clear_login():
    session.clear()


def current_user():
    user_id = session.get("id")
    if not user_id:
        return None
    user = get_user(user_id)
    if not user:
        clear_login()
        return None
    return user


def auth_state(user=None):
    active_user = user if user is not None else current_user()
    state = {
        "needs_setup": user_count() == 0,
        "authenticated": bool(active_user),
        "username": active_user["username"] if active_user else "",
        "totp_enabled": bool(active_user["totp_enabled"]) if active_user else False,
    }
    return state


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if user_count() == 0:
            return response_error("请先创建管理员账号", 401)
        if not current_user():
            return response_error("请先登录", 401)
        return fn(*args, **kwargs)
    return wrapper


def random_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def normalize_totp_secret(secret):
    return (secret or "").strip().replace(" ", "").upper()


def hotp(secret, counter, digits=6):
    normalized = normalize_totp_secret(secret)
    padded = normalized + "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def verify_totp(secret, code, window=1):
    value = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(value) != 6:
        return False
    counter = int(time.time() // 30)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(hotp(secret, counter + drift), value):
            return True
    return False


def totp_uri(username, secret):
    label = quote(f"light-metapi:{username}")
    issuer = quote("light-metapi")
    return f"otpauth://totp/{label}?secret={normalize_totp_secret(secret)}&issuer={issuer}&digits=6&period=30"


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


def prune_history(conn):
    conn.execute("DELETE FROM balance_history WHERE checked_at < ?", (history_cutoff_iso(),))


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


def channel_value(channel, key, default=None):
    try:
        value = channel[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def load_credential(channel):
    raw = channel_value(channel, "credential_enc", "")
    if not raw:
        return {}
    try:
        return json.loads(decrypt(raw))
    except Exception:
        return {}


def save_channel_credential(channel_id, credential):
    with db() as conn:
        conn.execute(
            """
            UPDATE channels
            SET credential_enc = ?, password_enc = '', updated_at = ?
            WHERE id = ?
            """,
            (encrypt(json.dumps(credential, ensure_ascii=False)), now_iso(), channel_id),
        )


SUB2API_PROFILE_KEYS = ("user", "profile", "account", "current_user")
SUB2API_BALANCE_KEYS = (
    "balance",
    "quota",
    "credit",
    "credits",
    "remaining_balance",
    "available_balance",
    "account_balance",
    "wallet_balance",
)
SUB2API_USED_KEYS = ("used_balance", "used_quota", "quota_used", "used", "total_used")


def extract_sub2api_payload(data):
    if isinstance(data, dict) and "data" in data:
        payload = data.get("data")
        if isinstance(payload, (dict, list)):
            return payload
    return data if isinstance(data, (dict, list)) else {}


def first_decimal(payload, keys):
    if not isinstance(payload, dict):
        return None, None, None
    for key in keys:
        if key not in payload:
            continue
        value = decimal_from(payload.get(key))
        if value is not None:
            return key, value, payload.get(key)
    return None, None, None


def find_sub2api_user_payload(payload):
    if isinstance(payload, list):
        for item in payload:
            found = find_sub2api_user_payload(item)
            if found:
                return found
        return {}
    if not isinstance(payload, dict):
        return {}

    if first_decimal(payload, SUB2API_BALANCE_KEYS)[1] is not None:
        return payload
    for key in SUB2API_PROFILE_KEYS:
        nested = payload.get(key)
        if isinstance(nested, (dict, list)):
            found = find_sub2api_user_payload(nested)
            if found:
                return found
    return payload


def paginated_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "records", "list", "rows", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = paginated_items(value)
            if nested:
                return nested
    return []


def iso_from_unix(value):
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def stable_time(value):
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return iso_from_unix(value)
    text = str(value)
    if text.isdigit():
        return iso_from_unix(text)
    return text


def successful_new_api_topup(status):
    return str(status or "").lower() == "success"


def successful_new_api_topup_value(item):
    status = str(item.get("status") or "").lower()
    if status in {"success", "succeeded", "paid", "completed", "1", "true"}:
        return True
    if item.get("status") in (1, True):
        return True
    return False


def successful_sub2api_order(status):
    return str(status or "").upper() in {"COMPLETED", "PAID", "SUCCESS", "SUCCEEDED"}


def first_amount(payload, keys):
    for key in keys:
        amount = decimal_from(payload.get(key))
        if amount is not None:
            return key, amount
    return None, None


NEW_API_LOG_TYPE_TOPUP = 1
NEW_API_LOG_TYPE_MANAGE = 3
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def new_api_headers(credential):
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    token = credential.get("access_token")
    user_id = credential.get("user_id")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if user_id:
        headers["New-Api-User"] = str(user_id)
    return headers


def new_api_topup_amount(base_url, item):
    quota_key, quota = first_amount(item, ("quota", "amount_quota", "quota_amount"))
    if quota is not None:
        return quota_key, quota / get_new_api_quota_unit(base_url)
    key, amount = first_amount(
        item,
        ("money", "pay_money", "pay_amount", "actual_amount", "total_amount", "amount"),
    )
    if amount is not None:
        return key, amount
    return None, None


def logged_quota_values(text, quota_unit):
    quota_unit = quota_unit or Decimal("500000")
    values = []
    for match in NUMBER_RE.finditer(str(text or "")):
        amount = decimal_from(match.group(0).replace(",", ""))
        if amount is None:
            continue
        before = text[max(0, match.start() - 3):match.start()]
        after = text[match.end():match.end() + 6]
        if "$" in before or "＄" in before:
            normalized = amount
        elif "¥" in before or "￥" in before:
            normalized = amount / DEFAULT_CNY_RATE
        elif "点额度" in after or (quota_unit and amount >= quota_unit):
            normalized = amount / quota_unit
        else:
            normalized = amount
        values.append(normalized)
    return values


def new_api_balance_log_item(base_url, item, quota_unit):
    content = str(item.get("content") or "")
    amount = None
    source_type = ""
    values = logged_quota_values(content, quota_unit)

    if "通过兑换码充值" in content:
        amount = values[0] if values else None
        source_type = "redemption"
    elif "管理员增加用户额度" in content:
        amount = values[0] if values else None
        source_type = "admin_add_quota"
    elif "管理员覆盖用户额度从" in content and len(values) >= 2:
        amount = values[1] - values[0]
        source_type = "admin_set_quota"

    if amount is None or amount <= 0:
        return None

    detected_at = stable_time(
        item.get("created_at") or item.get("created_time") or item.get("timestamp") or item.get("time")
    ) or now_iso()
    return {
        "amount_usd": amount,
        "detected_at": detected_at,
        "source_ref": source_hash(
            "new_api_log",
            item.get("created_at"),
            item.get("type"),
            content,
            amount,
        ),
        "source_status": "success",
        "source_type": source_type,
    }


def unique_recharge_logs(logs):
    result = []
    seen = set()
    for log in logs:
        key = log.get("source_ref") or source_hash(log.get("amount_usd"), log.get("detected_at"), log.get("source_type"))
        if key in seen:
            continue
        seen.add(key)
        result.append(log)
    return result


def sub2api_login(base_url, username, password):
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    login = session.post(
        urljoin(base_url, "/api/v1/auth/login"),
        json={"email": username, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    login_data = safe_json(login)
    if login.status_code >= 400:
        raise RuntimeError(read_message(login_data) or f"登录失败 HTTP {login.status_code}")

    payload = extract_sub2api_payload(login_data)
    access_token = payload.get("access_token")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else None
    if not access_token:
        raise RuntimeError("登录成功响应里没有 access_token")
    return {
        "kind": "sub2api_token",
        "access_token": access_token,
        "refresh_token": payload.get("refresh_token"),
        "expires_in": payload.get("expires_in"),
        "token_type": payload.get("token_type", "Bearer"),
        "issued_at": now_iso(),
    }, user


def sub2api_refresh(base_url, refresh_token):
    if not refresh_token:
        raise RuntimeError("refresh_token 为空，请重新添加渠道")
    resp = requests.post(
        urljoin(base_url, "/api/v1/auth/refresh"),
        json={"refresh_token": refresh_token},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    data = safe_json(resp)
    if resp.status_code >= 400:
        raise RuntimeError(read_message(data) or f"刷新 token 失败 HTTP {resp.status_code}")
    payload = extract_sub2api_payload(data)
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("刷新 token 响应里没有 access_token")
    return {
        "kind": "sub2api_token",
        "access_token": access_token,
        "refresh_token": payload.get("refresh_token") or refresh_token,
        "expires_in": payload.get("expires_in"),
        "token_type": payload.get("token_type", "Bearer"),
        "issued_at": now_iso(),
    }


def sub2api_profile(base_url, access_token):
    session = requests.Session()
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    last_error = ""
    first_payload = None
    for path in ("/api/v1/user/profile", "/api/v1/auth/me"):
        resp = session.get(urljoin(base_url, path), headers=headers, timeout=REQUEST_TIMEOUT)
        data = safe_json(resp)
        if resp.status_code < 400 and truthy_success(data):
            payload = extract_sub2api_payload(data)
            if first_payload is None:
                first_payload = payload
            if first_decimal(find_sub2api_user_payload(payload), SUB2API_BALANCE_KEYS)[1] is not None:
                return payload
            last_error = f"{path} 响应里没有可识别余额字段"
            continue
        last_error = read_message(data) or f"{path} 读取失败 HTTP {resp.status_code}"
    if first_payload is not None:
        return first_payload
    raise RuntimeError(last_error or "profile 读取失败")


def safe_sub2api_recharge_logs(base_url, access_token):
    try:
        return sub2api_recharge_logs(base_url, access_token)
    except (requests.RequestException, RuntimeError):
        return []


def sub2api_token_error(exc):
    message = str(exc).lower()
    return any(marker in message for marker in ("401", "unauthorized", "unauthenticated", "token", "expired"))


def sub2api_recharge_logs(base_url, access_token):
    resp = requests.get(
        urljoin(base_url, "/api/v1/payment/orders/my"),
        params={"page": 1, "page_size": 50, "order_type": "balance"},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    data = safe_json(resp)
    if resp.status_code >= 400:
        raise RuntimeError(read_message(data) or f"payment orders 读取失败 HTTP {resp.status_code}")
    logs = []
    for item in paginated_items(extract_sub2api_payload(data)):
        if not isinstance(item, dict) or not successful_sub2api_order(item.get("status")):
            continue
        _, amount = first_amount(item, ("amount", "pay_amount", "actual_amount", "total_amount"))
        if amount is None or amount <= 0:
            continue
        detected_at = stable_time(
            item.get("completed_at") or item.get("paid_at") or item.get("updated_at") or item.get("created_at")
        ) or now_iso()
        logs.append({
            "amount_usd": amount,
            "detected_at": detected_at,
            "source_ref": source_hash(
                "sub2api",
                item.get("id"),
                item.get("out_trade_no"),
                item.get("created_at"),
                item.get("completed_at"),
                item.get("status"),
                amount,
            ),
            "source_status": item.get("status", ""),
            "source_type": item.get("payment_type") or item.get("provider_key") or item.get("payment_method") or "",
        })
    return logs


def build_sub2api_result(profile_payload, fallback_user=None):
    if not profile_payload and fallback_user:
        profile_payload = fallback_user
    profile_payload = find_sub2api_user_payload(profile_payload)
    if (not profile_payload or first_decimal(profile_payload, SUB2API_BALANCE_KEYS)[1] is None) and fallback_user:
        profile_payload = find_sub2api_user_payload(fallback_user)

    balance_key, balance, raw_balance = first_decimal(profile_payload, SUB2API_BALANCE_KEYS)
    if balance is None:
        raise RuntimeError("profile 响应里没有可识别余额字段")

    used_key, used_balance, raw_used_balance = first_decimal(profile_payload, SUB2API_USED_KEYS)

    return {
        "balance": balance,
        "raw_balance": str(raw_balance),
        "used_balance": used_balance,
        "raw_used_balance": str(raw_used_balance) if used_key else None,
        "request_count": None,
        "currency": "USD",
        "status": "ok",
        "message": "",
        "recharge_logs": [],
        "raw_response": {
            "role": profile_payload.get("role"),
            "concurrency": profile_payload.get("concurrency"),
            "status": profile_payload.get("status"),
            "balance_field": balance_key,
            "used_balance_field": used_key,
        },
    }


def fetch_sub2api(channel):
    base_url = normalize_url(channel["base_url"])
    credential = load_credential(channel)
    channel_id = channel_value(channel, "id")

    if credential.get("access_token"):
        try:
            result = build_sub2api_result(sub2api_profile(base_url, credential["access_token"]))
            result["recharge_logs"] = safe_sub2api_recharge_logs(base_url, credential["access_token"])
            return result
        except RuntimeError as exc:
            if not sub2api_token_error(exc):
                raise
            credential = sub2api_refresh(base_url, credential.get("refresh_token"))
            if channel_id:
                save_channel_credential(channel_id, credential)
            result = build_sub2api_result(sub2api_profile(base_url, credential["access_token"]))
            result["recharge_logs"] = safe_sub2api_recharge_logs(base_url, credential["access_token"])
            return result

    password_enc = channel_value(channel, "password_enc", "")
    if not password_enc:
        raise RuntimeError("缺少可用令牌，请重新添加渠道")
    credential, user = sub2api_login(base_url, channel["username"], decrypt(password_enc))
    result = build_sub2api_result(sub2api_profile(base_url, credential["access_token"]), user)
    result["recharge_logs"] = safe_sub2api_recharge_logs(base_url, credential["access_token"])
    if channel_id:
        save_channel_credential(channel_id, credential)
    return result


def new_api_login(base_url, username, password, totp_code=""):
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    login = session.post(
        urljoin(base_url, "/api/user/login"),
        json={"username": username, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    login_data = safe_json(login)
    if login.status_code >= 400 or not truthy_success(login_data):
        raise RuntimeError(read_message(login_data) or f"登录失败 HTTP {login.status_code}")

    user = login_data.get("data") if isinstance(login_data.get("data"), dict) else {}
    if user.get("require_2fa"):
        code = "".join(ch for ch in str(totp_code or "") if ch.isdigit())
        if not code:
            raise RuntimeError("上游 New API 已开启 2FA，请填写验证码")
        twofa = session.post(
            urljoin(base_url, "/api/user/login/2fa"),
            json={"code": code},
            timeout=REQUEST_TIMEOUT,
        )
        twofa_data = safe_json(twofa)
        if twofa.status_code >= 400 or not truthy_success(twofa_data):
            raise RuntimeError(read_message(twofa_data) or f"2FA 登录失败 HTTP {twofa.status_code}")
        user = twofa_data.get("data") if isinstance(twofa_data.get("data"), dict) else {}
    return session, user


def new_api_generate_token(base_url, session, user_id):
    if not user_id:
        raise RuntimeError("登录响应里没有用户 ID")
    token_resp = session.get(
        urljoin(base_url, "/api/user/token"),
        headers={"New-Api-User": str(user_id), "X-Requested-With": "XMLHttpRequest"},
        timeout=REQUEST_TIMEOUT,
    )
    token_data = safe_json(token_resp)
    if token_resp.status_code >= 400 or not truthy_success(token_data):
        raise RuntimeError(read_message(token_data) or f"生成 access token 失败 HTTP {token_resp.status_code}")
    token = token_data.get("data")
    if not isinstance(token, str) or not token:
        raise RuntimeError("生成 access token 响应为空")
    return {
        "kind": "new_api_access_token",
        "access_token": token,
        "user_id": user_id,
        "issued_at": now_iso(),
    }


def new_api_self(base_url, credential, session=None):
    headers = new_api_headers(credential)
    user_id = credential.get("user_id")

    client = session or requests.Session()
    self_resp = client.get(urljoin(base_url, "/api/user/self"), headers=headers, timeout=REQUEST_TIMEOUT)
    self_data = safe_json(self_resp)
    if self_resp.status_code == 401 and "New-Api-User" in read_message(self_data) and user_id:
        headers["New-Api-User"] = str(user_id)
        self_resp = client.get(urljoin(base_url, "/api/user/self"), headers=headers, timeout=REQUEST_TIMEOUT)
        self_data = safe_json(self_resp)
    if self_resp.status_code >= 400 or not truthy_success(self_data):
        raise RuntimeError(read_message(self_data) or f"self 读取失败 HTTP {self_resp.status_code}")
    return self_data.get("data") if isinstance(self_data.get("data"), dict) else {}


def new_api_topup_logs(base_url, credential, session=None):
    headers = new_api_headers(credential)
    client = session or requests.Session()
    resp = client.get(
        urljoin(base_url, "/api/user/topup/self"),
        params={"p": 1, "page_size": 50},
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    data = safe_json(resp)
    if resp.status_code >= 400 or not truthy_success(data):
        raise RuntimeError(read_message(data) or f"topup 记录读取失败 HTTP {resp.status_code}")
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data
    logs = []
    for item in paginated_items(payload):
        if not isinstance(item, dict) or not successful_new_api_topup_value(item):
            continue
        _, amount = new_api_topup_amount(base_url, item)
        if amount is None or amount <= 0:
            continue
        detected_at = stable_time(
            item.get("complete_time") or item.get("completed_at") or item.get("paid_at") or item.get("create_time") or item.get("created_at")
        ) or now_iso()
        logs.append({
            "amount_usd": amount,
            "detected_at": detected_at,
            "source_ref": source_hash(
                "new_api",
                item.get("id"),
                item.get("trade_no"),
                item.get("create_time"),
                item.get("complete_time"),
                item.get("status"),
                amount,
            ),
            "source_status": item.get("status", ""),
            "source_type": item.get("payment_method") or item.get("payment_provider") or item.get("provider") or "",
        })
    return logs


def new_api_log_recharge_logs(base_url, credential, session=None):
    headers = new_api_headers(credential)
    client = session or requests.Session()
    quota_unit = None
    logs = []
    for log_type in (NEW_API_LOG_TYPE_TOPUP, NEW_API_LOG_TYPE_MANAGE):
        resp = client.get(
            urljoin(base_url, "/api/log/self"),
            params={"p": 1, "page_size": 100, "type": log_type},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        data = safe_json(resp)
        if resp.status_code >= 400 or not truthy_success(data):
            raise RuntimeError(read_message(data) or f"log 记录读取失败 HTTP {resp.status_code}")
        payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data
        for item in paginated_items(payload):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "")
            if not any(marker in content for marker in ("通过兑换码充值", "管理员增加用户额度", "管理员覆盖用户额度从")):
                continue
            if quota_unit is None:
                quota_unit = get_new_api_quota_unit(base_url)
            log = new_api_balance_log_item(base_url, item, quota_unit)
            if log:
                logs.append(log)
    return logs


def new_api_recharge_logs(base_url, credential, session=None):
    logs = new_api_topup_logs(base_url, credential, session=session)
    try:
        logs.extend(new_api_log_recharge_logs(base_url, credential, session=session))
    except (requests.RequestException, RuntimeError):
        pass
    return unique_recharge_logs(logs)


def safe_new_api_recharge_logs(base_url, credential, session=None):
    try:
        return new_api_recharge_logs(base_url, credential, session=session)
    except (requests.RequestException, RuntimeError):
        return []


def build_new_api_result(base_url, payload):
    raw_quota = decimal_from(payload.get("quota"))
    if raw_quota is None:
        raise RuntimeError("self 响应里没有 quota")

    quota_per_unit = get_new_api_quota_unit(base_url)
    used_quota = decimal_from(payload.get("used_quota"))
    balance = raw_quota / quota_per_unit
    used_balance = used_quota / quota_per_unit if used_quota is not None else None

    return {
        "balance": balance,
        "raw_balance": str(payload.get("quota")),
        "used_balance": used_balance,
        "raw_used_balance": str(payload.get("used_quota")) if payload.get("used_quota") is not None else None,
        "request_count": payload.get("request_count"),
        "currency": "USD",
        "status": "ok",
        "message": "",
        "recharge_logs": [],
        "raw_response": {
            "group": payload.get("group"),
            "quota_per_unit": float(quota_per_unit),
        },
    }


def fetch_new_api(channel):
    base_url = normalize_url(channel["base_url"])
    credential = load_credential(channel)
    channel_id = channel_value(channel, "id")

    if credential.get("access_token"):
        result = build_new_api_result(base_url, new_api_self(base_url, credential))
        result["recharge_logs"] = safe_new_api_recharge_logs(base_url, credential)
        return result

    password_enc = channel_value(channel, "password_enc", "")
    if not password_enc:
        raise RuntimeError("缺少可用令牌，请重新添加渠道")
    session, user = new_api_login(base_url, channel["username"], decrypt(password_enc))
    credential = new_api_generate_token(base_url, session, user.get("id"))
    result = build_new_api_result(base_url, new_api_self(base_url, credential, session=session))
    result["recharge_logs"] = safe_new_api_recharge_logs(base_url, credential, session=session)
    if channel_id:
        save_channel_credential(channel_id, credential)
    return result


def provision_channel(platform, base_url, username, password, totp_code=""):
    if platform == "new_api":
        session, user = new_api_login(base_url, username, password, totp_code)
        credential = new_api_generate_token(base_url, session, user.get("id"))
        result = build_new_api_result(base_url, new_api_self(base_url, credential, session=session))
        result["recharge_logs"] = safe_new_api_recharge_logs(base_url, credential, session=session)
        return credential, result
    if platform == "sub2api":
        credential, user = sub2api_login(base_url, username, password)
        result = build_sub2api_result(sub2api_profile(base_url, credential["access_token"]), user)
        result["recharge_logs"] = safe_sub2api_recharge_logs(base_url, credential["access_token"])
        return credential, result
    raise RuntimeError(f"未知平台: {platform}")


def get_new_api_quota_unit(base_url):
    try:
        resp = requests.get(urljoin(base_url, "/api/status"), timeout=REQUEST_TIMEOUT)
        data = safe_json(resp)
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        unit = decimal_from(payload.get("quota_per_unit"))
        if unit and unit > 0:
            return unit
    except requests.RequestException:
        pass
    return Decimal("500000")


def safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        return {"message": resp.text[:500]}


def read_message(data):
    if not isinstance(data, dict):
        return ""
    message = data.get("message") or data.get("error")
    return str(message) if message else ""


def truthy_success(data):
    if not isinstance(data, dict):
        return False
    if "success" in data:
        return bool(data.get("success"))
    if "code" in data:
        return data.get("code") in (0, "0")
    return True


def fetch_channel(channel):
    if channel["platform"] == "sub2api":
        return fetch_sub2api(channel)
    if channel["platform"] == "new_api":
        return fetch_new_api(channel)
    raise RuntimeError(f"未知平台: {channel['platform']}")


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
                updated_at = ?
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
                updated_at = ?
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
            rows = conn.execute("SELECT * FROM channels WHERE enabled = 1 ORDER BY id").fetchall()
        results = []
        for row in rows:
            try:
                refreshed = refresh_one(row["id"])
                results.append(refreshed)
            except Exception as exc:
                persist_failure(row["id"], exc)
                results.append(row_to_channel(get_channel(row["id"])))
        send_low_balance_alerts(results)
        if send_notify and setting_get("notify_enabled", "1") == "1":
            send_notification_summary(results)
        return results


def format_money(value):
    if value is None:
        return "-"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def parse_iso_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def wecom_webhook():
    webhook_enc = setting_get("wecom_webhook_enc")
    return decrypt(webhook_enc) if webhook_enc else ""


def feishu_webhook():
    webhook_enc = setting_get("feishu_webhook_enc")
    return decrypt(webhook_enc) if webhook_enc else ""


def notification_webhooks_configured():
    return bool(wecom_webhook() or feishu_webhook())


def low_balance_email_recipients(value=None):
    raw = setting_get("low_balance_email_recipients", os.getenv("LOW_BALANCE_EMAIL_RECIPIENTS", "")) if value is None else value
    return [item.strip() for item in re.split(r"[;,]", raw or "") if item.strip()]


def low_balance_email_configured(recipients=None):
    return bool(
        LOW_BALANCE_EMAIL_ENABLED
        and LOW_BALANCE_EMAIL_SMTP_SERVER
        and LOW_BALANCE_EMAIL_SMTP_USER
        and LOW_BALANCE_EMAIL_SMTP_TOKEN
        and low_balance_email_recipients(recipients)
    )


def post_wecom_text(content):
    webhook = wecom_webhook()
    if not webhook:
        return False
    payload = {"msgtype": "text", "text": {"content": content}}
    try:
        resp = requests.post(webhook, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return False
    return resp.status_code < 400


def feishu_response_ok(resp):
    if resp.status_code >= 400:
        return False
    data = safe_json(resp)
    if not isinstance(data, dict):
        return True
    code = data.get("code", data.get("StatusCode", 0))
    return code in (0, "0", None)


def post_feishu_text(content):
    webhook = feishu_webhook()
    if not webhook:
        return False
    payload = {"msg_type": "text", "content": {"text": content}}
    try:
        resp = requests.post(webhook, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return False
    return feishu_response_ok(resp)


def post_notification_text(content):
    sent = []
    try:
        wecom_sent = post_wecom_text(content)
    except requests.RequestException:
        wecom_sent = False
    try:
        feishu_sent = post_feishu_text(content)
    except requests.RequestException:
        feishu_sent = False
    if wecom_sent:
        sent.append("wecom")
    if feishu_sent:
        sent.append("feishu")
    return sent


def html_body_from_text(content):
    escaped = (
        str(content)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    return f"<div style=\"font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;line-height:1.6\">{escaped}</div>"


def post_low_balance_email(subject, content):
    recipients = low_balance_email_recipients()
    if not low_balance_email_configured():
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = LOW_BALANCE_EMAIL_FROM
    message["To"] = ", ".join(recipients)
    message.set_content(content)
    message.add_alternative(html_body_from_text(content), subtype="html")
    try:
        with smtplib.SMTP(LOW_BALANCE_EMAIL_SMTP_SERVER, LOW_BALANCE_EMAIL_SMTP_PORT, timeout=REQUEST_TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(LOW_BALANCE_EMAIL_SMTP_USER, LOW_BALANCE_EMAIL_SMTP_TOKEN)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        print(f"[low-balance-email] failed: {exc}", flush=True)
        return False
    return True


def send_notification_summary(channels):
    if not notification_webhooks_configured():
        return False
    ok_count = sum(1 for item in channels if item.get("status") == "ok")
    lines = [
        "上游余额巡检",
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"状态: {ok_count}/{len(channels)} 正常",
        "",
    ]
    for item in channels:
        icon = "OK" if item.get("status") == "ok" else "ERR"
        name = item.get("name") or item.get("base_url")
        balance = format_money(item.get("balance"))
        used = item.get("used_balance")
        used_text = f", used {format_money(used)}" if used is not None else ""
        message = f" - {item.get('message')}" if item.get("status") != "ok" and item.get("message") else ""
        lines.append(f"{icon} {name}: {balance} {item.get('currency', 'USD')}{used_text}{message}")
    return bool(post_notification_text("\n".join(lines)))


def send_wecom_summary(channels):
    return send_notification_summary(channels)


def low_balance_alert_key(channel_id):
    return f"low_balance_alerted_at:{channel_id}"


def should_send_low_balance_alert(channel):
    if channel.get("status") != "ok":
        return False
    cny_balance = decimal_from(channel.get("cny_balance"))
    if cny_balance is None:
        return False
    threshold = alert_threshold_from(channel.get("alert_cny"))
    key = low_balance_alert_key(channel["id"])
    if cny_balance > threshold:
        setting_delete(key)
        return False
    alerted_at = parse_iso_timestamp(setting_get(key))
    if not alerted_at:
        return True
    return datetime.now(timezone.utc) - alerted_at >= timedelta(seconds=LOW_BALANCE_ALERT_COOLDOWN_SECONDS)


def send_low_balance_alerts(channels):
    if setting_get("notify_enabled", "1") != "1":
        return []
    sent = []
    for channel in channels:
        if not should_send_low_balance_alert(channel):
            continue
        lines = [
            f"渠道名: {channel.get('name') or channel.get('base_url')}",
            f"余额: {format_money(channel.get('cny_balance'))} CNY",
            f"阈值: {format_money(as_float(alert_threshold_from(channel.get('alert_cny'))))} CNY",
        ]
        content = "\n".join(lines)
        notify_sent = post_notification_text(content) if notification_webhooks_configured() else []
        email_sent = post_low_balance_email("上游余额告警", content)
        if notify_sent or email_sent:
            setting_set(low_balance_alert_key(channel["id"]), now_iso())
            sent.append(channel)
    return sent


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


@app.get("/api/auth/bootstrap")
def auth_bootstrap():
    return jsonify({"ok": True, "data": auth_state()})


@app.post("/api/auth/register")
def auth_register():
    if user_count() > 0:
        return response_error("管理员账号已创建", 403)
    payload = request_payload(force=True)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if len(username) < 3:
        return response_error("账号至少 3 个字符")
    if len(password) < 8:
        return response_error("密码至少 8 个字符")
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, created_at, updated_at) VALUES(?, ?, ?, ?)",
            (username, generate_password_hash(password), ts, ts),
        )
    user = get_user(cur.lastrowid)
    setup_login(user)
    return jsonify({"ok": True, "data": auth_state(user)})


@app.post("/api/auth/login")
def auth_login():
    payload = request_payload(force=True)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    row = get_user_by_username(username)
    if not row or not check_password_hash(row["password_hash"], password):
        return response_error("账号或密码错误", 401)
    if row["totp_enabled"] and not verify_totp(row["totp_secret"], payload.get("totp")):
        return response_error("2FA 验证码错误", 401)
    setup_login(row)
    return jsonify({"ok": True, "data": auth_state(row)})


@app.post("/api/auth/logout")
def auth_logout():
    clear_login()
    return jsonify({"ok": True})


@app.post("/api/auth/2fa/setup")
@login_required
def auth_2fa_setup():
    row = current_user()
    secret = normalize_totp_secret(row["totp_secret"]) or random_totp_secret()
    with db() as conn:
        conn.execute("UPDATE users SET totp_secret = ?, updated_at = ? WHERE id = ?", (secret, now_iso(), row["id"]))
    return jsonify({"ok": True, "data": {"secret": secret, "otpauth_uri": totp_uri(row["username"], secret)}})


@app.post("/api/auth/2fa/confirm")
@login_required
def auth_2fa_confirm():
    row = current_user()
    if not row["totp_secret"]:
        return response_error("请先生成 2FA 密钥")
    payload = request_payload(force=True)
    if not verify_totp(row["totp_secret"], payload.get("totp")):
        return response_error("2FA 验证码错误", 401)
    with db() as conn:
        conn.execute("UPDATE users SET totp_enabled = 1, updated_at = ? WHERE id = ?", (now_iso(), row["id"]))
    updated = get_user(row["id"])
    setup_login(updated)
    return jsonify({"ok": True, "data": auth_state(updated)})


@app.post("/api/auth/2fa/disable")
@login_required
def auth_2fa_disable():
    row = current_user()
    payload = request_payload(force=True)
    if not check_password_hash(row["password_hash"], payload.get("password") or ""):
        return response_error("密码错误", 401)
    with db() as conn:
        conn.execute("UPDATE users SET totp_enabled = 0, totp_secret = '', updated_at = ? WHERE id = ?", (now_iso(), row["id"]))
    return jsonify({"ok": True, "data": auth_state()})


@app.get("/api/settings")
@login_required
def get_settings():
    return jsonify({"ok": True, "data": public_settings()})


@app.put("/api/settings")
@login_required
def update_settings():
    payload = request_payload(force=True)
    if "wecom_webhook" in payload:
        webhook = (payload.get("wecom_webhook") or "").strip()
        if webhook:
            setting_set("wecom_webhook_enc", encrypt(webhook))
        elif payload.get("clear_wecom"):
            setting_set("wecom_webhook_enc", "")
    if "feishu_webhook" in payload:
        webhook = (payload.get("feishu_webhook") or "").strip()
        if webhook:
            setting_set("feishu_webhook_enc", encrypt(webhook))
        elif payload.get("clear_feishu"):
            setting_set("feishu_webhook_enc", "")
    if "low_balance_email_recipients" in payload:
        setting_set("low_balance_email_recipients", (payload.get("low_balance_email_recipients") or "").strip())
    if "notify_enabled" in payload:
        setting_set("notify_enabled", "1" if payload.get("notify_enabled") else "0")
    return jsonify({"ok": True, "data": public_settings()})


@app.post("/api/settings/test-wecom")
@login_required
def test_wecom():
    webhook_enc = setting_get("wecom_webhook_enc")
    if not webhook_enc:
        return response_error("请先保存企业微信 webhook")
    webhook = decrypt(webhook_enc)
    payload = {
        "msgtype": "text",
        "text": {"content": f"light-metapi 测试消息\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
    }
    resp = requests.post(webhook, json=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 400:
        return response_error(f"企业微信返回 HTTP {resp.status_code}: {resp.text[:300]}", 502)
    return jsonify({"ok": True})


@app.post("/api/settings/test-feishu")
@login_required
def test_feishu():
    webhook_enc = setting_get("feishu_webhook_enc")
    if not webhook_enc:
        return response_error("请先保存飞书 webhook")
    webhook = decrypt(webhook_enc)
    payload = {
        "msg_type": "text",
        "content": {"text": f"light-metapi 测试消息\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
    }
    resp = requests.post(webhook, json=payload, timeout=REQUEST_TIMEOUT)
    if not feishu_response_ok(resp):
        return response_error(f"飞书返回 HTTP {resp.status_code}: {resp.text[:300]}", 502)
    return jsonify({"ok": True})


@app.post("/api/settings/test-email")
@login_required
def test_email():
    if not low_balance_email_configured():
        return response_error("请先配置低余额邮箱")
    content = "\n".join([
        "渠道名: 邮件测试",
        "余额: 0 CNY",
        f"阈值: {format_money(as_float(LOW_BALANCE_ALERT_CNY))} CNY",
    ])
    if not post_low_balance_email("上游余额告警测试", content):
        return response_error("邮件发送失败", 502)
    return jsonify({"ok": True})


@app.get("/api/channels")
@login_required
def api_list_channels():
    return jsonify({"ok": True, "data": list_channels()})


@app.get("/api/recharges")
@login_required
def api_recharge_logs():
    channel_id = request.args.get("channel_id", type=int)
    limit = request.args.get("limit", default=80, type=int)
    limit = max(1, min(limit, 200))
    return jsonify({"ok": True, "data": list_recharge_logs(channel_id, limit)})


@app.post("/api/channels")
@login_required
def api_create_channel():
    payload = request_payload(force=True)
    name = (payload.get("name") or "").strip()
    platform = (payload.get("platform") or "").strip()
    try:
        base_url = normalize_url(payload.get("base_url"))
    except ValueError as exc:
        log_channel_create(platform, payload.get("base_url"), payload.get("username"), f"failed: {exc}")
        return response_error(str(exc))
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    totp_code = payload.get("totp") or payload.get("totp_code") or ""
    cny_rate = rate_from(payload.get("cny_rate"))
    alert_cny = alert_threshold_from(payload.get("alert_cny"))
    boss_recharge_required = 1 if payload.get("boss_recharge_required") else 0
    if platform not in ("new_api", "sub2api"):
        return response_error("平台只支持 new_api 或 sub2api")
    if not name:
        name = base_url.replace("https://", "").replace("http://", "").strip("/")
    if not username or not password:
        return response_error("账号和密码必填")
    try:
        credential, result = provision_channel(platform, base_url, username, password, totp_code)
    except Exception as exc:
        log_channel_create(platform, base_url, username, f"failed: {exc}")
        return response_error(f"测试失败: {exc}", 502)
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO channels(
                name, platform, base_url, username, password_enc, credential_enc, cny_rate, alert_cny, enabled,
                boss_recharge_required, balance, raw_balance, used_balance, raw_used_balance, request_count,
                currency, status, message, raw_response, last_checked_at, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                platform,
                base_url,
                username,
                encrypt(json.dumps(credential, ensure_ascii=False)),
                as_float(cny_rate),
                as_float(alert_cny),
                1,
                boss_recharge_required,
                as_float(result.get("balance")),
                result.get("raw_balance"),
                as_float(result.get("used_balance")),
                result.get("raw_used_balance"),
                result.get("request_count"),
                result.get("currency", "USD"),
                result.get("status", "ok"),
                result.get("message", ""),
                json.dumps(result.get("raw_response") or {}, ensure_ascii=False),
                ts,
                ts,
                ts,
            ),
        )
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (cur.lastrowid,)).fetchone()
        if row:
            safe_sync_recharge_logs(conn, row, result.get("recharge_logs") or [])
        record_balance_history(conn, cur.lastrowid, result, ts)
        prune_history(conn)
    log_channel_create(platform, base_url, username, f"saved id={cur.lastrowid}")
    return jsonify({"ok": True, "data": row_to_channel(get_channel(cur.lastrowid))})


@app.put("/api/channels/<int:channel_id>")
@login_required
def api_update_channel(channel_id):
    row = get_channel(channel_id)
    if not row:
        return response_error("渠道不存在", 404)
    payload = request_payload(force=True)
    name = (payload.get("name") or row["name"]).strip()
    platform = (payload.get("platform") or row["platform"]).strip()
    base_url = normalize_url(payload.get("base_url") or row["base_url"])
    username = (payload.get("username") or row["username"]).strip()
    enabled = 1 if payload.get("enabled", row["enabled"]) else 0
    cny_rate = rate_from(payload.get("cny_rate", row["cny_rate"]))
    alert_cny = alert_threshold_from(payload.get("alert_cny"), channel_value(row, "alert_cny"))
    boss_recharge_required = 1 if payload.get("boss_recharge_required", channel_value(row, "boss_recharge_required", 0)) else 0
    if platform not in ("new_api", "sub2api"):
        return response_error("平台只支持 new_api 或 sub2api")
    password = payload.get("password") or ""
    totp_code = payload.get("totp") or payload.get("totp_code") or ""
    credential_enc = channel_value(row, "credential_enc", "")
    result = None
    if password:
        try:
            credential, result = provision_channel(platform, base_url, username, password, totp_code)
            credential_enc = encrypt(json.dumps(credential, ensure_ascii=False))
        except Exception as exc:
            return response_error(f"测试失败: {exc}", 502)
    ts = now_iso()
    with db() as conn:
        if result:
            conn.execute(
                """
                UPDATE channels
                SET name = ?,
                    platform = ?,
                    base_url = ?,
                    username = ?,
                    password_enc = '',
                    credential_enc = ?,
                    cny_rate = ?,
                    alert_cny = ?,
                    enabled = ?,
                    boss_recharge_required = ?,
                    balance = ?,
                    raw_balance = ?,
                    used_balance = ?,
                    raw_used_balance = ?,
                    request_count = ?,
                    currency = ?,
                    status = ?,
                    message = ?,
                    raw_response = ?,
                    last_checked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    platform,
                    base_url,
                    username,
                    credential_enc,
                    as_float(cny_rate),
                    as_float(alert_cny),
                    enabled,
                    boss_recharge_required,
                    as_float(result.get("balance")),
                    result.get("raw_balance"),
                    as_float(result.get("used_balance")),
                    result.get("raw_used_balance"),
                    result.get("request_count"),
                    result.get("currency", "USD"),
                    result.get("status", "ok"),
                    result.get("message", ""),
                    json.dumps(result.get("raw_response") or {}, ensure_ascii=False),
                    ts,
                    ts,
                    channel_id,
                ),
            )
            updated = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
            if updated:
                sync_recharge_logs(conn, updated, result.get("recharge_logs") or [])
            record_balance_history(conn, channel_id, result, ts)
            prune_history(conn)
        else:
            conn.execute(
                """
                UPDATE channels
                SET name = ?,
                    platform = ?,
                    base_url = ?,
                    username = ?,
                    credential_enc = ?,
                    password_enc = '',
                    cny_rate = ?,
                    alert_cny = ?,
                    enabled = ?,
                    boss_recharge_required = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    platform,
                    base_url,
                    username,
                    credential_enc,
                    as_float(cny_rate),
                    as_float(alert_cny),
                    enabled,
                    boss_recharge_required,
                    ts,
                    channel_id,
                ),
            )
    return jsonify({"ok": True, "data": row_to_channel(get_channel(channel_id))})


@app.delete("/api/channels/<int:channel_id>")
@login_required
def api_delete_channel(channel_id):
    with db() as conn:
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return jsonify({"ok": True})


@app.post("/api/channels/<int:channel_id>/refresh")
@login_required
def api_refresh_channel(channel_id):
    try:
        return jsonify({"ok": True, "data": refresh_one(channel_id)})
    except Exception as exc:
        persist_failure(channel_id, exc)
        return jsonify({"ok": False, "message": str(exc), "data": row_to_channel(get_channel(channel_id))}), 502


@app.post("/api/refresh")
@login_required
def api_refresh_all():
    payload = request_payload()
    data = refresh_all(send_notify=bool(payload.get("notify")))
    return jsonify({"ok": True, "data": data})


def register_api_aliases():
    for rule in list(app.url_map.iter_rules()):
        if not rule.rule.startswith("/api/"):
            continue
        alias = "/_ub_api/" + rule.rule[len("/api/"):]
        endpoint = f"ub_alias_{rule.endpoint}"
        if endpoint in app.view_functions:
            continue
        methods = set(rule.methods)
        methods.add("GET")
        app.add_url_rule(alias, endpoint, app.view_functions[rule.endpoint], methods=methods)


def scheduler_loop():
    last_notify_at = 0.0
    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            should_notify = time.time() - last_notify_at >= NOTIFY_INTERVAL_SECONDS
            refresh_all(send_notify=should_notify)
            if should_notify:
                last_notify_at = time.time()
        except Exception as exc:
            print(f"[scheduler] refresh failed: {exc}", flush=True)


def start_scheduler():
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()


init_db()
register_api_aliases()
start_scheduler()


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8756")))
