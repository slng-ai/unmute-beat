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

export type RunEvent = EventBase &
  (
    | { type: 'run.started'; agent: string; config: string; channel: 'voice' | 'text' | 'twilio'; entry: string; state: Record<string, unknown>; forked_from: string | null; forked_turn: number | null }
    | { type: 'caller.speech'; phase: 'started' | 'partial' | 'final' | 'stopped'; text: string; continues_turn: boolean }
    | { type: 'step.entered'; step: string; source: string | null; via: 'entry' | 'exit' | 'consult' | 'handback' | 'prerequisite' | 'return' | 'route' | 'then' }
    | { type: 'model.request'; step: string; request: number; budget: number }
    | { type: 'model.delta'; step: string; text: string }
    | { type: 'model.decision'; step: string; output: 'reply' | 'record' | 'operation' | 'hand_over' | 'goodbye' | 'done' | 'read' | 'options'; name: string; args: Record<string, unknown>; refused: string }
    | { type: 'model.ended'; step: string; tokens_in: number; tokens_out: number; ms: number }
    | { type: 'state.committed'; state: Record<string, unknown>; changed: string[]; reset: string[] }
    | { type: 'intent.changed'; intent: string; operation: string; status: 'prepared' | 'dispatched' | 'unknown' | 'succeeded' | 'rejected' | 'withdrawn'; args: Record<string, unknown>; receipt: Record<string, unknown> | null }
    | { type: 'bot.speech'; segment: string; phase: 'queued' | 'playing' | 'delivered' | 'cut' | 'cleared'; text: string; source: 'model' | 'code'; reason: string; evidence: string }
    | { type: 'turn.finished'; status: 'answered' | 'interrupted' | 'exhausted' | 'failed'; step: string; handoffs: string[]; requests: number; detail: string }
    | { type: 'run.ended'; outcome: 'goodbye' | 'hangup' | 'ceiling' | 'failed' | 'stopped'; detail: string }
  );

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

export interface TimedRequest {
  kind: 'stt' | 'llm' | 'tts';
  model: string;
  provider: string;
  duration_ms: number;
  first_byte_ms: number | null;
  error: boolean;
  span: string;
  level: Level;
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
  turn: number;
  voice_to_voice_ms: number | null;
  interrupted: boolean;
  requests: TimedRequest[];
  flags: Flag[];
  level: Level;
}

export interface ModelStats {
  kind: 'stt' | 'llm' | 'tts';
  model: string;
  provider: string;
  calls: number;
  p50_ms: number;
  p95_ms: number;
  errors: number;
  over_limit: number;
}

export interface Metrics {
  source: string;
  limits: Limits;
  summary: { turns: number; voice_to_voice_avg_ms: number | null; voice_to_voice_p95_ms: number | null; over_limit: number; errors: number };
  turns: TurnTiming[];
  models: ModelStats[];
}
