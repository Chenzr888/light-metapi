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

export interface RouteChannel {
  id: number;
  routeIds: number[];
  routeNames?: string[];
  name: string;
  routeStatus: number | null;
  baseUrl: string;
  groupName: string;
  models: string[];
  platform: Platform | null;
  monitor: Channel | null;
  discoveryState: string;
  discoveryMessage: string;
}

export interface RouteData {
  items: RouteChannel[];
  generatedAt: string | null;
  summary: {
    routes: number;
    addresses: number;
    monitoredAddresses: number;
    pendingAddresses: number;
    excludedAddresses?: number;
  };
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

export type CatalogSourceKind = "backup" | "manual";

export interface CatalogChannel {
  id: number;
  sourceKind: CatalogSourceKind;
  source: string;
  sourceId: string;
  name: string;
  alias: string;
  channelType: number | null;
  status: number | null;
  baseUrl: string;
  models: string[];
  groupName: string;
  priority: number | null;
  weight: number | null;
  balance: number | null;
  responseTime: number | null;
  sourceTag: string;
  remark: string;
  owner: string;
  note: string;
  localTags: string;
  presentInSource: boolean;
  syncedAt: string | null;
  usedQuota: number;
  quotaPerUnit: number;
  ledgerBalance: number | null;
  ledgerCalibratedAt: string | null;
  alertBalance: number;
  balanceCurrency: string;
  balanceConfigured: boolean;
  spentSinceCalibration: number;
  estimatedBalance: number | null;
}

export interface CatalogData {
  items: CatalogChannel[];
  syncs: Array<{
    source: string;
    generatedAt: string;
    syncedAt: string;
    itemCount: number;
  }>;
  summary: {
    total: number;
    synced: number;
    manual: number;
    disabled: number;
    missing: number;
    monitored: number;
    unmonitored: number;
    lowBalance: number;
    estimatedTotal: number;
  };
  changed?: boolean;
  createdId?: number;
}

export interface CatalogDraft {
  name: string;
  alias: string;
  baseUrl: string;
  group: string;
  models: string;
  owner: string;
  note: string;
  tags: string;
  status: number;
  ledgerBalance: string;
  alertBalance: string;
}
