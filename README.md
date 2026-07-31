# light-metapi

Lightweight upstream balance monitoring for AI relay operators. light-metapi currently supports New API and Sub2API, stores channel access tokens after login validation, tracks recharge records from upstream logs, and sends WeCom alerts when balances are low.

[中文文档](README.zh-CN.md) | [Citation](CITATION.md)

See [CY16 deployment runbook](DEPLOYMENT.md) for the reviewed release, canary, and rollback process.

## Preview

![Secure access screen](docs/images/login.png)

![Balance dashboard](docs/images/dashboard.png)

## Features

- First-run admin registration with optional TOTP 2FA.
- New API and Sub2API channel balance refresh.
- Channel add flow with live login test before saving.
- Encrypted upstream access tokens and encrypted WeCom webhook storage.
- Recharge log sync from upstream APIs.
- Hourly WeCom summary plus low-balance alert under the configured CNY threshold.
- SQLite storage and single-service Docker deployment.
- OpenCode Go multi-account quota page for rolling, weekly, and monthly windows, converted into pooled USD remaining.
- OpenCode Go cookies and API keys reuse the encrypted store and administrator session.
- OpenCode Go pool alerts (default 5h < $20, weekly < $80, monthly < $300) reuse the existing WeCom, Lark, and email channels.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

The app listens on `127.0.0.1:8756` by default.

Docker:

```bash
docker compose up -d --build
```

The Compose file binds the service to `127.0.0.1:8756`, which works well behind an HTTPS reverse proxy. It also caps container memory at 512 MiB, reserves 192 MiB, caps memory plus swap at 768 MiB, and limits the container to 128 processes.

## Data And Security

- SQLite database: `data/upstreams.sqlite3`
- Encryption key: `data/secret.key`
- Session key: `data/session.secret`
- OpenCode Go accounts: `opencode_accounts`; cookies and API keys are encrypted with `data/secret.key`.

For a one-time migration from the standalone OpenCode Go dashboard, copy its
`config.json` to `data/opencode-import.json`. On startup the service imports the accounts,
encrypts the credentials, and removes the plaintext import file immediately.

When a channel is added, the app uses the submitted upstream username and password for one login validation request. After validation succeeds, it stores the encrypted upstream access token and keeps the password field empty. The WeCom webhook is also encrypted with the local key.

Recharge records are synced from upstream APIs:

- New API: `/api/user/topup/self`
- Sub2API: `/api/v1/payment/orders/my`

Local recharge storage keeps amount, status, type, time, and a hashed source reference.

## Automation

- Balance refresh interval: every 5 minutes.
- Balance history retention: 72 hours.
- WeCom summary interval: every hour.
- Low balance alert threshold: CNY 100 by default.
- Low balance alert cooldown: 6 hours per channel by default.

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Flask bind host for local runs |
| `PORT` | `8756` | Service port |
| `REFRESH_INTERVAL_SECONDS` | `300` | Balance refresh interval |
| `NOTIFY_INTERVAL_SECONDS` | `3600` | WeCom summary interval |
| `HISTORY_RETENTION_HOURS` | `72` | Balance history retention |
| `DEFAULT_CNY_RATE` | `7.3` | Default CNY/USD conversion rate |
| `LOW_BALANCE_ALERT_CNY` | `100` | Low balance alert threshold |
| `LOW_BALANCE_ALERT_COOLDOWN_SECONDS` | `21600` | Alert cooldown per channel |
| `UPSTREAM_REQUEST_TIMEOUT` | `25` | Upstream request timeout |
| `OPENCODE_GO_ALERT_INTERVAL_SECONDS` | `60` | OpenCode Go alert check interval |
| `OPENCODE_GO_ALERT_ENABLED` | `1` | Set to `0` to disable only OpenCode Go notifications while keeping shared notification channels active |
| `OPENCODE_GO_ALERT_THRESHOLDS` | `20,5,0` | Legacy per-account remaining-percent levels (off by default) |
| `OPENCODE_GO_POOL_ALERT_USD` | `rolling=20,weekly=80,monthly=300` | OpenCode Go pooled remaining-USD alert lines |
| `OPENCODE_GO_REFRESH_DEADLINE_SECONDS` | `90` | Overall multi-account refresh deadline before returning partial results |
| `OPENCODE_GO_REFRESH_WORKERS` | `16` | OpenCode Go concurrent refresh workers |
| `OPENCODE_GO_IMPORT_FILE` | `data/opencode-import.json` | One-time plaintext migration file, removed after processing |

## API Endpoints

- `GET /api/auth/bootstrap`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `POST /api/auth/2fa/setup`
- `POST /api/auth/2fa/confirm`
- `GET /api/channels`
- `POST /api/channels`
- `PUT /api/channels/:id`
- `DELETE /api/channels/:id`
- `POST /api/channels/:id/refresh`
- `POST /api/refresh`
- `GET /api/recharges`
- `GET /api/settings`
- `PUT /api/settings`
- `GET /api/opencode/accounts`
- `POST /api/opencode/accounts`
- `PUT /api/opencode/accounts/:id`
- `DELETE /api/opencode/accounts/:id`
- `POST /api/opencode/refresh`
- `GET /api/opencode/alerts`
- `POST /api/opencode/alerts/test`
