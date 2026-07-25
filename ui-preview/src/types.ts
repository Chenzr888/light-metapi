export type Platform = "new_api" | "sub2api";
export type ChannelStatus = "ok" | "error" | "unknown";

export interface Channel {
  id: number;
  name: string;
  platform: Platform;
  baseUrl: string;
  username: string;
  status: ChannelStatus;
  balanceUsd: number | null;
  balanceCny: number | null;
  thresholdCny: number;
  cnyRate: number;
  lastCheckedAt: string | null;
  rechargeUrl: string;
  bossRechargeRequired: boolean;
  enabled: boolean;
  message?: string;
  history: number[];
}

export interface RechargeLog {
  id: number;
  channelName: string;
  amountUsd: number;
  amountCny: number;
  detectedAt: string;
  source: string;
}

export interface DraftChannel {
  name: string;
  platform: Platform;
  baseUrl: string;
  username: string;
  password: string;
  totp: string;
  cnyRate: string;
  thresholdCny: string;
  bossRechargeRequired: boolean;
}

export interface AuthState {
  needsSetup: boolean;
  authenticated: boolean;
  username: string;
  totpEnabled: boolean;
}

export interface SettingsState {
  wecomConfigured: boolean;
  feishuConfigured: boolean;
  emailConfigured: boolean;
  emailRecipients: string;
  notifyEnabled: boolean;
  defaultCnyRate: number;
  lowBalanceAlertCny: number;
  refreshIntervalSeconds: number;
}

export interface OpenCodeWindow {
  key: string;
  label: string;
  usedPercent: number;
  remainingPercent: number;
  resetInSeconds: number;
  resetsAt: string;
}

export interface OpenCodePoolWindow {
  key: string;
  label: string;
  capUsd: number;
  usedUsd: number;
  remainingUsd: number;
  totalUsd: number;
  usedPercent: number | null;
  remainingPercent: number | null;
  samples: number;
  accountCount: number;
  alertThresholdUsd: number;
  belowThreshold: boolean;
}

export interface OpenCodePool {
  windows: Record<string, OpenCodePoolWindow>;
  fetchedAt: string;
}

export interface OpenCodeErrorState {
  code: string;
  message: string;
}

export interface OpenCodeAccount {
  id: number;
  accountKey: string;
  label: string;
  workspaceId: string | null;
  quotaConfigured: boolean;
  modelsConfigured: boolean;
  hasAuthCookie: boolean;
  hasApiKey: boolean;
  apiKeyHint: string | null;
  enabled: boolean;
  quota: {
    windows: Record<string, OpenCodeWindow>;
    fetchedAt: string;
    cache?: { status: string; ageSeconds: number; warning?: string };
  } | null;
  quotaError: OpenCodeErrorState | null;
  models: {
    count: number;
    keyValid: boolean;
    upstreamState: string;
    fetchedAt: string;
    cache?: { status: string; ageSeconds: number; warning?: string };
  } | null;
  modelsError: OpenCodeErrorState | null;
}

export interface OpenCodeBundle {
  accounts: OpenCodeAccount[];
  pool: OpenCodePool | null;
}

export interface OpenCodeAlertStatus {
  enabled: boolean;
  running: boolean;
  intervalSeconds: number;
  thresholds: number[];
  poolThresholdsUsd: Record<string, number>;
  lastRunAt: string | null;
  lastSuccessAt: string | null;
  lastError: string | null;
  deliveredEvents: number;
}

export interface OpenCodeDraft {
  label: string;
  workspaceId: string;
  authCookie: string;
  apiKey: string;
}
