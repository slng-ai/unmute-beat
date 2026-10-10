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

/** A receipt or arguments as their fields, one per row, every value wrapped inside the box. */
export function Fields({ value, tone }: { value: unknown; tone?: string }) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return <div className="mono" style={{ fontSize: 11, overflowWrap: 'anywhere', color: tone }}>{short(value)}</div>;
  const entries = Object.entries(value as Record<string, unknown>);
  if (!entries.length) return null;
  return (
    <dl className="fields">
      {entries.map(([key, v]) => (
        <div key={key} style={{ display: 'contents' }}>
          <dt>{key}</dt>
          <dd style={{ color: tone }}>{v === null || v === '' ? <span className="dim">—</span> : typeof v === 'object' ? short(v) : String(v)}</dd>
        </div>
      ))}
    </dl>
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

/** The application's state as it stands, with what the last change touched marked. */
export function StatePanel({ state, changed }: { state: Record<string, unknown>; changed: string[] }) {
  const entries = Object.entries(state);
  if (!entries.length) return <p className="empty">No state yet.</p>;
  return (
    <div className="kv">
      {entries.map(([key, value]) => (
        <div key={key} style={changed.includes(key) ? { background: '#221f14' } : undefined}>
          <span style={changed.includes(key) ? { color: 'var(--yellow)' } : undefined}>{key}</span>
          <span>{value === null ? <span className="dim">—</span> : typeof value === 'object' ? <Fields value={value} /> : String(value)}</span>
        </div>
      ))}
    </div>
  );
}

const STATUS_TAG: Record<string, string> = { succeeded: 'tag-ok', rejected: 'tag-bad', unknown: 'tag-bad', withdrawn: '', prepared: 'tag-y', dispatched: 'tag-y' };

/** Every business action: read back (prepared), sent, and how it settled, with its receipt. */
export function IntentsPanel({ intents }: { intents: Intent[] }) {
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
          {Object.keys(i.args).length > 0 && <Fields value={i.args} tone="var(--soft)" />}
          {i.receipt && (
            <>
              <span className="lbl" style={{ marginTop: 4 }}>receipt</span>
              <Fields value={i.receipt} tone="var(--ok)" />
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
