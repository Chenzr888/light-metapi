import React, { FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  CircleDollarSign,
  Clock3,
  Cpu,
  ExternalLink,
  Eye,
  Gauge,
  KeyRound,
  LogOut,
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
import {
  confirmTwofa,
  createChannel as apiCreateChannel,
  deleteChannel as apiDeleteChannel,
  loadAuth,
  loadChannels,
  loadOpenCodeAccounts,
  loadOpenCodeAlerts,
  loadRecharges,
  loadSettings,
  login,
  logout as apiLogout,
  refreshAllChannels,
  refreshChannel,
  refreshOpenCodeAccounts,
  saveAlertSettings,
  saveOpenCodeAccount,
  setupTwofa,
  testOpenCodeAlerts,
  testWebhook,
  updateChannel,
  deleteOpenCodeAccount,
} from "./api";
import type {
  AuthState,
  Channel,
  DraftChannel,
  OpenCodeAccount,
  OpenCodeAlertStatus,
  OpenCodeDraft,
  OpenCodeWindow,
  Platform,
  RechargeLog,
  SettingsState,
} from "./types";
import "./styles.css";

type DrawerMode = "add" | "settings" | null;
type FilterMode = "all" | "low" | "error";
type ViewMode = "balance" | "opencode";
type OpenCodeFilterMode = "all" | "attention" | "healthy";
type OpenCodeSortMode = "usage" | "name";

const openCodeWindowKeys = ["rolling", "weekly", "monthly"] as const;
const openCodeWindowLabels: Record<(typeof openCodeWindowKeys)[number], string> = {
  rolling: "5H",
  weekly: "周",
  monthly: "月",
};

const emptyDraft: DraftChannel = {
  name: "",
  platform: "new_api",
  baseUrl: "",
  username: "",
  password: "",
  totp: "",
  cnyRate: "1",
  thresholdCny: "100",
  bossRechargeRequired: false,
};

const emptyAuth: AuthState = {
  needsSetup: false,
  authenticated: false,
  username: "",
  totpEnabled: false,
};

const emptyOpenCodeDraft: OpenCodeDraft = {
  label: "",
  workspaceId: "",
  authCookie: "",
  apiKey: "",
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

function resetTime(seconds: number) {
  if (seconds <= 0) return "即将重置";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.max(1, Math.floor((seconds % 3600) / 60));
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function quotaTone(remaining: number) {
  if (remaining <= 5) return "danger";
  if (remaining <= 20) return "warn";
  return "ok";
}

function openCodeNeedsAttention(account: OpenCodeAccount) {
  const windows = Object.values(account.quota?.windows || {});
  return Boolean(
    account.quotaError
    || account.modelsError
    || !account.quota
    || windows.some((window) => window.remainingPercent <= 20),
  );
}

function openCodePeakUsage(account: OpenCodeAccount) {
  const values = openCodeWindowKeys
    .map((key) => account.quota?.windows[key]?.usedPercent)
    .filter((value): value is number => typeof value === "number");
  return values.length ? Math.max(...values) : -1;
}

function OpenCodeQuotaCell({
  quota,
  label,
}: {
  quota?: OpenCodeWindow;
  label: string;
}) {
  if (!quota) {
    return (
      <div className="opencode-quota-cell empty" data-label={label}>
        <div className="opencode-quota-value"><strong>--</strong><span>已用</span></div>
        <div className="opencode-usage-track" aria-hidden="true"><i /></div>
        <small>暂未获取额度</small>
      </div>
    );
  }
  const used = Math.max(0, Math.min(100, quota.usedPercent));
  return (
    <div className={`opencode-quota-cell ${quotaTone(quota.remainingPercent)}`} data-label={label}>
      <div className="opencode-quota-value">
        <strong>{money(used, 1)}%</strong>
        <span>已用</span>
      </div>
      <div
        className="opencode-usage-track"
        role="progressbar"
        aria-label={`${label}已用 ${money(used, 1)}%`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={used}
      >
        <i style={{ width: `${used}%` }} />
      </div>
      <small>剩余 {money(quota.remainingPercent, 1)}% / {resetTime(quota.resetInSeconds)}后重置</small>
    </div>
  );
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

function OpenCodeWorkspace({
  accounts,
  alerts,
  syncing,
  loadError,
  onRefresh,
  onAdd,
  onEdit,
  onDelete,
  onTestAlerts,
}: {
  accounts: OpenCodeAccount[];
  alerts: OpenCodeAlertStatus | null;
  syncing: boolean;
  loadError: string;
  onRefresh: () => void;
  onAdd: () => void;
  onEdit: (account: OpenCodeAccount) => void;
  onDelete: (account: OpenCodeAccount) => void;
  onTestAlerts: () => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<OpenCodeFilterMode>("all");
  const [sort, setSort] = useState<OpenCodeSortMode>("usage");

  const active = accounts.filter((account) => account.quota && !account.quotaError).length;
  const warnings = accounts.filter(openCodeNeedsAttention).length;
  const aggregateWindows = useMemo(() => openCodeWindowKeys.map((key) => {
    const values = accounts
      .map((account) => account.quota?.windows[key]?.usedPercent)
      .filter((value): value is number => typeof value === "number");
    const average = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
    return { key, label: openCodeWindowLabels[key], average, samples: values.length };
  }), [accounts]);
  const visibleAccounts = useMemo(() => accounts
    .filter((account) => {
      const haystack = `${account.label} ${account.accountKey} ${account.workspaceId || ""}`.toLowerCase();
      const matchesQuery = haystack.includes(query.trim().toLowerCase());
      const needsAttention = openCodeNeedsAttention(account);
      const matchesFilter = filter === "all"
        || (filter === "attention" && needsAttention)
        || (filter === "healthy" && !needsAttention);
      return matchesQuery && matchesFilter;
    })
    .sort((left, right) => {
      if (sort === "name") return left.label.localeCompare(right.label, "zh-CN");
      return openCodePeakUsage(right) - openCodePeakUsage(left);
    }), [accounts, filter, query, sort]);

  return (
    <>
      <header className="topbar opencode-topbar">
        <div>
          <p className="eyebrow">OpenCode Go 统一监控</p>
          <h1>账号额度</h1>
        </div>
        <div className="top-actions">
          <button className="icon-button" onClick={onRefresh} disabled={syncing} title="刷新 OpenCode Go">
            <RefreshCw size={18} className={syncing ? "spin" : ""} />
          </button>
          <button className="primary-button" onClick={onAdd}><Plus size={18} /> 添加账号</button>
        </div>
      </header>

      <section className="kpis opencode-kpis">
        <article><span>账号总数</span><strong>{accounts.length}</strong><em>凭据只在服务端加密保存</em></article>
        <article className={warnings ? "warn" : ""}><span>关注 / 临界</span><strong>{warnings}</strong><em>额度、Cookie 或 Key 需要检查</em></article>
        <article><span>已连接</span><strong>{active}</strong><em>三个额度窗口读取正常</em></article>
      </section>

      {accounts.length ? (
        <section className="opencode-monitor">
          <div className="opencode-toolbar">
            <label className="search-box opencode-search">
              <Search size={16} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索账号、Workspace"
                aria-label="搜索 OpenCode Go 账号"
              />
            </label>
            <div className="opencode-toolbar-right">
              <div className="segments" aria-label="账号状态筛选">
                <button className={filter === "all" ? "selected" : ""} onClick={() => setFilter("all")}>全部</button>
                <button className={filter === "attention" ? "selected" : ""} onClick={() => setFilter("attention")}>需关注</button>
                <button className={filter === "healthy" ? "selected" : ""} onClick={() => setFilter("healthy")}>健康</button>
              </div>
              <select className="sort-select" value={sort} onChange={(event) => setSort(event.target.value as OpenCodeSortMode)} aria-label="账号排序">
                <option value="usage">按最高使用率</option>
                <option value="name">按账号名称</option>
              </select>
            </div>
          </div>

          <div className="opencode-aggregate" aria-label="平均额度消耗">
            {aggregateWindows.map((window) => {
              const remaining = window.average === null ? 100 : 100 - window.average;
              return (
                <div className={`opencode-aggregate-item ${quotaTone(remaining)}`} key={window.key}>
                  <div><span>{window.label} 平均已用</span><strong>{window.average === null ? "--" : `${money(window.average, 1)}%`}</strong></div>
                  <div className="opencode-aggregate-track" aria-hidden="true"><i style={{ width: `${window.average || 0}%` }} /></div>
                  <small>{window.samples ? `${window.samples} 个账号有实时数据` : "等待首次刷新"}</small>
                </div>
              );
            })}
          </div>

          <div className="opencode-table-scroll">
            <div className="opencode-table" role="table" aria-label="OpenCode Go 账号额度">
              <div className="opencode-row-grid opencode-table-header" role="row">
                <span role="columnheader">账号 / Workspace</span>
                <span role="columnheader">分组</span>
                <div className="opencode-quota-columns opencode-quota-head" role="columnheader">
                  <span>5H</span><span>周</span><span>月</span>
                </div>
                <span role="columnheader">模型</span>
                <span role="columnheader">状态</span>
                <span role="columnheader">操作</span>
              </div>
              {visibleAccounts.map((account, index) => {
                const needsAttention = openCodeNeedsAttention(account);
                const quotaIsLow = Object.values(account.quota?.windows || {})
                  .some((window) => window.remainingPercent <= 20);
                const cacheStatus = account.quota?.cache?.status;
                const healthLabel = account.quotaError
                  ? "额度异常"
                  : account.modelsError
                    ? "Key 异常"
                    : quotaIsLow
                      ? "额度临界"
                      : account.quota
                        ? "健康"
                        : "待配置";
                const healthDetail = cacheStatus === "stale" ? "缓存数据" : cacheStatus ? "实时数据" : "等待同步";
                return (
                  <article className={`opencode-row-grid opencode-account-row ${needsAttention ? "attention" : ""}`} role="row" key={account.id}>
                    <div className="opencode-account-identity" role="cell">
                      <span className="opencode-account-index">{String(index + 1).padStart(2, "0")}</span>
                      <div>
                        <strong>{account.label}</strong>
                        <span title={account.workspaceId || account.accountKey}>{account.workspaceId || account.accountKey}</span>
                      </div>
                    </div>
                    <div className="opencode-provider" role="cell"><strong>OpenCode</strong><span>Go</span></div>
                    <div className="opencode-quota-columns" role="cell">
                      {openCodeWindowKeys.map((key) => (
                        <OpenCodeQuotaCell key={key} quota={account.quota?.windows[key]} label={openCodeWindowLabels[key]} />
                      ))}
                    </div>
                    <div className="opencode-models" role="cell">
                      <strong>{account.models ? account.models.count : "--"}</strong>
                      <span>{account.models?.upstreamState === "rate_limited" ? "Key 限流" : account.modelsError?.message || "可用模型"}</span>
                    </div>
                    <div className="opencode-health" role="cell">
                      <span className={`state-pill ${needsAttention ? "warn" : "ok"}`}>{healthLabel}</span>
                      <small>{healthDetail}</small>
                    </div>
                    <div className="row-actions opencode-actions" role="cell">
                      <button className="icon-button" aria-label={`编辑 ${account.label}`} title="编辑账号" onClick={() => onEdit(account)}><Settings size={16} /></button>
                      <button className="icon-button danger-icon" aria-label={`移除 ${account.label}`} title="移除账号" onClick={() => onDelete(account)}><Trash2 size={16} /></button>
                    </div>
                  </article>
                );
              })}
              {!visibleAccounts.length && (
                <div className="opencode-no-results">
                  <Search size={20} />
                  <strong>没有匹配账号</strong>
                  <span>调整搜索词或状态筛选后再试。</span>
                </div>
              )}
            </div>
          </div>
        </section>
      ) : syncing ? (
        <section className="opencode-loading" aria-label="正在加载 OpenCode Go 账号">
          {Array.from({ length: 5 }, (_, index) => (
            <div className="opencode-loading-row" key={index}>
              <span /><span /><span /><span /><span />
            </div>
          ))}
        </section>
      ) : loadError ? (
        <section className="opencode-empty opencode-error-state">
          <div className="empty-orbit"><AlertTriangle size={28} /></div>
          <h2>OpenCode Go 加载失败</h2>
          <p>{loadError}</p>
          <button className="primary-button" onClick={onRefresh}><RefreshCw size={18} /> 重新加载</button>
        </section>
      ) : (
        <section className="opencode-empty">
          <div className="empty-orbit"><Cpu size={28} /></div>
          <h2>接入第一个 OpenCode Go 账号</h2>
          <p>保存 Workspace ID、auth Cookie 和 API Key，统一入口会持续读取三个额度窗口。</p>
          <button className="primary-button" onClick={onAdd}><Plus size={18} /> 添加账号</button>
        </section>
      )}

      <section className="panel opencode-alert-panel">
        <div className="section-head compact">
          <div><h2>额度告警</h2><p>复用当前企业微信、飞书和邮件通道。</p></div>
          <button className="text-button" onClick={onTestAlerts} disabled={!alerts?.enabled || syncing}>发送测试</button>
        </div>
        <div className="alert-stack horizontal-alerts">
          <div><CheckCircle2 size={18} /> {alerts?.enabled ? "监控运行中" : "配置通知通道后启用"}</div>
          <div><Bell size={18} /> 阈值 {alerts?.thresholds?.join(" / ") || "20 / 5 / 0"}%</div>
          <div><Clock3 size={18} /> 每 {alerts?.intervalSeconds || 60} 秒检查</div>
          <div className={alerts?.lastError ? "alert-error" : ""}><AlertTriangle size={18} /> {alerts?.lastError || `已发送 ${alerts?.deliveredEvents || 0} 条事件`}</div>
        </div>
      </section>
    </>
  );
}

function App() {
  const [auth, setAuth] = useState<AuthState>(emptyAuth);
  const [authReady, setAuthReady] = useState(false);
  const [loginTotp, setLoginTotp] = useState("");
  const [channels, setChannels] = useState<Channel[]>([]);
  const [recharges, setRecharges] = useState<RechargeLog[]>([]);
  const [settings, setSettings] = useState<SettingsState | null>(null);
  const [drawerMode, setDrawerMode] = useState<DrawerMode>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [filter, setFilter] = useState<FilterMode>("all");
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<DraftChannel>(emptyDraft);
  const [syncing, setSyncing] = useState(false);
  const [toast, setToast] = useState("");
  const [twofaSetup, setTwofaSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [twofaCode, setTwofaCode] = useState("");
  const [activeView, setActiveView] = useState<ViewMode>("balance");
  const [openCodeAccounts, setOpenCodeAccounts] = useState<OpenCodeAccount[]>([]);
  const [openCodeAlerts, setOpenCodeAlerts] = useState<OpenCodeAlertStatus | null>(null);
  const [openCodeLoaded, setOpenCodeLoaded] = useState(false);
  const [openCodeLoadError, setOpenCodeLoadError] = useState("");
  const [openCodeEditorId, setOpenCodeEditorId] = useState<number | null | undefined>(undefined);
  const [openCodeDraft, setOpenCodeDraft] = useState<OpenCodeDraft>(emptyOpenCodeDraft);

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

  useEffect(() => {
    loadAuth()
      .then((nextAuth) => {
        setAuth(nextAuth);
        setAuthReady(true);
        if (nextAuth.authenticated) {
          void loadDashboard();
        }
      })
      .catch((error: Error) => {
        setAuthReady(true);
        showToast(error.message);
      });
  }, []);

  function showToast(message: string) {
    setToast(message);
    window.clearTimeout(window.__uiPreviewToast);
    window.__uiPreviewToast = window.setTimeout(() => setToast(""), 2600);
  }

  function openAdd() {
    setDraft({
      ...emptyDraft,
      cnyRate: String(settings?.defaultCnyRate || 1),
      thresholdCny: String(settings?.lowBalanceAlertCny || 100),
    });
    setSelectedId(null);
    setDrawerMode("add");
  }

  function openSettings(channel: Channel) {
    setSelectedId(channel.id);
    setDrawerMode("settings");
  }

  async function loadDashboard() {
    const [nextSettings, nextChannels, nextRecharges] = await Promise.all([
      loadSettings(),
      loadChannels(),
      loadRecharges(),
    ]);
    setSettings(nextSettings);
    setChannels(nextChannels);
    setRecharges(nextRecharges);
  }

  async function openOpenCodeView() {
    setActiveView("opencode");
    if (openCodeLoaded) return;
    setSyncing(true);
    setOpenCodeLoadError("");
    try {
      const [accounts, alerts] = await Promise.all([loadOpenCodeAccounts(), loadOpenCodeAlerts()]);
      setOpenCodeAccounts(accounts);
      setOpenCodeAlerts(alerts);
      setOpenCodeLoaded(true);
    } catch (error) {
      const message = (error as Error).message;
      setOpenCodeLoadError(message);
      showToast(message);
    } finally {
      setSyncing(false);
    }
  }

  async function refreshOpenCode() {
    setSyncing(true);
    setOpenCodeLoadError("");
    try {
      const [accounts, alerts] = await Promise.all([refreshOpenCodeAccounts(), loadOpenCodeAlerts()]);
      setOpenCodeAccounts(accounts);
      setOpenCodeAlerts(alerts);
      setOpenCodeLoaded(true);
      showToast("OpenCode Go 额度已刷新");
    } catch (error) {
      const message = (error as Error).message;
      setOpenCodeLoadError(message);
      showToast(message);
    } finally {
      setSyncing(false);
    }
  }

  function openOpenCodeEditor(account?: OpenCodeAccount) {
    setOpenCodeEditorId(account?.id ?? null);
    setOpenCodeDraft({
      label: account?.label || "",
      workspaceId: account?.workspaceId || "",
      authCookie: "",
      apiKey: "",
    });
  }

  async function handleSaveOpenCode(event: FormEvent) {
    event.preventDefault();
    setSyncing(true);
    try {
      await saveOpenCodeAccount(openCodeDraft, openCodeEditorId ?? undefined);
      setOpenCodeEditorId(undefined);
      const [accounts, alerts] = await Promise.all([refreshOpenCodeAccounts(), loadOpenCodeAlerts()]);
      setOpenCodeAccounts(accounts);
      setOpenCodeAlerts(alerts);
      setOpenCodeLoaded(true);
      showToast(openCodeEditorId ? "OpenCode Go 账号已更新" : "OpenCode Go 账号已添加");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleDeleteOpenCode(account: OpenCodeAccount) {
    if (!window.confirm(`确认移除 ${account.label}？`)) return;
    setSyncing(true);
    try {
      await deleteOpenCodeAccount(account.id);
      setOpenCodeAccounts((items) => items.filter((item) => item.id !== account.id));
      showToast("OpenCode Go 账号已移除");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleTestOpenCodeAlerts() {
    setSyncing(true);
    try {
      await testOpenCodeAlerts();
      setOpenCodeAlerts(await loadOpenCodeAlerts());
      showToast("OpenCode Go 告警测试已发送");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function refreshOne(id: number) {
    setSyncing(true);
    try {
      const updated = await refreshChannel(id);
      setChannels((items) => items.map((item) => (item.id === id ? updated : item)));
      setRecharges(await loadRecharges());
      showToast("已刷新这条渠道");
    } catch (error) {
      showToast((error as Error).message);
      setChannels(await loadChannels());
    } finally {
      setSyncing(false);
    }
  }

  async function refreshAll(notify = false) {
    setSyncing(true);
    try {
      setChannels(await refreshAllChannels(notify));
      setRecharges(await loadRecharges());
      showToast(notify ? "刷新完成并已推送" : "已手动刷新余额");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function createChannel(event: FormEvent) {
    event.preventDefault();
    setSyncing(true);
    try {
      const next = await apiCreateChannel(draft);
      setChannels((items) => [next, ...items]);
      setRecharges(await loadRecharges());
      setDrawerMode(null);
      showToast("渠道已添加");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    const form = event.currentTarget as HTMLFormElement;
    const formData = new FormData(form);
    setSyncing(true);
    try {
      const updated = await updateChannel(selected, {
        name: String(formData.get("name") || selected.name),
        thresholdCny: Number(formData.get("thresholdCny") || selected.thresholdCny),
        cnyRate: Number(formData.get("cnyRate") || selected.cnyRate),
        bossRechargeRequired: formData.get("bossRechargeRequired") === "on",
      });
      setChannels((items) => items.map((item) => (item.id === selected.id ? updated : item)));
      setDrawerMode(null);
      showToast("渠道设置已保存");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function deleteSelected() {
    if (!selected) return;
    const ok = window.confirm(`删除 ${selected.name}？`);
    if (!ok) return;
    setSyncing(true);
    try {
      await apiDeleteChannel(selected.id);
      setChannels((items) => items.filter((item) => item.id !== selected.id));
      setRecharges(await loadRecharges());
      setDrawerMode(null);
      showToast("渠道已删除");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    setSyncing(true);
    try {
      const nextAuth = await login({
        username: String(formData.get("username") || "").trim(),
        password: String(formData.get("password") || ""),
        totp: loginTotp || undefined,
      }, auth.needsSetup);
      setAuth(nextAuth);
      setLoginTotp("");
      await loadDashboard();
      showToast(auth.needsSetup ? "管理员账号已创建" : "登录成功");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleLogout() {
    setSyncing(true);
    try {
      await apiLogout();
      setAuth({ ...emptyAuth, needsSetup: false });
      setChannels([]);
      setRecharges([]);
      setSettings(null);
      setOpenCodeAccounts([]);
      setOpenCodeAlerts(null);
      setOpenCodeLoaded(false);
      setActiveView("balance");
      setTwofaSetup(null);
      setDrawerMode(null);
      showToast("已退出");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleSetupTwofa() {
    setSyncing(true);
    try {
      setTwofaSetup(await setupTwofa());
      showToast("请用验证器添加密钥");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleConfirmTwofa(event: FormEvent) {
    event.preventDefault();
    setSyncing(true);
    try {
      setAuth(await confirmTwofa(twofaCode));
      setTwofaSetup(null);
      setTwofaCode("");
      showToast("2FA 已绑定");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function saveNotificationSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    setSyncing(true);
    try {
      setSettings(await saveAlertSettings({
        wecomWebhook: String(formData.get("wecomWebhook") || ""),
        feishuWebhook: String(formData.get("feishuWebhook") || ""),
        emailRecipients: String(formData.get("emailRecipients") || ""),
        notifyEnabled: formData.get("notifyEnabled") === "on",
      }));
      const wecomInput = form.elements.namedItem("wecomWebhook") as HTMLInputElement | null;
      const feishuInput = form.elements.namedItem("feishuWebhook") as HTMLInputElement | null;
      if (wecomInput) wecomInput.value = "";
      if (feishuInput) feishuInput.value = "";
      showToast("告警配置已保存");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  async function handleTestWebhook(kind: "wecom" | "feishu" | "email") {
    const labels = { wecom: "企业微信", feishu: "飞书", email: "邮件" };
    setSyncing(true);
    try {
      await testWebhook(kind);
      showToast(`${labels[kind]}测试已发送`);
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  if (!authReady) {
    return (
      <div className="login-screen">
        <div className="login-card">
          <div className="brand compact-brand">
            <div className="brand-mark">UB</div>
            <div>
              <strong>Upstream Balance</strong>
              <span>正在检查登录状态</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!auth.authenticated) {
    return (
      <div className="login-screen">
        <form className="login-card" onSubmit={handleLogin}>
          <div className="brand compact-brand">
            <div className="brand-mark">UB</div>
            <div>
              <strong>Upstream Balance</strong>
              <span>{auth.needsSetup ? "创建管理员账号" : "管理员登录"}</span>
            </div>
          </div>
          <label>账号<input name="username" autoComplete="username" required /></label>
          <label>密码<input name="password" type="password" autoComplete={auth.needsSetup ? "new-password" : "current-password"} required /></label>
          {!auth.needsSetup && (
            <label>2FA 验证码<input value={loginTotp} onChange={(event) => setLoginTotp(event.target.value)} inputMode="numeric" autoComplete="one-time-code" /></label>
          )}
          <button className="primary-button full" type="submit" disabled={syncing}>
            <KeyRound size={18} /> {auth.needsSetup ? "创建并进入" : "登录进入"}
          </button>
        </form>
        <div className={`toast ${toast ? "show" : ""}`}>{toast}</div>
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">UB</div>
          <div>
            <strong>Upstream Balance</strong>
            <span>Admin Console</span>
          </div>
        </div>
        <nav className="nav">
          <button className={`nav-item ${activeView === "balance" ? "active" : ""}`} onClick={() => setActiveView("balance")}><Gauge size={18} /> 总览</button>
          <button className="nav-item" onClick={() => setActiveView("balance")}><WalletCards size={18} /> 渠道</button>
          <button className={`nav-item ${activeView === "opencode" ? "active" : ""}`} onClick={() => void openOpenCodeView()}><Cpu size={18} /> OpenCode Go</button>
          <button className="nav-item" onClick={() => setActiveView("balance")}><Bell size={18} /> 告警</button>
          <button className="nav-item" onClick={() => setActiveView("balance")}><ShieldCheck size={18} /> 安全</button>
        </nav>
        <div className="sidebar-note user-note">
          <span>{auth.username}</span>
          <strong>{auth.totpEnabled ? "2FA 已绑定" : "建议绑定 2FA"}</strong>
          <button className="text-button left" onClick={handleLogout}><LogOut size={16} /> 退出登录</button>
        </div>
      </aside>

      <main className="main">
        {activeView === "balance" ? (
          <>
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
            <button className="icon-button" onClick={() => void refreshAll(false)} disabled={syncing} title="刷新余额">
              <RefreshCw size={18} className={syncing ? "spin" : ""} />
            </button>
            <button className="icon-button" onClick={() => void refreshAll(true)} disabled={syncing} title="刷新并推送">
              <Bell size={18} />
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
            <em>{settings?.emailConfigured ? "触发邮件 + 企业微信" : "按当前配置告警"}</em>
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
              <span className="trend-head">趋势</span>
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
                <div className="trend-cell">
                  <Sparkline values={channel.history} />
                </div>
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
              {recharges.map((item) => (
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
              <button className="text-button" onClick={() => void handleTestWebhook("wecom")}>测试企业微信</button>
            </div>
            <div className="alert-stack">
              <div><CheckCircle2 size={18} /> 企业微信{settings?.wecomConfigured ? "已配置" : "待配置"}</div>
              <div><CheckCircle2 size={18} /> 飞书{settings?.feishuConfigured ? "已配置" : "待配置"}</div>
              <div><Mail size={18} /> {settings?.emailRecipients || "未设置低余额邮箱"}</div>
              <div><AlertTriangle size={18} /> 低余额正文：渠道名 + 余额 + 阈值</div>
            </div>
            <form className="alert-form" onSubmit={saveNotificationSettings}>
              <label>低余额邮箱<input name="emailRecipients" defaultValue={settings?.emailRecipients || ""} /></label>
              <label>企业微信 Webhook<input name="wecomWebhook" type="password" placeholder={settings?.wecomConfigured ? "已配置，留空保持原值" : "https://qyapi.weixin.qq.com/..."} /></label>
              <label>飞书 Webhook<input name="feishuWebhook" type="password" placeholder={settings?.feishuConfigured ? "已配置，留空保持原值" : "https://open.feishu.cn/..."} /></label>
              <label className="check-line"><input name="notifyEnabled" type="checkbox" defaultChecked={settings?.notifyEnabled ?? true} /> 开启自动推送</label>
              <button className="primary-button full" type="submit">保存告警配置</button>
              <div className="alert-test-actions">
                <button className="text-button left" type="button" onClick={() => void handleTestWebhook("feishu")}>测试飞书</button>
                <button className="text-button left" type="button" onClick={() => void handleTestWebhook("email")}>测试邮件</button>
              </div>
            </form>
          </article>
        </section>

        {!auth.totpEnabled && (
          <section className="panel security-panel">
            <div className="section-head compact">
              <h2>安全</h2>
              <button className="text-button" onClick={handleSetupTwofa}>绑定 2FA</button>
            </div>
            {twofaSetup && (
              <form className="twofa-box" onSubmit={handleConfirmTwofa}>
                <code>{twofaSetup.secret}</code>
                <a href={twofaSetup.otpauth_uri}>打开 otpauth 链接</a>
                <label>验证码<input value={twofaCode} onChange={(event) => setTwofaCode(event.target.value)} inputMode="numeric" required /></label>
                <button className="primary-button full" type="submit">确认绑定</button>
              </form>
            )}
          </section>
        )}
          </>
        ) : (
          <OpenCodeWorkspace
            accounts={openCodeAccounts}
            alerts={openCodeAlerts}
            syncing={syncing}
            loadError={openCodeLoadError}
            onRefresh={() => void refreshOpenCode()}
            onAdd={() => openOpenCodeEditor()}
            onEdit={openOpenCodeEditor}
            onDelete={(account) => void handleDeleteOpenCode(account)}
            onTestAlerts={() => void handleTestOpenCodeAlerts()}
          />
        )}
      </main>

      {activeView === "balance" && drawerMode && (
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
                <label>New API 2FA 验证码<input value={draft.totp} onChange={(event) => setDraft({ ...draft, totp: event.target.value })} inputMode="numeric" autoComplete="one-time-code" /></label>
                <div className="form-grid">
                  <label>余额比例<input value={draft.cnyRate} onChange={(event) => setDraft({ ...draft, cnyRate: event.target.value })} type="number" step="0.0001" /></label>
                  <label>阈值<input value={draft.thresholdCny} onChange={(event) => setDraft({ ...draft, thresholdCny: event.target.value })} type="number" step="0.01" /></label>
                </div>
                <label className="check-line"><input type="checkbox" checked={draft.bossRechargeRequired} onChange={(event) => setDraft({ ...draft, bossRechargeRequired: event.target.checked })} /> 充值需联系老板</label>
                <button className="primary-button full" type="submit"><Eye size={18} /> 测试并保存</button>
              </form>
            ) : selected ? (
              <div className="drawer-stack">
                <article className={`drawer-channel-card ${isLow(selected) ? "low" : ""}`}>
                  <div className="channel-main">
                    <div className="status-dot" data-status={selected.status} />
                    <div>
                      <strong>{selected.name}</strong>
                      <span>{platformLabel(selected.platform)} · {selected.username}</span>
                      <a href={selected.baseUrl} target="_blank" rel="noreferrer">{selected.baseUrl}</a>
                    </div>
                  </div>
                  <div className="drawer-metrics">
                    <div>
                      <span>余额</span>
                      <strong>{money(selected.balanceCny)} CNY</strong>
                    </div>
                    <div>
                      <span>阈值</span>
                      <strong>{money(selected.thresholdCny)} CNY</strong>
                    </div>
                  </div>
                </article>

                <form className="drawer-form settings-card" onSubmit={saveSettings}>
                  <label>渠道名<input name="name" defaultValue={selected.name} /></label>
                  <div className="form-grid">
                    <label>阈值<input name="thresholdCny" type="number" step="0.01" defaultValue={selected.thresholdCny} /></label>
                    <label>余额比例<input name="cnyRate" type="number" step="0.0001" defaultValue={selected.cnyRate} /></label>
                  </div>
                  <label className="check-line"><input name="bossRechargeRequired" type="checkbox" defaultChecked={selected.bossRechargeRequired} /> 充值需联系老板</label>
                  <button className="primary-button full" type="submit">保存设置</button>
                </form>

                <section className="danger-zone">
                  <div>
                    <strong>删除渠道</strong>
                    <span>会从列表移除这条渠道和它的本地记录。</span>
                  </div>
                  <button className="danger-button full" type="button" onClick={deleteSelected}><Trash2 size={18} /> 删除渠道</button>
                </section>
              </div>
            ) : null}
          </aside>
        </div>
      )}

      {openCodeEditorId !== undefined && (
        <div className="drawer-layer" onClick={() => setOpenCodeEditorId(undefined)}>
          <aside className="drawer" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <span>OpenCode Go</span>
                <h2>{openCodeEditorId ? "编辑账号" : "添加账号"}</h2>
              </div>
              <button className="icon-button" onClick={() => setOpenCodeEditorId(undefined)}><X size={18} /></button>
            </div>
            <form className="drawer-form" onSubmit={handleSaveOpenCode}>
              <label>账号名称<input value={openCodeDraft.label} onChange={(event) => setOpenCodeDraft({ ...openCodeDraft, label: event.target.value })} required placeholder="例如 OpenCode Go 主账号" /></label>
              <label>Workspace ID<input value={openCodeDraft.workspaceId} onChange={(event) => setOpenCodeDraft({ ...openCodeDraft, workspaceId: event.target.value })} placeholder="wrk_..." /></label>
              <label>auth Cookie<input value={openCodeDraft.authCookie} onChange={(event) => setOpenCodeDraft({ ...openCodeDraft, authCookie: event.target.value })} type="password" placeholder={openCodeEditorId ? "留空保持现有 Cookie" : "auth=..."} autoComplete="off" /></label>
              <label>API Key<input value={openCodeDraft.apiKey} onChange={(event) => setOpenCodeDraft({ ...openCodeDraft, apiKey: event.target.value })} type="password" placeholder={openCodeEditorId ? "留空保持现有 Key" : "sk-..."} autoComplete="off" /></label>
              <div className="credential-note"><ShieldCheck size={17} /><span>Cookie 与 Key 使用现有服务端密钥加密，浏览器只读取掩码和状态。</span></div>
              <button className="primary-button full" type="submit" disabled={syncing}><Eye size={18} /> 保存并检查</button>
            </form>
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
