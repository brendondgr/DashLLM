/** Typed client for the relay API (see docs/api-contract.md).
 * Every call is logged; failures are logged as errors and rethrown. */

import { log } from './logger';
import type {
  EndpointOut,
  EndpointTestResult,
  LiveSnapshot,
  ProxyInfoOut,
  RangeSel,
  RecentResponse,
  RouterState,
  RuntimeSettings,
  StatsDataset,
  SummaryOut,
  TunnelOut,
  TunnelTestResult,
} from './types';

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  try {
    const res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const text = await res.text();
      log.error('api.fail', `${method} ${path} -> ${res.status} ${text.slice(0, 200)}`);
      throw new Error(`${method} ${path}: HTTP ${res.status}`);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  } catch (e) {
    if (!(e instanceof Error && e.message.startsWith(method))) {
      log.error('api.unreachable', `${method} ${path} — ${String(e)}`);
    }
    throw e;
  }
}

const get = <T>(path: string) => req<T>('GET', path);

/** window/custom range -> query string fragment */
export function rangeQuery(range: RangeSel): string {
  if (range.id === 'custom' && range.from != null && range.to != null) {
    return `from=${range.from}&to=${range.to}`;
  }
  return `window=${range.id}`;
}

export const api = {
  // ---- endpoints -----------------------------------------------------
  endpoints: () => get<EndpointOut[]>('/admin/endpoints'),
  createEndpoint: (body: {
    name: string; base_url: string; server_type: string;
    upstream_key?: string | null; priority?: number;
    alias?: string | null; model_override?: string | null;
  }) => req<EndpointOut>('POST', '/admin/endpoints', body),
  patchEndpoint: (id: string, body: Record<string, unknown>) =>
    req<EndpointOut>('PATCH', `/admin/endpoints/${id}`, body),
  deleteEndpoint: (id: string) =>
    req<void>('DELETE', `/admin/endpoints/${id}`),
  activateEndpoint: (id: string) =>
    req<RouterState>('POST', `/admin/endpoints/${id}/activate`),
  testEndpoint: (id: string) =>
    req<EndpointTestResult>('POST', `/admin/endpoints/${id}/test`),

  // ---- tunnels --------------------------------------------------------
  tunnels: () => get<TunnelOut[]>('/admin/tunnels'),
  createTunnel: (body: Record<string, unknown>) =>
    req<TunnelOut>('POST', '/admin/tunnels', body),
  patchTunnel: (id: string, body: Record<string, unknown>) =>
    req<TunnelOut>('PATCH', `/admin/tunnels/${id}`, body),
  deleteTunnel: (id: string) => req<void>('DELETE', `/admin/tunnels/${id}`),
  startTunnel: (id: string) => req<TunnelOut>('POST', `/admin/tunnels/${id}/start`),
  stopTunnel: (id: string) => req<TunnelOut>('POST', `/admin/tunnels/${id}/stop`),
  testTunnel: (id: string) =>
    req<TunnelTestResult>('POST', `/admin/tunnels/${id}/test`),
  tunnelCommand: (id: string) =>
    get<{ command: string }>(`/admin/tunnels/${id}/command`),

  // ---- settings / proxy -------------------------------------------------
  settings: () => get<RuntimeSettings>('/admin/settings'),
  updateSettings: (patch: Record<string, unknown>) =>
    req<RuntimeSettings>('PUT', '/admin/settings', patch),
  proxyInfo: () => get<ProxyInfoOut>('/admin/proxy'),
  regenerateKey: () =>
    req<{ api_key: string; api_key_masked: string }>('POST', '/admin/proxy/key'),

  // ---- stats ---------------------------------------------------------------
  summary: (r: RangeSel) => get<SummaryOut>(`/admin/stats/summary?${rangeQuery(r)}`),
  volume: (r: RangeSel) => get<StatsDataset>(`/admin/stats/volume?${rangeQuery(r)}`),
  tokensTimeseries: (r: RangeSel) =>
    get<StatsDataset>(`/admin/stats/tokens/timeseries?${rangeQuery(r)}`),
  tokensByHour: (r: RangeSel) =>
    get<StatsDataset>(`/admin/stats/tokens/by-hour?${rangeQuery(r)}`),
  tokensByDay: (r: RangeSel) =>
    get<StatsDataset>(`/admin/stats/tokens/by-day?${rangeQuery(r)}`),
  byModel: (r: RangeSel) => get<StatsDataset>(`/admin/stats/by-model?${rangeQuery(r)}`),
  byEndpoint: (r: RangeSel) =>
    get<StatsDataset>(`/admin/stats/by-endpoint?${rangeQuery(r)}`),
  latency: (r: RangeSel) => get<StatsDataset>(`/admin/stats/latency?${rangeQuery(r)}`),
  recent: (limit = 90) => get<RecentResponse>(`/admin/stats/recent?limit=${limit}`),
  live: () => get<LiveSnapshot>('/admin/stats/live'),
};
