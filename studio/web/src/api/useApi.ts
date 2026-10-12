import { useEffect, useState } from 'react';

export type Loaded<T> = { state: 'loading' } | { state: 'ok'; data: T; refreshing?: boolean } | { state: 'error'; message: string; status?: number };

/**
 * One request per change of `key`; a stale answer never overwrites a newer one. With `keep`, the last answer
 * stays on show (`refreshing`) while the next one loads, so a reload never blanks the page or unmounts an
 * edit in progress. Leave it off where the old answer would mislead, such as a different run or turn.
 */
export function useApi<T>(load: () => Promise<T>, key: string, keep = false): Loaded<T> {
  const [result, setResult] = useState<Loaded<T>>({ state: 'loading' });
  useEffect(() => {
    let live = true;
    setResult((now) => (keep && now.state === 'ok' ? { ...now, refreshing: true } : { state: 'loading' }));
    load().then(
      (data) => live && setResult({ state: 'ok', data }),
      (error: unknown) =>
        live &&
        setResult({
          state: 'error',
          message: error instanceof Error ? error.message : String(error),
          status: typeof error === 'object' && error !== null && 'status' in error ? Number(error.status) : undefined,
        }),
    );
    return () => {
      live = false;
    };
  }, [key]);
  return result;
}
