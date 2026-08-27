# light-metapi

light-metapi 是一个轻量上游真实余额监控工具。它支持 New API 和 Sub2API，添加账号时先验证登录并读取真实余额，之后只保留加密 Token 定时刷新。

[English README](README.md) | [引用说明](CITATION.md)

CY16 的受审核部署、自动回滚和员工权限流程见 [CY16 安全部署手册](DEPLOYMENT.md)。

## 效果预览

![安全登录页面](docs/images/login.png)

![余额管理面板](docs/images/dashboard.png)

## 功能

- 页面只保留管理员账号和密码登录，账号由服务器预设。
- 密码使用 scrypt 强哈希存储，连续 5 次失败后暂停登录 15 分钟。
- 支持 New API 的 Cookie 会话、Bearer Token 登录响应和可选 2FA。
- 支持 Sub2API 的 Bearer Token、双 profile 路径和 refresh token。
- 每 5 分钟自动刷新真实余额，也可手动刷新单个或全部账号。
- 上游访问令牌加密存储，企业微信 webhook 加密存储。
- 真实余额低于渠道告警线时发送通知。
- SQLite 数据库，Docker 单服务部署。

## 快速运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

默认监听 `127.0.0.1:8756`。

Docker:

```bash
docker compose up -d --build
```

`docker-compose.yml` 默认绑定 `127.0.0.1:8756`，适合放在 HTTPS 反向代理后面使用。容器硬内存上限为 512 MiB、内存预留为 192 MiB、交换区总上限为 768 MiB，并限制最多 128 个进程。

## 使用方式

1. 使用 `scripts/set-admin-password.py` 在服务器上预设管理员，再打开页面登录。
2. 点击“添加账号”，选择 New API 或 Sub2API，填写地址和账号密码。
3. 登录、用户资料和余额全部验证通过后才会保存。
4. 点击数据库图标可从最新小时备份中去重、识别并同步全部上游账号。
5. 之后系统自动刷新真实余额，不需要手工校准。

## 小时备份

备份脚本在完成小时备份时，会额外导出一份不含密钥的渠道清单，并默认同步到应用数据目录：

```text
data/channel-catalog.json
```

这份清单不含密钥，用于渠道目录和运营备份。余额监控的主数据源是上游账号自身的用户与余额接口，不使用消耗估算。

## 数据与安全

- SQLite 数据库：`data/upstreams.sqlite3`
- 加密密钥：`data/secret.key`
- Session 密钥：`data/session.secret`

添加渠道时，系统会使用提交的上游账号密码完成登录测试。测试成功后，本地使用 Fernet 保存加密后的密码和访问令牌，用于令牌过期后的自动重新登录；密钥文件权限为 `0600`，数据目录权限为 `0700`，接口不会返回密码或令牌。企业微信和飞书 webhook 同样使用本机密钥加密存储。

充值日志读取上游自身接口：

- New API：`/api/user/topup/self`
- Sub2API：`/api/v1/payment/orders/my`

本地充值日志只保存金额、状态、类型、时间和哈希后的来源引用。

## 自动化策略

- 每 5 分钟读取一次 New API/Sub2API 真实余额。
- 每小时检查最新备份，只尝试新增的上游地址；失败地址不会被定时重复撞登录。
- 真实余额低于渠道自定义告警线时发送通知。
- 余额读取失败时同样发送通知。
- 同一渠道低余额或读取失败告警默认 6 小时冷却一次，恢复后自动清除旧告警状态。
- 上游账号自动刷新默认开启。

## 环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 本地运行时监听地址 |
| `PORT` | `8756` | 服务端口 |
| `REFRESH_INTERVAL_SECONDS` | `300` | 余额刷新间隔 |
| `NOTIFY_INTERVAL_SECONDS` | `3600` | 企业微信汇总推送间隔 |
| `HISTORY_RETENTION_HOURS` | `72` | 余额历史保留时间 |
| `DEFAULT_CNY_RATE` | `7.3` | 默认 CNY/USD 折算汇率 |
| `LOW_BALANCE_ALERT_CNY` | `100` | 低余额告警阈值 |
| `LOW_BALANCE_ALERT_COOLDOWN_SECONDS` | `21600` | 单渠道告警冷却时间 |
| `CHANNEL_ERROR_ALERT_COOLDOWN_SECONDS` | `21600` | 余额读取失败告警冷却时间 |
| `UPSTREAM_REQUEST_TIMEOUT` | `25` | 上游请求超时时间 |
| `UPSTREAM_CHANNEL_CATALOG_PATH` | `data/channel-catalog.json` | 小时备份渠道清单路径 |
| `CATALOG_SYNC_INTERVAL_SECONDS` | `60` | 本地检查清单更新的间隔 |
| `CATALOG_ACCOUNT_SYNC_INTERVAL_SECONDS` | `3600` | 从备份检查新增上游账号的间隔 |
| `ACCOUNT_REFRESH_ENABLED` | `1` | 是否定时读取 New API/Sub2API 真实余额 |
| `ALLOW_ADMIN_REGISTRATION` | `0` | 是否开放网页首次注册，生产环境保持关闭 |
| `LOGIN_MAX_FAILURES` | `5` | 同一账号和来源地址允许的连续失败次数 |
| `LOGIN_ATTEMPT_WINDOW_SECONDS` | `900` | 登录失败计数窗口 |
| `LOGIN_LOCK_SECONDS` | `900` | 达到失败上限后的暂停登录时间 |

## 接口

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
- `GET /api/catalog`
- `POST /api/catalog/sync`
- `GET /api/catalog/accounts`
- `POST /api/catalog/accounts/sync`
- `POST /api/catalog/channels`
- `PUT /api/catalog/channels/:id`
- `DELETE /api/catalog/channels/:id`
