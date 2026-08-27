"""Read the live New API channel catalog from PostgreSQL when configured."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

def remote_dsn():
    return os.getenv("UPSTREAM_REMOTE_PG_DSN", "").strip()

def fetch_catalog():
    dsn = remote_dsn()
    if not dsn:
        return None
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("UPSTREAM_REMOTE_PG_DSN configured but psycopg is unavailable") from exc
    query = '''SELECT id,name,type,status,COALESCE(base_url,''),COALESCE(models,''),COALESCE("group",''),priority,weight,balance,response_time,used_quota,balance_updated_time,COALESCE(tag,''),COALESCE(remark,'') FROM channels ORDER BY id'''
    with psycopg.connect(dsn, connect_timeout=5, options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    keys = ('id','name','type','status','base_url','models','group','priority','weight','balance','response_time','used_quota','balance_updated_time','tag','remark')
    return {"source":"newapi_bluegreen", "generated_at":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "channels":[dict(zip(keys,row)) for row in rows]}

def write_catalog(payload, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    os.chmod(path, 0o600)
