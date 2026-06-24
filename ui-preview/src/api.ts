import type { AuthState, Channel, DraftChannel, Platform, RechargeLog, SettingsState } from "./types";

interface ApiEnvelope<T> {
  ok?: boolean;
  data?: T;
  message?: string;
}

interface RawAuthState {
  needs_setup: boolean;
  authenticated: boolean;
  username: string;
  totp_enabled: boolean;
}

interface RawSettings {
  wecom_configured: boolean;
  feishu_configured: boolean;
  email_configured: boolean;
  low_balance_email_recipients: string;
  notify_enabled: boolean;
  default_cny_rate: number;
  low_balance_alert_cny: number;
  refresh_interval_seconds: number;
}

interface RawHistory {
  balance: number | null;
  status: string;
}

interface RawChannel {
  id: number;
  name: string;
  platform: Platform;
  base_url: string;
  username: string;
  status: "ok" | "error" | "unknown";
  balance: number | null;
  cny_balance: number | null;
  alert_cny: number;
  cny_rate: number;
  last_checked_at: string | null;
  recharge_url: string;
  boss_recharge_required: boolean;
  enabled: boolean;
  message?: string;
  history?: RawHistory[];
}

interface RawRechargeLog {
  id: number;
  channel_name: string;
  amount_usd: number;
  amount_cny: number;
  detected_at: string;
  source_status?: string;
  source_type?: string;
}

const apiBase = new URL(".", window.location.href).pathname.replace(/\/$/, "");

function encodePayload(value: string) {
  return btoa(unescape(encodeURIComponent(value)));
}

function toPublicPath(path: string) {
  return path.startsWith("/api/") ? `/_ub_api/${path.slice(5)}` : path;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const publicPath = toPublicPath(path);
  const target = `${apiBase}${publicPath}`;
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  const requestOptions: RequestInit = { ...options, headers };

  if (publicPath.startsWith("/_ub_api/")) {
    const method = (requestOptions.method || "GET").toUpperCase();
    if (method !== "GET") {
      headers.set("X-UB-Method", method);
      if (typeof requestOptions.body === "string") {
        headers.set("X-UB-Payload", encodePayload(requestOptions.body));
      }
      requestOptions.method = "GET";
      delete requestOptions.body;
    }
  }

  const response = await fetch(target, {
    ...requestOptions,
    credentials: "same-origin",
  });
  const payload = (await response.json().catch(() => ({}))) as ApiEnvelope<T>;
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.message || `请求失败 ${response.status}`);
    error.name = response.status === 401 ? "UnauthorizedError" : "ApiError";
    throw error;
  }
  return payload.data as T;
}

function mapAuth(raw: RawAuthState): AuthState {
  return {
    needsSetup: raw.needs_setup,
    authenticated: raw.authenticated,
    username: raw.username,
    totpEnabled: raw.totp_enabled,
  };
}

function mapSettings(raw: RawSettings): SettingsState {
  return {
    wecomConfigured: raw.wecom_configured,
    feishuConfigured: raw.feishu_configured,
    emailConfigured: raw.email_configured,
    emailRecipients: raw.low_balance_email_recipients || "",
    notifyEnabled: raw.notify_enabled,
    defaultCnyRate: raw.default_cny_rate,
    lowBalanceAlertCny: raw.low_balance_alert_cny,
    refreshIntervalSeconds: raw.refresh_interval_seconds,
  };
}

function mapChannel(raw: RawChannel): Channel {
  const history = (raw.history || [])
    .filter((item) => item.status === "ok" && item.balance !== null)
    .map((item) => Number(item.balance));
  return {
    id: raw.id,
    name: raw.name,
    platform: raw.platform,
    baseUrl: raw.base_url,
    username: raw.username,
    status: raw.status,
    balanceUsd: raw.balance,
    balanceCny: raw.cny_balance,
    thresholdCny: raw.alert_cny,
    cnyRate: raw.cny_rate,
    lastCheckedAt: raw.last_checked_at,
    rechargeUrl: raw.recharge_url,
    bossRechargeRequired: raw.boss_recharge_required,
    enabled: raw.enabled,
    message: raw.message,
    history,
  };
}

function mapRecharge(raw: RawRechargeLog): RechargeLog {
  return {
    id: raw.id,
    channelName: raw.channel_name,
    amountUsd: raw.amount_usd,
    amountCny: raw.amount_cny,
    detectedAt: raw.detected_at,
    source: raw.source_type || raw.source_status || "-",
  };
}

export async function loadAuth() {
  return mapAuth(await request<RawAuthState>("/api/auth/bootstrap"));
}

export async function login(payload: { username: string; password: string; totp?: string }, registering: boolean) {
  return mapAuth(await request<RawAuthState>(registering ? "/api/auth/register" : "/api/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  }));
}

export async function logout() {
  await request<unknown>("/api/auth/logout", { method: "POST", body: "{}" });
}

export async function setupTwofa() {
  return request<{ secret: string; otpauth_uri: string }>("/api/auth/2fa/setup", { method: "POST", body: "{}" });
}

export async function confirmTwofa(totp: string) {
  return mapAuth(await request<RawAuthState>("/api/auth/2fa/confirm", {
    method: "POST",
    body: JSON.stringify({ totp }),
  }));
}

export async function loadSettings() {
  return mapSettings(await request<RawSettings>("/api/settings"));
}

export async function saveAlertSettings(payload: {
  wecomWebhook?: string;
  feishuWebhook?: string;
  emailRecipients: string;
  notifyEnabled: boolean;
  clearWecom?: boolean;
  clearFeishu?: boolean;
}) {
  return mapSettings(await request<RawSettings>("/api/settings", {
    method: "PUT",
    body: JSON.stringify({
      wecom_webhook: payload.wecomWebhook || "",
      feishu_webhook: payload.feishuWebhook || "",
      low_balance_email_recipients: payload.emailRecipients,
      notify_enabled: payload.notifyEnabled,
      clear_wecom: Boolean(payload.clearWecom),
      clear_feishu: Boolean(payload.clearFeishu),
    }),
  }));
}

export async function testWebhook(kind: "wecom" | "feishu" | "email") {
  await request<unknown>(`/api/settings/test-${kind}`, { method: "POST", body: "{}" });
}

export async function loadChannels() {
  return (await request<RawChannel[]>("/api/channels")).map(mapChannel);
}

export async function loadRecharges(limit = 80) {
  return (await request<RawRechargeLog[]>(`/api/recharges?limit=${limit}`)).map(mapRecharge);
}

export async function createChannel(draft: DraftChannel) {
  const payload = {
    name: draft.name,
    platform: draft.platform,
    base_url: draft.baseUrl,
    username: draft.username,
    password: draft.password,
    totp: draft.totp,
    cny_rate: draft.cnyRate,
    alert_cny: draft.thresholdCny,
    boss_recharge_required: draft.bossRechargeRequired,
  };
  return mapChannel(await request<RawChannel>("/api/channels", {
    method: "POST",
    body: JSON.stringify(payload),
  }));
}

export async function updateChannel(channel: Channel, payload: {
  name: string;
  thresholdCny: number;
  cnyRate: number;
  bossRechargeRequired: boolean;
}) {
  return mapChannel(await request<RawChannel>(`/api/channels/${channel.id}`, {
    method: "PUT",
    body: JSON.stringify({
      name: payload.name,
      platform: channel.platform,
      base_url: channel.baseUrl,
      username: channel.username,
      enabled: channel.enabled,
      cny_rate: payload.cnyRate,
      alert_cny: payload.thresholdCny,
      boss_recharge_required: payload.bossRechargeRequired,
    }),
  }));
}

export async function deleteChannel(id: number) {
  await request<unknown>(`/api/channels/${id}`, { method: "DELETE" });
}

export async function refreshChannel(id: number) {
  return mapChannel(await request<RawChannel>(`/api/channels/${id}/refresh`, { method: "POST", body: "{}" }));
}

export async function refreshAllChannels(notify: boolean) {
  return (await request<RawChannel[]>("/api/refresh", {
    method: "POST",
    body: JSON.stringify({ notify }),
  })).map(mapChannel);
}
