/** Visual tokens + shared style helpers, ported verbatim from the
 * prototype (`LLM Proxy Dashboard.dc.html`). The prototype is the design
 * spec — do not restyle. */

import type { CSSProperties } from 'react';

export const ACCENT = '#FFB224';

export const C = {
  bg: '#0B0E14',
  bgSidebar: '#0D1119',
  bgCard: '#11151D',
  bgInset: '#131926',
  bgHover: '#161C28',
  bgRow: '#1A2130',
  border: '#1D2430',
  borderStrong: '#2A3547',
  borderHover: '#3A4A63',
  text: '#E6EAF2',
  textMut: '#8A94A6',
  textDim: '#525C6E',
  axis: '#68738A',
  cyan: '#58C4DD',
  green: '#3FB950',
  red: '#F85149',
  purple: '#BC8CFF',
} as const;

export const MONO = "'JetBrains Mono',monospace";
export const SANS = "'IBM Plex Sans',sans-serif";

export const dot = (color: string, pulse: boolean): CSSProperties => ({
  width: 8, height: 8, borderRadius: '50%', background: color, flex: 'none',
  animation: pulse ? 'livepulse 2s infinite' : 'none',
});

export const statusColor = (s: string): string =>
  s === 'healthy' || s === 'online' || s === 'up' ? C.green
  : s === 'degraded' || s === 'unknown' || s === 'starting' ? ACCENT
  : C.red;

export const badge = (color: string): CSSProperties => ({
  padding: '2px 7px', borderRadius: 4, background: 'rgba(138,148,166,.1)',
  border: `1px solid ${C.borderStrong}`, color,
  font: `500 9.5px ${MONO}`,
});

export const track = (on: boolean, acc: string = ACCENT): CSSProperties => ({
  width: 34, height: 18, borderRadius: 10,
  background: on ? acc : C.borderStrong, position: 'relative',
  transition: 'background .2s', flex: 'none', cursor: 'pointer',
});

export const knob = (on: boolean): CSSProperties => ({
  width: 14, height: 14, borderRadius: '50%', background: C.bg,
  position: 'absolute', top: 2, left: on ? 18 : 2, transition: 'left .2s',
});

/** Segmented-control chip (range / layout / snippet tabs). */
export const segStyle = (on: boolean, acc: string = ACCENT): CSSProperties => ({
  padding: '4px 12px', borderRadius: 5, cursor: 'pointer',
  userSelect: 'none', font: `${on ? 600 : 400} 11px ${MONO}`,
  color: on ? C.bg : C.textMut, background: on ? acc : 'transparent',
});

export const input: CSSProperties = {
  background: C.bgSidebar, border: `1px solid ${C.borderStrong}`,
  borderRadius: 6, color: C.text, padding: '7px 10px',
  font: `400 12px ${MONO}`, outline: 'none',
};

export const card: CSSProperties = {
  background: C.bgCard, border: `1px solid ${C.border}`, borderRadius: 8,
};

export const sectionLabel: CSSProperties = {
  font: `600 11px ${SANS}`, letterSpacing: '.07em',
  textTransform: 'uppercase', color: C.textMut,
};
