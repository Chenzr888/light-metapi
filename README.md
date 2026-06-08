# Upstream Balance Monitor

Lightweight balance monitoring for upstream AI relay channels. It currently supports New API and Sub2API, stores channel access tokens after login validation, tracks recharge records from upstream logs, and sends WeCom alerts when balances are low.

[中文文档](README.zh-CN.md) | [Citation](CITATION.md)

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

The Compose file binds the service to `127.0.0.1:8756`, which works well behind an HTTPS reverse proxy.

## Data And Security

- SQLite database: `data/upstreams.sqlite3`
- Encryption key: `data/secret.key`
- Session key: `data/session.secret`

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
