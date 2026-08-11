/** Account screen for non-admin users: their proxy key, the private toggle,
 * and a copy-paste snippet.
 *
 * The key is never re-displayable — only its SHA-256 is stored — so this screen
 * shows the prefix and offers rotation, which returns a fresh key once. */

import React, { useState } from 'react';
import type { CSSProperties } from 'react';

import type { MeOut } from '../../lib/types';
import { api } from '../../lib/api';
import { log } from '../../lib/logger';
import {
  ACCENT, C, MONO, SANS, card, knob, sectionLabel, track,
} from '../../lib/styles';

const btn: CSSProperties = {
  padding: '8px 12px', borderRadius: 6, border: `1px solid ${C.borderStrong}`,
  background: C.bgInset, color: C.textMut, font: `500 11px ${SANS}`,
  cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap',
};

const box: CSSProperties = {
  flex: 1, background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6,
  padding: '9px 12px', font: `400 12.5px ${MONO}`, color: C.text,
  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
};

interface Props {
  me: MeOut;
  proxyPort: number | null;
  onRefresh: () => void;
}

export default function Account({ me, proxyPort, onRefresh }: Props): React.JSX.Element {
  const [freshKey, setFreshKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const baseUrl = `${window.location.origin}/v1`;

  const rotate = async (): Promise<void> => {
    if (!window.confirm(
      'Generate a new key? The current one stops working immediately.')) return;
    setBusy(true);
    log.info('account.rotate_key', me.username ?? '');
    try {
      setFreshKey((await api.rotateMyKey()).api_key);
      onRefresh();
    } catch {
      // api.ts logged it
    } finally {
      setBusy(false);
    }
  };

  const togglePrivate = async (): Promise<void> => {
    log.info('account.private', String(!me.private));
    try {
      await api.setPrivate(!me.private);
      onRefresh();
    } catch {
      // api.ts logged it
    }
  };

  return (
    <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ ...card, padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <span style={sectionLabel}>Your proxy key</span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <div style={box}>
            {freshKey ?? `${me.api_key_prefix ?? '—'}${'•'.repeat(28)}`}
          </div>
          <div style={btn} onClick={() => void rotate()}>
            {busy ? 'Working…' : 'Generate new'}
          </div>
        </div>
        <span style={{ font: `400 11px ${SANS}`, color: freshKey ? ACCENT : C.textMut, lineHeight: 1.5 }}>
          {freshKey
            ? 'Copy this now — only a hash is stored, so it will not be shown again.'
            : 'Only a hash of your key is stored, so it cannot be displayed again. Generate a new one if you have lost it.'}
        </span>
      </div>

      <div style={{ ...card, padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <span style={sectionLabel}>Privacy</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={track(me.private)} onClick={() => void togglePrivate()}>
            <div style={knob(me.private)} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <span style={{ font: `500 12px ${SANS}` }}>Keep my activity private</span>
            <span style={{ font: `400 10.5px ${SANS}`, color: C.textMut }}>
              Your requests always count toward the shared totals, and are never
              itemised to anyone but you and the administrator.
            </span>
          </div>
        </div>
      </div>

      <div style={{ ...card, padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <span style={sectionLabel}>Connect a client</span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <div style={{ ...box, color: C.cyan }}>{baseUrl}</div>
        </div>
        <pre style={{
          margin: 0, background: C.bg, border: `1px solid ${C.border}`,
          borderRadius: 6, padding: '12px 14px', font: `400 11.5px ${MONO}`,
          color: C.textMut, overflowX: 'auto',
        }}>
{`from openai import OpenAI

client = OpenAI(
    base_url="${baseUrl}",
    api_key="${me.api_key_prefix ?? 'rk_'}…",
)`}
        </pre>
        <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
          Requests carrying your key are attributed to you.
          {proxyPort ? ` Relay is listening on :${proxyPort}.` : ''}
        </span>
      </div>
    </div>
  );
}
