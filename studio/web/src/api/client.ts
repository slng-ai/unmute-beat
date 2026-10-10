import type { Agent, BootState } from './types';

/** A failed request, with the engine's own `detail` when it sent one. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { accept: 'application/json' } });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new ApiError(response.status, body.detail ?? `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const api = {
  boot: () => get<BootState>('/api/boot'),
  agents: () => get<Agent[]>('/api/agents'),
  agent: (id: string) => get<Agent>(`/api/agents/${encodeURIComponent(id)}`),
  runs: () => get<unknown[]>('/api/runs'),
};
