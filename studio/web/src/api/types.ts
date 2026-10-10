// Mirrors of the engine's models. Keep in step with studio/src/studio/{discovery,boot,events}.py;
// `/api/events/schema` is the events' JSON schema, for generating these once the format settles.

export type StepKind = 'conversation' | 'action' | 'end';
export type Role = 'entry' | 'gate' | 'consulted';
export type EdgeKind =
  | 'exit'
  | 'consult'
  | 'handback'
  | 'prerequisite'
  | 'return'
  | 'route'
  | 'then'
  | 'error'
  | 'unknown'
  | 'take_back'
  | 'goodbye';

export interface GraphNode {
  id: string;
  kind: StepKind;
  roles: Role[];
  agent: string;
  prompt: string;
  fields: string[];
  facts: string[] | null;
  operations: string[];
  choices: string[];
  offers: string[];
  requires: string[];
  opens_with: string;
  capabilities: string[];
  virtual: boolean;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: EdgeKind;
  purpose: string;
}

export interface Group {
  id: string;
  steps: string[];
}

export interface Graph {
  entry: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  groups: Group[];
}

export interface Agent {
  id: string;
  path: string;
  status: 'compiled' | 'error';
  error: string;
  app_version: string;
  entry: string;
  model: string;
  steps: number;
  operations: number;
  graph?: Graph | null;
}

export type BootStatus = 'waiting' | 'starting' | 'healthy' | 'failed' | 'skipped';

export interface BootStep {
  id: string;
  label: string;
  detail: string;
  status: BootStatus;
  message: string;
}

export interface BootState {
  steps: BootStep[];
  ready: boolean;
  failed: boolean;
  agents: string[];
}

interface EventBase {
  run: string;
  seq: number;
  turn: number;
  t_ms: number;
}

export type IntentStatus = 'prepared' | 'dispatched' | 'unknown' | 'succeeded' | 'rejected' | 'withdrawn';

/** core.events, as the runner sends them and the store keeps them; plus the studio's own two markers. */
export type RunEvent = EventBase &
  (
    | { type: 'run.started'; agent: string; config: string; channel: 'voice' | 'text' | 'twilio'; entry: string; state: Record<string, unknown>; forked_from: string | null; forked_turn: number | null }
    | { type: 'caller.speech'; phase: 'started' | 'partial' | 'final' | 'stopped'; text: string; continues_turn: boolean }
    | { type: 'step.entered'; step: string; source: string | null; via: string }
    | { type: 'model.request'; step: string; request: number; budget: number }
    | { type: 'model.delta'; step: string; kind: 'text' | 'args'; name: string; text: string }
    | { type: 'model.decision'; step: string; name: string; args: Record<string, unknown>; refused: string }
    | { type: 'model.ended'; step: string; model: string; tokens_in: number; tokens_out: number; ms: number; first_ms: number | null }
    | { type: 'state.committed'; state: Record<string, unknown>; changed: string[] }
    | { type: 'intent.changed'; intent: string; operation: string; status: IntentStatus; args: Record<string, unknown> }
    | { type: 'receipt'; operation: string; intent: string; receipt: Record<string, unknown>; succeeded: boolean }
    | { type: 'bot.speech'; segment: string; phase: 'queued' | 'delivered' | 'cut'; text: string; source: 'model' | 'code'; reason: string; evidence: string }
    | { type: 'turn.finished'; status: 'answered' | 'interrupted' | 'exhausted' | 'failed'; step: string; handoffs: string[]; requests: number; detail: string }
    | { type: 'turn.snapshot' }
    | { type: 'run.ended'; outcome: 'goodbye' | 'hangup' | 'ceiling' | 'failed' | 'stopped'; detail: string; dropped: number }
  );

/** Sent by the studio on the live socket, never stored: the replay is done, or the run is unknown. */
export type StudioMarker = { type: 'studio.replayed' | 'studio.missing'; run: string };

export type Level = 'ok' | 'slow' | 'bad';

export interface Limit {
  slow: number;
  bad: number;
}

export interface Limits {
  voice_to_voice: Limit;
  stt_first_byte: Limit;
  llm_first_token: Limit;
  llm_request: Limit;
  tts_first_byte: Limit;
  requests_per_turn: Limit;
}

export type Kind = 'stt' | 'llm' | 'tts';

export interface TimedRequest {
  kind: Kind;
  model: string;
  provider: string;
  duration_ms: number;
  first_byte_ms: number | null;
  error: boolean;
  span: string;
  level: Level;
  at: number;
  trace: string;
}

export interface Flag {
  level: 'slow' | 'bad';
  what: string;
  value: number;
  limit: number;
  span: string;
}

export interface TurnTiming {
  trace: string;
  run: string;
  agent: string;
  turn: number;
  voice_to_voice_ms: number | null;
  interrupted: boolean;
  requests: TimedRequest[];
  flags: Flag[];
  level: Level;
}

export interface ModelStats {
  kind: Kind;
  model: string;
  provider: string;
  requests: number;
  runs: number;
  first_byte_p90_ms: number | null;
  total_p90_ms: number;
  errors: number;
  over_limit: number;
  first_seen: string;
  last_seen: string;
}

export interface Summary {
  turns: number;
  runs: number;
  voice_to_voice_p90_ms: number | null;
  llm_first_token_p90_ms: number | null;
  tts_first_audio_p90_ms: number | null;
  stt_first_byte_p90_ms: number | null;
  over_limit: number;
  errors: number;
  mixed: Kind[];
}

export interface TraceRun {
  trace: string;
  run: string;
  agent: string;
  started: string;
  turns: number;
}

export interface Metrics {
  source: string;
  scope: 'overview' | 'run';
  agent: string;
  limits: Limits;
  summary: Summary;
  runs: TraceRun[];
  turns: TurnTiming[];
  models: ModelStats[];
}

export interface RunRow {
  id: string;
  agent: string;
  config: string;
  channel: 'voice' | 'text' | 'twilio';
  started_at: string;
  ended_at: string | null;
  outcome: string | null;
  forked_from: string | null;
  forked_turn: number | null;
  runner: string;
  turns: number;
  step: string;
}

export interface RunDetail {
  run: RunRow;
  live: boolean;
  turns: number[];
}

export interface RunnerInfo {
  runner: string;
  url: string;
  pid: number;
  max_calls: number;
  agents: string[];
  frameworks: string[];
  ready: boolean;
  warming: boolean;
  stopping: boolean;
  active: number;
  calls: { id: string; agent: string; channel: string; status: string; step: string }[];
  lag_p50_ms: number | null;
  connected: boolean;
}

export interface StartRequest {
  agent: string;
  channel: 'voice' | 'text';
  caller_id?: string;
  resume_from?: { run: string; turn: number };
  facts?: Record<string, unknown>;
  delivery?: 'marks' | 'simulated';
}

export interface StartResult {
  run: string;
  agent: string;
  channel: 'voice' | 'text';
  config: string;
  runner: string;
  said: string[];
  step: string;
  turn: number;
  offer?: string;
}

export interface TurnResult {
  said: string[];
  status: string;
  step: string;
  turn: number;
  requests: number;
  ended: boolean;
}

export interface SaidLine {
  turn: number;
  who: 'caller' | 'bot';
  text: string;
  heard?: boolean;
}

export interface GoBack {
  run: RunRow;
  turn: number;
  step: string;
  pending: string | null;
  consulting: string | null;
  facts: Record<string, unknown>;
  said: SaidLine[];
  intents: { id: string; operation: string; status: IntentStatus; args: string }[];
  receipts: Record<string, unknown>[];
  done_after: { turn: number; operation: string; receipt: Record<string, unknown> }[];
  turns: number[];
}

export interface StateSchema {
  state: { properties?: Record<string, { type?: string; description?: string; anyOf?: unknown[]; enum?: unknown[]; format?: string; title?: string }>; $defs?: Record<string, unknown> };
  steps: string[];
  start: string;
}

export interface Refusal {
  loc: (string | number)[];
  msg: string;
  type: string;
}

/** One closing line and the result it is for: receipt values, `refused`, or none (any other success). */
export interface Case {
  line: string;
  when?: Record<string, unknown> | 'refused' | null;
}

/** A closing line: one line, or lines by result tried in order (`core.closing`). */
export type Words = string | Case[];

/** An operation's latest receipt of each result, from recent runs. */
export interface LastReceipts {
  done?: Record<string, unknown>;
  refused?: Record<string, unknown>;
}

export interface EditorOperation {
  name: string;
  say: Words;
  recap: Words;
  changes: boolean;
  approval: string;
  fields: string[];
  /** The fields a refusal has; empty when it is never refused with a receipt. */
  refused_fields: string[];
  /** Each field of a success with a closed set of values, and those values. */
  values: Record<string, unknown[]>;
  /** The same for a refusal. */
  refused_values: Record<string, unknown[]>;
  form: 'class' | 'function';
  route: string;
}

export interface EditorStep {
  id: string;
  kind: 'conversation' | 'action' | 'end';
  prompt: string;
  agent: string;
  fields: string[];
  operations: string[];
  operation: string;
  then: string;
  on_error: string;
  on_unknown: string;
  returns: boolean;
}

export interface Problem {
  where: string;
  what: string;
  level: 'error' | 'warning';
}

export interface EditorConfig {
  agent: string;
  path: string;
  version: string;
  manifest: string;
  files: Record<string, string>;
  steps: EditorStep[];
  operations: EditorOperation[];
  problems: Problem[];
}

export interface ConfigEdit {
  base?: string;
  manifest?: string | null;
  files?: Record<string, string>;
  closing?: Record<string, { say?: Words | null; recap?: Words | null }>;
  actions?: Record<string, { then?: string | null; on_error?: string | null; on_unknown?: string | null; returns?: boolean | null }>;
}

export interface Checked {
  ok: boolean;
  version: string;
  problems: Problem[];
  operations: EditorOperation[];
  changed: string[];
}

export interface Saved extends Checked {
  saved: boolean;
  config: ConfigVersion | null;
}

export interface ConfigVersion {
  agent: string;
  version: string;
  path: string;
  parent: string | null;
  created_at: string | null;
}
