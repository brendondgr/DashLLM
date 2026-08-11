import React, { useState, useCallback, type CSSProperties } from 'react';
import { usePoll } from '../../hooks/usePoll';
import { api } from '../../lib/api';
import { fmt, fmtClock } from '../../lib/format';
import { log } from '../../lib/logger';
import { ACCENT, C, MONO, SANS } from '../../lib/styles';
import type { RequestRow, RecentResponse } from '../../lib/types';

// ── constants ────────────────────────────────────────────────────────────────

type Chip = 'all' | 'streaming' | 'done' | 'error';
const CHIP_NAMES: Chip[] = ['all', 'streaming', 'done', 'error'];

// ── helpers ──────────────────────────────────────────────────────────────────

function chipColor(state: RequestRow['state']): string {
  if (state === 'streaming') return C.cyan;
  if (state === 'done') return C.green;
  return C.red;
}

function chipRgb(state: RequestRow['state']): string {
  if (state === 'streaming') return '88,196,221';
  if (state === 'done') return '63,185,80';
  return '248,81,73';
}

function statusChipStyle(state: RequestRow['state']): CSSProperties {
  const rgb = chipRgb(state);
  const color = chipColor(state);
  return {
    padding: '2px 8px',
    borderRadius: 4,
    font: `500 10px ${MONO}`,
    color,
    background: `rgba(${rgb},.12)`,
    border: `1px solid rgba(${rgb},.35)`,
  };
}

function statusLabel(state: RequestRow['state']): string {
  if (state === 'streaming') return '⟳ stream';
  return state;
}

function liveDuration(row: RequestRow): string {
  if (row.state === 'streaming') {
    const secs = Date.now() / 1000 - row.ts;
    return secs.toFixed(1) + 's';
  }
  if (row.latency_ms == null) return '—';
  return (row.latency_ms / 1000).toFixed(1) + 's';
}

function tokRate(row: RequestRow): string {
  if (row.state === 'streaming') return '…';
  if (row.tokens_per_sec == null) return '—';
  return String(Math.round(row.tokens_per_sec));
}

function timingDetail(row: RequestRow): string {
  const ttfb = row.ttft_ms != null ? `ttfb ${Math.round(row.ttft_ms)}ms` : 'ttfb —';
  let gen = '—';
  if (row.latency_ms != null) {
    const genMs = row.ttft_ms != null ? row.latency_ms - row.ttft_ms : row.latency_ms;
    gen = (genMs / 1000).toFixed(1) + 's';
  }
  return `${ttfb} · gen ${gen}`;
}

function paramsDetail(row: RequestRow): string {
  const temp = row.temperature != null ? String(row.temperature) : '—';
  const max = row.max_tokens != null ? String(row.max_tokens) : '—';
  const mode = row.stream ? 'stream' : 'sync';
  return `temp ${temp} · max ${max} · ${mode}`;
}

function upstreamDetail(row: RequestRow): string {
  const ep = row.endpoint_name ?? '—';
  const st = row.status != null ? String(row.status) : '…';
  return `${ep} · ${st}`;
}

// ── sub-components ───────────────────────────────────────────────────────────

const labelStyle: CSSProperties = {
  font: `600 9.5px ${SANS}`,
  letterSpacing: '.07em',
  textTransform: 'uppercase',
  color: C.textDim,
};

const detailValStyle: CSSProperties = {
  font: `400 11px ${MONO}`,
  color: C.textMut,
};

interface ExpandedPanelProps { row: RequestRow }

function ExpandedPanel({ row }: ExpandedPanelProps) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(4,1fr)',
      gap: 10,
      padding: '12px 16px',
      background: C.bgSidebar,
      borderBottom: `1px solid ${C.border}`,
    }}>
      {[
        { label: 'Request ID', value: row.id },
        { label: 'Params', value: paramsDetail(row) },
        { label: 'Timing', value: timingDetail(row) },
        { label: 'Upstream', value: upstreamDetail(row) },
      ].map(({ label, value }) => (
        <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <span style={labelStyle}>{label}</span>
          <span style={detailValStyle}>{value}</span>
        </div>
      ))}
    </div>
  );
}

interface RowProps {
  row: RequestRow;
  expanded: boolean;
  onToggle: () => void;
}

function RequestRowItem({ row, expanded, onToggle }: RowProps) {
  const streaming = row.state === 'streaming';

  const rowStyle: CSSProperties = {
    display: 'grid',
    gridTemplateColumns: '76px 96px 1fr 90px 90px 80px 82px 24px',
    gap: 8,
    padding: '7px 14px',
    borderBottom: `1px solid #161C28`,
    alignItems: 'center',
    cursor: 'pointer',
    animation: streaming ? 'rowstream 1.6s infinite' : 'none',
  };

  const model = row.model ?? '—';
  const ep = row.endpoint_name ?? '—';

  return (
    <div>
      <div onClick={onToggle} style={rowStyle}>
        <span style={{ color: C.textMut, font: `400 11px ${MONO}` }}>
          {fmtClock(row.ts)}
        </span>
        <span>
          <span style={statusChipStyle(row.state)}>
            {statusLabel(row.state)}
          </span>
        </span>
        <span style={{
          font: `400 11.5px ${MONO}`,
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}>
          {model}{' '}
          <span style={{ color: '#525C6E' }}>· {ep}</span>
        </span>
        <span style={{ textAlign: 'right', font: `400 11.5px ${MONO}`, color: C.cyan }}>
          {fmt(row.prompt_tokens)}
        </span>
        <span style={{ textAlign: 'right', font: `400 11.5px ${MONO}`, color: ACCENT }}>
          {fmt(row.completion_tokens)}
        </span>
        <span style={{ textAlign: 'right', font: `400 11.5px ${MONO}`, color: C.textMut }}>
          {tokRate(row)}
        </span>
        <span style={{ textAlign: 'right', font: `400 11.5px ${MONO}`, color: C.textMut }}>
          {liveDuration(row)}
        </span>
        <span style={{ color: C.textDim, fontSize: 9, textAlign: 'center' }}>
          {expanded ? '▲' : '▼'}
        </span>
      </div>
      {expanded && <ExpandedPanel row={row} />}
    </div>
  );
}

// ── main component ───────────────────────────────────────────────────────────

interface Props {
  /** Carried by api.ts; passed in only so switching it re-triggers the poll.
   * The backend self-scopes this feed for non-admins regardless. */
  scope?: string;
}

export default function Requests({ scope = 'all' }: Props): React.JSX.Element {
  const [chip, setChip] = useState<Chip>('all');
  const [search, setSearch] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const fetcher = useCallback(() => api.recent(90), []);
  const { data } = usePoll<RecentResponse>(fetcher, 1500, [scope]);

  const rows = data?.rows ?? [];
  const counts = data?.counts ?? { all: 0, streaming: 0, done: 0, error: 0 };

  const q = search.toLowerCase();
  const filtered = rows
    .filter(r =>
      (chip === 'all' || r.state === chip) &&
      (!q || r.id.toLowerCase().includes(q) || (r.model ?? '').toLowerCase().includes(q))
    )
    .slice(0, 40);

  function handleChip(c: Chip) {
    setChip(c);
    log.info('requests.chip', c);
  }

  function handleToggle(id: string) {
    const next = expandedId === id ? null : id;
    setExpandedId(next);
    if (next !== null) log.info('requests.expand', id);
  }

  return (
    <div style={{ padding: '14px 18px 24px', display: 'flex', flexDirection: 'column', gap: 10 }}>
      {/* toolbar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {CHIP_NAMES.map(c => {
          const on = chip === c;
          const chipStyle: CSSProperties = {
            padding: '5px 12px',
            borderRadius: 6,
            cursor: 'pointer',
            userSelect: 'none',
            font: `${on ? 600 : 400} 11px ${MONO}`,
            color: on ? '#0B0E14' : C.textMut,
            background: on ? ACCENT : C.bgInset,
            border: `1px solid ${on ? ACCENT : C.border}`,
          };
          return (
            <div key={c} onClick={() => handleChip(c)} style={chipStyle}>
              {c} · {counts[c]}
            </div>
          );
        })}
        <div style={{ flex: 1 }} />
        <input
          placeholder="filter by id / model…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          style={{
            width: 220,
            background: C.bgInset,
            border: `1px solid ${C.borderStrong}`,
            borderRadius: 6,
            color: C.text,
            padding: '6px 10px',
            font: `400 11.5px ${MONO}`,
            outline: 'none',
          }}
        />
      </div>

      {/* table */}
      <div style={{
        background: C.bgCard,
        border: `1px solid ${C.border}`,
        borderRadius: 8,
        overflow: 'hidden',
      }}>
        {/* header */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: '76px 96px 1fr 90px 90px 80px 82px 24px',
          gap: 8,
          padding: '8px 14px',
          borderBottom: `1px solid ${C.border}`,
          font: `600 10px ${SANS}`,
          letterSpacing: '.07em',
          textTransform: 'uppercase',
          color: C.textMut,
        }}>
          <span>Time</span>
          <span>Status</span>
          <span>Model · endpoint</span>
          <span style={{ textAlign: 'right' }}>Tok in</span>
          <span style={{ textAlign: 'right' }}>Tok out</span>
          <span style={{ textAlign: 'right' }}>Tok/s</span>
          <span style={{ textAlign: 'right' }}>Duration</span>
          <span />
        </div>

        {/* rows */}
        {filtered.map(row => (
          <RequestRowItem
            key={row.id}
            row={row}
            expanded={expandedId === row.id}
            onToggle={() => handleToggle(row.id)}
          />
        ))}
      </div>
    </div>
  );
}
