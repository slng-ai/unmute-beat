import { ModelView, type Schema } from '../model';
import type { Intent, Line, Request, RunView } from './useRun';

const TALK = { caller: 'var(--caller)', bot: 'var(--bot)' };

/** What the caller and the bot said, turn by turn, and whether each bot line was heard (a playback report or a read acknowledgement). */
export function Transcript({ lines, thinking }: { lines: Line[]; thinking?: boolean }) {
  if (!lines.length && !thinking) return <p className="empty">Nothing said yet.</p>;
  return (
    <div className="transcript" aria-live="polite">
      {lines.map((line, i) => (
        <div key={`${line.segment ?? 'c'}-${i}`} className="said">
          <span className="said-who mono" style={{ color: TALK[line.who] }}>
            {line.who === 'caller' ? `T${line.turn} caller` : 'bot'}
          </span>
          <span className="said-text" style={{ color: line.cut ? 'var(--dim)' : undefined }}>
            {line.text}
            {line.who === 'bot' && (
              <span className="said-mark mono" title={line.heard ? 'heard: the page reported it played in full, or the reader got it' : line.cut ? 'cut: the caller talked over it' : 'not heard yet'}>
                {line.heard ? ' ✓ heard' : line.cut ? ' ✂ cut' : ' · sent'}
                {line.source === 'code' ? ' · code' : ''}
              </span>
            )}
          </span>
        </div>
      ))}
      {thinking && (
        <div className="said">
          <span className="said-who mono" style={{ color: 'var(--yellow)' }}>engine</span>
          <span className="said-text pulse" style={{ color: 'var(--yellow)' }}>thinking…</span>
        </div>
      )}
    </div>
  );
}

function short(value: unknown): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return text.length > 220 ? `${text.slice(0, 220)}…` : text;
}

/** Each model request: its step, what it wrote as it streamed (its arguments), what it decided, and what it cost. */
export function ModelPanel({ requests, last = 6 }: { requests: Request[]; last?: number }) {
  if (!requests.length) return <p className="empty">No model request yet. Conversation steps ask the model; action steps never do.</p>;
  return (
    <div className="requests">
      {requests.slice(-last).map((r, i) => (
        <div key={`${r.turn}-${r.request}-${i}`} className="request">
          <div className="request-h mono">
            <span>T{r.turn} · {r.step} · request {r.request}{r.budget ? ` of ${r.budget}` : ''}</span>
            <span style={{ flex: '1 1 auto' }} />
            {r.ended ? (
              <span className="dim">
                {r.ended.first_ms !== null ? `first ${Math.round(r.ended.first_ms)} ms · ` : ''}
                {Math.round(r.ended.ms)} ms · {r.ended.tokens_in}→{r.ended.tokens_out} tok
              </span>
            ) : (
              <span className="pulse" style={{ color: 'var(--yellow)' }}>streaming</span>
            )}
          </div>
          {!(r.ended && r.decisions.length) && Object.entries(r.args).map(([name, text]) => (
            <div key={name} className="request-args mono">
              <span style={{ color: 'var(--yellow)' }}>{name}</span> {text}
            </div>
          ))}
          {r.text && <div className="request-args mono">{r.text}</div>}
          {r.decisions.map((d, j) => (
            <div key={j} className="decision mono" style={{ color: d.refused ? 'var(--bad)' : 'var(--ok)' }}>
              {d.refused ? '✕ sent back' : '→'} {d.name}
              {d.refused ? <span className="dim"> · {short(d.refused)}</span> : Object.keys(d.args).length ? <span className="dim"> {short(d.args)}</span> : null}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/** The application's state as it stands, drawn from its model's schema, with what the last change touched marked. */
export function StatePanel({ state, changed, schema }: { state: Record<string, unknown>; changed: string[]; schema?: Schema }) {
  if (!Object.keys(state).length) return <p className="empty">No state yet.</p>;
  return <ModelView value={state} schema={schema} changed={changed} />;
}

const STATUS_TAG: Record<string, string> = { succeeded: 'tag-ok', rejected: 'tag-bad', unknown: 'tag-bad', withdrawn: '', prepared: 'tag-y', dispatched: 'tag-y' };

/** Every business action: read back (prepared), sent, and how it settled, with its receipt. */
export function IntentsPanel({ intents, receipts = {} }: { intents: Intent[]; receipts?: Record<string, Schema> }) {
  if (!intents.length) return <p className="empty">No business action yet.</p>;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {intents.map((i) => (
        <div key={i.id} className="intent">
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span className="mono" style={{ fontSize: 13 }}>{i.operation}</span>
            <span className={`tag ${STATUS_TAG[i.status] ?? ''}`}>{i.status}</span>
            <span style={{ flex: '1 1 auto' }} />
            <span className="mono dim" style={{ fontSize: 10 }}>T{i.turn}</span>
          </div>
          {Object.keys(i.args).length > 0 && <ModelView value={i.args} tone="var(--soft)" />}
          {i.receipt && (
            <>
              <span className="lbl" style={{ marginTop: 4 }}>receipt</span>
              <ModelView value={i.receipt} schema={receipts[i.operation]} tone="var(--ok)" />
            </>
          )}
        </div>
      ))}
    </div>
  );
}

export function statusTag(view: RunView): { label: string; tone: string } {
  if (view.status === 'live') return { label: view.thinking ? 'thinking' : 'live', tone: 'tag-y' };
  if (view.status === 'ended') return { label: view.outcome || 'ended', tone: view.outcome === 'failed' ? 'tag-bad' : 'tag-ok' };
  if (view.status === 'missing') return { label: 'not found', tone: 'tag-bad' };
  return { label: 'connecting', tone: '' };
}
