const state = {
  channels: [],
  recharges: [],
  settings: null,
  auth: null,
  busy: false,
  channelFilter: "all",
};

const apiBase = new URL(".", window.location.href).pathname.replace(/\/$/, "");
const el = (id) => document.getElementById(id);

function encodePayload(value) {
  return btoa(unescape(encodeURIComponent(value)));
}

function money(value, digits = 4) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function timeText(value) {
  if (!value) return "未刷新";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function shortTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function platformText(platform) {
  return platform === "sub2api" ? "Sub2API" : "New API";
}

function thresholdCny(item) {
  return Number(item.alert_cny || state.settings?.low_balance_alert_cny || 100);
}

function isLowChannel(item) {
  if (item.status !== "ok") return false;
  const balance = Number(item.cny_balance);
  const threshold = thresholdCny(item);
  return Number.isFinite(balance) && Number.isFinite(threshold) && balance <= threshold;
}

function toast(message) {
  const node = el("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
}

function setAuthed(value) {
  el("loginView").classList.toggle("hidden", value);
  document.querySelector(".app-shell").classList.toggle("hidden", !value);
}

function renderAuthControls() {
  const authed = Boolean(state.auth?.authenticated);
  const twofaEnabled = Boolean(state.auth?.totp_enabled);
  const userBadge = el("userBadge");
  const twofaBtn = el("twofaBtn");
  const logoutBtn = el("logoutBtn");

  if (userBadge) {
    userBadge.textContent = state.auth?.username || "已登录";
  }
  if (twofaBtn) {
    twofaBtn.textContent = twofaEnabled ? "2FA 已绑定" : "绑定 2FA";
    twofaBtn.disabled = state.busy || !authed || twofaEnabled;
  }
  if (logoutBtn) {
    logoutBtn.disabled = state.busy || !authed;
  }
}

function renderAuth() {
  const auth = state.auth || {};
  const authed = Boolean(auth.authenticated);
  const needsSetup = Boolean(auth.needs_setup);
  const form = el("loginForm");

  setAuthed(authed);
  el("loginTitle").textContent = "light-metapi";
  el("loginCopy").textContent = needsSetup ? "首次打开请创建管理员账号，进入后可选择绑定 2FA。" : "输入管理员账号密码；已绑定 2FA 时填写验证码。";
  el("loginSubmit").textContent = needsSetup ? "创建并进入" : "登录进入";
  el("totpLoginField").classList.toggle("hidden", needsSetup);
  form.elements.password.autocomplete = needsSetup ? "new-password" : "current-password";
  form.elements.totp.required = false;

  if (!authed || auth.totp_enabled) {
    el("twofaPanel").classList.add("hidden");
  }
  renderAuthControls();
}

async function api(path, options = {}) {
  const publicPath = path.startsWith("/api/") ? `/_ub_api/${path.slice(5)}` : path;
  const target = `${apiBase}${publicPath}`;
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const requestOptions = { ...options };
  if (publicPath.startsWith("/_ub_api/")) {
    const method = (requestOptions.method || "GET").toUpperCase();
    if (method !== "GET") {
      headers["X-UB-Method"] = method;
      if (requestOptions.body) {
        headers["X-UB-Payload"] = encodePayload(requestOptions.body);
      }
      requestOptions.method = "GET";
      delete requestOptions.body;
    }
  }
  const res = await fetch(target, {
    ...requestOptions,
    credentials: "same-origin",
    headers,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    if (res.status === 401) {
      state.auth = { needs_setup: false, authenticated: false, username: "", totp_enabled: false };
      window.localStorage.removeItem("upstreamBalanceUser");
      renderAuth();
    }
    throw new Error(data.message || `请求失败 ${res.status}`);
  }
  return data.data;
}

async function loadAuth() {
  state.auth = await api("/api/auth/bootstrap");
  renderAuth();
  return state.auth;
}

function storeAuth(data) {
  state.auth = data;
  if (state.auth) {
    window.localStorage.setItem("upstreamBalanceUser", JSON.stringify(state.auth));
  }
}

function setBusy(value) {
  state.busy = value;
  for (const node of document.querySelectorAll("button")) {
    node.disabled = value;
  }
  for (const node of document.querySelectorAll("a[data-action='recharge']")) {
    node.classList.toggle("disabled", value);
  }
  renderAuthControls();
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  el("notifyEnabled").checked = Boolean(state.settings.notify_enabled);
  const refreshSeconds = Number(state.settings.refresh_interval_seconds || 30);
  const refreshText = refreshSeconds >= 60 ? `${Math.round(refreshSeconds / 60)} 分钟` : `${refreshSeconds} 秒`;
  el("wecomHint").textContent = `每 ${refreshText} 探测一次，低于 ${money(state.settings.low_balance_alert_cny, 2)} CNY 自动告警。`;
  const wecomStatus = el("wecomStatus");
  wecomStatus.textContent = state.settings.wecom_configured ? "已配置" : "未配置";
  wecomStatus.className = `pill ${state.settings.wecom_configured ? "ok" : ""}`;
  const feishuStatus = el("feishuStatus");
  feishuStatus.textContent = state.settings.feishu_configured ? "已配置" : "未配置";
  feishuStatus.className = `pill ${state.settings.feishu_configured ? "ok" : ""}`;
}

async function loadChannels() {
  state.channels = await api("/api/channels");
  renderChannels();
}

async function loadRecharges() {
  state.recharges = await api("/api/recharges?limit=80");
  renderRecharges();
}

function sparkline(history) {
  const rawPoints = (history || [])
    .filter((item) => item.status === "ok" && item.balance !== null && item.balance !== undefined);
  const points = rawPoints.length > 80
    ? rawPoints.filter((_, index) => index % Math.ceil(rawPoints.length / 80) === 0).slice(-80)
    : rawPoints;
  if (points.length < 2) {
    return '<div class="spark empty-spark">暂无趋势</div>';
  }
  const values = points.map((item) => Number(item.balance));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const coords = values.map((value, index) => {
    const x = (index / (values.length - 1)) * 120;
    const y = 34 - ((value - min) / span) * 28;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `
    <svg class="spark" viewBox="0 0 120 40" role="img" aria-label="72 小时余额趋势">
      <polyline points="${coords}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"></polyline>
    </svg>
  `;
}

function renderChannels() {
  const list = el("channelList");
  const channels = state.channels;
  if (!channels.length) {
    list.innerHTML = '<div class="empty">还没有渠道，先添加一个 New API 或 Sub2API 上游。</div>';
    el("summaryText").textContent = "0 个渠道";
    return;
  }

  const ok = channels.filter((item) => item.status === "ok").length;
  const total = channels.length;
  const totalBalance = channels
    .filter((item) => item.status === "ok")
    .reduce((sum, item) => sum + Number(item.balance || 0), 0);
  const totalCny = channels
    .filter((item) => item.status === "ok")
    .reduce((sum, item) => sum + Number(item.cny_balance || 0), 0);
  const lowCount = channels.filter(isLowChannel).length;
  el("summaryText").textContent = `${ok}/${total} 正常，合计 ${money(totalCny, 2)} CNY / ${money(totalBalance)} USD，${lowCount} 个低于阈值`;
  el("showAllBtn").classList.toggle("active", state.channelFilter === "all");
  el("showLowBtn").classList.toggle("active", state.channelFilter === "low");

  const visibleChannels = state.channelFilter === "low" ? channels.filter(isLowChannel) : channels;
  if (!visibleChannels.length) {
    list.innerHTML = '<div class="empty compact-empty">没有低于阈值的渠道。</div>';
    return;
  }

  list.innerHTML = visibleChannels.map((item) => {
    const statusClass = item.status === "ok" ? "ok" : item.status === "error" ? "error" : "";
    const statusText = item.status === "ok" ? "正常" : item.status === "error" ? "异常" : "待刷新";
    const rechargeRestricted = Boolean(item.boss_recharge_required);
    const rechargeAction = rechargeRestricted
      ? `<button class="btn ghost icon-btn" data-action="boss-recharge" data-id="${item.id}" type="button">充值</button>`
      : `<a class="btn ghost icon-btn link-btn" data-action="recharge" href="${escapeAttr(item.recharge_url)}" target="_blank" rel="noopener noreferrer">充值</a>`;
    const used = item.used_balance !== null && item.used_balance !== undefined
      ? `<div class="used">已用 ${money(item.cny_used_balance, 2)} CNY / ${money(item.used_balance)} USD</div>`
      : "";
    const message = item.status === "error" && item.message
      ? `<div class="used">${escapeHtml(item.message)}</div>`
      : used;
    const low = isLowChannel(item);
    return `
      <article class="channel-card ${low ? "low" : ""}">
        <div class="channel-title">
          <strong title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong>
          <a class="site-link" href="${escapeAttr(item.base_url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(item.base_url)}">
            ${escapeHtml(item.base_url)}
          </a>
        </div>
        <div>
          <span class="platform">${platformText(item.platform)}</span>
          <div class="meta">${escapeHtml(item.username)}</div>
        </div>
        <div>
          <span class="pill ${statusClass}">${statusText}</span>
          <div class="last-check">${timeText(item.last_checked_at)}</div>
        </div>
        <div>
          <div class="balance">${money(item.cny_balance, 2)} CNY</div>
          <div class="cny">${money(item.balance)} USD</div>
          <div class="used ${low ? "low-text" : ""}">阈值 ${money(thresholdCny(item), 2)} CNY</div>
          ${message}
        </div>
        <div class="trend-cell">
          ${sparkline(item.history)}
          <div class="meta">${(item.history || []).length} 点 / 72h</div>
        </div>
        <div class="channel-controls">
          <form class="rate-form channel-rate-form" data-id="${item.id}">
            <label>
              <span>比例</span>
              <input name="cny_rate" type="number" min="0.0001" step="0.0001" value="${escapeAttr(item.cny_rate || 7.3)}" />
            </label>
            <label>
              <span>阈值</span>
              <input name="alert_cny" type="number" min="0.01" step="0.01" value="${escapeAttr(thresholdCny(item))}" />
            </label>
            <label class="switch-line boss-switch" title="开启后充值按钮会提示联系老板">
              <input name="boss_recharge_required" type="checkbox" ${rechargeRestricted ? "checked" : ""} />
              <span>老板</span>
            </label>
            <button class="btn ghost" type="submit">保存</button>
          </form>
          <div class="card-actions">
            ${rechargeAction}
            <button class="btn icon-btn" data-action="refresh" data-id="${item.id}" type="button">刷新</button>
            <button class="btn danger icon-btn" data-action="delete" data-id="${item.id}" type="button">删除</button>
          </div>
        </div>
      </article>
    `;
  }).join("");
}

function renderRecharges() {
  const list = el("rechargeList");
  const logs = state.recharges || [];
  el("rechargeSummary").textContent = logs.length ? `最近 ${logs.length} 条上游充值记录` : "暂未读取到充值记录";
  if (!logs.length) {
    list.innerHTML = '<div class="empty compact-empty">刷新渠道后会读取 New API / Sub2API 的上游充值记录。</div>';
    return;
  }
  list.innerHTML = logs.map((item) => `
    <article class="recharge-row">
      <div>
        <strong>${escapeHtml(item.channel_name)}</strong>
        <a class="site-link" href="${escapeAttr(item.base_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.base_url)}</a>
      </div>
      <div>
        <span class="balance small">+${money(item.amount_usd)} USD</span>
        <div class="used">${money(item.amount_cny, 2)} CNY，比例 ${money(item.cny_rate, 4)} USD/CNY</div>
      </div>
      <div class="used">${escapeHtml(item.source_status || "-")} ${escapeHtml(item.source_type || "")}</div>
      <div class="last-check">${shortTime(item.detected_at)}</div>
    </article>
  `).join("");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

async function handleLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.username = (payload.username || "").trim();
  if (!payload.totp) delete payload.totp;
  const registering = Boolean(state.auth?.needs_setup);

  setBusy(true);
  try {
    const data = await api(registering ? "/api/auth/register" : "/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    storeAuth(data);
    form.reset();
    renderAuth();
    await loadApp();
    toast(registering ? "管理员账号已创建" : "登录成功");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function logout() {
  setBusy(true);
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
    window.localStorage.removeItem("upstreamBalanceAuth");
    window.localStorage.removeItem("upstreamBalanceUser");
    await loadAuth();
    el("loginForm").reset();
    toast("已退出");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function setupTwofa() {
  setBusy(true);
  try {
    const data = await api("/api/auth/2fa/setup", { method: "POST", body: "{}" });
    el("twofaSecret").textContent = data.secret;
    el("twofaUri").href = data.otpauth_uri;
    el("twofaPanel").classList.remove("hidden");
    toast("请用验证器添加密钥");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function confirmTwofa(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  setBusy(true);
  try {
    const data = await api("/api/auth/2fa/confirm", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    storeAuth(data);
    form.reset();
    el("twofaPanel").classList.add("hidden");
    renderAuth();
    toast("2FA 已绑定");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function createChannel(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.boss_recharge_required = Boolean(form.elements.boss_recharge_required?.checked);
  if (!payload.totp) delete payload.totp;
  const status = el("channelFormStatus");
  if (status) {
    status.textContent = "正在测试上游登录并保存渠道...";
    status.className = "form-status";
  }
  setBusy(true);
  try {
    await api("/api/channels", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    const rateInput = form.querySelector('[name="cny_rate"]');
    if (rateInput) rateInput.value = String(state.settings?.default_cny_rate || 7.3);
    state.channelFilter = "all";
    await Promise.all([loadChannels(), loadRecharges()]);
    if (status) {
      status.textContent = "渠道已添加到列表。";
      status.className = "form-status ok";
    }
    toast("渠道已添加");
  } catch (error) {
    if (status) {
      status.textContent = error.message;
      status.className = "form-status error";
    }
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  setBusy(true);
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        wecom_webhook: el("wecomWebhook").value.trim(),
        feishu_webhook: el("feishuWebhook").value.trim(),
        notify_enabled: el("notifyEnabled").checked,
      }),
    });
    el("wecomWebhook").value = "";
    el("feishuWebhook").value = "";
    await loadSettings();
    toast("配置已保存");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function clearWecom() {
  setBusy(true);
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ clear_wecom: true, wecom_webhook: "", notify_enabled: el("notifyEnabled").checked }),
    });
    await loadSettings();
    toast("企业微信配置已清空");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function clearFeishu() {
  setBusy(true);
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ clear_feishu: true, feishu_webhook: "", notify_enabled: el("notifyEnabled").checked }),
    });
    await loadSettings();
    toast("飞书配置已清空");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function testWecom() {
  setBusy(true);
  try {
    await api("/api/settings/test-wecom", { method: "POST", body: "{}" });
    toast("测试消息已发送");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function testFeishu() {
  setBusy(true);
  try {
    await api("/api/settings/test-feishu", { method: "POST", body: "{}" });
    toast("飞书测试消息已发送");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function refreshAll(notify = false) {
  setBusy(true);
  try {
    state.channels = await api("/api/refresh", {
      method: "POST",
      body: JSON.stringify({ notify }),
    });
    await loadRecharges();
    renderChannels();
    toast(notify ? "刷新完成并已推送" : "刷新完成");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function updateRate(form) {
  const id = form.dataset.id;
  const channel = state.channels.find((item) => String(item.id) === String(id));
  if (!channel) return;
  const payload = {
    name: channel.name,
    platform: channel.platform,
    base_url: channel.base_url,
    username: channel.username,
    enabled: channel.enabled,
    cny_rate: form.elements.cny_rate.value,
    alert_cny: form.elements.alert_cny.value,
    boss_recharge_required: Boolean(form.elements.boss_recharge_required?.checked),
  };
  setBusy(true);
  try {
    await api(`/api/channels/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    await loadChannels();
    toast("比例已保存");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function handleListClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.action;
  if (action === "boss-recharge") {
    toast("请联系老板进行充值");
    return;
  }
  if (action === "refresh") {
    setBusy(true);
    try {
      await api(`/api/channels/${id}/refresh`, { method: "POST", body: "{}" });
      await Promise.all([loadChannels(), loadRecharges()]);
      toast("渠道已刷新");
    } catch (error) {
      await loadChannels();
      toast(error.message);
    } finally {
      setBusy(false);
    }
  }
  if (action === "delete") {
    const channel = state.channels.find((item) => String(item.id) === String(id));
    const ok = window.confirm(`删除 ${channel?.name || "这个渠道"}？`);
    if (!ok) return;
    setBusy(true);
    try {
      await api(`/api/channels/${id}`, { method: "DELETE" });
      await Promise.all([loadChannels(), loadRecharges()]);
      toast("渠道已删除");
    } catch (error) {
      toast(error.message);
    } finally {
      setBusy(false);
    }
  }
}

async function handleListSubmit(event) {
  const form = event.target.closest(".rate-form");
  if (!form) return;
  event.preventDefault();
  await updateRate(form);
}

async function loadApp() {
  await Promise.all([loadSettings(), loadChannels(), loadRecharges()]);
  const rateInput = document.querySelector('#channelForm [name="cny_rate"]');
  if (rateInput && state.settings?.default_cny_rate) {
    rateInput.value = String(state.settings.default_cny_rate);
  }
}

async function boot() {
  el("loginForm").addEventListener("submit", handleLogin);
  el("logoutBtn").addEventListener("click", logout);
  el("twofaBtn").addEventListener("click", setupTwofa);
  el("twofaConfirmForm").addEventListener("submit", confirmTwofa);
  el("settingsForm").addEventListener("submit", saveSettings);
  el("channelForm").addEventListener("submit", createChannel);
  el("refreshAllBtn").addEventListener("click", () => refreshAll(false));
  el("notifyBtn").addEventListener("click", () => refreshAll(true));
  el("testWecomBtn").addEventListener("click", testWecom);
  el("testFeishuBtn").addEventListener("click", testFeishu);
  el("clearWecomBtn").addEventListener("click", clearWecom);
  el("clearFeishuBtn").addEventListener("click", clearFeishu);
  el("showAllBtn").addEventListener("click", () => {
    state.channelFilter = "all";
    renderChannels();
  });
  el("showLowBtn").addEventListener("click", () => {
    state.channelFilter = "low";
    renderChannels();
  });
  el("channelList").addEventListener("click", handleListClick);
  el("channelList").addEventListener("submit", handleListSubmit);

  try {
    const auth = await loadAuth();
    if (auth.authenticated) {
      await loadApp();
    }
  } catch (error) {
    toast(error.message);
  }
}

boot();
