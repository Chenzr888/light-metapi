import type { AuthState, CatalogData, CatalogDraft, Channel, DraftChannel, Platform, RechargeLog, RouteData, SettingsState } from "./types";

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

interface RawRouteData {
  items: Array<{
    id: number; route_ids: number[]; route_names?: string[]; name: string; route_status: number | null; base_url: string;
    group: string; models: string[]; platform: Platform | null;
    monitor: RawChannel | null; discovery_state: string; discovery_message: string;
  }>;
  generated_at: string | null;
  summary: { routes: number; addresses: number; monitored_addresses: number; pending_addresses: number; excluded_addresses?: number };
}

export async function loadRoutes(): Promise<RouteData> {
  const raw = await request<RawRouteData>("/api/routes");
  return mapRouteData(raw);
}

function mapRouteData(raw: RawRouteData): RouteData {
  return {
    items: raw.items.map((item) => ({
      id: item.id,
      routeIds: item.route_ids || [item.id],
      routeNames: item.route_names || [],
      name: item.name,
      routeStatus: item.route_status,
      baseUrl: item.base_url,
      groupName: item.group,
      models: item.models || [],
      platform: item.platform,
      monitor: item.monitor ? mapChannel(item.monitor) : null,
      discoveryState: item.discovery_state,
      discoveryMessage: item.discovery_message,
    })),
    generatedAt: raw.generated_at,
    summary: {
      routes: raw.summary.routes,
      addresses: raw.summary.addresses,
      monitoredAddresses: raw.summary.monitored_addresses,
      pendingAddresses: raw.summary.pending_addresses,
      excludedAddresses: raw.summary.excluded_addresses || 0,
    },
  };
}

export async function excludeRoute(baseUrl: string, reason = "手动移除") {
  const raw = await request<RawRouteData>("/api/routes/exclude", {
    method: "POST", body: JSON.stringify({ base_url: baseUrl, reason }),
  });
  return mapRouteData(raw);
}

export async function restoreRoute(baseUrl: string) {
  const raw = await request<RawRouteData>("/api/routes/restore", {
    method: "POST", body: JSON.stringify({ base_url: baseUrl }),
  });
  return mapRouteData(raw);
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
    access_token: draft.accessToken || "",
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

export interface AccountSyncResult {
  total: number;
  existing: number;
  imported: number;
  failed: number;
  unknown: number;
}

export async function syncCatalogAccounts(payload: {
  newApiUsername: string;
  newApiPassword: string;
  sub2apiUsername: string;
  sub2apiPassword: string;
}) {
  return request<AccountSyncResult>("/api/catalog/accounts/sync", {
    method: "POST",
    body: JSON.stringify({
      new_api_username: payload.newApiUsername,
      new_api_password: payload.newApiPassword,
      sub2api_username: payload.sub2apiUsername,
      sub2api_password: payload.sub2apiPassword,
    }),
  });
}

interface RawCatalogData {
  items: Array<{
    id: number; source_kind: "backup" | "manual"; source: string; source_id: string;
    name: string; alias: string; channel_type: number | null; status: number | null;
    base_url: string; models: string[]; group_name: string; priority: number | null;
    weight: number | null; balance: number | null; response_time: number | null;
    source_tag: string; remark: string; owner: string; note: string; local_tags: string;
    present_in_source: boolean; synced_at: string | null;
    used_quota: number; quota_per_unit: number; ledger_balance: number | null;
    ledger_calibrated_at: string | null; alert_balance: number; balance_currency: string;
    balance_configured: boolean; spent_since_calibration: number; estimated_balance: number | null;
  }>;
  syncs: Array<{ source: string; generated_at: string; synced_at: string; item_count: number }>;
  summary: Omit<CatalogData["summary"], "lowBalance" | "estimatedTotal" | "unmonitored"> & {
    low_balance: number; estimated_total: number; unmonitored: number;
  };
  changed?: boolean;
  created_id?: number;
}

function mapCatalog(raw: RawCatalogData): CatalogData {
  return {
    items: raw.items.map((item) => ({
      id: item.id, sourceKind: item.source_kind, source: item.source, sourceId: item.source_id,
      name: item.name, alias: item.alias, channelType: item.channel_type, status: item.status,
      baseUrl: item.base_url, models: item.models || [], groupName: item.group_name,
      priority: item.priority, weight: item.weight, balance: item.balance,
      responseTime: item.response_time, sourceTag: item.source_tag, remark: item.remark,
      owner: item.owner, note: item.note, localTags: item.local_tags,
      presentInSource: item.present_in_source, syncedAt: item.synced_at,
      usedQuota: item.used_quota, quotaPerUnit: item.quota_per_unit,
      ledgerBalance: item.ledger_balance, ledgerCalibratedAt: item.ledger_calibrated_at,
      alertBalance: item.alert_balance, balanceCurrency: item.balance_currency,
      balanceConfigured: item.balance_configured,
      spentSinceCalibration: item.spent_since_calibration,
      estimatedBalance: item.estimated_balance,
    })),
    syncs: raw.syncs.map((item) => ({
      source: item.source, generatedAt: item.generated_at,
      syncedAt: item.synced_at, itemCount: item.item_count,
    })),
    summary: {
      ...raw.summary,
      lowBalance: raw.summary.low_balance,
      estimatedTotal: raw.summary.estimated_total,
      unmonitored: raw.summary.unmonitored,
    },
    changed: raw.changed,
    createdId: raw.created_id,
  };
}

export async function loadCatalog() {
  return mapCatalog(await request<RawCatalogData>("/api/catalog"));
}

export async function syncCatalog() {
  return mapCatalog(await request<RawCatalogData>("/api/catalog/sync", { method: "POST", body: "{}" }));
}

export async function createCatalogChannel(draft: CatalogDraft) {
  return mapCatalog(await request<RawCatalogData>("/api/catalog/channels", {
    method: "POST",
    body: JSON.stringify({
      name: draft.name, base_url: draft.baseUrl, group: draft.group, models: draft.models,
      owner: draft.owner, note: draft.note, tags: draft.tags, status: draft.status,
      ledger_balance: draft.ledgerBalance, alert_balance: draft.alertBalance,
    }),
  }));
}

export async function updateCatalogChannel(id: number, draft: CatalogDraft) {
  return mapCatalog(await request<RawCatalogData>(`/api/catalog/channels/${id}`, {
    method: "PUT",
    body: JSON.stringify({
      name: draft.name, alias: draft.alias, base_url: draft.baseUrl, group: draft.group,
      models: draft.models, owner: draft.owner, note: draft.note, tags: draft.tags,
      status: draft.status,
      ledger_balance: draft.ledgerBalance, alert_balance: draft.alertBalance,
    }),
  }));
}

export async function deleteCatalogChannel(id: number) {
  return mapCatalog(await request<RawCatalogData>(`/api/catalog/channels/${id}`, { method: "DELETE" }));
}
