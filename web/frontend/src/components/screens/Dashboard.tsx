/** Dashboard screen: range/layout controls, KPI row, chart grid, endpoint
 * breakdown. 1:1 structural port of the prototype; every value is live. */

import { useMemo, useState, type CSSProperties } from 'react';

import { usePoll } from '../../hooks/usePoll';
import { api } from '../../lib/api';
import {
  concurrencyOption,
  dailyOption,
  hourOfDayOption,
  tokensOption,
  volumeOption,
} from '../../lib/chartOptions';
import { fmt } from '../../lib/format';
import { log } from '../../lib/logger';
import { ACCENT, C, MONO, SANS, segStyle } from '../../lib/styles';
import type {
  EndpointOut,
  LiveSnapshot,
  RangeId,
  RangeSel,
} from '../../lib/types';
import EChart from '../EChart';

type LayoutId = 'A' | 'B' | 'C';

interface Props {
  live: LiveSnapshot | null;
  endpoints: EndpointOut[];
}

const RANGES: [RangeId, string][] = [
  ['1h', '1H live'], ['24h', '24H'], ['7d', '7D'], ['30d', '30D'],
  ['custom', 'Custom'],
];
const LAYOUTS: [LayoutId, string][] = [
  ['A', 'Overview'], ['B', 'Timeline'], ['C', 'Dense'],
];

/** Panel grids per layout: [span, height, order?] — verbatim from prototype. */
const PANELS: Record<LayoutId, Record<string, number[]>> = {
  A: { vol: [12, 250], tok: [8, 300], conc: [4, 300], hod: [4, 250],
       daily: [4, 250], break: [4, 250] },
  B: { tok: [9, 360, 1], conc: [3, 360, 2], vol: [6, 230, 3],
       hod: [6, 230, 4], daily: [8, 220, 5], break: [4, 220, 6] },
  C: { vol: [4, 230, 1], tok: [4, 230, 2], conc: [4, 230, 3],
       hod: [4, 230, 4], daily: [4, 230, 5], break: [4, 230, 6] },
};

const RANGE_CAPTIONS: Record<RangeId, string> = {
  '1h': 'per minute · live', '24h': 'per hour', '7d': 'per hour',
  '30d': 'per hour', custom: 'per hour · custom range',
};

const isoDay = (offsetDays: number): string =>
  new Date(Date.now() - offsetDays * 864e5).toISOString().slice(0, 10);

export default function Dashboard({ live, endpoints }: Props) {
  const [rangeId, setRangeId] = useState<RangeId>('24h');
  const [layout, setLayout] = useState<LayoutId>('A');
  const [customFrom, setCustomFrom] = useState(isoDay(7));
  const [customTo, setCustomTo] = useState(isoDay(0));

  const range: RangeSel = useMemo(() => {
    if (rangeId !== 'custom') return { id: rangeId };
    return {
      id: 'custom',
      from: Date.parse(customFrom) / 1000,
      to: Date.parse(customTo) / 1000 + 86400,
    };
  }, [rangeId, customFrom, customTo]);
  const rangeKey = `${rangeId}:${customFrom}:${customTo}`;
  const statsInterval = rangeId === '1h' ? 2500 : 5000;

  const { data: summary } = usePoll(
    () => api.summary(range), statsInterval, [rangeKey]);
  const { data: volume } = usePoll(
    () => api.volume(range), statsInterval, [rangeKey]);
  const { data: tokens } = usePoll(
    () => api.tokensTimeseries(range), statsInterval, [rangeKey]);
  const { data: byHour } = usePoll(
    () => api.tokensByHour({ id: '30d' }), 15000);
  const dailyRange: RangeSel = rangeId === '30d' || rangeId === 'custom'
    ? range : { id: '7d' };
  const { data: byDay } = usePoll(
    () => api.tokensByDay(dailyRange), statsInterval, [rangeKey]);
  const { data: byEndpoint } = usePoll(
    () => api.byEndpoint(range), 10000, [rangeKey]);

  const volOpt = useMemo(
    () => (volume ? volumeOption(volume, rangeId) : null), [volume, rangeId]);
  const tokOpt = useMemo(
    () => (tokens ? tokensOption(tokens, rangeId) : null), [tokens, rangeId]);
  const concOpt = useMemo(
    () => (live ? concurrencyOption(live.series, live.max_concurrency) : null),
    [live]);
  const hodOpt = useMemo(
    () => (byHour ? hourOfDayOption(byHour) : null), [byHour]);
  const dailyOpt = useMemo(
    () => (byDay ? dailyOption(byDay) : null), [byDay]);

  const compact = layout === 'C';
  const P = PANELS[layout];
  const pstyle = (k: string): CSSProperties => ({
    gridColumn: `span ${P[k]![0]}`, height: P[k]![1],
    order: P[k]![2] ?? 0, minWidth: 0,
  });

  const setRange = (id: RangeId) => {
    log.info('dash.range', id);
    setRangeId(id);
  };
  const pickLayout = (id: LayoutId) => {
    log.info('dash.layout', id);
    setLayout(id);
  };

  // KPI row from the live summary
  const errRate = summary && summary.requests
    ? (summary.error_rate * 100).toFixed(2) + '% rate' : '—';
  const kpis: [string, string, string, string][] = [
    ['Requests', fmt(summary?.requests), 'over range', C.text],
    ['Tokens in', fmt(summary?.prompt_tokens), 'prompt', C.cyan],
    ['Tokens out', fmt(summary?.completion_tokens), 'completion', ACCENT],
    ['Avg tok/s', summary?.tokens_per_sec.p50 != null
      ? String(Math.round(summary.tokens_per_sec.p50)) : '—',
     'generation', C.text],
    ['p95 latency', summary?.latency_ms.p95 != null
      ? (summary.latency_ms.p95 / 1000).toFixed(2) + 's' : '—',
     'end-to-end', C.text],
    ['Errors', String(summary?.errors ?? '—'), errRate,
     (summary?.errors ?? 0) > 0 ? C.red : C.green],
  ];

  // by-endpoint breakdown rows (dims: endpoint, requests, input, output,
  // errors, share, endpoint_id)
  const activeId = endpoints.find((e) => e.active)?.id;
  const breakRows = (byEndpoint?.source ?? []).map((r) => ({
    name: String(r[0]),
    stat: `${fmt(Number(r[1]))} req · ${fmt(Number(r[2]) + Number(r[3]))} tok`,
    share: Number(r[5]),
    active: r[6] === activeId,
  }));

  const dailyTitle = rangeId === '30d' ? 'Tokens per day · 30d'
    : rangeId === 'custom' ? 'Tokens per day · range' : 'Tokens per day · 7d';

  const panelCard: CSSProperties = {
    height: '100%', display: 'flex', flexDirection: 'column',
    background: C.bgCard, border: `1px solid ${C.border}`, borderRadius: 8,
    padding: '12px 14px',
  };
  const panelTitle: CSSProperties = {
    font: `600 11px ${SANS}`, letterSpacing: '.07em',
    textTransform: 'uppercase', color: C.textMut,
  };

  return (
    <div style={{
      padding: '14px 18px 24px', display: 'flex', flexDirection: 'column',
      gap: 12,
    }}>
      {/* controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <div style={{
          display: 'flex', background: C.bgInset,
          border: `1px solid ${C.border}`, borderRadius: 7, padding: 2, gap: 2,
        }}>
          {RANGES.map(([id, label]) => (
            <div key={id} onClick={() => setRange(id)} style={segStyle(rangeId === id)}>
              {label}
            </div>
          ))}
        </div>
        {rangeId === 'custom' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <input type="date" value={customFrom}
              onChange={(e) => setCustomFrom(e.target.value)}
              style={{
                background: C.bgInset, border: `1px solid ${C.borderStrong}`,
                borderRadius: 6, color: C.text, padding: '4px 8px',
                font: `400 11px ${MONO}`, colorScheme: 'dark',
              }} />
            <span style={{ color: C.textDim }}>→</span>
            <input type="date" value={customTo}
              onChange={(e) => setCustomTo(e.target.value)}
              style={{
                background: C.bgInset, border: `1px solid ${C.borderStrong}`,
                borderRadius: 6, color: C.text, padding: '4px 8px',
                font: `400 11px ${MONO}`, colorScheme: 'dark',
              }} />
          </div>
        )}
        <div style={{ flex: 1 }} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{
            font: `500 10px ${SANS}`, letterSpacing: '.06em',
            textTransform: 'uppercase', color: C.textDim,
          }}>Layout</span>
          <div style={{
            display: 'flex', background: C.bgInset,
            border: `1px solid ${C.border}`, borderRadius: 7, padding: 2, gap: 2,
          }}>
            {LAYOUTS.map(([id, label]) => (
              <div key={id} onClick={() => pickLayout(id)} style={segStyle(layout === id)}>
                {label}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* KPI row */}
      <div style={{ display: 'flex', gap: compact ? 10 : 12, flexWrap: 'wrap' }}>
        {kpis.map(([label, value, sub, color]) => (
          <div key={label} style={{
            flex: 1, minWidth: 120, display: 'flex', flexDirection: 'column',
            gap: 3, background: C.bgCard, border: `1px solid ${C.border}`,
            borderRadius: 8, padding: compact ? '9px 12px' : '12px 14px',
          }}>
            <span style={{
              font: `500 10px ${SANS}`, letterSpacing: '.07em',
              textTransform: 'uppercase', color: C.textMut,
            }}>{label}</span>
            <span style={{
              font: `700 ${compact ? 17 : 20}px ${MONO}`, color,
            }}>{value}</span>
            <span style={{ font: `400 10.5px ${MONO}`, color: C.textDim }}>{sub}</span>
          </div>
        ))}
      </div>

      {/* chart grid */}
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(12,1fr)', gap: 12,
      }}>
        <div style={pstyle('vol')}>
          <div style={panelCard}>
            <div style={{
              display: 'flex', alignItems: 'baseline',
              justifyContent: 'space-between', marginBottom: 4,
            }}>
              <span style={panelTitle}>Request volume</span>
              <span style={{ font: `400 10px ${MONO}`, color: C.textDim }}>
                {RANGE_CAPTIONS[rangeId]}
              </span>
            </div>
            <EChart option={volOpt} />
          </div>
        </div>

        <div style={pstyle('tok')}>
          <div style={panelCard}>
            <div style={{
              display: 'flex', alignItems: 'baseline',
              justifyContent: 'space-between', marginBottom: 4,
            }}>
              <span style={panelTitle}>Tokens in / out</span>
              <span style={{ font: `400 10px ${MONO}`, color: C.textDim }}>
                drag to zoom · brush below
              </span>
            </div>
            <EChart option={tokOpt} />
          </div>
        </div>

        <div style={pstyle('conc')}>
          <div style={panelCard}>
            <div style={{
              display: 'flex', alignItems: 'baseline',
              justifyContent: 'space-between', marginBottom: 4,
            }}>
              <span style={panelTitle}>Concurrency · live</span>
              <span style={{ font: `700 15px ${MONO}`, color: ACCENT }}>
                {live?.in_flight ?? 0}
                <span style={{ font: `400 10px ${MONO}`, color: C.textDim }}>
                  {' '}/ {live?.max_concurrency ?? 18} max
                </span>
              </span>
            </div>
            <EChart option={concOpt} />
          </div>
        </div>

        <div style={pstyle('hod')}>
          <div style={panelCard}>
            <span style={{ ...panelTitle, marginBottom: 4 }}>
              Tokens by hour of day
            </span>
            <EChart option={hodOpt} />
          </div>
        </div>

        <div style={pstyle('daily')}>
          <div style={panelCard}>
            <span style={{ ...panelTitle, marginBottom: 4 }}>{dailyTitle}</span>
            <EChart option={dailyOpt} />
          </div>
        </div>

        <div style={pstyle('break')}>
          <div style={{ ...panelCard, overflow: 'hidden' }}>
            <span style={{ ...panelTitle, marginBottom: 10 }}>By endpoint</span>
            <div style={{
              display: 'flex', flexDirection: 'column', gap: 11,
              overflowY: 'auto',
            }}>
              {breakRows.length === 0 && (
                <span style={{ font: `400 11px ${SANS}`, color: C.textDim }}>
                  no traffic in range
                </span>
              )}
              {breakRows.map((b) => (
                <div key={b.name} style={{
                  display: 'flex', flexDirection: 'column', gap: 4,
                }}>
                  <div style={{
                    display: 'flex', justifyContent: 'space-between',
                    alignItems: 'baseline',
                  }}>
                    <span style={{ font: `500 11.5px ${MONO}` }}>{b.name}</span>
                    <span style={{ font: `400 10.5px ${MONO}`, color: C.textMut }}>
                      {b.stat}
                    </span>
                  </div>
                  <div style={{
                    height: 5, borderRadius: 3, background: C.bgRow,
                    overflow: 'hidden',
                  }}>
                    <div style={{
                      height: '100%', width: `${b.share * 100}%`,
                      borderRadius: 3,
                      background: b.active ? ACCENT : C.borderHover,
                    }} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
