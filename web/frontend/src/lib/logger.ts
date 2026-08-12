/** Frontend event logging: everything the dashboard does is batched and
 * shipped to the backend log stream (POST /admin/logs/frontend), so client
 * actions are auditable next to proxy/router/tunnel events. */

type Level = 'debug' | 'info' | 'warn' | 'error';

interface Pending {
  ts: number;
  level: Level;
  event: string;
  detail?: string;
}

const queue: Pending[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;

async function flush(): Promise<void> {
  timer = null;
  if (!queue.length) return;
  const events = queue.splice(0, 200);
  try {
    await fetch('/admin/logs/frontend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events }),
      keepalive: true,
    });
  } catch {
    // Logging must never break the UI; drop the batch on network failure.
  }
}

function push(level: Level, event: string, detail?: string): void {
  const line = `[relay:${level}] ${event}${detail ? ` — ${detail}` : ''}`;
  if (level === 'error') console.error(line);
  else if (level === 'warn') console.warn(line);
  else console.debug(line);
  queue.push({ ts: Date.now() / 1000, level, event, detail });
  if (queue.length >= 50) void flush();
  else if (!timer) timer = setTimeout(() => void flush(), 2000);
}

export const log = {
  debug: (event: string, detail?: string) => push('debug', event, detail),
  info: (event: string, detail?: string) => push('info', event, detail),
  warn: (event: string, detail?: string) => push('warn', event, detail),
  error: (event: string, detail?: string) => push('error', event, detail),
};

if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => void flush());
}
