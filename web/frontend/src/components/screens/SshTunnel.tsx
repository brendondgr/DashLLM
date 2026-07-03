import { useState, useEffect, useRef } from 'react';
import type { CSSProperties, JSX } from 'react';
import { C, ACCENT, MONO, SANS, track, knob, input, sectionLabel } from '../../lib/styles';
import { fmtUptime } from '../../lib/format';
import { api } from '../../lib/api';
import { log } from '../../lib/logger';
import { usePoll } from '../../hooks/usePoll';
import type { TunnelOut } from '../../lib/types';

interface LogLine {
  ts: string;
  msg: string;
  kind: 'dim' | 'ok' | 'acc' | 'err';
}

function now8(): string {
  return new Date().toTimeString().slice(0, 8);
}

export default function SshTunnel(): JSX.Element {
  const [user, setUser] = useState('sander');
  const [host, setHost] = useState('gpu-box.lan');
  const [rport, setRport] = useState('8000');
  const [lport, setLport] = useState('8443');
  const [key, setKey] = useState('~/.ssh/id_ed25519');
  const [compress, setCompress] = useState(true);
  const [keepalive, setKeepalive] = useState(true);

  const [tunnelId, setTunnelId] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [tunnelLog, setTunnelLog] = useState<LogLine[]>([]);
  const [copied, setCopied] = useState(false);
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load existing 'dashboard' tunnel on mount
  useEffect(() => {
    api.tunnels().then((ts) => {
      const dash = ts.find((t) => t.name === 'dashboard');
      if (dash) {
        setTunnelId(dash.id);
        setUser(dash.ssh_user);
        setHost(dash.ssh_host);
        setRport(String(dash.remote_port));
        setLport(String(dash.local_port));
        if (dash.key_path) setKey(dash.key_path);
        setCompress(dash.compress);
        setKeepalive(dash.keepalive);
      }
    }).catch(() => { /* no tunnels yet, use defaults */ });
  }, []);

  // Poll active tunnels every 3s
  const { data: allTunnels } = usePoll(() => api.tunnels(), 3000);
  const activeTunnels: TunnelOut[] = (allTunnels ?? []).filter(
    (t) => t.status === 'up' || t.status === 'starting'
  );

  // Compute command locally
  const sshCmd =
    `ssh -N${compress ? ' -C' : ''}${keepalive ? ' -o ServerAliveInterval=30' : ''} -i ${key} -L ${lport}:127.0.0.1:${rport} ${user}@${host}`;

  function appendLine(line: LogLine) {
    setTunnelLog((prev) => [...prev, line]);
  }

  async function runTest() {
    if (testing) return;
    setTesting(true);
    setTunnelLog([]);

    appendLine({ ts: now8(), msg: `$ ssh -N -L ${lport}:127.0.0.1:${rport} ${user}@${host}`, kind: 'dim' });

    let id = tunnelId;
    try {
      const payload = {
        name: 'dashboard',
        ssh_host: host,
        ssh_user: user,
        remote_port: +rport,
        local_port: +lport,
        key_path: key,
        compress,
        keepalive,
      };
      if (!id) {
        const created = await api.createTunnel(payload);
        id = created.id;
        setTunnelId(id);
      } else {
        await api.patchTunnel(id, payload);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      appendLine({ ts: now8(), msg: `✕ ${msg}`, kind: 'err' });
      setTesting(false);
      return;
    }

    try {
      const result = await api.testTunnel(id!);

      if (result.ssh_ok) {
        appendLine({
          ts: now8(),
          msg: `Connected to ${host} (${result.ssh_latency_ms}ms)`,
          kind: 'ok',
        });
      } else {
        appendLine({
          ts: now8(),
          msg: `✕ ${result.error ?? 'SSH connection failed'}`,
          kind: 'err',
        });
        log.info('tunnel.test', `ssh_fail host=${host}`);
        setTesting(false);
        return;
      }

      if (result.endpoint_ok) {
        appendLine({
          ts: now8(),
          msg: `GET http://127.0.0.1:${lport}/v1/models → 200 OK · ${result.models.length} model(s) · ${result.endpoint_latency_ms}ms`,
          kind: 'ok',
        });
        appendLine({ ts: now8(), msg: '✓ Endpoint reachable through tunnel', kind: 'acc' });
        log.info('tunnel.test', `ok host=${host} models=${result.models.length}`);
      } else {
        appendLine({
          ts: now8(),
          msg: result.error ?? 'Endpoint not reachable (tunnel may not be running)',
          kind: 'dim',
        });
        log.info('tunnel.test', `endpoint_fail host=${host} err=${result.error ?? ''}`);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      appendLine({ ts: now8(), msg: `✕ ${msg}`, kind: 'err' });
      log.info('tunnel.test', `exception ${msg}`);
    }

    setTesting(false);
  }

  async function copyCmd() {
    try {
      await navigator.clipboard.writeText(sshCmd);
    } catch {
      /* ignore clipboard errors */
    }
    setCopied(true);
    if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current);
    copiedTimerRef.current = setTimeout(() => setCopied(false), 1600);
  }

  // ---- styles ----

  const cardStyle: CSSProperties = {
    background: C.bgCard,
    border: `1px solid ${C.border}`,
    borderRadius: 8,
    padding: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  };

  const fieldLabel: CSSProperties = {
    font: `500 11px ${SANS}`,
    color: C.textMut,
  };

  const fieldWrap: CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  };

  function logColor(kind: LogLine['kind']): string {
    if (kind === 'ok') return C.green;
    if (kind === 'acc') return ACCENT;
    if (kind === 'err') return C.red;
    return C.textMut;
  }

  return (
    <div
      style={{
        padding: '14px 18px 24px',
        display: 'grid',
        gridTemplateColumns: '340px 1fr',
        gap: 14,
        maxWidth: 1100,
        alignItems: 'start',
      }}
    >
      {/* LEFT: Tunnel configuration */}
      <div style={cardStyle}>
        <span style={sectionLabel}>Tunnel configuration</span>

        <div style={fieldWrap}>
          <span style={fieldLabel}>User</span>
          <input
            value={user}
            onChange={(e) => setUser(e.target.value)}
            style={input}
          />
        </div>

        <div style={fieldWrap}>
          <span style={fieldLabel}>Remote host</span>
          <input
            value={host}
            onChange={(e) => setHost(e.target.value)}
            style={input}
          />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div style={fieldWrap}>
            <span style={fieldLabel}>Remote port</span>
            <input
              value={rport}
              onChange={(e) => setRport(e.target.value)}
              style={input}
            />
          </div>
          <div style={fieldWrap}>
            <span style={fieldLabel}>Local port</span>
            <input
              value={lport}
              onChange={(e) => setLport(e.target.value)}
              style={input}
            />
          </div>
        </div>

        <div style={fieldWrap}>
          <span style={fieldLabel}>Identity file</span>
          <input
            value={key}
            onChange={(e) => setKey(e.target.value)}
            style={input}
          />
        </div>

        {/* Compression toggle */}
        <div
          onClick={() => setCompress((v) => !v)}
          style={{ display: 'flex', alignItems: 'center', gap: 9, cursor: 'pointer', userSelect: 'none' }}
        >
          <div style={track(compress)}>
            <div style={knob(compress)} />
          </div>
          <span style={{ font: `400 12px ${SANS}`, color: C.textMut }}>
            Compression <span style={{ fontFamily: MONO }}>(-C)</span>
          </span>
        </div>

        {/* Keep-alive toggle */}
        <div
          onClick={() => setKeepalive((v) => !v)}
          style={{ display: 'flex', alignItems: 'center', gap: 9, cursor: 'pointer', userSelect: 'none' }}
        >
          <div style={track(keepalive)}>
            <div style={knob(keepalive)} />
          </div>
          <span style={{ font: `400 12px ${SANS}`, color: C.textMut }}>
            Keep-alive every 30s
          </span>
        </div>
      </div>

      {/* RIGHT column */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

        {/* Generated command card */}
        <div
          style={{
            background: C.bgCard,
            border: `1px solid ${C.border}`,
            borderRadius: 8,
            padding: 16,
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={sectionLabel}>Generated command</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <div
                onClick={copyCmd}
                style={{
                  padding: '5px 12px',
                  borderRadius: 6,
                  border: `1px solid ${C.borderStrong}`,
                  background: C.bgInset,
                  color: C.textMut,
                  font: `500 11px ${SANS}`,
                  cursor: 'pointer',
                  userSelect: 'none',
                }}
              >
                {copied ? '✓ Copied' : 'Copy'}
              </div>
              <div
                onClick={runTest}
                style={{
                  padding: '5px 12px',
                  borderRadius: 6,
                  background: ACCENT,
                  color: C.bg,
                  font: `600 11px ${SANS}`,
                  cursor: 'pointer',
                  userSelect: 'none',
                }}
              >
                {testing ? '⟳ Testing…' : 'Test connection'}
              </div>
            </div>
          </div>

          <div
            style={{
              background: C.bg,
              border: `1px solid ${C.border}`,
              borderRadius: 6,
              padding: '12px 14px',
              font: `400 12px/1.6 ${MONO}`,
              color: C.cyan,
              wordBreak: 'break-all',
            }}
          >
            {sshCmd}
          </div>

          <span style={{ font: `400 11px ${SANS}`, color: C.textDim }}>
            Forwards{' '}
            <span style={{ fontFamily: MONO, color: C.textMut }}>localhost:{lport}</span>
            {' → '}
            <span style={{ fontFamily: MONO, color: C.textMut }}>{host}:{rport}</span>
            . Point an endpoint at the local port once connected.
          </span>
        </div>

        {/* Test log card (only when lines exist) */}
        {tunnelLog.length > 0 && (
          <div
            style={{
              background: C.bg,
              border: `1px solid ${C.border}`,
              borderRadius: 8,
              padding: '14px 16px',
              display: 'flex',
              flexDirection: 'column',
              gap: 5,
            }}
          >
            {tunnelLog.map((line, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
                <span
                  style={{
                    font: `400 10.5px ${MONO}`,
                    color: C.textDim,
                  }}
                >
                  {line.ts}
                </span>
                <span
                  style={{
                    font: `400 11.5px ${MONO}`,
                    color: logColor(line.kind),
                  }}
                >
                  {line.msg}
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Active tunnels card */}
        <div
          style={{
            background: C.bgCard,
            border: `1px solid ${C.border}`,
            borderRadius: 8,
            padding: 16,
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
          }}
        >
          <span style={sectionLabel}>Active tunnels</span>

          {activeTunnels.length === 0 ? (
            <span style={{ font: `400 11px ${MONO}`, color: C.textDim }}>
              no active tunnels
            </span>
          ) : (
            activeTunnels.map((tn) => (
              <div
                key={tn.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  padding: '9px 12px',
                  background: C.bgSidebar,
                  border: `1px solid ${C.border}`,
                  borderRadius: 6,
                }}
              >
                <div
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: '50%',
                    background: C.green,
                    animation: 'livepulse 2s infinite',
                  }}
                />
                <span
                  style={{
                    font: `400 11.5px ${MONO}`,
                    flex: 1,
                  }}
                >
                  {`localhost:${tn.local_port} → ${tn.ssh_user}@${tn.ssh_host}:${tn.remote_port}`}
                </span>
                <span style={{ font: `400 10.5px ${MONO}`, color: C.textDim }}>
                  {tn.status === 'up'
                    ? `up ${fmtUptime(tn.uptime_s)}`
                    : tn.status}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
