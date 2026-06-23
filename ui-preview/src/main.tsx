import React, { FormEvent, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  ChevronDown,
  CircleDollarSign,
  ExternalLink,
  Eye,
  Gauge,
  Mail,
  Plus,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Trash2,
  WalletCards,
  X,
} from "lucide-react";
import { initialChannels, initialRecharges } from "./data";
import type { Channel, DraftChannel, Platform } from "./types";
import "./styles.css";

type DrawerMode = "add" | "settings" | null;
type FilterMode = "all" | "low" | "error";

const emptyDraft: DraftChannel = {
  name: "",
  platform: "new_api",
  baseUrl: "",
  username: "",
  password: "",
  cnyRate: "1",
  thresholdCny: "100",
  bossRechargeRequired: false,
};

function money(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function shortTime(value: string | null) {
  if (!value) return "未刷新";
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function platformLabel(platform: Platform) {
  return platform === "sub2api" ? "Sub2API" : "New API";
}

function isLow(channel: Channel) {
  return channel.status === "ok" && channel.balanceCny !== null && channel.balanceCny <= channel.thresholdCny;
}

function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) return <div className="spark-empty">暂无趋势</div>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * 120;
      const y = 36 - ((value - min) / span) * 30;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="sparkline" viewBox="0 0 120 42" aria-hidden="true">
      <polyline points={points} />
    </svg>
  );
}

function App() {
  const [channels, setChannels] = useState(initialChannels);
  const [drawerMode, setDrawerMode] = useState<DrawerMode>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [filter, setFilter] = useState<FilterMode>("all");
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<DraftChannel>(emptyDraft);
  const [syncing, setSyncing] = useState(false);
  const [toast, setToast] = useState("本地 TypeScript 预览");

  const selected = selectedId ? channels.find((item) => item.id === selectedId) ?? null : null;

  const stats = useMemo(() => {
    const ok = channels.filter((item) => item.status === "ok").length;
    const low = channels.filter(isLow).length;
    const error = channels.filter((item) => item.status === "error").length;
    const totalCny = channels
      .filter((item) => item.status === "ok")
      .reduce((sum, item) => sum + (item.balanceCny ?? 0), 0);
    const lastChecked = channels
      .map((item) => item.lastCheckedAt)
      .filter(Boolean)
      .sort()
      .at(-1) ?? null;
    return { ok, low, error, totalCny, lastChecked };
  }, [channels]);

  const visibleChannels = channels.filter((channel) => {
    const matchQuery = `${channel.name} ${channel.baseUrl} ${channel.username}`.toLowerCase().includes(query.toLowerCase());
    const matchFilter = filter === "all" || (filter === "low" && isLow(channel)) || (filter === "error" && channel.status === "error");
    return matchQuery && matchFilter;
  });

  function showToast(message: string) {
    setToast(message);
    window.clearTimeout(window.__uiPreviewToast);
    window.__uiPreviewToast = window.setTimeout(() => setToast(""), 2600);
  }

  function openAdd() {
    setDraft(emptyDraft);
    setSelectedId(null);
    setDrawerMode("add");
  }

  function openSettings(channel: Channel) {
    setSelectedId(channel.id);
    setDrawerMode("settings");
  }

  function refreshOne(id: number) {
    setChannels((items) =>
      items.map((item) =>
        item.id === id
          ? {
              ...item,
              status: "ok",
              balanceCny: item.balanceCny === null ? 0 : Number((item.balanceCny - Math.random() * 3).toFixed(2)),
              balanceUsd: item.balanceUsd === null ? 0 : Number((item.balanceUsd - Math.random() * 3).toFixed(2)),
              lastCheckedAt: new Date().toISOString(),
              message: "",
            }
          : item,
      ),
    );
    showToast("已刷新这条渠道");
  }

  function refreshAll() {
    setSyncing(true);
    window.setTimeout(() => {
      setChannels((items) =>
        items.map((item) => ({
          ...item,
          lastCheckedAt: new Date().toISOString(),
          balanceCny: item.balanceCny === null ? null : Number((item.balanceCny - Math.random() * 2).toFixed(2)),
          balanceUsd: item.balanceUsd === null ? null : Number((item.balanceUsd - Math.random() * 2).toFixed(2)),
        })),
      );
      setSyncing(false);
      showToast("已手动刷新余额");
    }, 550);
  }

  function createChannel(event: FormEvent) {
    event.preventDefault();
    const next: Channel = {
      id: Date.now(),
      name: draft.name.trim() || draft.baseUrl.replace(/^https?:\/\//, "").replace(/\/$/, ""),
      platform: draft.platform,
      baseUrl: draft.baseUrl.trim(),
      username: draft.username.trim(),
      status: "ok",
      balanceUsd: 100,
      balanceCny: 100,
      thresholdCny: Number(draft.thresholdCny || 100),
      cnyRate: Number(draft.cnyRate || 1),
      lastCheckedAt: new Date().toISOString(),
      rechargeUrl: `${draft.baseUrl.replace(/\/$/, "")}/${draft.platform === "sub2api" ? "purchase" : "console/topup"}`,
      bossRechargeRequired: draft.bossRechargeRequired,
      history: [100, 100],
    };
    setChannels((items) => [next, ...items]);
    setDrawerMode(null);
    showToast("渠道已通过本地预览添加");
  }

  function saveSettings(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    const form = event.currentTarget as HTMLFormElement;
    const formData = new FormData(form);
    setChannels((items) =>
      items.map((item) =>
        item.id === selected.id
          ? {
              ...item,
              name: String(formData.get("name") || item.name),
              thresholdCny: Number(formData.get("thresholdCny") || item.thresholdCny),
              cnyRate: Number(formData.get("cnyRate") || item.cnyRate),
              bossRechargeRequired: formData.get("bossRechargeRequired") === "on",
            }
          : item,
      ),
    );
    setDrawerMode(null);
    showToast("渠道设置已保存");
  }

  function deleteSelected() {
    if (!selected) return;
    const ok = window.confirm(`删除 ${selected.name}？`);
    if (!ok) return;
    setChannels((items) => items.filter((item) => item.id !== selected.id));
    setDrawerMode(null);
    showToast("渠道已删除");
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">UB</div>
          <div>
            <strong>Upstream Balance</strong>
            <span>TypeScript Preview</span>
          </div>
        </div>
        <nav className="nav">
          <button className="nav-item active"><Gauge size={18} /> 总览</button>
          <button className="nav-item"><WalletCards size={18} /> 渠道</button>
          <button className="nav-item"><Bell size={18} /> 告警</button>
          <button className="nav-item"><ShieldCheck size={18} /> 安全</button>
        </nav>
        <div className="sidebar-note">
          <span>核心后端保持现状</span>
          <strong>本地先看交互</strong>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <p className="eyebrow">余额监控工作台</p>
            <h1>渠道状态</h1>
          </div>
          <div className="top-actions">
            <div className="search-box">
              <Search size={16} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索渠道、URL、账号" />
            </div>
            <button className="icon-button" onClick={refreshAll} disabled={syncing} title="刷新余额">
              <RefreshCw size={18} className={syncing ? "spin" : ""} />
            </button>
            <button className="primary-button" onClick={openAdd}>
              <Plus size={18} /> 添加渠道
            </button>
          </div>
        </header>

        <section className="kpis">
          <article>
            <span>总余额</span>
            <strong>{money(stats.totalCny)} CNY</strong>
            <em>{channels.length} 条渠道</em>
          </article>
          <article>
            <span>正常</span>
            <strong>{stats.ok}</strong>
            <em>最近同步 {shortTime(stats.lastChecked)}</em>
          </article>
          <article className={stats.low ? "warn" : ""}>
            <span>低于阈值</span>
            <strong>{stats.low}</strong>
            <em>触发邮件 + 企业微信</em>
          </article>
          <article className={stats.error ? "danger" : ""}>
            <span>异常</span>
            <strong>{stats.error}</strong>
            <em>保留上次余额</em>
          </article>
        </section>

        <section className="workbench">
          <div className="section-head">
            <div>
              <h2>渠道列表</h2>
              <p>打开页面先显示缓存快照；刷新余额是明确的人工操作。</p>
            </div>
            <div className="segments">
              <button className={filter === "all" ? "selected" : ""} onClick={() => setFilter("all")}>全部</button>
              <button className={filter === "low" ? "selected" : ""} onClick={() => setFilter("low")}>低余额</button>
              <button className={filter === "error" ? "selected" : ""} onClick={() => setFilter("error")}>异常</button>
            </div>
          </div>

          <div className="channel-table">
            <div className="table-header">
              <span>渠道</span>
              <span>余额</span>
              <span>阈值</span>
              <span>趋势</span>
              <span>操作</span>
            </div>
            {visibleChannels.map((channel) => (
              <article className={`channel-row ${isLow(channel) ? "low" : ""}`} key={channel.id}>
                <div className="channel-main">
                  <div className="status-dot" data-status={channel.status} />
                  <div>
                    <strong>{channel.name}</strong>
                    <span>{platformLabel(channel.platform)} · {channel.username}</span>
                    <a href={channel.baseUrl} target="_blank" rel="noreferrer">{channel.baseUrl}</a>
                  </div>
                </div>
                <div>
                  <strong>{money(channel.balanceCny)} CNY</strong>
                  <span>{money(channel.balanceUsd)} USD</span>
                </div>
                <div>
                  <strong>{money(channel.thresholdCny)} CNY</strong>
                  <span>{isLow(channel) ? "需要关注" : "安全"}</span>
                </div>
                <Sparkline values={channel.history} />
                <div className="row-actions">
                  <button className="icon-button" title="刷新" onClick={() => refreshOne(channel.id)}><RefreshCw size={17} /></button>
                  <a className="icon-button" title="充值" href={channel.rechargeUrl} target="_blank" rel="noreferrer"><ExternalLink size={17} /></a>
                  <button className="icon-button" title="设置" onClick={() => openSettings(channel)}><Settings size={17} /></button>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="bottom-grid">
          <article className="panel">
            <div className="section-head compact">
              <h2>充值记录</h2>
              <button className="text-button">查看全部</button>
            </div>
            <div className="recharge-list">
              {initialRecharges.map((item) => (
                <div className="recharge-item" key={item.id}>
                  <CircleDollarSign size={18} />
                  <div>
                    <strong>{item.channelName}</strong>
                    <span>{item.source} · {shortTime(item.detectedAt)}</span>
                  </div>
                  <em>+{money(item.amountCny)} CNY</em>
                </div>
              ))}
            </div>
          </article>

          <article className="panel">
            <div className="section-head compact">
              <h2>告警通道</h2>
              <button className="text-button">编辑</button>
            </div>
            <div className="alert-stack">
              <div><CheckCircle2 size={18} /> 企业微信已配置</div>
              <div><CheckCircle2 size={18} /> 飞书已配置</div>
              <div><Mail size={18} /> cheny2812@qq.com</div>
              <div><AlertTriangle size={18} /> 低余额正文：渠道名 + 余额 + 阈值</div>
            </div>
          </article>
        </section>
      </main>

      {drawerMode && (
        <div className="drawer-layer" onClick={() => setDrawerMode(null)}>
          <aside className="drawer" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <span>{drawerMode === "add" ? "新增上游" : "渠道设置"}</span>
                <h2>{drawerMode === "add" ? "添加渠道" : selected?.name}</h2>
              </div>
              <button className="icon-button" onClick={() => setDrawerMode(null)}><X size={18} /></button>
            </div>

            {drawerMode === "add" ? (
              <form className="drawer-form" onSubmit={createChannel}>
                <label>渠道名<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如 青山 - 企业" /></label>
                <label>类型
                  <select value={draft.platform} onChange={(event) => setDraft({ ...draft, platform: event.target.value as Platform })}>
                    <option value="new_api">New API</option>
                    <option value="sub2api">Sub2API</option>
                  </select>
                </label>
                <label>URL<input value={draft.baseUrl} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} required placeholder="https://example.com/" /></label>
                <label>账号<input value={draft.username} onChange={(event) => setDraft({ ...draft, username: event.target.value })} required /></label>
                <label>密码<input value={draft.password} onChange={(event) => setDraft({ ...draft, password: event.target.value })} required type="password" /></label>
                <div className="form-grid">
                  <label>余额比例<input value={draft.cnyRate} onChange={(event) => setDraft({ ...draft, cnyRate: event.target.value })} type="number" step="0.0001" /></label>
                  <label>阈值<input value={draft.thresholdCny} onChange={(event) => setDraft({ ...draft, thresholdCny: event.target.value })} type="number" step="0.01" /></label>
                </div>
                <label className="check-line"><input type="checkbox" checked={draft.bossRechargeRequired} onChange={(event) => setDraft({ ...draft, bossRechargeRequired: event.target.checked })} /> 充值需联系老板</label>
                <button className="primary-button full" type="submit"><Eye size={18} /> 测试并保存</button>
              </form>
            ) : selected ? (
              <form className="drawer-form" onSubmit={saveSettings}>
                <label>渠道名<input name="name" defaultValue={selected.name} /></label>
                <label>阈值<input name="thresholdCny" type="number" step="0.01" defaultValue={selected.thresholdCny} /></label>
                <label>余额比例<input name="cnyRate" type="number" step="0.0001" defaultValue={selected.cnyRate} /></label>
                <label className="check-line"><input name="bossRechargeRequired" type="checkbox" defaultChecked={selected.bossRechargeRequired} /> 充值需联系老板</label>
                <button className="primary-button full" type="submit">保存设置</button>
                <button className="danger-button full" type="button" onClick={deleteSelected}><Trash2 size={18} /> 删除渠道</button>
              </form>
            ) : null}
          </aside>
        </div>
      )}

      <div className={`toast ${toast ? "show" : ""}`}>{toast}</div>
    </div>
  );
}

declare global {
  interface Window {
    __uiPreviewToast?: number;
  }
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
