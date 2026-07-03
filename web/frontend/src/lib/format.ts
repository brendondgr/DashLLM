/** Number/time formatting, ported from the prototype. */

import type { RangeId } from './types';

/** 1234 -> "1.2k", 2500000 -> "2.50M" */
export const fmt = (n: number | null | undefined): string => {
  if (n == null) return '—';
  return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M'
    : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k'
    : String(Math.round(n));
};

/** Bucket key "2026-07-03T14:00" -> range-appropriate axis label. */
export const fmtBucket = (bucket: string, range: RangeId): string => {
  const [datePart, timePart] = bucket.split('T');
  if (!datePart) return bucket;
  const [, m, d] = datePart.split('-');
  const hhmm = timePart ?? '';
  if (range === '1h') return hhmm;
  if (range === '24h') return hhmm;
  const md = `${Number(m)}/${Number(d)}`;
  if (range === '7d') return `${md} ${Number(hhmm.slice(0, 2))}h`;
  return md;
};

/** "2026-07-01" -> "07/01" (daily chart axis, prototype style). */
export const fmtDay = (date: string): string =>
  date.slice(5).replace('-', '/');

export const fmtClock = (tsSeconds: number): string =>
  new Date(tsSeconds * 1000).toTimeString().slice(0, 8);

export const fmtDuration = (ms: number | null | undefined): string =>
  ms == null ? '—' : (ms / 1000).toFixed(1) + 's';

export const fmtUptime = (s: number): string => {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${Math.floor(s % 60)}s`;
};
