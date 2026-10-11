import type {
  Agent,
  BootState,
  Checked,
  ConfigEdit,
  ConfigVersion,
  EditorConfig,
  FactChecked,
  FactEdit,
  FactsView,
  GoBack,
  LastReceipts,
  Metrics,
  RunDetail,
  RunEvent,
  RunnerInfo,
  RunRow,
  Saved,
  StartRequest,
  StartResult,
  StateSchema,
  TurnResult,
} from './types';

/** A failed request, with the engine's own `detail` when it sent one (a string, or a refusal's structure). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail: unknown = undefined,
  ) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { accept: 'application/json', ...(body === undefined ? {} : { 'content-type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const found = (await response.json().catch(() => ({}))) as { detail?: unknown };
    const detail = found.detail;
    const message = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `${response.status} ${response.statusText}`;
    throw new ApiError(response.status, message, detail);
  }
  return (await response.json()) as T;
}

const get = <T>(path: string) => call<T>('GET', path);
const enc = encodeURIComponent;

export const api = {
  boot: () => get<BootState>('/api/boot'),
  agents: () => get<Agent[]>('/api/agents'),
  agent: (id: string) => get<Agent>(`/api/agents/${enc(id)}`),
  schema: (id: string) => get<StateSchema>(`/api/agents/${enc(id)}/schema`),
  config: (id: string) => get<EditorConfig>(`/api/agents/${enc(id)}/config`),
  check: (id: string, edit: ConfigEdit) => call<Checked>('POST', `/api/agents/${enc(id)}/config/check`, edit),
  save: (id: string, edit: ConfigEdit) => call<Saved>('PUT', `/api/agents/${enc(id)}/config`, edit),
  facts: (id: string) => get<FactsView>(`/api/agents/${enc(id)}/facts`),
  checkFact: (id: string, edit: FactEdit) => call<FactChecked>('POST', `/api/agents/${enc(id)}/facts/check`, edit),
  saveFact: (id: string, edit: FactEdit) => call<FactChecked>('PUT', `/api/agents/${enc(id)}/facts`, edit),
  versions: (id: string) => get<ConfigVersion[]>(`/api/agents/${enc(id)}/configs`),
  receipts: (id: string) => get<Record<string, LastReceipts>>(`/api/agents/${enc(id)}/receipts`),
  runners: () => get<RunnerInfo[]>('/api/runners'),
  runs: (agent = '') => get<RunRow[]>(`/api/runs${agent ? `?agent=${enc(agent)}` : ''}`),
  run: (id: string) => get<RunDetail>(`/api/runs/${enc(id)}`),
  events: (id: string) => get<RunEvent[]>(`/api/runs/${enc(id)}/events`),
  start: (ask: StartRequest) => call<StartResult>('POST', '/api/runs', ask),
  turn: (id: string, text: string) => call<TurnResult>('POST', `/api/runs/${enc(id)}/turns`, { text }),
  hangUp: (id: string) => call<{ status: string }>('DELETE', `/api/runs/${enc(id)}`),
  goBack: (id: string, turn: number) => get<GoBack>(`/api/runs/${enc(id)}/turns/${turn}`),
  metrics: (lookback = '1h', agent = '') =>
    get<Metrics>(`/api/metrics?lookback=${enc(lookback)}${agent ? `&agent=${enc(agent)}` : ''}`),
  traceMetrics: (trace: string) => get<Metrics>(`/api/metrics/runs/${enc(trace)}`),
  runMetrics: (run: string) => get<Metrics>(`/api/metrics/run/${enc(run)}`),
};
