"""Refresh scheduling policy shared by the application and future workers."""
from datetime import datetime, timezone


def refresh_due(next_refresh_at, now=None):
    if not next_refresh_at:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        value = datetime.fromisoformat(str(next_refresh_at).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value <= now
    except ValueError:
        return True


def backoff_after_failures(failures):
    """Return retry interval in seconds; third failure enters hourly mode."""
    return 3600 if int(failures or 0) >= 3 else 0
