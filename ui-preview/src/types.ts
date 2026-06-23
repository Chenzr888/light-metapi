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
  cnyRate: string;
  thresholdCny: string;
  bossRechargeRequired: boolean;
}
