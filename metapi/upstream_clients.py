"""Domain implementation loaded into the shared Flask application namespace."""

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
            SET credential_enc = ?, updated_at = ?
            WHERE id = ?
            """,
            (encrypt(json.dumps(credential, ensure_ascii=False)), now_iso(), channel_id),
        )


def save_channel_password(channel_id, password):
    if not password:
        return
    with db() as conn:
        conn.execute(
            "UPDATE channels SET password_enc = ?, updated_at = ? WHERE id = ?",
            (encrypt(password), now_iso(), channel_id),
        )


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


def new_api_headers(credential):
    headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
    token = credential.get("access_token")
    user_id = credential.get("user_id")
    if token and credential.get("auth_mode") != "session_cookie":
        headers["Authorization"] = f"Bearer {token}"
    if user_id:
        headers["New-Api-User"] = str(user_id)
    return headers


def new_api_client(credential, session=None):
    client = session or requests.Session()
    cookies = credential.get("cookies") if isinstance(credential, dict) else None
    if isinstance(cookies, dict) and cookies:
        client.cookies.update(cookies)
    return client


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
    return any(marker in message for marker in (
        "401", "unauthorized", "unauthenticated", "token", "expired", "fingerprint", "login again",
    ))


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
            try:
                credential = sub2api_refresh(base_url, credential.get("refresh_token"))
                if channel_id:
                    save_channel_credential(channel_id, credential)
                result = build_sub2api_result(sub2api_profile(base_url, credential["access_token"]))
                result["recharge_logs"] = safe_sub2api_recharge_logs(base_url, credential["access_token"])
                return result
            except RuntimeError:
                if credential.get("manual_token") or not channel_value(channel, "password_enc", ""):
                    raise

    password_enc = channel_value(channel, "password_enc", "")
    if not password_enc:
        raise RuntimeError("缺少可用令牌，请重新添加渠道")
    credential, user = sub2api_login(base_url, channel["username"], decrypt(password_enc))
    result = build_sub2api_result(sub2api_profile(base_url, credential["access_token"]), user)
    result["recharge_logs"] = safe_sub2api_recharge_logs(base_url, credential["access_token"])
    if channel_id:
        save_channel_credential(channel_id, credential)
    return result


def new_api_login_capabilities(base_url, session):
    try:
        status = session.get(
            urljoin(base_url, "/api/status"),
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        payload = safe_json(status)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "turnstile_required": bool(data.get("turnstile_check")),
            "user_agreement_enabled": bool(data.get("user_agreement_enabled")),
            "privacy_policy_enabled": bool(data.get("privacy_policy_enabled")),
            "password_login_enabled": data.get("password_login_enabled", True) is not False,
            "version": str(data.get("version") or ""),
        }
    except (AttributeError, requests.RequestException):
        return {}


def new_api_session_user(base_url, session):
    response = session.get(
        urljoin(base_url, "/api/user/self"),
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        timeout=REQUEST_TIMEOUT,
    )
    payload = safe_json(response)
    if response.status_code >= 400 or not truthy_success(payload):
        raise RuntimeError(read_message(payload) or f"读取登录用户失败 HTTP {response.status_code}")
    return payload.get("data") if isinstance(payload.get("data"), dict) else {}


def apply_new_api_login_payload(session, payload):
    data = payload if isinstance(payload, dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    access_token = data.get("access_token")
    if isinstance(access_token, str) and access_token:
        token_type = str(data.get("token_type") or "Bearer").strip() or "Bearer"
        session.headers["Authorization"] = f"{token_type} {access_token}"
        if user.get("id"):
            session.headers["New-Api-User"] = str(user["id"])
    return user


def new_api_login(base_url, username, password, totp_code=""):
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    capabilities = new_api_login_capabilities(base_url, session)
    if capabilities.get("password_login_enabled") is False:
        raise RuntimeError("上游已关闭账号密码登录，无法自动添加该渠道")
    if capabilities.get("turnstile_required"):
        raise RuntimeError("上游登录开启了 Turnstile 人机验证，暂不支持自动添加；请先在上游关闭登录 Turnstile")
    login = session.post(
        urljoin(base_url, "/api/user/login"),
        params={"turnstile": ""},
        json={"username": username, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    login_data = safe_json(login)
    if login.status_code >= 400 or not truthy_success(login_data):
        detail = read_message(login_data) or f"HTTP {login.status_code}"
        raise RuntimeError(f"登录阶段失败: {detail}")

    login_payload = login_data.get("data") if isinstance(login_data.get("data"), dict) else {}
    user = apply_new_api_login_payload(session, login_payload)
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
            detail = read_message(twofa_data) or f"HTTP {twofa.status_code}"
            raise RuntimeError(f"2FA 登录阶段失败: {detail}")
        twofa_payload = twofa_data.get("data") if isinstance(twofa_data.get("data"), dict) else {}
        user = apply_new_api_login_payload(session, twofa_payload)
    if not user.get("id"):
        try:
            user = new_api_session_user(base_url, session)
        except (AttributeError, requests.RequestException, RuntimeError) as exc:
            raise RuntimeError(f"登录成功但读取用户信息失败: {exc}") from exc
    return session, user


def new_api_generate_token(base_url, session, user_id):
    if not user_id:
        raise RuntimeError("令牌生成阶段失败: 登录响应里没有用户 ID")
    authorization = str(session.headers.get("Authorization") or "")
    try:
        cookies = requests.utils.dict_from_cookiejar(session.cookies)
    except (AttributeError, TypeError):
        cookies = {}
    if authorization.lower().startswith("bearer ") and authorization[7:].strip():
        return {
            "kind": "new_api_access_token",
            "access_token": authorization[7:].strip(),
            "user_id": user_id,
            "auth_mode": "bearer",
            "cookies": cookies,
            "issued_at": now_iso(),
        }
    token_resp = session.get(
        urljoin(base_url, "/api/user/token"),
        headers={"New-Api-User": str(user_id), "X-Requested-With": "XMLHttpRequest"},
        timeout=REQUEST_TIMEOUT,
    )
    token_data = safe_json(token_resp)
    if token_resp.status_code >= 400 or not truthy_success(token_data):
        detail = read_message(token_data) or f"HTTP {token_resp.status_code}"
        raise RuntimeError(f"令牌生成阶段失败: {detail}")
    token = token_data.get("data")
    if not isinstance(token, str) or not token:
        raise RuntimeError("令牌生成阶段失败: access token 响应为空")
    return {
        "kind": "new_api_access_token",
        "access_token": token,
        "user_id": user_id,
        "auth_mode": "session_cookie" if cookies else "bearer",
        "cookies": cookies,
        "issued_at": now_iso(),
    }


def new_api_self(base_url, credential, session=None):
    headers = new_api_headers(credential)
    user_id = credential.get("user_id")

    client = new_api_client(credential, session)
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
    client = new_api_client(credential, session)
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
    client = new_api_client(credential, session)
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
    password_enc = channel_value(channel, "password_enc", "")

    if credential.get("access_token"):
        try:
            result = build_new_api_result(base_url, new_api_self(base_url, credential))
            result["recharge_logs"] = safe_new_api_recharge_logs(base_url, credential)
            return result
        except RuntimeError as exc:
            message = str(exc).lower()
            if credential.get("manual_token") or not password_enc or not any(marker in message for marker in (
                "unauthorized", "not logged in", "invalid access token", "登录",
            )):
                raise

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


def provision_channel_token(platform, base_url, token):
    token = str(token or "").strip()
    if not token:
        raise RuntimeError("访问 token 不能为空")
    credential = {"kind": f"{platform}_token", "access_token": token, "manual_token": True, "issued_at": now_iso()}
    if platform == "new_api":
        result = build_new_api_result(base_url, new_api_self(base_url, credential))
        result["recharge_logs"] = safe_new_api_recharge_logs(base_url, credential)
    elif platform == "sub2api":
        result = build_sub2api_result(sub2api_profile(base_url, token))
        result["recharge_logs"] = safe_sub2api_recharge_logs(base_url, token)
    else:
        raise RuntimeError(f"未知平台: {platform}")
    return credential, result


def persist_provisioned_channel(name, platform, base_url, username, password, credential, result,
                                cny_rate=None, alert_cny=None, boss_recharge_required=False):
    ts = now_iso()
    cny_rate = rate_from(cny_rate)
    alert_cny = alert_threshold_from(alert_cny)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO channels(
                name, platform, base_url, username, password_enc, credential_enc, cny_rate, alert_cny, enabled,
                boss_recharge_required, balance, raw_balance, used_balance, raw_used_balance, request_count,
                currency, status, message, raw_response, last_checked_at, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                platform,
                normalize_url(base_url),
                username,
                encrypt(password),
                encrypt(json.dumps(credential, ensure_ascii=False)),
                as_float(cny_rate),
                as_float(alert_cny),
                1 if boss_recharge_required else 0,
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
    return cur.lastrowid


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
