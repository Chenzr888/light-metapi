import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import requests


DASHBOARD_WINDOWS = {
    "rolling": {"field": "rollingUsage", "label": "5 小时"},
    "weekly": {"field": "weeklyUsage", "label": "每周"},
    "monthly": {"field": "monthlyUsage", "label": "每月"},
}
DEFAULT_ALERT_THRESHOLDS = [20, 5, 0]
# Official OpenCode Go dollar caps per account (opencode.ai/docs/go).
WINDOW_CAPS_USD = {
    "rolling": 12.0,
    "weekly": 30.0,
    "monthly": 60.0,
}
# Pool remaining USD alert lines across all accounts with live data.
DEFAULT_POOL_ALERT_USD = {
    "rolling": 20.0,
    "weekly": 80.0,
    "monthly": 300.0,
}


class OpenCodeError(RuntimeError):
    def __init__(self, message, code="UPSTREAM_ERROR", status_code=502, details=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def mask_api_key(value):
    key = str(value or "").strip()
    if not key:
        return None
    if len(key) <= 12:
        return f"{key[:3]}...{key[-3:]}"
    return f"{key[:7]}...{key[-4:]}"


def normalize_dashboard_html(value):
    return (
        html.unescape(str(value or ""))
        .replace("\\u0022", '"')
        .replace("\\u0027", "'")
        .replace('\\"', '"')
    )


def parse_number(body, field):
    match = re.search(rf'["\']?{re.escape(field)}["\']?\s*:\s*"?(-?\d+(?:\.\d+)?)"?', body)
    return float(match.group(1)) if match else None


def parse_dashboard_usage(document, now=None):
    current = now or utc_now()
    text = normalize_dashboard_html(document)
    windows = {}
    for key, definition in DASHBOARD_WINDOWS.items():
        pattern = (
            rf'["\']?{definition["field"]}["\']?\s*:\s*'
            rf'(?:\$R\[\d+\]\s*=\s*)?\{{(?P<body>[^{{}}]*)\}}'
        )
        match = re.search(pattern, text, flags=re.DOTALL)
        if not match:
            continue
        used_percent = parse_number(match.group("body"), "usagePercent")
        reset_seconds_raw = parse_number(match.group("body"), "resetInSec")
        if used_percent is None or reset_seconds_raw is None:
            continue
        safe_used = max(0.0, min(100.0, used_percent))
        reset_seconds = max(0, int(reset_seconds_raw))
        windows[key] = {
            "key": key,
            "label": definition["label"],
            "used_percent": safe_used,
            "remaining_percent": max(0.0, 100.0 - safe_used),
            "reset_in_seconds": reset_seconds,
            "resets_at": iso(current + timedelta(seconds=reset_seconds)),
        }
    if not windows:
        raise OpenCodeError(
            "控制台响应中没有找到额度窗口",
            code="QUOTA_MARKUP_NOT_FOUND",
            details="登录 Cookie 可能已失效，控制台页面结构也可能已更新",
        )
    return windows


def normalize_auth_cookie(value):
    cookie = str(value or "").strip()
    return cookie if re.search(r"(?:^|;\s*)auth=", cookie) else f"auth={cookie}"


def response_excerpt(response):
    try:
        return str(response.text or "")[:400]
    except Exception:
        return ""


def fetch_dashboard_quota(
    workspace_id,
    auth_cookie,
    origin="https://opencode.ai",
    timeout=15,
    now=None,
    session=requests,
):
    current = now or utc_now()
    target = urljoin(origin.rstrip("/") + "/", f"workspace/{quote(workspace_id)}/go")
    try:
        response = session.get(
            target,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": normalize_auth_cookie(auth_cookie),
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
            },
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise OpenCodeError(f"连接 OpenCode 控制台失败: {exc}", code="DASHBOARD_NETWORK_ERROR") from exc

    if response.status_code >= 400:
        is_auth_error = response.status_code in (401, 403)
        raise OpenCodeError(
            f"OpenCode 控制台返回 HTTP {response.status_code}",
            code="DASHBOARD_AUTH_FAILED" if is_auth_error else "DASHBOARD_HTTP_ERROR",
            status_code=401 if is_auth_error else 502,
            details=response_excerpt(response),
        )

    final_path = urlparse(response.url or target).path
    if re.search(r"/(auth|login)(?:/|$)", final_path):
        raise OpenCodeError("OpenCode 控制台登录态已失效", code="DASHBOARD_AUTH_FAILED", status_code=401)

    document = str(response.text or "")
    if re.search(r'data-slot=["\']promo-models["\']', document, flags=re.I) and re.search(
        r"OpenCode Go starts at", document, flags=re.I
    ):
        raise OpenCodeError(
            "当前网页登录 Workspace 没有启用 OpenCode Go",
            code="GO_SUBSCRIPTION_NOT_FOUND",
            status_code=409,
            details="API key 与当前浏览器登录账号可能属于不同的 Workspace",
        )
    return {
        "workspace_id": workspace_id,
        "windows": parse_dashboard_usage(document, current),
        "fetched_at": iso(current),
    }


def validate_api_key(api_key, origin="https://opencode.ai", timeout=15, session=requests):
    target = urljoin(origin.rstrip("/") + "/", "zen/go/v1/chat/completions")
    try:
        response = session.post(
            target,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": "kimi-k3", "messages": "quota-key-validation"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise OpenCodeError(f"验证 OpenCode API key 失败: {exc}", code="API_KEY_VALIDATION_NETWORK_ERROR") from exc

    body = response_excerpt(response)
    if response.status_code == 401 and re.search(r"invalid api key", body, flags=re.I):
        raise OpenCodeError("OpenCode API key 无效", code="API_KEY_AUTH_FAILED", status_code=401)
    if response.status_code == 401 and re.search(r"no payment method|add a payment method", body, flags=re.I):
        return {
            "valid": True,
            "method": "authenticated_billing_probe",
            "upstream_state": "billing_required",
        }
    if response.status_code == 400 and re.search(r"invalid json request body|expected array", body, flags=re.I):
        return {"valid": True, "method": "authenticated_schema_probe", "upstream_state": "available"}
    if response.status_code == 429 and not re.search(r"invalid api key", body, flags=re.I):
        return {
            "valid": True,
            "method": "authenticated_schema_probe",
            "upstream_state": "rate_limited",
        }
    raise OpenCodeError(
        f"OpenCode key 鉴权探测返回 HTTP {response.status_code}",
        code="API_KEY_VALIDATION_UNEXPECTED",
        details=body,
    )


def fetch_models(api_key, origin="https://opencode.ai", timeout=15, session=requests):
    validation = validate_api_key(api_key, origin=origin, timeout=timeout, session=session)
    target = urljoin(origin.rstrip("/") + "/", "zen/go/v1/models")
    try:
        response = session.get(
            target,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise OpenCodeError(f"连接 OpenCode 模型接口失败: {exc}", code="MODELS_NETWORK_ERROR") from exc

    if response.status_code >= 400:
        is_auth_error = response.status_code in (401, 403)
        raise OpenCodeError(
            f"OpenCode 模型接口返回 HTTP {response.status_code}",
            code="API_KEY_AUTH_FAILED" if is_auth_error else "MODELS_HTTP_ERROR",
            status_code=401 if is_auth_error else 502,
            details=response_excerpt(response),
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenCodeError("OpenCode 模型接口返回了无效 JSON", code="MODELS_INVALID_RESPONSE") from exc
    source = payload if isinstance(payload, list) else payload.get("data", payload.get("models"))
    if not isinstance(source, list):
        raise OpenCodeError("OpenCode 模型接口返回了未知的数据结构", code="MODELS_INVALID_RESPONSE")
    models = [
        {
            "id": model.get("id") or model.get("name") or "unknown",
            "name": model.get("name") or model.get("id") or "unknown",
            "owned_by": model.get("owned_by") or model.get("ownedBy"),
        }
        for model in source
        if isinstance(model, dict)
    ]
    return {
        "count": len(models),
        "models": models,
        "key_valid": bool(validation.get("valid")),
        "validation_method": validation.get("method"),
        "upstream_state": validation.get("upstream_state", "available"),
        "fetched_at": iso(utc_now()),
    }


def public_error(error):
    return {
        "code": getattr(error, "code", "UPSTREAM_ERROR"),
        "message": str(error) or "上游请求失败",
        "details": getattr(error, "details", None),
        "status_code": int(getattr(error, "status_code", 502)),
    }


def parse_alert_thresholds(value=None):
    source = str(value or "").strip().split(",") if str(value or "").strip() else DEFAULT_ALERT_THRESHOLDS
    thresholds = []
    for item in source:
        try:
            number = round(float(item))
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 100 and number not in thresholds:
            thresholds.append(number)
    return sorted(thresholds, reverse=True)


def parse_pool_alert_usd(value=None):
    """Parse `rolling=20,weekly=80,monthly=300` (or bare defaults)."""
    result = dict(DEFAULT_POOL_ALERT_USD)
    text = str(value or "").strip()
    if not text:
        return result
    for part in text.split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, raw = item.split("=", 1)
        key = key.strip().lower()
        if key not in WINDOW_CAPS_USD:
            continue
        try:
            number = float(raw.strip())
        except (TypeError, ValueError):
            continue
        if number >= 0:
            result[key] = number
    return result


def round_usd(value):
    return round(float(value), 2)


def compute_pool_summary(accounts, caps=None, alert_thresholds=None, now=None):
    """Convert per-account usage % into a pooled USD remaining/used view."""
    current = now or utc_now()
    window_caps = caps or WINDOW_CAPS_USD
    thresholds = alert_thresholds or DEFAULT_POOL_ALERT_USD
    account_count = len(accounts or [])
    windows = {}
    for key, definition in DASHBOARD_WINDOWS.items():
        cap = float(window_caps.get(key, WINDOW_CAPS_USD[key]))
        used_usd = 0.0
        remaining_usd = 0.0
        samples = 0
        for account in accounts or []:
            if account.get("quota_error"):
                continue
            window = ((account.get("quota") or {}).get("windows") or {}).get(key)
            if not isinstance(window, dict):
                continue
            try:
                used_percent = float(window.get("used_percent"))
                remaining_percent = float(
                    window.get("remaining_percent", max(0.0, 100.0 - used_percent))
                )
            except (TypeError, ValueError):
                continue
            used_usd += max(0.0, min(100.0, used_percent)) / 100.0 * cap
            remaining_usd += max(0.0, min(100.0, remaining_percent)) / 100.0 * cap
            samples += 1
        total_usd = samples * cap
        alert_usd = float(thresholds.get(key, DEFAULT_POOL_ALERT_USD[key]))
        used_percent = round(used_usd / total_usd * 100.0, 1) if total_usd else None
        remaining_percent = round(remaining_usd / total_usd * 100.0, 1) if total_usd else None
        windows[key] = {
            "key": key,
            "label": definition["label"],
            "cap_usd": round_usd(cap),
            "used_usd": round_usd(used_usd),
            "remaining_usd": round_usd(remaining_usd),
            "total_usd": round_usd(total_usd),
            "used_percent": used_percent,
            "remaining_percent": remaining_percent,
            "samples": samples,
            "account_count": account_count,
            "alert_threshold_usd": round_usd(alert_usd),
            "below_threshold": bool(samples > 0 and remaining_usd <= alert_usd),
        }
    return {"windows": windows, "fetched_at": iso(current)}


def clean_error(error):
    if not error:
        return None
    return {
        "code": error.get("code") or "UNKNOWN_ERROR",
        "message": error.get("message") or "未知错误",
    }


def reset_key(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return round(parsed.timestamp() / 60)
    except (TypeError, ValueError):
        return None


def threshold_for_remaining(remaining_percent, thresholds):
    candidates = sorted(thresholds)
    return next((threshold for threshold in candidates if remaining_percent <= threshold), None)


def evaluate_alerts(
    accounts,
    previous_state=None,
    thresholds=None,
    pool_thresholds=None,
    now=None,
    include_account_quota_alerts=False,
):
    """Evaluate OpenCode alerts.

    Default monitoring is pooled USD remaining (5h / week / month). Per-account
    percentage thresholds stay available only when explicitly enabled.
    """
    current = now or utc_now()
    levels = thresholds or DEFAULT_ALERT_THRESHOLDS
    pool_levels = pool_thresholds or DEFAULT_POOL_ALERT_USD
    previous_accounts = (previous_state or {}).get("accounts", {})
    previous_pool = (previous_state or {}).get("pool", {})
    next_accounts = {}
    events = []
    for account in accounts:
        account_key = str(account.get("account_key") or account.get("id"))
        previous = previous_accounts.get(account_key, {})
        next_state = {
            "quota_error": clean_error(account.get("quota_error")),
            "models_error": clean_error(account.get("models_error")),
            "upstream_state": (account.get("models") or {}).get("upstream_state"),
            "windows": {},
        }
        common = {"account_id": account_key, "account_label": account.get("label") or account_key}
        if next_state["quota_error"] and next_state["quota_error"].get("code") != (previous.get("quota_error") or {}).get("code"):
            events.append({"type": "quota_error", **common, "error": next_state["quota_error"]})
        elif not next_state["quota_error"] and previous.get("quota_error"):
            events.append({"type": "quota_recovered", **common, "previous_error": previous["quota_error"]})

        if next_state["models_error"] and next_state["models_error"].get("code") != (previous.get("models_error") or {}).get("code"):
            events.append({"type": "key_error", **common, "error": next_state["models_error"]})
        elif not next_state["models_error"] and previous.get("models_error"):
            events.append({"type": "key_recovered", **common, "previous_error": previous["models_error"]})

        if next_state["upstream_state"] == "rate_limited" and previous.get("upstream_state") != "rate_limited":
            events.append({"type": "key_rate_limited", **common})
        elif (
            next_state["upstream_state"]
            and next_state["upstream_state"] != "rate_limited"
            and previous.get("upstream_state") == "rate_limited"
        ):
            events.append({"type": "key_recovered", **common, "previous_error": {"code": "RATE_LIMITED"}})

        for window_key, window in ((account.get("quota") or {}).get("windows") or {}).items():
            current_reset_key = reset_key(window.get("resets_at"))
            previous_window = (previous.get("windows") or {}).get(window_key, {})
            same_window = previous_window.get("reset_key") == current_reset_key
            notified = list(previous_window.get("notified_thresholds") or []) if same_window else []
            remaining = float(window.get("remaining_percent", 0))
            if include_account_quota_alerts:
                threshold = threshold_for_remaining(remaining, levels)
                if threshold is not None and threshold not in notified:
                    notified.append(threshold)
                    events.append(
                        {
                            "type": "quota_threshold",
                            **common,
                            "window_key": window_key,
                            "window_label": window.get("label") or window_key,
                            "threshold": threshold,
                            "remaining_percent": remaining,
                            "used_percent": float(window.get("used_percent", 0)),
                            "resets_at": window.get("resets_at"),
                        }
                    )
            next_state["windows"][window_key] = {
                "reset_key": current_reset_key,
                "resets_at": window.get("resets_at"),
                "remaining_percent": remaining,
                "notified_thresholds": sorted(notified, reverse=True),
            }
        next_accounts[account_key] = next_state

    pool = compute_pool_summary(accounts, alert_thresholds=pool_levels, now=current)
    next_pool = {}
    for window_key, window in (pool.get("windows") or {}).items():
        previous_window = previous_pool.get(window_key, {})
        was_below = bool(previous_window.get("below_threshold"))
        is_below = bool(window.get("below_threshold"))
        next_pool[window_key] = {
            "below_threshold": is_below,
            "remaining_usd": window.get("remaining_usd"),
            "alert_threshold_usd": window.get("alert_threshold_usd"),
            "samples": window.get("samples"),
        }
        if window.get("samples", 0) <= 0:
            continue
        if is_below and not was_below:
            events.append(
                {
                    "type": "pool_threshold",
                    "window_key": window_key,
                    "window_label": window.get("label") or window_key,
                    "remaining_usd": window.get("remaining_usd"),
                    "used_usd": window.get("used_usd"),
                    "total_usd": window.get("total_usd"),
                    "threshold_usd": window.get("alert_threshold_usd"),
                    "samples": window.get("samples"),
                    "account_count": window.get("account_count"),
                }
            )
        elif was_below and not is_below:
            events.append(
                {
                    "type": "pool_recovered",
                    "window_key": window_key,
                    "window_label": window.get("label") or window_key,
                    "remaining_usd": window.get("remaining_usd"),
                    "threshold_usd": window.get("alert_threshold_usd"),
                    "samples": window.get("samples"),
                    "account_count": window.get("account_count"),
                }
            )

    return {
        "events": events,
        "pool": pool,
        "state": {
            "version": 2,
            "updated_at": iso(current),
            "accounts": next_accounts,
            "pool": next_pool,
        },
    }


def format_alert_message(event, now=None):
    current = (now or utc_now()).astimezone(timezone(timedelta(hours=8)))
    account = event.get("account_label") or event.get("account_id") or "-"
    event_type = event.get("type")
    if event_type == "pool_threshold":
        title = "OpenCode Go 池额度预警"
        samples = event.get("samples")
        account_count = event.get("account_count")
        coverage = (
            f"{samples}/{account_count} 个账号计入"
            if samples is not None and account_count is not None
            else f"{samples or 0} 个账号计入"
        )
        lines = [
            title,
            f"窗口: {event.get('window_label')}",
            f"池剩余: ${event.get('remaining_usd')} / ${event.get('total_usd')}",
            f"已用: ${event.get('used_usd')}",
            f"告警线: ${event.get('threshold_usd')}",
            f"覆盖: {coverage}",
        ]
    elif event_type == "pool_recovered":
        lines = [
            "OpenCode Go 池额度已恢复",
            f"窗口: {event.get('window_label')}",
            f"池剩余: ${event.get('remaining_usd')}",
            f"告警线: ${event.get('threshold_usd')}",
        ]
    elif event_type == "quota_threshold":
        title = "OpenCode Go 额度已用完" if event.get("remaining_percent", 0) <= 0 else "OpenCode Go 额度预警"
        lines = [
            title,
            f"账号: {account}",
            f"窗口: {event.get('window_label')}",
            f"剩余: {event.get('remaining_percent')}%（已用 {event.get('used_percent')}%）",
            f"告警线: {event.get('threshold')}%",
            f"重置: {event.get('resets_at')}",
        ]
    elif event_type == "quota_error":
        lines = ["OpenCode Go 额度读取异常", f"账号: {account}", f"错误: {(event.get('error') or {}).get('message')}"]
    elif event_type == "quota_recovered":
        lines = ["OpenCode Go 额度读取已恢复", f"账号: {account}"]
    elif event_type == "key_error":
        lines = ["OpenCode Go Key 检查异常", f"账号: {account}", f"错误: {(event.get('error') or {}).get('message')}"]
    elif event_type == "key_rate_limited":
        lines = ["OpenCode Go Key 已触发限流", f"账号: {account}"]
    elif event_type == "key_recovered":
        lines = ["OpenCode Go Key 状态已恢复", f"账号: {account}"]
    elif event_type == "test":
        lines = ["OpenCode Go 看板告警测试成功", "统一监控通知通道已连通"]
    else:
        lines = ["OpenCode Go 看板通知", f"事件: {event_type}"]
    lines.append(f"时间: {current.strftime('%m-%d %H:%M')}")
    return "\n".join(lines)
