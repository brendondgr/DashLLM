/** Polling hook: fetch immediately, then on an interval; pauses while the
 * tab is hidden; refetches when deps change. */

import { useCallback, useEffect, useRef, useState } from 'react';

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
): { data: T | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const aliveRef = useRef(true);

  const tick = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      if (aliveRef.current) {
        setData(result);
        setError(null);
      }
    } catch (e) {
      if (aliveRef.current) setError(String(e));
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    void tick();
    const id = setInterval(() => {
      if (!document.hidden) void tick();
    }, intervalMs);
    return () => {
      aliveRef.current = false;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, tick, ...deps]);

  return { data, error, refresh: tick };
}
