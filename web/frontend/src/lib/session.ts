/** Session plumbing shared by api.ts and logger.ts.
 *
 * Lives in its own module because logger.ts posts with a bare fetch and api.ts
 * logs through logger.ts — importing one from the other would cycle.
 *
 * The session token itself is an HttpOnly cookie and is deliberately not
 * readable here. What is readable is the CSRF token, which every mutating
 * request must echo back in a header (double-submit); the browser attaches the
 * session cookie on its own. */

const CSRF_COOKIE = 'relay_csrf';
export const CSRF_HEADER = 'X-Relay-CSRF';

export function csrfToken(): string {
  if (typeof document === 'undefined') return '';
  const hit = document.cookie
    .split('; ')
    .find((c) => c.startsWith(`${CSRF_COOKIE}=`));
  return hit ? decodeURIComponent(hit.slice(CSRF_COOKIE.length + 1)) : '';
}

export function csrfHeaders(): Record<string, string> {
  const token = csrfToken();
  return token ? { [CSRF_HEADER]: token } : {};
}

type Handler = () => void;
let onUnauthorizedHandler: Handler | null = null;

/** Registered by App.tsx so any 401 anywhere drops the UI back to the login
 * screen instead of leaving a shell that silently fails to poll. */
export function setUnauthorizedHandler(fn: Handler | null): void {
  onUnauthorizedHandler = fn;
}

export function notifyUnauthorized(): void {
  onUnauthorizedHandler?.();
}
