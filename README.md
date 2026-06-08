# Upstream Balance Monitor

轻量上游余额监控，当前支持 New API 和 Sub2API。

## 运行

```bash
python3 app.py
```

默认监听 `127.0.0.1:8756`。

首次打开页面会创建管理员账号。登录后可以在页面里绑定 TOTP 2FA。

## 数据

- SQLite: `data/upstreams.sqlite3`
- 加密密钥: `data/secret.key`
- Session 密钥: `data/session.secret`

渠道新增时会先用账号密码测试上游登录，然后只保存加密后的上游访问令牌。企业微信 webhook 使用本机密钥加密后存储。

## 自动化

- 每 5 分钟探测一次余额。
- 保留 72 小时历史点。
- 企业微信汇总默认每小时一次。
- 折算 CNY 余额低于 100 时发送企业微信告警，同一渠道默认 6 小时冷却一次。

## 接口

- `GET /api/auth/bootstrap`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `POST /api/auth/2fa/setup`
- `POST /api/auth/2fa/confirm`
- `GET /api/channels`
- `POST /api/channels`
- `POST /api/channels/:id/refresh`
- `POST /api/refresh`
- `GET /api/recharges`
- `GET /api/settings`
- `PUT /api/settings`
