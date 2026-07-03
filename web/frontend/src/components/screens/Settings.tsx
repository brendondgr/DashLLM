import React, { useState, useEffect, useRef } from 'react';
import type { CSSProperties } from 'react';
import type { RuntimeSettings } from '../../lib/types';
import { C, ACCENT, MONO, SANS, card, track, knob, input } from '../../lib/styles';
import { api } from '../../lib/api';
import { log } from '../../lib/logger';

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
  const portTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retentionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    api.settings().then(s => setSettings(s)).catch(() => {});
  }, []);

  if (!settings) return <></>;

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

  const handleRetentionChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = +e.target.value;
    setSettings(prev => prev ? { ...prev, retention_days: v } : prev);
    if (retentionTimerRef.current) clearTimeout(retentionTimerRef.current);
    retentionTimerRef.current = setTimeout(() => {
      api.updateSettings({ retention_days: v }).catch(() => {});
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

        {/* Log retention row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1 }}>
            <span style={{ font: `500 12.5px ${SANS}` }}>Log retention</span>
            <span style={{ font: `400 11px ${SANS}`, color: C.textMut }}>
              Request bodies &amp; stats kept for {settings.retention_days} days
            </span>
          </div>
          <input
            type="range"
            min={1}
            max={90}
            value={settings.retention_days}
            onChange={handleRetentionChange}
            style={{ width: 180, accentColor: ACCENT } as CSSProperties}
          />
          <span style={{ font: `500 12px ${MONO}`, width: 34, textAlign: 'right' }}>
            {settings.retention_days}d
          </span>
        </div>
      </div>
    </div>
  );
}
