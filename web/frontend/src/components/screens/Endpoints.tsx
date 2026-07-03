import React, { useState, useCallback, useEffect } from 'react';
import type { CSSProperties } from 'react';
import type { EndpointOut, EndpointTestResult, TunnelSessionStatus } from '../../lib/types';
import { C, ACCENT, MONO, SANS, dot, badge, statusColor } from '../../lib/styles';
import { api } from '../../lib/api';
import { log } from '../../lib/logger';
import { usePoll } from '../../hooks/usePoll';

interface EndpointsProps {
  endpoints: EndpointOut[];
  swapping: string | null;
  onActivate: (id: string) => void;
  refresh: () => void;
}

type TestState = 'testing' | 'ok' | 'fail' | null;

interface TestEntry {
  state: TestState;
  result: EndpointTestResult | null;
}

const inputStyle: CSSProperties = {
  background: C.bgSidebar,
  border: `1px solid ${C.borderStrong}`,
  borderRadius: 6,
  color: C.text,
  padding: '7px 10px',
  font: `400 12px ${MONO}`,
  outline: 'none',
};

const selectStyle: CSSProperties = {
  background: C.bgSidebar,
  border: `1px solid ${C.borderStrong}`,
  borderRadius: 6,
  color: C.text,
  padding: '7px 8px',
  font: `400 12px ${MONO}`,
  outline: 'none',
};

export default function Endpoints(props: EndpointsProps): React.JSX.Element {
  const { endpoints, swapping, onActivate, refresh } = props;

  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState({ name: '', type: 'llama.cpp', alias: '', url: '', key: '', tunnel: '' });
  const [tests, setTests] = useState<Record<string, TestEntry>>({});

  // Live interactive tunnel-session state, keyed by endpoint id. Polled fast
  // while a prompt could be pending; nothing connects on its own.
  const { data: sessionList } = usePoll(() => api.tunnelSessions(), 1500);
  const sessions: Record<string, TunnelSessionStatus> = {};
  for (const s of sessionList ?? []) sessions[s.endpoint_id] = s;

  const [connecting, setConnecting] = useState<Record<string, boolean>>({});
  const [reply, setReply] = useState('');

  // The single endpoint whose ssh is currently waiting for input drives the modal.
  const awaiting = (sessionList ?? []).find(s => s.status === 'awaiting_input') ?? null;
  useEffect(() => { setReply(''); }, [awaiting?.endpoint_id, awaiting?.prompt]);

  const toggleAdd = useCallback(() => setAddOpen(o => !o), []);

  const handleFormChange = (field: string) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setForm(f => ({ ...f, [field]: e.target.value }));

  const saveEp = useCallback(async () => {
    if (!form.name || !form.url) return;
    try {
      await api.createEndpoint({
        name: form.name,
        base_url: form.url,
        server_type: form.type,
        upstream_key: form.key || null,
        alias: form.alias.trim() || null,
        tunnel_command: form.tunnel.trim() || null,
      });
      log.info('endpoints.create', `${form.name}${form.alias ? ` (alias ${form.alias})` : ''}${form.tunnel.trim() ? ' +tunnel' : ''}`);
      setAddOpen(false);
      setForm({ name: '', type: 'llama.cpp', alias: '', url: '', key: '', tunnel: '' });
      refresh();
    } catch {
      // api already logs errors
    }
  }, [form, refresh]);

  const connectTunnel = useCallback(async (id: string) => {
    setConnecting(c => ({ ...c, [id]: true }));
    log.info('endpoints.tunnel.connect', id);
    try {
      await api.connectEndpointTunnel(id);
    } catch {
      // api already logs errors
    } finally {
      setConnecting(c => ({ ...c, [id]: false }));
    }
  }, []);

  const disconnectTunnel = useCallback(async (id: string) => {
    log.info('endpoints.tunnel.disconnect', id);
    try {
      await api.disconnectEndpointTunnel(id);
    } catch {
      // api already logs errors
    }
  }, []);

  const submitReply = useCallback(async () => {
    if (!awaiting) return;
    const id = awaiting.endpoint_id;
    log.info('endpoints.tunnel.respond', `${id} secret=${awaiting.prompt_secret}`);
    try {
      await api.respondEndpointTunnel(id, reply);
      setReply('');
    } catch {
      // api already logs errors
    }
  }, [awaiting, reply]);

  const testEndpoint = useCallback(async (id: string, baseUrl: string) => {
    setTests(t => ({ ...t, [id]: { state: 'testing', result: null } }));
    log.info('endpoints.test', id);
    try {
      const result = await api.testEndpoint(id);
      setTests(t => ({ ...t, [id]: { state: result.ok ? 'ok' : 'fail', result } }));
      setTimeout(() => setTests(t => ({ ...t, [id]: { state: null, result: null } })), 3500);
    } catch {
      setTests(t => ({ ...t, [id]: { state: 'fail', result: null } }));
      setTimeout(() => setTests(t => ({ ...t, [id]: { state: null, result: null } })), 3500);
    }
  }, []);

  return (
    <div style={{ padding: '14px 18px 24px', display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 980 }}>
      {/* Top row */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ font: `400 12px ${SANS}`, color: C.textMut }}>
          Requests route to the <b style={{ color: C.text }}>active</b> endpoint. Hot-swapping drains in-flight requests first.
        </span>
        <div
          onClick={toggleAdd}
          style={{
            padding: '6px 14px',
            borderRadius: 6,
            background: ACCENT,
            color: '#0B0E14',
            font: `600 12px ${SANS}`,
            cursor: 'pointer',
            userSelect: 'none',
          }}
        >
          {addOpen ? 'Close' : '+ Add endpoint'}
        </div>
      </div>

      {/* Add form */}
      {addOpen && (
        <div style={{
          background: C.bgCard,
          border: `1px solid ${C.borderStrong}`,
          borderRadius: 8,
          padding: 14,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}>
          <span style={{ font: `600 11px ${SANS}`, letterSpacing: '.07em', textTransform: 'uppercase', color: C.textMut }}>
            New endpoint
          </span>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 130px 110px 1fr 1fr', gap: 10 }}>
            <input
              placeholder="name"
              value={form.name}
              onChange={handleFormChange('name')}
              style={inputStyle}
            />
            <select
              value={form.type}
              onChange={handleFormChange('type')}
              style={selectStyle}
            >
              <option value="llama.cpp">llama.cpp</option>
              <option value="vLLM">vLLM</option>
              <option value="ollama">ollama</option>
              <option value="openai">OpenAI-compat</option>
            </select>
            <input
              placeholder="alias (skynet)"
              value={form.alias}
              onChange={handleFormChange('alias')}
              style={inputStyle}
            />
            <input
              placeholder="http://127.0.0.1:8000/v1"
              value={form.url}
              onChange={handleFormChange('url')}
              style={inputStyle}
            />
            <input
              placeholder="api key (optional)"
              value={form.key}
              onChange={handleFormChange('key')}
              style={inputStyle}
            />
          </div>
          <input
            placeholder='ssh tunnel command (optional) — e.g. ssh -N -L 127.0.0.1:9090:localhost:9090 skynet-alt'
            value={form.tunnel}
            onChange={handleFormChange('tunnel')}
            style={inputStyle}
          />
          <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
            alias = routing name: clients send it as the <span style={{ fontFamily: MONO }}>model</span> to reach this server directly
            (e.g. <span style={{ fontFamily: MONO }}>"model": "skynet"</span>); relay rewrites it to the server's real model.
            Add an <b style={{ color: C.textMut }}>ssh tunnel command</b> for a remote server you reach over SSH — you connect it manually
            with <b style={{ color: C.textMut }}>Connect</b>, and any password/passphrase prompt appears here.
          </span>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <div
              onClick={toggleAdd}
              style={{
                padding: '6px 14px',
                borderRadius: 6,
                border: `1px solid ${C.borderStrong}`,
                color: C.textMut,
                font: `500 12px ${SANS}`,
                cursor: 'pointer',
              }}
            >
              Cancel
            </div>
            <div
              onClick={saveEp}
              style={{
                padding: '6px 14px',
                borderRadius: 6,
                background: '#3FB950',
                color: '#0B0E14',
                font: `600 12px ${SANS}`,
                cursor: 'pointer',
              }}
            >
              Save endpoint
            </div>
          </div>
        </div>
      )}

      {/* Endpoint cards */}
      {endpoints.map(e => {
        const testEntry = tests[e.id] ?? { state: null, result: null };
        const ts = testEntry.state;
        const tr = testEntry.result;

        const isSwapping = swapping === e.id;
        const locLabel = e.kind === 'local' ? 'local' : 'remote';
        const locColor = e.kind === 'local' ? C.green : C.purple;

        // Interactive tunnel state for this endpoint (only when it has a command).
        const hasTunnel = !!e.tunnel_command;
        const sess = sessions[e.id];
        const sStatus = sess?.status ?? 'idle';
        const tunnelUp = sStatus === 'up';
        const tunnelBusy = sStatus === 'connecting' || sStatus === 'awaiting_input' || connecting[e.id];
        const tunnelColor =
          tunnelUp ? C.green
          : sStatus === 'error' ? C.red
          : sStatus === 'awaiting_input' ? ACCENT
          : sStatus === 'connecting' ? C.cyan
          : C.textMut;
        const tunnelLabel =
          connecting[e.id] && sStatus === 'idle' ? '⟳ connecting…'
          : tunnelUp ? '● Disconnect'
          : sStatus === 'connecting' ? '⟳ connecting…'
          : sStatus === 'awaiting_input' ? '⌨ needs input'
          : sStatus === 'error' ? '↻ Reconnect'
          : 'Connect';
        const tunnelMsg =
          sStatus === 'up' && sess?.local_port ? `tunnel up · forwarding 127.0.0.1:${sess.local_port}`
          : sStatus === 'connecting' ? 'ssh connecting…'
          : sStatus === 'awaiting_input' ? `waiting: ${sess?.prompt ?? 'input required'}`
          : sStatus === 'error' ? (sess?.last_error ? `tunnel error: ${sess.last_error}` : 'tunnel error')
          : '';

        const latDisplay = (e.ewma_latency_ms != null && e.health !== 'failed')
          ? `${e.ewma_latency_ms}ms`
          : '—';

        const testLabel =
          ts === 'testing' ? '⟳ testing…'
          : ts === 'ok' ? '✓ OK'
          : ts === 'fail' ? '✕ failed'
          : 'Test connection';

        const testBorderColor =
          ts === 'ok' ? C.green
          : ts === 'fail' ? C.red
          : C.borderStrong;

        const testTextColor =
          ts === 'ok' ? C.green
          : ts === 'fail' ? C.red
          : C.textMut;

        const testStyle: CSSProperties = {
          padding: '6px 12px',
          borderRadius: 6,
          border: `1px solid ${testBorderColor}`,
          background: C.bgInset,
          color: testTextColor,
          font: `500 11.5px ${SANS}`,
          cursor: 'pointer',
          userSelect: 'none',
          whiteSpace: 'nowrap',
        };

        const testMsg =
          ts === 'ok' && tr
            ? `GET ${e.base_url}/models → 200 OK · ${tr.latency_ms}ms · ${tr.models[0] ?? ''}`
          : ts === 'fail' && tr?.error
            ? `GET ${e.base_url}/models → ${tr.error}`
          : ts === 'fail'
            ? `GET ${e.base_url}/models → connection refused (timeout 5s)`
          : ts === 'testing'
            ? 'Probing /v1/models…'
          : '';

        const testMsgColor =
          ts === 'fail' ? C.red
          : ts === 'ok' ? C.green
          : C.textMut;

        const cardStyle: CSSProperties = {
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
          background: C.bgCard,
          border: `1px solid ${e.active ? 'rgba(255,178,36,.45)' : C.border}`,
          borderRadius: 8,
          padding: '14px 16px',
        };

        return (
          <div key={e.id} style={cardStyle}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={dot(statusColor(e.health), e.health === 'healthy')} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ font: `600 13px ${MONO}` }}>{e.name}</span>
                  <span style={badge(C.cyan)}>{e.server_type}</span>
                  <span style={badge(locColor)}>{locLabel}</span>
                  {e.alias && (
                    <span style={badge(ACCENT)} title="model alias — send as &quot;model&quot; to route here">
                      model:{e.alias}
                    </span>
                  )}
                  {e.active && (
                    <span style={{
                      padding: '2px 8px',
                      borderRadius: 4,
                      background: 'rgba(255,178,36,.14)',
                      border: '1px solid rgba(255,178,36,.4)',
                      color: ACCENT,
                      font: `700 9.5px ${SANS}`,
                      letterSpacing: '.08em',
                      textTransform: 'uppercase',
                    }}>
                      Active
                    </span>
                  )}
                </div>
                <span style={{ font: `400 11px ${MONO}`, color: C.textMut }}>
                  {e.base_url} <span style={{ color: C.textDim }}>·</span> {e.model ?? '—'}
                </span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 1, marginRight: 6 }}>
                <span style={{ font: `500 12px ${MONO}`, color: C.text }}>{latDisplay}</span>
                <span style={{ font: `400 9.5px ${SANS}`, color: C.textDim }}>avg latency</span>
              </div>
              <div
                onClick={() => testEndpoint(e.id, e.base_url)}
                style={testStyle}
              >
                {testLabel}
              </div>
              {hasTunnel && (
                <div
                  onClick={() => (tunnelUp ? disconnectTunnel(e.id) : connectTunnel(e.id))}
                  title={e.tunnel_command ?? undefined}
                  style={{
                    padding: '6px 12px',
                    borderRadius: 6,
                    border: `1px solid ${tunnelColor}`,
                    background: tunnelUp ? 'rgba(63,185,80,.10)' : C.bgInset,
                    color: tunnelColor,
                    font: `500 11.5px ${SANS}`,
                    cursor: tunnelBusy && !tunnelUp ? 'default' : 'pointer',
                    userSelect: 'none',
                    whiteSpace: 'nowrap',
                    opacity: tunnelBusy && !tunnelUp && sStatus !== 'awaiting_input' ? 0.75 : 1,
                  }}
                >
                  {tunnelLabel}
                </div>
              )}
              {!e.active && (
                <div
                  onClick={() => onActivate(e.id)}
                  style={{
                    padding: '6px 12px',
                    borderRadius: 6,
                    border: `1px solid ${C.borderStrong}`,
                    background: C.bgInset,
                    color: C.text,
                    font: `500 11.5px ${SANS}`,
                    cursor: 'pointer',
                    userSelect: 'none',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {isSwapping ? 'draining…' : 'Set active'}
                </div>
              )}
            </div>
            {testMsg && (
              <div style={{
                font: `400 11px ${MONO}`,
                color: testMsgColor,
                background: C.bg,
                border: `1px solid ${C.border}`,
                borderRadius: 6,
                padding: '8px 12px',
              }}>
                {testMsg}
              </div>
            )}
            {tunnelMsg && (
              <div style={{
                font: `400 11px ${MONO}`,
                color: tunnelColor,
                background: C.bg,
                border: `1px solid ${C.border}`,
                borderRadius: 6,
                padding: '8px 12px',
              }}>
                {tunnelMsg}
              </div>
            )}
          </div>
        );
      })}

      {/* Interactive prompt modal — appears when ssh is waiting for input. */}
      {awaiting && (
        <div
          onClick={() => { /* backdrop click is a no-op; use buttons */ }}
          style={{
            position: 'fixed', inset: 0, zIndex: 50,
            background: 'rgba(4,6,10,.62)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <div style={{
            width: 460, maxWidth: '92vw',
            background: C.bgCard,
            border: `1px solid ${C.borderStrong}`,
            borderRadius: 10,
            padding: 20,
            display: 'flex', flexDirection: 'column', gap: 12,
            boxShadow: '0 18px 50px rgba(0,0,0,.5)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: ACCENT, animation: 'livepulse 2s infinite' }} />
              <span style={{ font: `600 12px ${SANS}`, letterSpacing: '.04em', color: C.text }}>
                SSH is asking for input
              </span>
            </div>
            <div style={{
              font: `400 12px ${MONO}`,
              color: C.cyan,
              background: C.bg,
              border: `1px solid ${C.border}`,
              borderRadius: 6,
              padding: '10px 12px',
              wordBreak: 'break-word',
            }}>
              {awaiting.prompt ?? 'input required'}
            </div>
            <input
              autoFocus
              type={awaiting.prompt_secret ? 'password' : 'text'}
              value={reply}
              onChange={(ev) => setReply(ev.target.value)}
              onKeyDown={(ev) => { if (ev.key === 'Enter') submitReply(); }}
              placeholder={awaiting.prompt_secret ? 'password / passphrase' : 'type your answer (e.g. yes)'}
              style={{ ...inputStyle, padding: '9px 12px' }}
            />
            <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
              {awaiting.prompt_secret
                ? 'Sent straight to ssh over the local PTY; never stored or logged.'
                : 'Sent to the ssh process. For a host-key prompt, answer yes.'}
            </span>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <div
                onClick={() => disconnectTunnel(awaiting.endpoint_id)}
                style={{
                  padding: '7px 14px', borderRadius: 6,
                  border: `1px solid ${C.borderStrong}`,
                  color: C.textMut, font: `500 12px ${SANS}`, cursor: 'pointer', userSelect: 'none',
                }}
              >
                Cancel
              </div>
              <div
                onClick={submitReply}
                style={{
                  padding: '7px 16px', borderRadius: 6,
                  background: ACCENT, color: '#0B0E14',
                  font: `600 12px ${SANS}`, cursor: 'pointer', userSelect: 'none',
                }}
              >
                Submit
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
