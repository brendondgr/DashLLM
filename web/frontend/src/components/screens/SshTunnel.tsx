import { useState, useEffect, useRef } from 'react';
import type { CSSProperties, JSX } from 'react';
import { C, ACCENT, MONO, SANS, track, knob, input, sectionLabel } from '../../lib/styles';
import { api } from '../../lib/api';
import { log } from '../../lib/logger';
import { usePoll } from '../../hooks/usePoll';
import type { SshHostOut, TunnelSessionStatus } from '../../lib/types';

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
  const [key, setKey] = useState('');
  const [compress, setCompress] = useState(true);
  const [keepalive, setKeepalive] = useState(true);

  // ~/.ssh/config hosts, so a shorthand alias fills in the *real* host/user/key.
  const [sshHosts, setSshHosts] = useState<SshHostOut[]>([]);
  const [pickedAlias, setPickedAlias] = useState('');
  const [keyHint, setKeyHint] = useState<string | null>(null);

  const [tunnelId, setTunnelId] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [tunnelLog, setTunnelLog] = useState<LogLine[]>([]);
  const [copied, setCopied] = useState(false);
  const [termInput, setTermInput] = useState('');
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const termScrollRef = useRef<HTMLDivElement | null>(null);

  // Load existing 'dashboard' tunnel + ssh config hosts on mount
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
    api.sshHosts().then(setSshHosts).catch(() => { /* no ~/.ssh/config */ });
  }, []);

  // Live PTY session state for the dashboard tunnel (drives the terminal).
  const { data: session } = usePoll<TunnelSessionStatus | null>(
    () => (tunnelId ? api.tunnelSession(tunnelId) : Promise.resolve(null)),
    1500,
    [tunnelId],
  );
  const sStatus = session?.status ?? 'idle';
  const sessionUp = sStatus === 'up';
  const sessionLive = sStatus === 'connecting' || sStatus === 'awaiting_input' || sessionUp;
  const awaiting = sStatus === 'awaiting_input';

  // Auto-scroll the terminal to the newest line.
  useEffect(() => {
    const el = termScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [session?.output?.length, sStatus]);

  // Command mirrors what the backend runs; identity flag only when a key is set
  // (blank key ⇒ ssh resolves the IdentityFile from ~/.ssh/config, not a guess).
  const sshCmd =
    `ssh -N${compress ? ' -C' : ''}${keepalive ? ' -o ServerAliveInterval=30' : ''}` +
    `${key.trim() ? ` -i ${key.trim()}` : ''} -L ${lport}:127.0.0.1:${rport} ${user}@${host}`;

  function appendLine(line: LogLine) {
    setTunnelLog((prev) => [...prev, line]);
  }

  function applyHost(alias: string) {
    setPickedAlias(alias);
    const h = sshHosts.find((x) => x.alias === alias);
    if (!h) return;
    if (h.user) setUser(h.user);
    // The dashboard forwards through the alias; ssh handles HostName/ProxyJump.
    setHost(alias);
    if (h.identity_explicit && h.identity_file) {
      setKey(h.identity_file);
      setKeyHint(`${h.identity_file} — from ~/.ssh/config`);
    } else {
      setKey('');
      setKeyHint(
        h.identity_files.length > 1
          ? 'no specific key configured — ssh will try your default keys'
          : null,
      );
    }
    log.info('tunnel.ssh_host', `${alias}${h.proxyjump ? ` via ${h.proxyjump}` : ''}`);
  }

  async function ensureTunnel(): Promise<string> {
    const payload = {
      name: 'dashboard',
      ssh_host: host,
      ssh_user: user,
      remote_port: +rport,
      local_port: +lport,
      key_path: key.trim() || null,
      compress,
      keepalive,
    };
    if (!tunnelId) {
      const created = await api.createTunnel(payload);
      setTunnelId(created.id);
      return created.id;
    }
    await api.patchTunnel(tunnelId, payload);
    return tunnelId;
  }

  async function runTest() {
    if (testing) return;
    setTesting(true);
    appendLine({ ts: now8(), msg: `$ probe ${host}:22`, kind: 'dim' });
    try {
      const id = await ensureTunnel();
      const result = await api.testTunnel(id);
      if (result.ssh_ok) {
        appendLine({ ts: now8(), msg: `Reachable ${host} (${result.ssh_latency_ms}ms)`, kind: 'ok' });
      } else {
        appendLine({ ts: now8(), msg: `✕ ${result.error ?? 'SSH connection failed'}`, kind: 'err' });
      }
      if (result.endpoint_ok) {
        appendLine({
          ts: now8(),
          msg: `GET 127.0.0.1:${lport}/v1/models → 200 · ${result.models.length} model(s)`,
          kind: 'acc',
        });
      }
      log.info('tunnel.test', `host=${host} ssh_ok=${result.ssh_ok}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      appendLine({ ts: now8(), msg: `✕ ${msg}`, kind: 'err' });
    }
    setTesting(false);
  }

  async function connect() {
    if (connecting || sessionLive) return;
    setConnecting(true);
    setTunnelLog([]);
    appendLine({ ts: now8(), msg: `$ ${sshCmd}`, kind: 'dim' });
    try {
      const id = await ensureTunnel();
      await api.connectTunnelSession(id);
      log.info('tunnel.connect', `host=${host} lport=${lport}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      appendLine({ ts: now8(), msg: `✕ ${msg}`, kind: 'err' });
    }
    setConnecting(false);
  }

  async function disconnect() {
    if (!tunnelId) return;
    try {
      await api.disconnectTunnelSession(tunnelId);
      log.info('tunnel.disconnect', tunnelId);
    } catch { /* api logs errors */ }
  }

  async function sendInput() {
    if (!tunnelId || !sessionLive) return;
    const text = termInput;
    setTermInput('');
    try {
      await api.respondTunnelSession(tunnelId, text);
    } catch { /* api logs errors */ }
  }

  async function copyCmd() {
    try {
      await navigator.clipboard.writeText(sshCmd);
    } catch { /* ignore clipboard errors */ }
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

  const fieldLabel: CSSProperties = { font: `500 11px ${SANS}`, color: C.textMut };
  const fieldWrap: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 };

  function logColor(kind: LogLine['kind']): string {
    if (kind === 'ok') return C.green;
    if (kind === 'acc') return ACCENT;
    if (kind === 'err') return C.red;
    return C.textMut;
  }

  const statusColor =
    sessionUp ? C.green
    : sStatus === 'error' ? C.red
    : awaiting ? ACCENT
    : sStatus === 'connecting' ? C.cyan
    : C.textMut;
  const statusLabel =
    sessionUp ? `up · forwarding 127.0.0.1:${session?.local_port ?? lport}`
    : sStatus === 'connecting' ? 'connecting…'
    : awaiting ? 'waiting for input'
    : sStatus === 'error' ? (session?.last_error ? `error: ${session.last_error}` : 'error')
    : sStatus === 'stopped' ? 'disconnected'
    : 'idle';

  return (
    <div
      style={{
        padding: '14px 18px 24px',
        display: 'grid',
        gridTemplateColumns: '340px 1fr',
        gap: 14,
        maxWidth: 1180,
        alignItems: 'start',
      }}
    >
      {/* LEFT: Tunnel configuration */}
      <div style={cardStyle}>
        <span style={sectionLabel}>Tunnel configuration</span>

        {sshHosts.length > 0 && (
          <div style={fieldWrap}>
            <span style={fieldLabel}>Load from ~/.ssh/config</span>
            <select
              value={pickedAlias}
              onChange={(e) => applyHost(e.target.value)}
              style={{ ...input, cursor: 'pointer' }}
            >
              <option value="">— pick a host —</option>
              {sshHosts.map((h) => (
                <option key={h.alias} value={h.alias}>
                  {h.alias}{h.hostname ? ` (${h.hostname})` : ''}{h.proxyjump ? ` ↝ ${h.proxyjump}` : ''}
                </option>
              ))}
            </select>
          </div>
        )}

        <div style={fieldWrap}>
          <span style={fieldLabel}>User</span>
          <input value={user} onChange={(e) => setUser(e.target.value)} style={input} />
        </div>

        <div style={fieldWrap}>
          <span style={fieldLabel}>Remote host / alias</span>
          <input value={host} onChange={(e) => setHost(e.target.value)} style={input} />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div style={fieldWrap}>
            <span style={fieldLabel}>Remote port</span>
            <input value={rport} onChange={(e) => setRport(e.target.value)} style={input} />
          </div>
          <div style={fieldWrap}>
            <span style={fieldLabel}>Local port</span>
            <input value={lport} onChange={(e) => setLport(e.target.value)} style={input} />
          </div>
        </div>

        <div style={fieldWrap}>
          <span style={fieldLabel}>Identity file <span style={{ color: C.textDim }}>(optional)</span></span>
          <input
            value={key}
            placeholder="leave blank to use ~/.ssh/config"
            onChange={(e) => { setKey(e.target.value); setKeyHint(null); }}
            style={input}
          />
          {keyHint && (
            <span style={{ font: `400 10px ${MONO}`, color: C.textDim, wordBreak: 'break-all' }}>
              {keyHint}
            </span>
          )}
        </div>

        {/* Compression toggle */}
        <div
          onClick={() => setCompress((v) => !v)}
          style={{ display: 'flex', alignItems: 'center', gap: 9, cursor: 'pointer', userSelect: 'none' }}
        >
          <div style={track(compress)}><div style={knob(compress)} /></div>
          <span style={{ font: `400 12px ${SANS}`, color: C.textMut }}>
            Compression <span style={{ fontFamily: MONO }}>(-C)</span>
          </span>
        </div>

        {/* Keep-alive toggle */}
        <div
          onClick={() => setKeepalive((v) => !v)}
          style={{ display: 'flex', alignItems: 'center', gap: 9, cursor: 'pointer', userSelect: 'none' }}
        >
          <div style={track(keepalive)}><div style={knob(keepalive)} /></div>
          <span style={{ font: `400 12px ${SANS}`, color: C.textMut }}>Keep-alive every 30s</span>
        </div>
      </div>

      {/* RIGHT column */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>

        {/* Generated command card */}
        <div style={{ ...cardStyle, gap: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={sectionLabel}>Generated command</span>
            <div style={{ display: 'flex', gap: 8 }}>
              <div
                onClick={copyCmd}
                style={{
                  padding: '5px 12px', borderRadius: 6, border: `1px solid ${C.borderStrong}`,
                  background: C.bgInset, color: C.textMut, font: `500 11px ${SANS}`,
                  cursor: 'pointer', userSelect: 'none',
                }}
              >
                {copied ? '✓ Copied' : 'Copy'}
              </div>
              <div
                onClick={runTest}
                style={{
                  padding: '5px 12px', borderRadius: 6, border: `1px solid ${C.borderStrong}`,
                  background: C.bgInset, color: C.textMut, font: `500 11px ${SANS}`,
                  cursor: 'pointer', userSelect: 'none',
                }}
              >
                {testing ? '⟳ Probing…' : 'Quick test'}
              </div>
              {sessionLive ? (
                <div
                  onClick={disconnect}
                  style={{
                    padding: '5px 12px', borderRadius: 6, background: 'rgba(248,81,73,.14)',
                    border: `1px solid ${C.red}`, color: C.red, font: `600 11px ${SANS}`,
                    cursor: 'pointer', userSelect: 'none',
                  }}
                >
                  ● Disconnect
                </div>
              ) : (
                <div
                  onClick={connect}
                  style={{
                    padding: '5px 12px', borderRadius: 6, background: ACCENT, color: C.bg,
                    font: `600 11px ${SANS}`, cursor: 'pointer', userSelect: 'none',
                  }}
                >
                  {connecting ? '⟳ Connecting…' : 'Connect'}
                </div>
              )}
            </div>
          </div>

          <div
            style={{
              background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6,
              padding: '12px 14px', font: `400 12px/1.6 ${MONO}`, color: C.cyan,
              wordBreak: 'break-all',
            }}
          >
            {sshCmd}
          </div>

          <span style={{ font: `400 11px ${SANS}`, color: C.textDim }}>
            Forwards <span style={{ fontFamily: MONO, color: C.textMut }}>localhost:{lport}</span>
            {' → '}<span style={{ fontFamily: MONO, color: C.textMut }}>{host}:{rport}</span>.
            {' '}<b style={{ color: C.textMut }}>Connect</b> runs it in a real terminal below.
          </span>
        </div>

        {/* Interactive terminal card */}
        <div style={{ ...cardStyle, gap: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
            <div style={{
              width: 8, height: 8, borderRadius: '50%', background: statusColor,
              animation: sessionLive ? 'livepulse 2s infinite' : 'none',
            }} />
            <span style={sectionLabel}>Interactive terminal</span>
            <span style={{ font: `400 10.5px ${MONO}`, color: statusColor, marginLeft: 'auto' }}>
              {statusLabel}
            </span>
          </div>

          <div
            ref={termScrollRef}
            style={{
              background: '#05070B', border: `1px solid ${C.border}`, borderRadius: 6,
              padding: '12px 14px', height: 300, overflowY: 'auto',
              font: `400 11.5px/1.55 ${MONO}`, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}
          >
            {tunnelLog.map((line, i) => (
              <div key={`log-${i}`} style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
                <span style={{ color: C.textDim, font: `400 10px ${MONO}` }}>{line.ts}</span>
                <span style={{ color: logColor(line.kind) }}>{line.msg}</span>
              </div>
            ))}
            {(session?.output ?? []).map((ln, i) => (
              <div key={`out-${i}`} style={{ color: C.text }}>{ln}</div>
            ))}
            {tunnelLog.length === 0 && (session?.output?.length ?? 0) === 0 && (
              <span style={{ color: C.textDim }}>
                idle — press <b style={{ color: C.textMut }}>Connect</b> to open an ssh session here.
              </span>
            )}
          </div>

          {/* Input line: types straight into the ssh PTY (answers prompts too). */}
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span style={{ font: `600 13px ${MONO}`, color: awaiting ? ACCENT : C.textDim }}>
              {awaiting ? '?' : '›'}
            </span>
            <input
              value={termInput}
              type={session?.prompt_secret ? 'password' : 'text'}
              disabled={!sessionLive}
              placeholder={
                !sessionLive ? 'connect to type into the session'
                : awaiting ? (session?.prompt ?? 'ssh is asking for input')
                : 'send a line to the terminal (e.g. yes)'
              }
              onChange={(e) => setTermInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void sendInput(); }}
              style={{ ...input, flex: 1, opacity: sessionLive ? 1 : 0.6 }}
            />
            <div
              onClick={() => void sendInput()}
              style={{
                padding: '7px 14px', borderRadius: 6,
                background: sessionLive ? ACCENT : C.bgInset,
                border: sessionLive ? 'none' : `1px solid ${C.borderStrong}`,
                color: sessionLive ? C.bg : C.textDim,
                font: `600 11px ${SANS}`, cursor: sessionLive ? 'pointer' : 'default',
                userSelect: 'none', whiteSpace: 'nowrap',
              }}
            >
              Send
            </div>
          </div>
          {session?.prompt_secret && awaiting && (
            <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
              Sent straight to ssh over the local PTY; never stored or logged.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
