/** Dashboard shell: sidebar nav, header (in-flight chip + hot-swap
 * dropdown), screen switching. 1:1 structural port of the prototype;
 * endpoint pool + live concurrency are polled from the backend and shared
 * with the screens that need them. */

import { useState, type CSSProperties } from 'react';

import { usePoll } from '../hooks/usePoll';
import { api } from '../lib/api';
import { log } from '../lib/logger';
import { ACCENT, C, MONO, SANS, dot, statusColor } from '../lib/styles';
import Dashboard from './screens/Dashboard';
import Endpoints from './screens/Endpoints';
import ProxyInfo from './screens/ProxyInfo';
import Requests from './screens/Requests';
import Settings from './screens/Settings';
import SshTunnel from './screens/SshTunnel';

type ScreenId = 'dash' | 'req' | 'eps' | 'ssh' | 'proxy' | 'settings';

const SCREENS: [ScreenId, string, string][] = [
  ['dash', 'Dashboard', 'M1.5 1.5h5v5h-5zM9.5 1.5h5v5h-5zM1.5 9.5h5v5h-5zM9.5 9.5h5v5h-5z'],
  ['req', 'Requests', 'M2 3.5h12M2 8h12M2 12.5h7'],
  ['eps', 'Endpoints', 'M2 3.5h12v3.5H2zM2 9h12v3.5H2zM4.5 5.25h.01M4.5 10.75h.01'],
  ['ssh', 'SSH Tunnel', 'M2 4l4 4-4 4M8.5 12.5H14'],
  ['proxy', 'Proxy Info', 'M9.5 8.5h5M12 8.5v2.5M9.5 8.5a3.5 3.5 0 1 1-1.02-2.48A3.5 3.5 0 0 1 9.5 8.5z'],
  ['settings', 'Settings', 'M2 4.5h12M2 11.5h12M10.5 2.5v4M5.5 9.5v4'],
];

const TITLES: Record<ScreenId, string> = {
  dash: 'Dashboard', req: 'Live requests', eps: 'Endpoints',
  ssh: 'SSH tunnel', proxy: 'Proxy endpoint', settings: 'Settings',
};

export default function App() {
  const [screen, setScreen] = useState<ScreenId>('dash');
  const [swapOpen, setSwapOpen] = useState(false);
  const [swapping, setSwapping] = useState<string | null>(null);

  const { data: endpoints, refresh: refreshEndpoints } =
    usePoll(() => api.endpoints(), 5000);
  const { data: live } = usePoll(() => api.live(), 2000);
  const { data: proxyInfo } = usePoll(() => api.proxyInfo(), 30000);

  const eps = endpoints ?? [];
  const active = eps.find((e) => e.active) ?? eps[0] ?? null;
  const inFlight = live?.in_flight ?? 0;

  const hotSwap = async (id: string) => {
    setSwapOpen(false);
    if (id === active?.id) return;
    const target = eps.find((e) => e.id === id);
    log.info('hotswap', `${active?.name ?? '—'} -> ${target?.name ?? id}`);
    setSwapping(id);
    try {
      await api.activateEndpoint(id);
      refreshEndpoints();
    } catch {
      // api.ts already logged it
    }
    setTimeout(() => setSwapping(null), 900);
  };

  const go = (id: ScreenId) => {
    log.info('screen.switch', `${screen} -> ${id}`);
    setScreen(id);
    setSwapOpen(false);
  };

  const navStyle = (id: ScreenId): CSSProperties => ({
    display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
    borderRadius: 6, cursor: 'pointer', userSelect: 'none',
    font: `${screen === id ? 600 : 400} 12.5px ${SANS}`,
    color: screen === id ? C.text : C.textMut,
    background: screen === id ? C.bgHover : 'transparent',
    boxShadow: screen === id ? `inset 2px 0 0 ${ACCENT}` : 'none',
  });

  return (
    <div style={{
      display: 'flex', height: '100vh', overflow: 'hidden', background: C.bg,
      color: C.text, fontFamily: SANS, fontSize: 13,
    }}>
      {/* ============ SIDEBAR ============ */}
      <div style={{
        width: 200, flex: 'none', display: 'flex', flexDirection: 'column',
        background: C.bgSidebar, borderRight: `1px solid ${C.border}`,
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '16px 16px 14px', borderBottom: `1px solid ${C.border}`,
        }}>
          {/* One drawing, three places: assets/logo.svg is the original,
              tools/make_header.py copies it to public/logo.svg for this mark
              and the favicon, and embeds it in the README banner. */}
          <img src="/logo.svg" width={26} height={26} alt=""
               style={{ flex: 'none', display: 'block' }} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <span style={{ font: `700 14px ${MONO}`, letterSpacing: '.02em' }}>relay</span>
            <span style={{ font: `400 10px ${SANS}`, color: C.textMut }}>LLM proxy · v0.4.2</span>
          </div>
        </div>
        <div style={{
          display: 'flex', flexDirection: 'column', gap: 2,
          padding: '10px 8px', flex: 1,
        }}>
          {SCREENS.map(([id, label, icon]) => (
            <div key={id} className="hv-bg" onClick={() => go(id)} style={navStyle(id)}>
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none"
                stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
                strokeLinejoin="round"><path d={icon} /></svg>
              <span>{label}</span>
            </div>
          ))}
        </div>
        <div style={{
          padding: '12px 16px', borderTop: `1px solid ${C.border}`,
          display: 'flex', flexDirection: 'column', gap: 6,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
            <div style={{
              width: 7, height: 7, borderRadius: '50%', background: C.green,
              animation: 'livepulse 2s infinite',
            }} />
            <span style={{ font: `500 11px ${MONO}`, color: C.textMut }}>
              listening :{proxyInfo?.port ?? '…'}
            </span>
          </div>
          <span style={{ font: `400 10px ${SANS}`, color: C.textDim }}>
            OpenAI-compatible · /v1
          </span>
        </div>
      </div>

      {/* ============ MAIN ============ */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        {/* header */}
        <div style={{
          height: 48, flex: 'none', display: 'flex', alignItems: 'center',
          gap: 14, padding: '0 18px', borderBottom: `1px solid ${C.border}`,
          background: C.bgSidebar,
        }}>
          <span style={{ font: `600 14px ${SANS}` }}>{TITLES[screen]}</span>
          <div style={{ flex: 1 }} />
          <div style={{
            display: 'flex', alignItems: 'center', gap: 7, padding: '4px 10px',
            borderRadius: 6, background: C.bgInset, border: `1px solid ${C.border}`,
          }}>
            <div style={dot(inFlight > 0 ? C.cyan : C.textDim, inFlight > 0)} />
            <span style={{ font: `500 11px ${MONO}`, color: C.textMut }}>in-flight</span>
            <span style={{ font: `700 13px ${MONO}`, color: C.text }}>{inFlight}</span>
          </div>
          <div style={{ position: 'relative' }}>
            <div className="hv-border" onClick={() => setSwapOpen(!swapOpen)} style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '5px 12px',
              borderRadius: 6, background: C.bgInset,
              border: `1px solid ${C.borderStrong}`, cursor: 'pointer',
              userSelect: 'none',
            }}>
              <div style={dot(
                swapping ? ACCENT : statusColor(active?.health ?? 'failed'), true)} />
              <span style={{ font: `500 12px ${MONO}` }}>
                {swapping ? 'draining…' : active?.name ?? 'no endpoints'}
              </span>
              <span style={{ color: C.textMut, fontSize: 10 }}>▾</span>
            </div>
            {swapOpen && (
              <div style={{
                position: 'absolute', top: 36, right: 0, zIndex: 50, width: 280,
                background: C.bgInset, border: `1px solid ${C.borderStrong}`,
                borderRadius: 8, boxShadow: '0 12px 32px rgba(0,0,0,.5)',
                padding: 6, display: 'flex', flexDirection: 'column', gap: 2,
              }}>
                <span style={{
                  font: `600 10px ${SANS}`, letterSpacing: '.08em',
                  textTransform: 'uppercase', color: C.textMut,
                  padding: '6px 8px 4px',
                }}>Hot-swap endpoint</span>
                {eps.map((e) => (
                  <div key={e.id} className="hv-row" onClick={() => void hotSwap(e.id)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 10,
                      padding: '8px 8px', borderRadius: 6, cursor: 'pointer',
                    }}>
                    <div style={dot(statusColor(e.health), e.health === 'healthy')} />
                    <div style={{
                      display: 'flex', flexDirection: 'column', gap: 1,
                      flex: 1, minWidth: 0,
                    }}>
                      <span style={{
                        font: `500 12px ${MONO}`, whiteSpace: 'nowrap',
                        overflow: 'hidden', textOverflow: 'ellipsis',
                      }}>{e.name}</span>
                      <span style={{ font: `400 10px ${SANS}`, color: C.textMut }}>
                        {e.model ?? '—'} · {e.health}
                        {e.ewma_latency_ms ? ` · ${Math.round(e.ewma_latency_ms)}ms` : ''}
                      </span>
                    </div>
                    {e.active && (
                      <span style={{
                        font: `700 9px ${SANS}`, letterSpacing: '.08em',
                        color: ACCENT,
                      }}>ACTIVE</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* content */}
        <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          {screen === 'dash' && <Dashboard live={live} endpoints={eps} />}
          {screen === 'req' && <Requests />}
          {screen === 'eps' && (
            <Endpoints endpoints={eps} swapping={swapping}
              onActivate={(id) => void hotSwap(id)} refresh={refreshEndpoints} />
          )}
          {screen === 'ssh' && <SshTunnel />}
          {screen === 'proxy' && <ProxyInfo />}
          {screen === 'settings' && <Settings />}
        </div>
      </div>
    </div>
  );
}
