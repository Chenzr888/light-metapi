"""Domain implementation loaded into the shared Flask application namespace."""

def scheduler_loop():
    last_catalog_sync = 0.0
    last_catalog_account_sync = 0.0
    last_account_refresh = 0.0
    tick_seconds = min(30, CATALOG_SYNC_INTERVAL_SECONDS, REFRESH_INTERVAL_SECONDS)
    while True:
        time.sleep(tick_seconds)
        current = time.monotonic()
        if current - last_catalog_sync >= CATALOG_SYNC_INTERVAL_SECONDS:
            try:
                refresh_channel_catalog(send_alerts=True)
            except Exception as exc:
                print(f"[catalog-scheduler] sync failed: {exc}", flush=True)
            last_catalog_sync = current
        account_sync_enabled = CATALOG_ACCOUNT_SYNC_ENABLED or setting_get("catalog_account_sync_enabled", "0") == "1"
        if account_sync_enabled and current - last_catalog_account_sync >= CATALOG_ACCOUNT_SYNC_INTERVAL_SECONDS:
            try:
                sync_catalog_accounts(*catalog_sync_credentials(), retry_failed=False)
            except Exception as exc:
                print(f"[catalog-account-scheduler] sync failed: {exc}", flush=True)
            last_catalog_account_sync = current
        if ACCOUNT_REFRESH_ENABLED and current - last_account_refresh >= REFRESH_INTERVAL_SECONDS:
            try:
                refresh_all(send_notify=False)
            except Exception as exc:
                print(f"[balance-scheduler] refresh failed: {exc}", flush=True)
            last_account_refresh = current


def start_scheduler():
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
