/** Number/time formatting, ported from the prototype. */

import type { RangeId } from './types';

/** 1234 -> "1.2k", 2500000 -> "2.50M" */
export const fmt = (n: number | null | undefined): string => {
  if (n == null) return '—';
  return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M'
    : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k'
    : String(Math.round(n));
};

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Bucket key -> axis label. Keys arrive shaped by the backend bucket unit:
 * "2026-07" (month), "2026-07-03" (day), "2026-07-03T14:00" / ":15" (time).
 * The label detail scales with the selected range. */
export const fmtBucket = (bucket: string, range: RangeId): string => {
  // month bucket: "2026-07"
  if (/^\d{4}-\d{2}$/.test(bucket)) {
    const [y, m] = bucket.split('-');
    return `${MONTHS[Number(m) - 1]} '${y!.slice(2)}`;
  }
  const [datePart, timePart] = bucket.split('T');
  if (!datePart) return bucket;
  const [, m, d] = datePart.split('-');
  const md = `${Number(m)}/${Number(d)}`;
  if (!timePart) return md;                    // day bucket "YYYY-MM-DD"
  if (range === '1h' || range === '24h') return timePart;  // within a day
  return `${md} ${Number(timePart.slice(0, 2))}h`;         // day + hour
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
