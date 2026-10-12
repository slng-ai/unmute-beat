import { useEffect, useReducer } from 'react';
import type { IntentStatus, RunEvent, StudioMarker } from '../api/types';

export interface Line {
  turn: number;
  who: 'caller' | 'bot';
  text: string;
  segment?: string;
  source?: 'model' | 'code';
  reason?: string;
  heard?: boolean;
  cut?: boolean;
}

export interface Request {
  turn: number;
  step: string;
  request: number;
  budget: number;
  args: Record<string, string>;
  text: string;
  decisions: { name: string; args: Record<string, unknown>; refused: string }[];
  ended?: { ms: number; first_ms: number | null; tokens_in: number; tokens_out: number; model: string };
}

export interface Intent {
  id: string;
  operation: string;
  status: IntentStatus;
  args: Record<string, unknown>;
  receipt?: Record<string, unknown>;
  turn: number;
}

export interface TurnEnd {
  turn: number;
  step: string;
  status: string;
  requests: number;
  detail: string;
}

/** One run as the page draws it: built from its events alone, in order, whether live or read back. */
export interface RunView {
  status: 'connecting' | 'live' | 'ended' | 'missing';
  agent: string;
  channel: string;
  config: string;
  forked: { run: string; turn: number } | null;
  step: string;
  state: Record<string, unknown>;
  changed: string[];
  lines: Line[];
  requests: Request[];
  intents: Intent[];
  turns: TurnEnd[];
  snapshots: number[];
  path: { turn: number; step: string; via: string }[];
  outcome: string;
  thinking: boolean;
  events: number;
}

const EMPTY: RunView = {
  status: 'connecting',
  agent: '',
  channel: '',
  config: '',
  forked: null,
  step: '',
  state: {},
  changed: [],
  lines: [],
  requests: [],
  intents: [],
  turns: [],
  snapshots: [],
  path: [],
  outcome: '',
  thinking: false,
  events: 0,
};

type Message = RunEvent | StudioMarker | { type: 'reset' };

export function fold(view: RunView, event: Message): RunView {
  if (event.type === 'reset') return EMPTY;
  if (event.type === 'studio.replayed') return { ...view, status: view.outcome ? 'ended' : view.status === 'connecting' ? 'ended' : view.status };
  if (event.type === 'studio.missing') return { ...view, status: 'missing' };
  const next: RunView = { ...view, events: view.events + 1 };
  switch (event.type) {
    case 'run.started':
      return {
        ...next,
        status: 'live',
        agent: event.agent,
        channel: event.channel,
        config: event.config,
        forked: event.forked_from ? { run: event.forked_from, turn: event.forked_turn ?? 0 } : null,
        step: event.entry,
        state: event.state,
        path: [{ turn: event.turn, step: event.entry, via: event.forked_from ? 'resume' : 'entry' }],
      };
    case 'caller.speech': {
      if (event.phase !== 'final' || !event.text) return next;
      const last = next.lines.at(-1);
      if (event.continues_turn && last?.who === 'caller' && last.turn === event.turn) {
        return { ...next, lines: [...next.lines.slice(0, -1), { ...last, text: `${last.text} ${event.text}` }] };
      }
      return { ...next, lines: [...next.lines, { turn: event.turn, who: 'caller', text: event.text }] };
    }
    case 'step.entered':
      return { ...next, step: event.step, path: [...next.path, { turn: event.turn, step: event.step, via: event.via }] };
    case 'model.request':
      return {
        ...next,
        thinking: true,
        requests: [...next.requests, { turn: event.turn, step: event.step, request: event.request, budget: event.budget, args: {}, text: '', decisions: [] }],
      };
    case 'model.delta': {
      const last = next.requests.at(-1);
      if (!last) return next;
      const updated =
        event.kind === 'text'
          ? { ...last, text: last.text + event.text }
          : { ...last, args: { ...last.args, [event.name || Object.keys(last.args).at(-1) || '?']: (last.args[event.name || Object.keys(last.args).at(-1) || '?'] ?? '') + event.text } };
      return { ...next, requests: [...next.requests.slice(0, -1), updated] };
    }
    case 'model.decision': {
      const at = next.requests.findLastIndex((r) => r.step === event.step);
      if (at < 0) return next;
      const request = next.requests[at];
      if (!request) return next;
      const decided = { ...request, decisions: [...request.decisions, { name: event.name, args: event.args, refused: event.refused }] };
      return { ...next, requests: next.requests.map((r, i) => (i === at ? decided : r)) };
    }
    case 'model.ended': {
      const last = next.requests.at(-1);
      if (!last) return { ...next, thinking: false };
      const ended = { ms: event.ms, first_ms: event.first_ms, tokens_in: event.tokens_in, tokens_out: event.tokens_out, model: event.model };
      return { ...next, thinking: false, requests: [...next.requests.slice(0, -1), { ...last, ended }] };
    }
    case 'state.committed':
      return { ...next, state: event.state, changed: event.changed };
    case 'intent.changed': {
      const known = next.intents.find((i) => i.id === event.intent);
      const intent: Intent = { ...(known ?? { turn: event.turn, receipt: undefined }), id: event.intent, operation: event.operation, status: event.status, args: event.args };
      return { ...next, intents: known ? next.intents.map((i) => (i.id === event.intent ? intent : i)) : [...next.intents, intent] };
    }
    case 'receipt': {
      const known = next.intents.find((i) => i.id === event.intent);
      if (!known) {
        return { ...next, intents: [...next.intents, { id: event.intent || `r${event.seq}`, operation: event.operation, status: event.succeeded ? 'succeeded' : 'rejected', args: {}, receipt: event.receipt, turn: event.turn }] };
      }
      return { ...next, intents: next.intents.map((i) => (i.id === event.intent ? { ...i, receipt: event.receipt } : i)) };
    }
    case 'bot.speech': {
      if (event.phase === 'queued') {
        return { ...next, lines: [...next.lines, { turn: event.turn, who: 'bot', text: event.text, segment: event.segment, source: event.source, reason: event.reason }] };
      }
      return {
        ...next,
        lines: next.lines.map((l) =>
          l.segment === event.segment ? { ...l, heard: event.phase === 'delivered', cut: event.phase === 'cut', text: event.text || l.text } : l,
        ),
      };
    }
    case 'turn.finished':
      return { ...next, thinking: false, turns: [...next.turns, { turn: event.turn, step: event.step, status: event.status, requests: event.requests, detail: event.detail }] };
    case 'turn.snapshot':
      return { ...next, snapshots: next.snapshots.includes(event.turn) ? next.snapshots : [...next.snapshots, event.turn] };
    case 'run.ended':
      return { ...next, status: 'ended', outcome: event.outcome, thinking: false };
  }
  return next;
}

/** Follow one run: every event so far, then each new one, from the studio's live socket. */
export function useRun(run: string | undefined): RunView {
  const [view, dispatch] = useReducer(fold, EMPTY);
  useEffect(() => {
    dispatch({ type: 'reset' });
    if (!run) return;
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${scheme}://${window.location.host}/api/runs/${encodeURIComponent(run)}/live`);
    socket.onmessage = (message) => dispatch(JSON.parse(message.data as string) as Message);
    return () => socket.close();
  }, [run]);
  return view;
}
