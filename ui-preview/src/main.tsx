import { FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { AlertTriangle, Bell, Database, Edit3, ExternalLink, LogOut, Plus, RefreshCw, RotateCcw, Search, Server, Trash2, UserRound, X } from "lucide-react";
import {
  createChannel,
  deleteChannel,
  excludeRoute,
  loadAuth,
  loadChannels,
  loadRoutes,
  loadSettings,
  login,
  logout as apiLogout,
  refreshAllChannels,
  refreshChannel,
  restoreRoute,
  saveAlertSettings,
  syncCatalogAccounts,
  updateChannel,
} from "./api";
import type { AuthState, Channel, DraftChannel, Platform, RouteChannel, RouteData, SettingsState } from "./types";
import "./styles.css";

type FilterMode = "all" | Platform | "error" | "excluded";

const emptyAuth: AuthState = { needsSetup: false, authenticated: false, username: "", totpEnabled: false };
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
const emptySettings: SettingsState = {
  wecomConfigured: false,
  feishuConfigured: false,
  emailConfigured: false,
  emailRecipients: "",
  notifyEnabled: true,
  defaultCnyRate: 7.3,
  lowBalanceAlertCny: 100,
  refreshIntervalSeconds: 300,
};
const emptyRoutes: RouteData = {
  items: [], generatedAt: null,
  summary: { routes: 0, addresses: 0, monitoredAddresses: 0, pendingAddresses: 0 },
};

function money(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function shortTime(value: string | null) {
  if (!value) return "尚未刷新";
  return new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function platformLabel(platform: Platform) {
  return platform === "sub2api" ? "Sub2API" : "New API";
}

function isLow(channel: Channel) {
  return channel.status === "ok" && channel.balanceCny !== null && channel.balanceCny <= channel.thresholdCny;
}

function discoveryLabel(route: RouteChannel) {
  if (route.monitor) return route.monitor.status === "error" ? "余额读取异常" : "已关联余额";
  if (route.discoveryState === "excluded") return "已关闭";
  const message = `${route.discoveryState} ${route.discoveryMessage}`.toLowerCase();
  if (message.includes("turnstile") || message.includes("captcha") || message.includes("verification") || message.includes("cap verification")) return "需要人工验证";
  if (message.includes("username or password") || message.includes("账号") || message.includes("password")) return "账号 / IP 待确认";
  if (message.includes("502") || message.includes("timeout") || message.includes("unreachable")) return "上游 / IP 不可达";
  if (message.includes("404") || message.includes("non-standard")) return "不是管理接口";
  return "尚未识别";
}

function App() {
  const [auth, setAuth] = useState<AuthState>(emptyAuth);
  const [authReady, setAuthReady] = useState(false);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [routes, setRoutes] = useState<RouteData>(emptyRoutes);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<FilterMode>("all");
  const [busy, setBusy] = useState(false);
  const [refreshingId, setRefreshingId] = useState<number | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [syncOpen, setSyncOpen] = useState(false);
  const [notifyOpen, setNotifyOpen] = useState(false);
  const [settings, setSettings] = useState<SettingsState>(emptySettings);
  const [editing, setEditing] = useState<Channel | null>(null);
  const [draft, setDraft] = useState<DraftChannel>(emptyDraft);
  const [toast, setToast] = useState("");
  const [activeTab, setActiveTab] = useState("余额");

  const summary = useMemo(() => ({
    totalUsd: channels.filter((item) => item.status === "ok").reduce((sum, item) => sum + (item.balanceUsd || 0), 0),
    healthy: channels.filter((item) => item.status === "ok").length,
    low: channels.filter(isLow).length,
    error: channels.filter((item) => item.status === "error").length,
  }), [channels]);

  const visible = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return routes.items.filter((route) => {
      const matchesFilter = filter === "all"
        ? route.discoveryState !== "excluded"
        : filter === "excluded"
          ? route.discoveryState === "excluded"
          : filter === "error"
            ? route.discoveryState !== "excluded" && (!route.monitor || route.monitor.status === "error")
            : route.discoveryState !== "excluded" && route.platform === filter;
      const matchesQuery = !keyword || [String(route.id), ...route.routeIds.map(String), route.name, ...(route.routeNames || []), route.baseUrl, route.monitor?.username || "", route.platform ? platformLabel(route.platform) : ""]
        .some((value) => value.toLowerCase().includes(keyword));
      return matchesFilter && matchesQuery;
    });
  }, [routes, filter, query]);

  useEffect(() => {
    loadAuth()
      .then(async (state) => {
        setAuth(state);
        if (state.authenticated) {
          const [nextChannels, nextRoutes, nextSettings] = await Promise.all([loadChannels(), loadRoutes(), loadSettings()]);
          setChannels(nextChannels);
          setRoutes(nextRoutes);
          setSettings(nextSettings);
        }
      })
      .catch((error) => showToast(error.message))
      .finally(() => setAuthReady(true));
  }, []);

  function showToast(message: string) {
    setToast(message);
    window.clearTimeout(window.__balanceToast);
    window.__balanceToast = window.setTimeout(() => setToast(""), 3200);
  }

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const nextAuth = await login({
        username: String(data.get("username") || "").trim(),
        password: String(data.get("password") || ""),
      }, false);
      setToast("");
      setAuth(nextAuth);
      const [nextChannels, nextRoutes, nextSettings] = await Promise.all([loadChannels(), loadRoutes(), loadSettings()]);
      setChannels(nextChannels);
      setRoutes(nextRoutes);
      setSettings(nextSettings);
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleLogout() {
    await apiLogout();
    setAuth(emptyAuth);
    setChannels([]);
    setRoutes(emptyRoutes);
  }

  async function handleRefreshAll() {
    setBusy(true);
    try {
      setChannels(await refreshAllChannels(false));
      setRoutes(await loadRoutes());
      showToast("真实余额已刷新");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleRefreshOne(channel: Channel) {
    setRefreshingId(channel.id);
    try {
      const next = await refreshChannel(channel.id);
      setChannels((items) => items.map((item) => item.id === next.id ? next : item));
      setRoutes(await loadRoutes());
      showToast(`${channel.name} 已刷新`);
    } catch (error) {
      showToast((error as Error).message);
      setChannels(await loadChannels());
      setRoutes(await loadRoutes());
    } finally {
      setRefreshingId(null);
    }
  }

  async function openNotifications() {
    setBusy(true);
    try {
      setSettings(await loadSettings());
      setNotifyOpen(true);
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleSyncAccounts(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const result = await syncCatalogAccounts({
        newApiUsername: String(data.get("new_api_username") || "").trim(),
        newApiPassword: String(data.get("new_api_password") || ""),
        sub2apiUsername: String(data.get("sub2api_username") || "").trim(),
        sub2apiPassword: String(data.get("sub2api_password") || ""),
      });
      setChannels(await loadChannels());
      setRoutes(await loadRoutes());
      setSyncOpen(false);
      showToast(`新增 ${result.imported}，已有 ${result.existing}，失败 ${result.failed + result.unknown}`);
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleExcludeRoute(route: RouteChannel) {
    if (!window.confirm(`确认移除 ${route.baseUrl}？以后备份再次出现时也不会自动加入。`)) return;
    setBusy(true);
    try {
      setRoutes(await excludeRoute(route.baseUrl));
      showToast("地址已移除，后续同步会自动跳过");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleRestoreRoute(route: RouteChannel) {
    setBusy(true);
    try {
      setRoutes(await restoreRoute(route.baseUrl));
      showToast("地址已恢复，下一次同步会重新识别");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveNotifications(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const next = await saveAlertSettings({
        wecomWebhook: String(data.get("wecom_webhook") || "").trim(),
        feishuWebhook: String(data.get("feishu_webhook") || "").trim(),
        emailRecipients: String(data.get("email_recipients") || "").trim(),
        notifyEnabled: data.get("notify_enabled") === "on",
      });
      setSettings(next);
      setNotifyOpen(false);
      showToast("通知设置已保存");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function openAdd() {
    setEditing(null);
    setDraft(emptyDraft);
    setDrawerOpen(true);
  }

  function openEdit(channel: Channel) {
    setEditing(channel);
    setDraft({
      ...emptyDraft,
      name: channel.name,
      platform: channel.platform,
      baseUrl: channel.baseUrl,
      username: channel.username,
      cnyRate: String(channel.cnyRate),
      thresholdCny: String(channel.thresholdCny),
      bossRechargeRequired: channel.bossRechargeRequired,
    });
    setDrawerOpen(true);
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      if (editing) {
        const updated = await updateChannel(editing, {
          name: draft.name,
          thresholdCny: Number(draft.thresholdCny),
          cnyRate: Number(draft.cnyRate),
          bossRechargeRequired: draft.bossRechargeRequired,
        });
        setChannels((items) => items.map((item) => item.id === updated.id ? updated : item));
        setRoutes(await loadRoutes());
        showToast("监控设置已保存");
      } else {
        const created = await createChannel(draft);
        setChannels((items) => [...items, created]);
        setRoutes(await loadRoutes());
        showToast("账号验证通过，已读取真实余额");
      }
      setDrawerOpen(false);
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!editing || !window.confirm(`移除 ${editing.name}？以后备份再次出现时也不会自动加入。`)) return;
    setBusy(true);
    try {
      await excludeRoute(editing.baseUrl, "已删除监控账号，后续同步跳过");
      await deleteChannel(editing.id);
      setChannels((items) => items.filter((item) => item.id !== editing.id));
      setRoutes(await loadRoutes());
      setDrawerOpen(false);
      showToast("上游账号已移除，后续同步会自动跳过");
    } catch (error) {
      showToast((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!authReady) return <div className="loading-page">正在打开余额监控</div>;

  if (!auth.authenticated) {
    return (
      <div className="login-screen">
        <form className="login-card" onSubmit={handleLogin}>
          <div className="brand-row"><span className="brand-mark"><Server size={19} /></span><strong>渠道余额</strong></div>
          <div className="login-copy"><h1>登录</h1><p>使用管理员账号查看真实上游余额。</p></div>
          <label>账号<input name="username" autoComplete="username" required /></label>
          <label>密码<input name="password" type="password" autoComplete="current-password" minLength={8} required /></label>
          <button className="primary-button full" type="submit" disabled={busy}>登录</button>
        </form>
        <div className={`toast ${toast ? "show" : ""}`}>{toast}</div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand-row"><span className="brand-mark"><Server size={19} /></span><div><strong>渠道余额</strong><span>SandboxAI</span></div></div>
        <div className="header-actions"><span className="user-name"><UserRound size={15} />{auth.username}</span><button className="icon-button" type="button" title="退出登录" onClick={handleLogout}><LogOut size={17} /></button></div>
      </header>
      <nav className="tabs" aria-label="主导航">{["余额", "渠道", "统计", "测活", "服务器"].map((tab) => <button key={tab} className={activeTab === tab ? "selected" : ""} onClick={() => setActiveTab(tab)}>{tab}</button>)}</nav>
      {activeTab !== "余额" && <section className="empty-state"><h2>{activeTab}</h2><p>规划中</p></section>}
      {activeTab === "余额" && <>

      <main className="content">
        <section className="page-head">
          <div><h1>渠道余额</h1><p>{routes.generatedAt ? `备份时间 ${new Date(routes.generatedAt).toLocaleString("zh-CN")}` : "等待渠道备份"}</p></div>
          <div className="page-actions">
            <button className="icon-button" title="同步最新备份渠道" type="button" onClick={() => setSyncOpen(true)} disabled={busy}><Database size={18} /></button>
            <button className="icon-button" title="通知设置" type="button" onClick={openNotifications} disabled={busy}><Bell size={18} /></button>
            <button className="icon-button" title="刷新全部真实余额" type="button" onClick={handleRefreshAll} disabled={busy}><RefreshCw size={18} className={busy ? "spin" : ""} /></button>
            <button className="primary-button" type="button" onClick={openAdd}><Plus size={17} />添加账号</button>
          </div>
        </section>

        <section className="summary-strip five" aria-label="余额概览">
          <div><span>真实余额</span><strong>${money(summary.totalUsd)}</strong></div>
          <div><span>管理地址</span><strong>{routes.summary.routes}</strong></div>
          <div><span>余额账户</span><strong>{channels.length}</strong></div>
          <div className={routes.summary.pendingAddresses ? "attention" : ""}><span>待处理地址</span><strong>{routes.summary.pendingAddresses}</strong></div>
          <div className={summary.low ? "attention" : ""}><span>低余额账户</span><strong>{summary.low}</strong></div>
        </section>

        <button className="notification-status" type="button" onClick={openNotifications}>
          <span><Bell size={16} />定时提醒</span>
          <strong>{settings.notifyEnabled ? "已启用" : "已关闭"}</strong>
          <small>{[settings.wecomConfigured && "企业微信", settings.feishuConfigured && "飞书", settings.emailConfigured && "邮箱"].filter(Boolean).join("、") || "未配置接收方式"}</small>
        </button>

        <section className="catalog-panel">
          <div className="toolbar">
            <label className="search-box"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索渠道 ID、名称或地址" /></label>
            <div className="segments">
              {(["all", "new_api", "sub2api", "error", "excluded"] as FilterMode[]).map((value) => (
                <button key={value} type="button" className={filter === value ? "selected" : ""} onClick={() => setFilter(value)}>
                  {value === "all" ? "全部" : value === "new_api" ? "New API" : value === "sub2api" ? "Sub2API" : value === "error" ? "异常" : `已关闭 ${routes.summary.excludedAddresses || 0}`}
                </button>
              ))}
            </div>
          </div>
          <div className="catalog-table balance-table">
            <div className="table-header"><span>渠道 ID / 名称</span><span>USD 余额</span><span>上游类型</span><span>监控状态</span><span>更新时间</span><span /></div>
            {visible.map((route) => {
              const channel = route.monitor;
              const attention = !channel || channel.status === "error" || isLow(channel);
              return <article className={`catalog-row ${attention ? "needs-attention" : ""}`} key={route.id}>
                <div className="channel-identity"><span className="route-id">#{route.id}{route.routeIds.length > 1 ? ` +${route.routeIds.length - 1}` : ""}</span><div><strong>{route.name}</strong><span>{route.routeIds.length > 1 ? `${route.routeIds.length} 个渠道共用此地址` : (route.groupName || "默认分组")} · {route.models.length} 个模型</span><small>{route.baseUrl}</small></div></div>
                <div><strong className={channel && isLow(channel) ? "balance-low" : ""}>${money(channel?.balanceUsd)}</strong><small>{channel ? "共享上游实时值" : "尚未读取"}</small></div>
                <div><strong>{route.platform ? platformLabel(route.platform) : "待识别"}</strong><small>{channel?.username || "-"}</small></div>
                <div><strong>{discoveryLabel(route)}</strong><small title={channel?.message || route.discoveryMessage}>{channel?.message || route.discoveryMessage}</small></div>
                <div><strong>{shortTime(channel?.lastCheckedAt || null)}</strong><small>{route.routeStatus === 1 ? "渠道启用" : "渠道停用"}</small></div>
                <div className="row-actions">
                  {route.discoveryState === "excluded" && <button className="icon-button" type="button" title="恢复地址" onClick={() => handleRestoreRoute(route)} disabled={busy}><RotateCcw size={16} /></button>}
                  {!channel && route.discoveryState !== "excluded" && <button className="icon-button danger-icon" type="button" title="移除并永久跳过此地址" onClick={() => handleExcludeRoute(route)} disabled={busy}><Trash2 size={16} /></button>}
                  {channel && <a className="icon-button" href={channel.rechargeUrl} target="_blank" rel="noreferrer" title="打开上游充值页"><ExternalLink size={16} /></a>}
                  {channel && <button className="icon-button" type="button" title="立即刷新" onClick={() => handleRefreshOne(channel)} disabled={refreshingId === channel.id}><RefreshCw size={16} className={refreshingId === channel.id ? "spin" : ""} /></button>}
                  {channel && <button className="icon-button" type="button" title="编辑监控设置" onClick={() => openEdit(channel)}><Edit3 size={16} /></button>}
                </div>
              </article>;
            })}
            {!visible.length && <div className="empty-state"><AlertTriangle size={22} /><strong>没有匹配的渠道</strong><span>调整搜索或筛选条件</span></div>}
          </div>
        </section>
      </main>

      {drawerOpen && (
        <div className="drawer-layer" onClick={() => setDrawerOpen(false)}>
          <aside className="drawer" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head"><div><span>{editing ? "监控设置" : "验证上游"}</span><h2>{editing ? editing.name : "添加账号"}</h2></div><button className="icon-button" type="button" title="关闭" onClick={() => setDrawerOpen(false)}><X size={18} /></button></div>
            <form className="drawer-form" onSubmit={handleSave}>
              {!editing && <div className="platform-segments segments">
                {(["new_api", "sub2api"] as Platform[]).map((platform) => <button key={platform} type="button" className={draft.platform === platform ? "selected" : ""} onClick={() => setDraft({ ...draft, platform })}>{platformLabel(platform)}</button>)}
              </div>}
              <label>渠道名<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required /></label>
              {!editing && <>
                <label>上游地址<input value={draft.baseUrl} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} placeholder="https://" required /></label>
                <label>登录账号<input value={draft.username} onChange={(event) => setDraft({ ...draft, username: event.target.value })} autoComplete="username" placeholder="可留空，改用 token" /></label>
                <label>登录密码<input value={draft.password} onChange={(event) => setDraft({ ...draft, password: event.target.value })} type="password" autoComplete="new-password" placeholder="可留空，改用 token" /></label>
                <label>访问 token（人工兜底）<input value={draft.accessToken || ""} onChange={(event) => setDraft({ ...draft, accessToken: event.target.value })} type="password" autoComplete="off" placeholder="粘贴后将跳过账号密码登录" /></label>
                {draft.platform === "new_api" && <label>2FA 验证码<input value={draft.totp} onChange={(event) => setDraft({ ...draft, totp: event.target.value })} inputMode="numeric" placeholder="未开启可留空" /></label>}
              </>}
              <div className="balance-form-grid">
                <label>USD/CNY 比例<input value={draft.cnyRate} onChange={(event) => setDraft({ ...draft, cnyRate: event.target.value })} type="number" min="0.0001" step="0.0001" required /></label>
                <label>低余额告警线（CNY）<input value={draft.thresholdCny} onChange={(event) => setDraft({ ...draft, thresholdCny: event.target.value })} type="number" min="0" step="1" required /></label>
              </div>
              <label className="checkbox-row"><input type="checkbox" checked={draft.bossRechargeRequired} onChange={(event) => setDraft({ ...draft, bossRechargeRequired: event.target.checked })} />需负责人手动充值</label>
              <button className="primary-button full" type="submit" disabled={busy}>{editing ? "保存" : "验证并添加"}</button>
              {editing && <button className="danger-button full" type="button" onClick={handleDelete}><Trash2 size={16} />删除账号</button>}
            </form>
          </aside>
        </div>
      )}
      {syncOpen && (
        <div className="drawer-layer" onClick={() => setSyncOpen(false)}>
          <aside className="drawer" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head"><div><span>最新小时备份</span><h2>同步渠道账号</h2></div><button className="icon-button" type="button" title="关闭" onClick={() => setSyncOpen(false)}><X size={18} /></button></div>
            <form className="drawer-form" onSubmit={handleSyncAccounts}>
              <div className="form-section-title">New API</div>
              <label>登录账号<input name="new_api_username" placeholder="请输入 New API 账号" autoComplete="username" required /></label>
              <label>登录密码<input name="new_api_password" type="password" autoComplete="new-password" required /></label>
              <div className="form-section-title">Sub2API</div>
              <label>登录邮箱<input name="sub2api_username" placeholder="请输入 Sub2API 邮箱" autoComplete="username" required /></label>
              <label>登录密码<input name="sub2api_password" type="password" autoComplete="new-password" required /></label>
              <button className="primary-button full" type="submit" disabled={busy}>{busy ? "正在识别并登录" : "开始同步"}</button>
            </form>
          </aside>
        </div>
      )}
      {notifyOpen && (
        <div className="modal-layer" onClick={() => setNotifyOpen(false)}>
          <section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="notification-title" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head"><div><span>定时提醒</span><h2 id="notification-title">通知设置</h2></div><button className="icon-button" type="button" title="关闭" onClick={() => setNotifyOpen(false)}><X size={18} /></button></div>
            <form className="drawer-form" onSubmit={handleSaveNotifications}>
              <label className="checkbox-row"><input name="notify_enabled" type="checkbox" defaultChecked={settings.notifyEnabled} />启用余额与异常提醒</label>
              <label>企业微信 Webhook<input name="wecom_webhook" type="password" placeholder={settings.wecomConfigured ? "已配置，留空保持不变" : "https://qyapi.weixin.qq.com/..."} autoComplete="off" /></label>
              <label>飞书 Webhook<input name="feishu_webhook" type="password" placeholder={settings.feishuConfigured ? "已配置，留空保持不变" : "https://open.feishu.cn/..."} autoComplete="off" /></label>
              <label>提醒邮箱<input name="email_recipients" type="text" defaultValue={settings.emailRecipients} placeholder="多个邮箱用逗号分隔" /></label>
              <div className="settings-facts"><span>自动刷新</span><strong>{Math.round(settings.refreshIntervalSeconds / 60)} 分钟</strong><span>默认告警线</span><strong>¥{money(settings.lowBalanceAlertCny, 0)}</strong></div>
              <div className="modal-actions"><button className="secondary-button" type="button" onClick={() => setNotifyOpen(false)}>取消</button><button className="primary-button" type="submit" disabled={busy}>确认</button></div>
            </form>
          </section>
        </div>
      )}
      <div className={`toast ${toast ? "show" : ""}`}>{toast}</div>
      </>}
    </div>
  );
}

declare global {
  interface Window { __balanceToast?: number }
}

createRoot(document.getElementById("root")!).render(<App />);
