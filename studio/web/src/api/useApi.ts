import { useEffect, useState } from 'react';

export type Loaded<T> = { state: 'loading' } | { state: 'ok'; data: T } | { state: 'error'; message: string; status?: number };

/** One request per change of `key`; a stale answer never overwrites a newer one. */
export function useApi<T>(load: () => Promise<T>, key: string): Loaded<T> {
  const [result, setResult] = useState<Loaded<T>>({ state: 'loading' });
  useEffect(() => {
    let live = true;
    setResult({ state: 'loading' });
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
