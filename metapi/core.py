import base64
import concurrent.futures
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
import auth_security
import channel_catalog
from cryptography.fernet import Fernet
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("UPSTREAM_BALANCE_DATA_DIR", str(ROOT / "data"))).resolve()
DB_PATH = DATA_DIR / "upstreams.sqlite3"
KEY_PATH = DATA_DIR / "secret.key"
SESSION_KEY_PATH = DATA_DIR / "session.secret"
CHANNEL_CATALOG_PATH = Path(
    os.getenv("UPSTREAM_CHANNEL_CATALOG_PATH", str(DATA_DIR / "channel-catalog.json"))
).resolve()
STATIC_DIR = ROOT / "static"
REFRESH_INTERVAL_SECONDS = max(30, int(os.getenv("REFRESH_INTERVAL_SECONDS", "300")))
CATALOG_SYNC_INTERVAL_SECONDS = max(30, int(os.getenv("CATALOG_SYNC_INTERVAL_SECONDS", "60")))
CATALOG_ACCOUNT_SYNC_ENABLED = os.getenv("CATALOG_ACCOUNT_SYNC_ENABLED", "0") == "1"
CATALOG_ACCOUNT_SYNC_INTERVAL_SECONDS = max(300, int(os.getenv("CATALOG_ACCOUNT_SYNC_INTERVAL_SECONDS", "3600")))
CATALOG_NEW_API_USERNAME = os.getenv("CATALOG_NEW_API_USERNAME", "")
CATALOG_NEW_API_PASSWORD = os.getenv("CATALOG_NEW_API_PASSWORD", "")
CATALOG_SUB2API_USERNAME = os.getenv("CATALOG_SUB2API_USERNAME", "")
CATALOG_SUB2API_PASSWORD = os.getenv("CATALOG_SUB2API_PASSWORD", "")
ACCOUNT_REFRESH_ENABLED = os.getenv(
    "ACCOUNT_REFRESH_ENABLED",
    os.getenv("LEGACY_ACCOUNT_REFRESH_ENABLED", "1"),
) == "1"
NOTIFY_INTERVAL_SECONDS = int(os.getenv("NOTIFY_INTERVAL_SECONDS", "3600"))
REQUEST_TIMEOUT = int(os.getenv("UPSTREAM_REQUEST_TIMEOUT", "25"))
HISTORY_RETENTION_HOURS = int(os.getenv("HISTORY_RETENTION_HOURS", "72"))
HOURLY_HISTORY_RETENTION_DAYS = int(os.getenv("HOURLY_HISTORY_RETENTION_DAYS", "180"))
DEFAULT_CNY_RATE = Decimal(os.getenv("DEFAULT_CNY_RATE", "7.3"))
RECHARGE_ROUNDING_UNIT = Decimal(os.getenv("RECHARGE_ROUNDING_UNIT", "100"))
LOW_BALANCE_ALERT_CNY = Decimal(os.getenv("LOW_BALANCE_ALERT_CNY", "100"))
LOW_BALANCE_ALERT_COOLDOWN_SECONDS = int(os.getenv("LOW_BALANCE_ALERT_COOLDOWN_SECONDS", "21600"))
CHANNEL_ERROR_ALERT_COOLDOWN_SECONDS = int(os.getenv("CHANNEL_ERROR_ALERT_COOLDOWN_SECONDS", "21600"))
LOW_BALANCE_EMAIL_ENABLED = os.getenv("LOW_BALANCE_EMAIL_ENABLED", "0") == "1"
LOW_BALANCE_EMAIL_FROM = os.getenv("LOW_BALANCE_EMAIL_FROM", "noreply@mail.sandboxai.top")
LOW_BALANCE_EMAIL_SMTP_SERVER = os.getenv("LOW_BALANCE_EMAIL_SMTP_SERVER", "smtp.resend.com")
LOW_BALANCE_EMAIL_SMTP_PORT = int(os.getenv("LOW_BALANCE_EMAIL_SMTP_PORT", "2587"))
LOW_BALANCE_EMAIL_SMTP_USER = os.getenv("LOW_BALANCE_EMAIL_SMTP_USER", "resend")
LOW_BALANCE_EMAIL_SMTP_TOKEN = os.getenv("LOW_BALANCE_EMAIL_SMTP_TOKEN", "")
ALLOW_ADMIN_REGISTRATION = os.getenv("ALLOW_ADMIN_REGISTRATION", "0") == "1"
LOGIN_MAX_FAILURES = max(3, int(os.getenv("LOGIN_MAX_FAILURES", "5")))
LOGIN_ATTEMPT_WINDOW_SECONDS = max(60, int(os.getenv("LOGIN_ATTEMPT_WINDOW_SECONDS", "900")))
LOGIN_LOCK_SECONDS = max(60, int(os.getenv("LOGIN_LOCK_SECONDS", "900")))
def read_or_create_secret(path, size=32):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
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
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
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
































































def channel_value(channel, key, default=None):
    try:
        value = channel[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value








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






















NEW_API_LOG_TYPE_TOPUP = 1
NEW_API_LOG_TYPE_MANAGE = 3
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


































































































































def _load_domain_module(name):
    path = Path(__file__).with_name(f"{name}.py")
    exec(compile(path.read_text(), str(path), "exec"), globals())


for _domain_module in ("db", "auth", "catalog", "upstream_clients", "balance", "recharge", "notify", "scheduler"):
    _load_domain_module(_domain_module)

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "database": "ready", "time": now_iso()})
    except Exception:
        return jsonify({"ok": False, "database": "unavailable", "time": now_iso()}), 503


@app.get("/api/auth/bootstrap")
def auth_bootstrap():
    return jsonify({"ok": True, "data": auth_state()})


@app.post("/api/auth/register")
def auth_register():
    if not ALLOW_ADMIN_REGISTRATION:
        return response_error("管理员账号由服务器预设", 403)
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
    attempt_key = login_attempt_key(username)
    with db() as conn:
        retry_after = auth_security.retry_after(
            conn, attempt_key, LOGIN_ATTEMPT_WINDOW_SECONDS
        )
    if retry_after:
        return login_rate_limit_response(retry_after)
    row = get_user_by_username(username)
    if not row or not check_password_hash(row["password_hash"], password):
        with db() as conn:
            retry_after = auth_security.record_failure(
                conn,
                attempt_key,
                LOGIN_MAX_FAILURES,
                LOGIN_ATTEMPT_WINDOW_SECONDS,
                LOGIN_LOCK_SECONDS,
            )
        if retry_after:
            return login_rate_limit_response(retry_after)
        return response_error("账号或密码错误", 401)
    if row["totp_enabled"] and not verify_totp(row["totp_secret"], payload.get("totp")):
        return response_error("2FA 验证码错误", 401)
    with db() as conn:
        auth_security.clear_failures(conn, attempt_key)
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


@app.get("/api/catalog")
@login_required
def api_catalog():
    try:
        data, _ = refresh_channel_catalog()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[catalog-sync] ignored invalid catalog: {exc}", flush=True)
        with db() as conn:
            data = channel_catalog.list_catalog(conn)
    return jsonify({"ok": True, "data": data})


@app.post("/api/catalog/sync")
@login_required
def api_catalog_sync():
    if not CHANNEL_CATALOG_PATH.exists():
        return response_error("还没有收到备份渠道清单", 404)
    try:
        data, changed = refresh_channel_catalog(send_alerts=True)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return response_error(f"渠道清单不可用: {exc}")
    data["changed"] = changed
    return jsonify({"ok": True, "data": data})


@app.get("/api/catalog/accounts")
@login_required
def api_catalog_accounts():
    candidates = catalog_account_candidates()
    items = list_discovery_results()
    return jsonify({
        "ok": True,
        "data": {
            "source_count": len(candidates),
            "items": items,
        },
    })


@app.get("/api/routes")
@login_required
def api_routes():
    try:
        data = list_catalog_routes()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return response_error(f"读取渠道备份失败: {exc}", 400)
    return jsonify({"ok": True, "data": data})


@app.post("/api/routes/exclude")
@login_required
def api_exclude_route():
    payload = request_payload(force=True)
    try:
        exclude_catalog_address(payload.get("base_url"), payload.get("reason") or "手动移除")
        return jsonify({"ok": True, "data": list_catalog_routes()})
    except ValueError as exc:
        return response_error(str(exc))


@app.post("/api/routes/restore")
@login_required
def api_restore_route():
    payload = request_payload(force=True)
    try:
        restore_catalog_address(payload.get("base_url"))
        return jsonify({"ok": True, "data": list_catalog_routes()})
    except ValueError as exc:
        return response_error(str(exc))


@app.post("/api/catalog/accounts/sync")
@login_required
def api_catalog_accounts_sync():
    payload = request_payload(force=True)
    new_api_username = str(payload.get("new_api_username") or "").strip()
    new_api_password = payload.get("new_api_password") or ""
    sub2api_username = str(payload.get("sub2api_username") or "").strip()
    sub2api_password = payload.get("sub2api_password") or ""
    try:
        data = sync_catalog_accounts(
            new_api_username,
            new_api_password,
            sub2api_username,
            sub2api_password,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return response_error(f"读取渠道备份失败: {exc}", 400)
    save_catalog_sync_credentials(
        new_api_username, new_api_password, sub2api_username, sub2api_password
    )
    return jsonify({"ok": True, "data": data})


@app.post("/api/catalog/channels")
@login_required
def api_catalog_create():
    try:
        with db() as conn:
            catalog_id = channel_catalog.create_manual(conn, request_payload(force=True))
            data = channel_catalog.list_catalog(conn)
    except ValueError as exc:
        return response_error(str(exc))
    data["created_id"] = catalog_id
    return jsonify({"ok": True, "data": data})


@app.put("/api/catalog/channels/<int:catalog_id>")
@login_required
def api_catalog_update(catalog_id):
    try:
        with db() as conn:
            channel_catalog.update_local_fields(conn, catalog_id, request_payload(force=True))
            data = channel_catalog.list_catalog(conn)
    except LookupError as exc:
        return response_error(str(exc), 404)
    except ValueError as exc:
        return response_error(str(exc))
    return jsonify({"ok": True, "data": data})


@app.delete("/api/catalog/channels/<int:catalog_id>")
@login_required
def api_catalog_delete(catalog_id):
    try:
        with db() as conn:
            channel_catalog.delete_manual(conn, catalog_id)
            data = channel_catalog.list_catalog(conn)
    except LookupError as exc:
        return response_error(str(exc), 404)
    except ValueError as exc:
        return response_error(str(exc))
    return jsonify({"ok": True, "data": data})


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
    access_token = payload.get("access_token") or payload.get("token") or ""
    totp_code = payload.get("totp") or payload.get("totp_code") or ""
    cny_rate = rate_from(payload.get("cny_rate"))
    alert_cny = alert_threshold_from(payload.get("alert_cny"))
    boss_recharge_required = 1 if payload.get("boss_recharge_required") else 0
    if platform not in ("new_api", "sub2api"):
        return response_error("平台只支持 new_api 或 sub2api")
    if not name:
        name = base_url.replace("https://", "").replace("http://", "").strip("/")
    if not access_token and (not username or not password):
        return response_error("账号密码或访问 token 必填")
    try:
        credential, result = (provision_channel_token(platform, base_url, access_token)
                              if access_token else provision_channel(platform, base_url, username, password, totp_code))
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
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                platform,
                base_url,
                username,
                encrypt(password),
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
                    password_enc = ?,
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
                    encrypt(password),
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


@app.post("/api/channels/<int:channel_id>/token")
@login_required
def api_set_channel_token(channel_id):
    row = get_channel(channel_id)
    if not row:
        return response_error("渠道不存在", 404)
    payload = request_payload(force=True)
    try:
        credential, result = provision_channel_token(row["platform"], row["base_url"], payload.get("access_token") or payload.get("token"))
    except Exception as exc:
        return response_error(f"token 验证失败: {exc}", 502)
    ts = now_iso()
    with db() as conn:
        conn.execute("UPDATE channels SET credential_enc=?, password_enc='', status=?, message=?, balance=?, raw_balance=?, used_balance=?, raw_used_balance=?, request_count=?, currency=?, raw_response=?, last_checked_at=?, updated_at=?, refresh_failures=0, next_refresh_at=NULL WHERE id=?",
                     (encrypt(json.dumps(credential, ensure_ascii=False)), result.get("status", "ok"), result.get("message", ""), as_float(result.get("balance")), result.get("raw_balance"), as_float(result.get("used_balance")), result.get("raw_used_balance"), result.get("request_count"), result.get("currency", "USD"), json.dumps(result.get("raw_response") or {}, ensure_ascii=False), ts, ts, channel_id))
        record_balance_history(conn, channel_id, result, ts)
        prune_history(conn)
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
        app.add_url_rule(alias, endpoint, app.view_functions[rule.endpoint], methods=methods)






init_db()
register_api_aliases()
start_scheduler()


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8756")))
