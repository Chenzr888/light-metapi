# Upstream Balance UI Preview

Local TypeScript preview for reworking the upstream-balance admin experience.

## Run

```bash
npm install
npm run dev -- --port 5177
```

Open `http://127.0.0.1:5177/`.

## Scope

- Uses Vite, React, TypeScript, and lucide-react.
- Uses local mock data for interaction design.
- Keeps the existing Python backend, production static UI, channel probing, balance refresh, and notifications unchanged.

## Previewed Flow

- First screen is a dashboard with KPIs and channel status.
- Channel add lives behind a clear `添加渠道` drawer.
- Per-channel actions are `刷新 / 充值 / 设置`.
- Delete lives inside the settings drawer.
- Low-balance alert copy is shown as `渠道名 + 余额 + 阈值`.
