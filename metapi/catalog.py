"""Domain implementation loaded into the shared Flask application namespace."""

def catalog_account_candidates(path=CHANNEL_CATALOG_PATH):
    if not Path(path).exists():
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    grouped = {}
    for item in payload.get("channels", []):
        try:
            base_url = normalize_url(item.get("base_url"))
        except (AttributeError, ValueError):
            continue
        entry = grouped.setdefault(base_url, {"base_url": base_url, "names": []})
        name = str(item.get("name") or "").strip()
        if name and name not in entry["names"]:
            entry["names"].append(name)
    return list(grouped.values())


def detect_upstream_platform(base_url):
    base_url = normalize_url(base_url)
    errors = []
    try:
        response = requests.get(
            urljoin(base_url, "/api/status"),
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        payload = safe_json(response)
        data = payload.get("data") if isinstance(payload, dict) else None
        if response.status_code < 400 and isinstance(data, dict) and data:
            return "new_api", "识别为 New API"
        errors.append(f"New API HTTP {response.status_code}")
    except requests.RequestException as exc:
        errors.append(f"New API {type(exc).__name__}")

    try:
        response = requests.get(
            urljoin(base_url, "/api/v1/auth/me"),
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        payload = safe_json(response)
        if response.status_code in (401, 403) and isinstance(payload, dict):
            return "sub2api", "识别为 Sub2API"
        errors.append(f"Sub2API HTTP {response.status_code}")
    except requests.RequestException as exc:
        errors.append(f"Sub2API {type(exc).__name__}")
    return "", "; ".join(errors)


def save_discovery_result(candidate, platform, state, message, channel_id=None):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO channel_discovery(base_url, source_names, platform, state, message, channel_id, checked_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(base_url) DO UPDATE SET
                source_names = excluded.source_names,
                platform = excluded.platform,
                state = excluded.state,
                message = excluded.message,
                channel_id = excluded.channel_id,
                checked_at = excluded.checked_at
            """,
            (
                candidate["base_url"],
                json.dumps(candidate.get("names") or [], ensure_ascii=False),
                platform or None,
                state,
                str(message or "")[:1000],
                channel_id,
                now_iso(),
            ),
        )


def list_discovery_results():
    with db() as conn:
        rows = conn.execute(
            "SELECT base_url, source_names, platform, state, message, channel_id, checked_at "
            "FROM channel_discovery ORDER BY base_url"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["source_names"] = json.loads(item["source_names"] or "[]")
        except json.JSONDecodeError:
            item["source_names"] = []
        result.append(item)
    return result


def list_catalog_exclusions():
    with db() as conn:
        rows = conn.execute("SELECT base_url, reason FROM channel_exclusions").fetchall()
    return {normalize_url(row["base_url"]): row["reason"] for row in rows}


def exclude_catalog_address(base_url, reason="手动移除"):
    base_url = normalize_url(base_url)
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO channel_exclusions(base_url, reason, created_at, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(base_url) DO UPDATE SET reason=excluded.reason, updated_at=excluded.updated_at
            """,
            (base_url, str(reason or "手动移除")[:200], ts, ts),
        )
        conn.execute(
            """
            INSERT INTO channel_discovery(base_url, source_names, state, message, checked_at)
            VALUES(?, '[]', 'excluded', ?, ?)
            ON CONFLICT(base_url) DO UPDATE SET state='excluded', message=excluded.message, checked_at=excluded.checked_at
            """,
            (base_url, str(reason or "手动移除")[:200], ts),
        )


def restore_catalog_address(base_url):
    base_url = normalize_url(base_url)
    with db() as conn:
        conn.execute("DELETE FROM channel_exclusions WHERE base_url = ?", (base_url,))
        conn.execute(
            "UPDATE channel_discovery SET state='pending', message='等待重新识别', checked_at=? WHERE base_url=? AND state='excluded'",
            (now_iso(), base_url),
        )


def list_catalog_routes():
    if not CHANNEL_CATALOG_PATH.exists():
        return {"items": [], "summary": {"routes": 0, "addresses": 0, "monitored_addresses": 0, "pending_addresses": 0, "excluded_addresses": 0}}
    payload = json.loads(CHANNEL_CATALOG_PATH.read_text(encoding="utf-8"))
    with db() as conn:
        monitor_rows = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
    monitors = {normalize_url(row["base_url"]): row_to_channel(row) for row in monitor_rows}
    discoveries = {item["base_url"]: item for item in list_discovery_results()}
    exclusions = list_catalog_exclusions()
    grouped = {}
    for source in payload.get("channels", []):
        try:
            base_url = normalize_url(source.get("base_url"))
        except (AttributeError, ValueError):
            base_url = str(source.get("base_url") or "")
        if not base_url:
            continue
        models = source.get("models") or []
        if isinstance(models, str):
            models = [item.strip() for item in models.split(",") if item.strip()]
        item = grouped.setdefault(base_url, {
            "id": source.get("id"),
            "route_ids": [],
            "name": source.get("name") or f"渠道 {source.get('id')}",
            "route_status": source.get("status"),
            "base_url": base_url,
            "group": source.get("group") or "",
            "models": [],
        })
        if source.get("id") is not None:
            item["route_ids"].append(source.get("id"))
        if source.get("name") and source.get("name") != item["name"]:
            item.setdefault("route_names", []).append(source.get("name"))
        if source.get("status") == 1:
            item["route_status"] = 1
        if not item["group"] and source.get("group"):
            item["group"] = source.get("group")
        item["models"] = sorted(set(item["models"]) | set(models))

    items = []
    for base_url, item in grouped.items():
        monitor = monitors.get(base_url)
        discovery = discoveries.get(base_url, {})
        if base_url in exclusions and not monitor:
            discovery = {**discovery, "state": "excluded", "message": exclusions[base_url]}
        item["platform"] = monitor.get("platform") if monitor else discovery.get("platform")
        item["monitor"] = monitor
        item["discovery_state"] = discovery.get("state") or "pending"
        item["discovery_message"] = discovery.get("message") or "等待识别"
        items.append(item)
    items.sort(key=lambda item: (int(item["id"]) if str(item.get("id", "")).isdigit() else 0, item["base_url"]))
    monitored_addresses = len({item["base_url"] for item in items if item["monitor"]})
    excluded_addresses = len({item["base_url"] for item in items if item["discovery_state"] == "excluded"})
    return {
        "items": items,
        "generated_at": payload.get("generated_at"),
        "summary": {
            "routes": len(items),
            "addresses": len(items),
            "monitored_addresses": monitored_addresses,
            "pending_addresses": len(items) - monitored_addresses - excluded_addresses,
            "excluded_addresses": excluded_addresses,
        },
    }


def save_catalog_sync_credentials(new_api_username, new_api_password, sub2api_username, sub2api_password):
    setting_set("catalog_new_api_username", new_api_username)
    setting_set("catalog_new_api_password_enc", encrypt(new_api_password))
    setting_set("catalog_sub2api_username", sub2api_username)
    setting_set("catalog_sub2api_password_enc", encrypt(sub2api_password))
    setting_set("catalog_account_sync_enabled", "1")


def catalog_sync_credentials():
    new_password_enc = setting_get("catalog_new_api_password_enc")
    sub_password_enc = setting_get("catalog_sub2api_password_enc")
    return (
        setting_get("catalog_new_api_username", CATALOG_NEW_API_USERNAME),
        decrypt(new_password_enc) if new_password_enc else CATALOG_NEW_API_PASSWORD,
        setting_get("catalog_sub2api_username", CATALOG_SUB2API_USERNAME),
        decrypt(sub_password_enc) if sub_password_enc else CATALOG_SUB2API_PASSWORD,
    )


def sync_catalog_accounts(new_api_username, new_api_password, sub2api_username, sub2api_password,
                          retry_failed=True):
    candidates = catalog_account_candidates()
    previous = {item["base_url"]: item for item in list_discovery_results()}
    with db() as conn:
        existing_rows = conn.execute("SELECT id, base_url, platform FROM channels").fetchall()
    existing = {
        normalize_url(row["base_url"]): {"id": row["id"], "platform": row["platform"]}
        for row in existing_rows
    }
    summary = {"total": len(candidates), "existing": 0, "imported": 0, "failed": 0, "unknown": 0}

    pending = []
    for candidate in candidates:
        current = existing.get(candidate["base_url"])
        if candidate["base_url"] in list_catalog_exclusions():
            summary.setdefault("excluded", 0)
            summary["excluded"] += 1
            continue
        if current:
            password = new_api_password if current["platform"] == "new_api" else sub2api_password
            save_channel_password(current["id"], password)
            save_discovery_result(
                candidate, current["platform"], "existing", "已在余额监控中", current["id"]
            )
            summary["existing"] += 1
        elif not retry_failed and candidate["base_url"] in previous:
            state = previous[candidate["base_url"]].get("state")
            if state in ("failed", "unknown"):
                summary[state] += 1
            else:
                summary["failed"] += 1
        else:
            pending.append(candidate)

    def inspect_and_login(candidate):
        platform, message = detect_upstream_platform(candidate["base_url"])
        if not platform:
            return candidate, platform, "unknown", message, None, None, "", ""
        accounts = [(sub2api_username, sub2api_password)] if platform == "sub2api" else [
            (new_api_username, new_api_password),
            (sub2api_username, sub2api_password),
        ]
        accounts = list(dict.fromkeys((username, password) for username, password in accounts if username and password))
        if not accounts:
            return candidate, platform, "failed", f"{message}，但未提供对应账密", None, None, "", ""
        failures = []
        for username, password in accounts:
            try:
                credential, result = provision_channel(platform, candidate["base_url"], username, password)
                return candidate, platform, "ready", message, credential, result, username, password
            except Exception as exc:
                failures.append(str(exc))
                if any(marker in str(exc).lower() for marker in ("turnstile", "verification", "2fa")):
                    break
        return candidate, platform, "failed", failures[-1], None, None, "", ""

    max_workers = min(6, max(1, len(pending)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        inspected = list(executor.map(inspect_and_login, pending))

    for candidate, platform, state, message, credential, result, used_username, used_password in inspected:
        if state == "ready":
            name = candidate["names"][0] if candidate.get("names") else candidate["base_url"]
            try:
                channel_id = persist_provisioned_channel(
                    name, platform, candidate["base_url"], used_username, used_password, credential, result
                )
            except Exception as exc:
                save_discovery_result(candidate, platform, "failed", exc)
                summary["failed"] += 1
                continue
            save_discovery_result(candidate, platform, "imported", "登录成功并读取到真实余额", channel_id)
            summary["imported"] += 1
        else:
            save_discovery_result(candidate, platform, state, message)
            summary[state] += 1
    summary["items"] = list_discovery_results()
    return summary


def refresh_channel_catalog(send_alerts=False):
    with db() as conn:
        changed = channel_catalog.sync_file(conn, CHANNEL_CATALOG_PATH)
        data = channel_catalog.list_catalog(conn)
    if changed and send_alerts:
        send_catalog_balance_alerts(data)
    return data, changed
