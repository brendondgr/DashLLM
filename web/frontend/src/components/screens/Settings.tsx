import React, { useState, useEffect, useRef } from 'react';
import type { CSSProperties } from 'react';
import type { RuntimeSettings, ProxyInfoOut } from '../../lib/types';
import { C, ACCENT, MONO, SANS, card, track, knob, input } from '../../lib/styles';
import { api } from '../../lib/api';
import { log } from '../../lib/logger';

// Restored when "keep forever" is switched off, so the slider has a sane value.
const DEFAULT_RETENTION_DAYS = 30;

function fmtBytes(n: number): string {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

type ToggleKey = 'stream_passthrough' | 'queue_requests' | 'auto_failover' | 'log_bodies' | 'allow_cors';

interface TogDef {
  key: ToggleKey;
  label: string;
  desc: string;
}

const togDefs: TogDef[] = [
  { key: 'stream_passthrough', label: 'Streaming passthrough', desc: 'Forward SSE chunks as they arrive from upstream' },
  { key: 'queue_requests', label: 'Request queueing', desc: 'Queue beyond 18 concurrent instead of rejecting with 429' },
  { key: 'auto_failover', label: 'Automatic failover', desc: 'Retry on next healthy endpoint when active fails' },
  { key: 'log_bodies', label: 'Log request bodies', desc: 'Store full prompts & completions (uses retention policy)' },
  { key: 'allow_cors', label: 'Allow CORS', desc: 'Accept browser requests from any origin' },
];

export default function Settings(): React.JSX.Element {
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [proxy, setProxy] = useState<ProxyInfoOut | null>(null);
  const portTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retentionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    api.settings().then(s => setSettings(s)).catch(() => {});
    api.proxyInfo().then(p => setProxy(p)).catch(() => {});
  }, []);

  if (!settings) return <></>;

  // retention_days <= 0 means "keep forever" (never prune raw rows).
  const forever = settings.retention_days <= 0;

  const pushRetention = (v: number) => {
    setSettings(prev => prev ? { ...prev, retention_days: v } : prev);
    if (retentionTimerRef.current) clearTimeout(retentionTimerRef.current);
    retentionTimerRef.current = setTimeout(() => {
      api.updateSettings({ retention_days: v }).catch(() => {});
    }, 600);
  };

  const handleForeverToggle = () => {
    const v = forever ? DEFAULT_RETENTION_DAYS : 0;
    // "forever" flips immediately (no debounce) since it's a discrete choice.
    setSettings(prev => prev ? { ...prev, retention_days: v } : prev);
    api.updateSettings({ retention_days: v }).catch(() => {});
    log.info('settings.retention', v <= 0 ? 'forever' : `${v}d`);
  };

  const handleToggle = (key: ToggleKey) => {
    const value = !settings[key];
    setSettings(prev => prev ? { ...prev, [key]: value } : prev);
    api.updateSettings({ [key]: value }).catch(() => {});
    log.info('settings.toggle', `${key}=${String(value)}`);
  };

  const handlePortChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value.replace(/\D/g, '');
    setSettings(prev => prev ? { ...prev, proxy_port: +v } : prev);
    if (portTimerRef.current) clearTimeout(portTimerRef.current);
    portTimerRef.current = setTimeout(() => {
      api.updateSettings({ proxy_port: +v }).catch(() => {});
    }, 600);
  };

  return (
    <div style={{ padding: '14px 18px 24px', display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 640 }}>
      {/* Card 1: toggles */}
      <div style={{ ...card, padding: '6px 16px' }}>
        {togDefs.map(({ key, label, desc }, i) => (
          <div
            key={key}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 14,
              padding: '13px 0',
              borderBottom: i < togDefs.length - 1 ? `1px solid ${C.border}` : 'none',
            } as CSSProperties}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
              <span style={{ font: `500 12.5px ${SANS}` }}>{label}</span>
              <span style={{ font: `400 11px ${SANS}`, color: C.textMut }}>{desc}</span>
            </div>
            <div onClick={() => handleToggle(key)} style={track(settings[key], ACCENT)}>
              <div style={knob(settings[key])} />
            </div>
          </div>
        ))}
      </div>

      {/* Card 2: port + retention */}
      <div style={{ ...card, padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}>
        {/* Proxy port row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
            <span style={{ font: `500 12.5px ${SANS}` }}>Proxy port</span>
            <span style={{ font: `400 11px ${SANS}`, color: C.textMut }}>Restart required · updates base URL &amp; snippets</span>
          </div>
          <input
            value={String(settings.proxy_port)}
            onChange={handlePortChange}
            style={{ ...input, width: 90, textAlign: 'right' } as CSSProperties}
          />
        </div>

        {/* Keep-forever toggle */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, borderTop: `1px solid ${C.border}`, paddingTop: 14 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
            <span style={{ font: `500 12.5px ${SANS}` }}>Keep logs forever</span>
            <span style={{ font: `400 11px ${SANS}`, color: C.textMut }}>
              Never delete request rows. Aggregate charts are always kept regardless.
            </span>
          </div>
          <div onClick={handleForeverToggle} style={track(forever, ACCENT)}>
            <div style={knob(forever)} />
          </div>
        </div>

        {/* Log retention slider — only when not keeping forever */}
        {!forever && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
              <span style={{ font: `500 12.5px ${SANS}` }}>Log retention</span>
              <span style={{ font: `400 11px ${SANS}`, color: C.textMut }}>
                Raw request rows &amp; bodies deleted after {settings.retention_days} days
              </span>
            </div>
            <input
              type="range"
              min={1}
              max={90}
              value={settings.retention_days}
              onChange={e => pushRetention(+e.target.value)}
              style={{ width: 180, accentColor: ACCENT } as CSSProperties}
            />
            <span style={{ font: `500 12px ${MONO}`, width: 34, textAlign: 'right' }}>
              {settings.retention_days}d
            </span>
          </div>
        )}

        {/* Database size + body-logging caveat */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, borderTop: `1px solid ${C.border}`, paddingTop: 14 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
            <span style={{ font: `500 12.5px ${SANS}` }}>Database size</span>
            <span style={{ font: `400 11px ${SANS}`, color: C.textMut }}>
              {proxy ? `${proxy.requests_total.toLocaleString()} requests logged` : 'Loading…'}
              {forever && settings.log_bodies ? ' · body logging on — the main growth driver' : ''}
            </span>
          </div>
          <span style={{ font: `500 12px ${MONO}` }}>
            {proxy ? fmtBytes(proxy.db_size_bytes) : '—'}
          </span>
        </div>
      </div>
    </div>
  );
}
