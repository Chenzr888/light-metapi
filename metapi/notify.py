"""Domain implementation loaded into the shared Flask application namespace."""

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


def channel_error_alert_key(channel_id):
    return f"channel_error_alerted_at:{channel_id}"


def should_send_channel_error_alert(channel):
    key = channel_error_alert_key(channel["id"])
    if channel.get("status") != "error":
        setting_delete(key)
        return False
    alerted_at = parse_iso_timestamp(setting_get(key))
    if not alerted_at:
        return True
    return datetime.now(timezone.utc) - alerted_at >= timedelta(seconds=CHANNEL_ERROR_ALERT_COOLDOWN_SECONDS)


def send_channel_error_alerts(channels):
    if setting_get("notify_enabled", "1") != "1":
        return []
    sent = []
    for channel in channels:
        if not should_send_channel_error_alert(channel):
            continue
        content = "\n".join([
            f"渠道名: {channel.get('name') or channel.get('base_url')}",
            "状态: 余额读取失败",
            f"原因: {str(channel.get('message') or '未知错误')[:300]}",
            f"上游: {channel.get('base_url')}",
        ])
        notify_sent = post_notification_text(content) if notification_webhooks_configured() else []
        email_sent = post_low_balance_email("上游余额读取失败", content)
        if notify_sent or email_sent:
            setting_set(channel_error_alert_key(channel["id"]), now_iso())
            sent.append(channel)
    return sent


def catalog_balance_alert_key(catalog_id):
    return f"catalog_balance_alerted_at:{catalog_id}"


def send_catalog_balance_alerts(data):
    if setting_get("notify_enabled", "1") != "1":
        return []
    sent = []
    for channel in data.get("items", []):
        if not channel.get("balance_configured") or not channel.get("present_in_source", True):
            continue
        balance = channel.get("estimated_balance")
        threshold = channel.get("alert_balance")
        if balance is None or threshold is None:
            continue
        key = catalog_balance_alert_key(channel["id"])
        if balance > threshold:
            setting_delete(key)
            continue
        alerted_at = parse_iso_timestamp(setting_get(key))
        if alerted_at and datetime.now(timezone.utc) - alerted_at < timedelta(seconds=LOW_BALANCE_ALERT_COOLDOWN_SECONDS):
            continue
        content = "\n".join([
            f"渠道名: {channel.get('alias') or channel.get('name')}",
            f"账本余额: {format_money(balance)} USD",
            f"告警线: {format_money(threshold)} USD",
            f"本期扣减: {format_money(channel.get('spent_since_calibration'))} USD",
            "来源: New API 小时消耗估算",
        ])
        notify_sent = post_notification_text(content) if notification_webhooks_configured() else []
        email_sent = post_low_balance_email("渠道账本余额告警", content)
        if notify_sent or email_sent:
            setting_set(key, now_iso())
            sent.append(channel)
    return sent
