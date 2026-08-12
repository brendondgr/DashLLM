import React, { useState, useCallback } from 'react';
import type { CSSProperties } from 'react';
import type { ProxyInfoOut } from '../../lib/types';
import { C, MONO, SANS, card, sectionLabel } from '../../lib/styles';
import { fmt, fmtUptime } from '../../lib/format';
import { api } from '../../lib/api';
import { usePoll } from '../../hooks/usePoll';

type SnipTab = 'curl' | 'python' | 'node';

// relay takes no API key. The SDK snippets still pass one because the OpenAI
// clients require the field to be non-empty — the value is ignored here.
function getSnippet(tab: SnipTab, baseUrl: string, model: string): string {
  if (tab === 'curl') {
    return (
      `curl ${baseUrl}/chat/completions \\\n` +
      `  -H "Content-Type: application/json" \\\n` +
      `  -d '{\n` +
      `    "model": "${model}",\n` +
      `    "stream": true,\n` +
      `    "messages": [{"role": "user", "content": "Hello"}]\n` +
      `  }'`
    );
  }
  if (tab === 'python') {
    return (
      `from openai import OpenAI\n\n` +
      `client = OpenAI(\n` +
      `    base_url="${baseUrl}",\n` +
      `    api_key="unused",  # relay does not check it\n` +
      `)\n\n` +
      `resp = client.chat.completions.create(\n` +
      `    model="${model}",\n` +
      `    messages=[{"role": "user", "content": "Hello"}],\n` +
      `    stream=True,\n` +
      `)`
    );
  }
  // node
  return (
    `import OpenAI from "openai";\n\n` +
    `const client = new OpenAI({\n` +
    `  baseURL: "${baseUrl}",\n` +
    `  apiKey: "unused", // relay does not check it\n` +
    `});\n\n` +
    `const stream = await client.chat.completions.create({\n` +
    `  model: "${model}",\n` +
    `  messages: [{ role: "user", content: "Hello" }],\n` +
    `  stream: true,\n` +
    `});`
  );
}

const btnStyle: CSSProperties = {
  padding: '8px 12px',
  borderRadius: 6,
  border: `1px solid ${C.borderStrong}`,
  background: C.bgInset,
  color: C.textMut,
  font: `500 11px ${SANS}`,
  cursor: 'pointer',
  userSelect: 'none',
  whiteSpace: 'nowrap',
};

const urlBoxStyle: CSSProperties = {
  flex: 1,
  background: C.bg,
  border: `1px solid ${C.border}`,
  borderRadius: 6,
  padding: '9px 12px',
  font: `400 12.5px ${MONO}`,
  color: C.cyan,
};

export default function ProxyInfo(): React.JSX.Element {
  const [copied, setCopied] = useState<Record<string, boolean>>({});
  const [snipTab, setSnipTab] = useState<SnipTab>('curl');

  const { data } = usePoll<ProxyInfoOut>(api.proxyInfo, 5000);

  const copyFeedback = useCallback((k: string, text: string) => {
    try { void navigator.clipboard.writeText(text); } catch { /* ignored */ }
    setCopied(prev => ({ ...prev, [k]: true }));
    setTimeout(() => setCopied(prev => ({ ...prev, [k]: false })), 1600);
  }, []);

  const baseUrl = data?.base_url ?? '';
  const snippet = getSnippet(snipTab, baseUrl, data?.example_model ?? 'auto');

  const tabs: SnipTab[] = ['curl', 'python', 'node'];

  const statCards = [
    { label: 'Uptime', value: data ? fmtUptime(data.uptime_s) : '—' },
    { label: 'Requests proxied', value: data ? fmt(data.requests_total) : '—' },
    { label: 'Active clients', value: data ? String(data.active_clients) : '—' },
  ];

  return (
    <div style={{ padding: '14px 18px 24px', display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 860 }}>
      {/* Base URL */}
      <div style={{ ...card, padding: 16, display: 'flex', flexDirection: 'column', gap: 9 }}>
        <span style={sectionLabel}>Base URL</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={urlBoxStyle}>{baseUrl}</span>
          <div
            onClick={() => copyFeedback('url', baseUrl)}
            style={btnStyle}
          >
            {copied['url'] ? '✓' : 'Copy'}
          </div>
        </div>
        <span style={{ font: `400 11px ${SANS}`, color: C.textDim }}>
          Drop-in replacement — set this as{' '}
          <span style={{ fontFamily: MONO }}>base_url</span>{' '}
          in any OpenAI client. No API key: send{' '}
          <span style={{ fontFamily: MONO }}>model</span>{' '}
          and go.
        </span>
      </div>

      {/* Snippets */}
      <div style={{ ...card, padding: 16, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {tabs.map(t => (
            <div
              key={t}
              onClick={() => setSnipTab(t)}
              style={{
                padding: '4px 12px',
                borderRadius: 5,
                cursor: 'pointer',
                userSelect: 'none',
                font: `${snipTab === t ? 600 : 400} 11px ${MONO}`,
                color: snipTab === t ? C.text : C.textMut,
                background: snipTab === t ? '#1A2233' : 'transparent',
                border: `1px solid ${snipTab === t ? C.borderStrong : 'transparent'}`,
              } as CSSProperties}
            >
              {t}
            </div>
          ))}
        </div>
        <pre style={{
          margin: 0,
          background: C.bg,
          border: `1px solid ${C.border}`,
          borderRadius: 6,
          padding: '14px 16px',
          font: `400 12px/1.7 ${MONO}`,
          color: '#B9C4D6',
          overflowX: 'auto',
        }}>
          {snippet}
        </pre>
      </div>

      {/* Stat cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 14 }}>
        {statCards.map(s => (
          <div key={s.label} style={{ ...card, padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 3 }}>
            <span style={{ font: `500 10px ${SANS}`, letterSpacing: '.07em', textTransform: 'uppercase', color: C.textMut }}>{s.label}</span>
            <span style={{ font: `700 17px ${MONO}` }}>{s.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
