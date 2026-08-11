/** Login / signup gate. Rendered instead of the dashboard shell whenever
 * GET /auth/status says nobody is signed in.
 *
 * The signup tab only appears when the server has an invite code configured —
 * with no code, registration is off and offering the form would just produce
 * 403s. The key returned by signup is shown once and never again, so the
 * success panel is deliberately loud about copying it. */

import React, { useState } from 'react';
import type { CSSProperties } from 'react';

import { api } from '../../lib/api';
import { log } from '../../lib/logger';
import { ACCENT, C, MONO, SANS, card, input, segStyle } from '../../lib/styles';

type Tab = 'login' | 'signup';

const button = (disabled: boolean): CSSProperties => ({
  padding: '9px 14px', borderRadius: 6, border: 'none', background: ACCENT,
  color: C.bg, font: `600 12px ${SANS}`, cursor: disabled ? 'default' : 'pointer',
  opacity: disabled ? 0.5 : 1, width: '100%',
});

const label: CSSProperties = {
  font: `500 10.5px ${SANS}`, letterSpacing: '.06em',
  textTransform: 'uppercase', color: C.textMut,
};

interface Props {
  signupEnabled: boolean;
  onAuthenticated: () => void;
}

export default function Login({ signupEnabled, onAuthenticated }: Props): React.JSX.Element {
  const [tab, setTab] = useState<Tab>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issuedKey, setIssuedKey] = useState<string | null>(null);

  const submit = async (e: React.SyntheticEvent): Promise<void> => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (tab === 'login') {
        await api.login(username, password);
        log.info('auth.login', username);
        onAuthenticated();
      } else {
        const res = await api.signup(username, password, code);
        log.info('auth.signup', username);
        setIssuedKey(res.api_key);
      }
    } catch {
      // api.ts already logged the detail; keep the surface message vague so it
      // never reveals whether an account exists.
      setError(tab === 'login'
        ? 'Invalid username or password.'
        : 'Sign-up failed. Check the invite code, or try another username.');
    } finally {
      setBusy(false);
    }
  };

  if (issuedKey) {
    return (
      <Shell>
        <div style={{ ...card, padding: 20, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <span style={{ font: `600 14px ${SANS}`, color: C.green }}>
            Account created
          </span>
          <span style={{ font: `400 12px ${SANS}`, color: C.textMut, lineHeight: 1.5 }}>
            This is your proxy key. Copy it now — only a hash is stored, so it
            cannot be shown again. You can generate a new one from the Account
            tab at any time.
          </span>
          <code style={{
            background: C.bg, border: `1px solid ${C.borderStrong}`,
            borderRadius: 6, padding: '10px 12px', font: `400 12px ${MONO}`,
            color: ACCENT, wordBreak: 'break-all',
          }}>{issuedKey}</code>
          <button type="button" style={button(false)} onClick={onAuthenticated}>
            Continue to dashboard
          </button>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      {signupEnabled && (
        <div style={{
          display: 'flex', gap: 4, padding: 4, borderRadius: 7,
          background: C.bgInset, border: `1px solid ${C.border}`,
        }}>
          {(['login', 'signup'] as Tab[]).map((t) => (
            <div key={t} style={{ ...segStyle(tab === t), flex: 1, textAlign: 'center' }}
              onClick={() => { setTab(t); setError(null); }}>
              {t === 'login' ? 'Sign in' : 'Sign up'}
            </div>
          ))}
        </div>
      )}

      <form onSubmit={(e) => void submit(e)}
        style={{ ...card, padding: 20, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <Field id="username" label="Username">
          <input id="username" style={input} value={username} autoComplete="username"
            onChange={(e) => setUsername(e.target.value)} required />
        </Field>
        <Field id="password" label="Password">
          <input id="password" style={input} type="password" value={password}
            autoComplete={tab === 'login' ? 'current-password' : 'new-password'}
            onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        {tab === 'signup' && (
          <Field id="code" label="Invite code">
            <input id="code" style={input} value={code}
              onChange={(e) => setCode(e.target.value)} required />
          </Field>
        )}
        {tab === 'signup' && (
          <span style={{ font: `400 10.5px ${SANS}`, color: C.textDim }}>
            Minimum 12 characters. Your account comes with its own proxy key.
          </span>
        )}
        {error && (
          <span style={{ font: `400 11.5px ${SANS}`, color: C.red }}>{error}</span>
        )}
        <button type="submit" disabled={busy} style={button(busy)}>
          {busy ? 'Working…' : tab === 'login' ? 'Sign in' : 'Create account'}
        </button>
      </form>
    </Shell>
  );
}

function Field({ id, label: text, children }: {
  id: string; label: string; children: React.ReactNode;
}): React.JSX.Element {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <label htmlFor={id} style={label}>{text}</label>
      {children}
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }): React.JSX.Element {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      height: '100vh', background: C.bg, color: C.text, fontFamily: SANS,
    }}>
      <div style={{
        width: 340, display: 'flex', flexDirection: 'column', gap: 14,
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '0 2px 4px',
        }}>
          <div style={{
            width: 30, height: 30, flex: 'none', borderRadius: 7,
            background: ACCENT, display: 'grid', placeItems: 'center',
            color: C.bg, font: `700 15px ${MONO}`,
          }}>⇄</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <span style={{ font: `700 15px ${MONO}`, letterSpacing: '.02em' }}>relay</span>
            <span style={{ font: `400 10px ${SANS}`, color: C.textMut }}>
              LLM proxy · sign in to continue
            </span>
          </div>
        </div>
        {children}
      </div>
    </div>
  );
}
