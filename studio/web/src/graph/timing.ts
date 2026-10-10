import type { Level } from '../api/types';

/** Milliseconds as the page says them: under a second in ms, above in seconds with two places. */
export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
}

export const LEVEL_COLOR: Record<Level, string> = { ok: 'var(--muted)', slow: 'var(--yellow)', bad: 'var(--bad)' };
