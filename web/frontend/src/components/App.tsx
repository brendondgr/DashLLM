/** Dashboard shell: sidebar nav, header (in-flight chip + hot-swap
 * dropdown), screen switching. 1:1 structural port of the prototype;
 * endpoint pool + live concurrency are polled from the backend and shared
 * with the screens that need them.
 *
 * Auth gate: nothing renders until GET /auth/status answers. Non-admins get a
 * reduced sidebar (no endpoints / SSH / proxy / settings) and an Account tab.
 * That filtering is presentation only — the backend guards those routes, and
 * hiding a tab is never what keeps anyone out. */

import { useCallback, useEffect, useState, type CSSProperties } from 'react';

import { usePoll } from '../hooks/usePoll';
import { api, setStatScope, type StatScope } from '../lib/api';
import { log } from '../lib/logger';
import { setUnauthorizedHandler } from '../lib/session';
import { ACCENT, C, MONO, SANS, dot, segStyle, statusColor } from '../lib/styles';
import type { MeOut } from '../lib/types';
import Account from './screens/Account';
import Dashboard from './screens/Dashboard';
import Endpoints from './screens/Endpoints';
import Login from './screens/Login';
import ProxyInfo from './screens/ProxyInfo';
import Requests from './screens/Requests';
import Settings from './screens/Settings';
import SshTunnel from './screens/SshTunnel';

type ScreenId = 'dash' | 'req' | 'eps' | 'ssh' | 'proxy' | 'settings' | 'account';

const SCREENS: [ScreenId, string, string][] = [
  ['dash', 'Dashboard', 'M1.5 1.5h5v5h-5zM9.5 1.5h5v5h-5zM1.5 9.5h5v5h-5zM9.5 9.5h5v5h-5z'],
  ['req', 'Requests', 'M2 3.5h12M2 8h12M2 12.5h7'],
  ['eps', 'Endpoints', 'M2 3.5h12v3.5H2zM2 9h12v3.5H2zM4.5 5.25h.01M4.5 10.75h.01'],
  ['ssh', 'SSH Tunnel', 'M2 4l4 4-4 4M8.5 12.5H14'],
  ['proxy', 'Proxy Info', 'M9.5 8.5h5M12 8.5v2.5M9.5 8.5a3.5 3.5 0 1 1-1.02-2.48A3.5 3.5 0 0 1 9.5 8.5z'],
  ['settings', 'Settings', 'M2 4.5h12M2 11.5h12M10.5 2.5v4M5.5 9.5v4'],
  ['account', 'Account', 'M8 8.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM2.5 14c0-2.5 2.5-4 5.5-4s5.5 1.5 5.5 4'],
];

/** Screens backed by admin-only routes. Mirrors the backend's admin_guard
 * wiring — if a router moves planes, this list moves with it. */
const ADMIN_SCREENS = new Set<ScreenId>(['eps', 'ssh', 'proxy', 'settings']);

const TITLES: Record<ScreenId, string> = {
  dash: 'Dashboard', req: 'Live requests', eps: 'Endpoints',
  ssh: 'SSH tunnel', proxy: 'Proxy endpoint', settings: 'Settings',
  account: 'Account',
};

export default function App() {
  const [me, setMe] = useState<MeOut | null>(null);
  const [signupEnabled, setSignupEnabled] = useState(false);
  const [checked, setChecked] = useState(false);
  const [screen, setScreen] = useState<ScreenId>('dash');
  const [swapOpen, setSwapOpen] = useState(false);
  const [swapping, setSwapping] = useState<string | null>(null);
  const [scope, setScope] = useState<StatScope>('all');

  const isAdmin = me?.kind === 'admin';

  const refreshAuth = useCallback(async () => {
    try {
      const status = await api.authStatus();
      setSignupEnabled(status.signup_enabled);
      setMe(status.authenticated ? status.me : null);
    } catch {
      setMe(null);
    } finally {
      setChecked(true);
    }
  }, []);

  useEffect(() => { void refreshAuth(); }, [refreshAuth]);

  // A 401 from any call — an expired session, a revoked account — drops the
  // whole shell back to the login screen rather than leaving it polling into
  // a wall.
  useEffect(() => {
    setUnauthorizedHandler(() => setMe(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  const { data: endpoints, refresh: refreshEndpoints } = usePoll(
    () => (isAdmin ? api.endpoints() : Promise.resolve([])), 5000, [isAdmin]);
  const { data: live } = usePoll(
    () => (me ? api.live() : Promise.resolve(null)), 2000, [me !== null]);
  const { data: proxyInfo } = usePoll(
    () => (isAdmin ? api.proxyInfo() : Promise.resolve(null)), 30000, [isAdmin]);

  const eps = endpoints ?? [];
  const active = eps.find((e) => e.active) ?? eps[0] ?? null;
  const inFlight = live?.in_flight ?? 0;

  if (!checked) return <div style={{ height: '100vh', background: C.bg }} />;
  if (!me) {
    return <Login signupEnabled={signupEnabled}
      onAuthenticated={() => void refreshAuth()} />;
  }

  const visible = SCREENS.filter(([id]) =>
    id === 'account' ? !isAdmin : isAdmin || !ADMIN_SCREENS.has(id));
  const current = visible.some(([id]) => id === screen) ? screen : 'dash';

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
    log.info('screen.switch', `${current} -> ${id}`);
    setScreen(id);
    setSwapOpen(false);
  };

  const pickScope = (s: StatScope) => {
    log.info('stats.scope', s);
    setStatScope(s);
    setScope(s);
  };

  const signOut = async () => {
    log.info('auth.logout', me.username ?? '');
    try {
      await api.logout();
    } finally {
      setStatScope('all');
      setMe(null);
    }
  };

  const navStyle = (id: ScreenId): CSSProperties => ({
    display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
    borderRadius: 6, cursor: 'pointer', userSelect: 'none',
    font: `${current === id ? 600 : 400} 12.5px ${SANS}`,
    color: current === id ? C.text : C.textMut,
    background: current === id ? C.bgHover : 'transparent',
    boxShadow: current === id ? `inset 2px 0 0 ${ACCENT}` : 'none',
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
          <div style={{
            width: 26, height: 26, flex: 'none', borderRadius: 6,
            background: ACCENT, display: 'grid', placeItems: 'center',
            color: C.bg, font: `700 13px ${MONO}`,
          }}>⇄</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <span style={{ font: `700 14px ${MONO}`, letterSpacing: '.02em' }}>relay</span>
            <span style={{ font: `400 10px ${SANS}`, color: C.textMut }}>LLM proxy · v0.4.2</span>
          </div>
        </div>
        <div style={{
          display: 'flex', flexDirection: 'column', gap: 2,
          padding: '10px 8px', flex: 1,
        }}>
          {visible.map(([id, label, icon]) => (
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
              listening :{proxyInfo?.port ?? window.location.port ?? '…'}
            </span>
          </div>
          <span style={{ font: `400 10px ${SANS}`, color: C.textDim }}>
            OpenAI-compatible · /v1
          </span>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6, marginTop: 4,
            paddingTop: 8, borderTop: `1px solid ${C.border}`,
          }}>
            <span style={{
              font: `500 11px ${MONO}`, color: C.textMut, flex: 1,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{me.username ?? '—'}{isAdmin ? ' · admin' : ''}</span>
            <span className="hv-row" onClick={() => void signOut()} style={{
              font: `500 10.5px ${SANS}`, color: C.textDim, cursor: 'pointer',
              userSelect: 'none', padding: '2px 4px', borderRadius: 4,
            }}>Sign out</span>
          </div>
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
          <span style={{ font: `600 14px ${SANS}` }}>{TITLES[current]}</span>
          <div style={{ flex: 1 }} />
          {!isAdmin && (current === 'dash' || current === 'req') && (
            <div style={{
              display: 'flex', gap: 3, padding: 3, borderRadius: 6,
              background: C.bgInset, border: `1px solid ${C.border}`,
            }}>
              <div style={segStyle(scope === 'all')} onClick={() => pickScope('all')}>
                Everyone
              </div>
              <div style={segStyle(scope === 'me')} onClick={() => pickScope('me')}>
                My data
              </div>
            </div>
          )}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 7, padding: '4px 10px',
            borderRadius: 6, background: C.bgInset, border: `1px solid ${C.border}`,
          }}>
            <div style={dot(inFlight > 0 ? C.cyan : C.textDim, inFlight > 0)} />
            <span style={{ font: `500 11px ${MONO}`, color: C.textMut }}>in-flight</span>
            <span style={{ font: `700 13px ${MONO}`, color: C.text }}>{inFlight}</span>
          </div>
          {isAdmin && (
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
          )}
        </div>

        {/* content */}
        <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
          {current === 'dash' && <Dashboard live={live} endpoints={eps} scope={scope} />}
          {current === 'req' && <Requests scope={scope} />}
          {current === 'eps' && (
            <Endpoints endpoints={eps} swapping={swapping}
              onActivate={(id) => void hotSwap(id)} refresh={refreshEndpoints} />
          )}
          {current === 'ssh' && <SshTunnel />}
          {current === 'proxy' && <ProxyInfo />}
          {current === 'settings' && <Settings />}
          {current === 'account' && (
            <Account me={me} proxyPort={proxyInfo?.port ?? null}
              onRefresh={() => void refreshAuth()} />
          )}
        </div>
      </div>
    </div>
  );
}
