import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { GoBack as GoBackData, Refusal, StateSchema } from '../api/types';
import { useApi } from '../api/useApi';
import { Shell } from '../components/Shell';
import { href, go, type Route } from '../router';

type Draft = Record<string, string | boolean>;

function shown(value: unknown): string | boolean {
  if (typeof value === 'boolean') return value;
  if (value === null || value === undefined) return '';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

/** The edited text back into the value the state holds: JSON for objects and lists, a number for a number, '' for none. */
function parsed(text: string | boolean, before: unknown): unknown {
  if (typeof text === 'boolean') return text;
  if (text.trim() === '') return null;
  if (before !== null && typeof before === 'object') return JSON.parse(text);
  if (typeof before === 'number') return Number(text);
  return text;
}

/**
 * Going back to the end of a turn and carrying on from there, with the state edited first if you like.
 * The page says what comes along (the history, what was said and heard, the state, every receipt),
 * what starts fresh (offers, a waiting read-back: a yes never carries across) and what stays done in
 * the business after that turn (a booking made later is still in the diary). An edit the agent's own
 * types refuse is shown here, and nothing opens.
 */
export function GoBack({ route }: { route: Extract<Route, { page: 'back' }> }) {
  const run = useApi(() => api.run(route.run), `back-run-${route.run}`);
  const turns = run.state === 'ok' ? run.data.turns : [];
  const turn = route.turn ?? turns.at(-1);
  const found = useApi(() => (turn !== undefined ? api.goBack(route.run, turn) : Promise.reject(new Error('no saved turn'))), `back-${route.run}-${turn}`);
  const agent = run.state === 'ok' ? run.data.run.agent : '';
  const schema = useApi(() => (agent ? api.schema(agent) : Promise.reject(new Error('no agent'))), `schema-${agent}`);
  const data = found.state === 'ok' ? found.data : undefined;
  const [draft, setDraft] = useState<Draft>({});
  const [refused, setRefused] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (data) setDraft(Object.fromEntries(Object.entries(data.facts).map(([k, v]) => [k, shown(v)])));
    setRefused({});
    setError('');
  }, [data]);

  const carryOn = async (channel: 'text' | 'voice') => {
    if (!data || turn === undefined) return;
    setBusy(true);
    setError('');
    setRefused({});
    let facts: Record<string, unknown>;
    try {
      facts = Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, parsed(v, data.facts[k])]));
    } catch (failure) {
      setError(`A field is not valid JSON: ${String(failure)}`);
      setBusy(false);
      return;
    }
    const edited = Object.keys(facts).some((k) => JSON.stringify(facts[k]) !== JSON.stringify(data.facts[k]));
    try {
      const started = await api.start({ agent, channel, resume_from: { run: route.run, turn }, ...(edited ? { facts } : {}) });
      go({ page: 'agents', agent, tab: 'build', run: started.run });
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 422) {
        const detail = failure.detail as { refused?: Refusal[] } | undefined;
        const by = Object.fromEntries((detail?.refused ?? []).map((r) => [String(r.loc[0] ?? 'state'), r.msg]));
        setRefused(by);
        setError('The agent refused this state: nothing was opened. Fix the marked fields.');
      } else {
        setError(failure instanceof ApiError ? failure.message : String(failure));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell
      route={route}
      agent={agent}
      children={
        <>
          <span className="lbl">Go back and carry on</span>
          <a className="mono" style={{ fontSize: 13 }} href={href({ page: 'run', run: route.run })}>{route.run}</a>
          {agent && <span className="mono dim" style={{ fontSize: 12 }}>{agent}</span>}
        </>
      }
      body={
        <div className="cols">
          <nav className="col-steps" aria-label="Turns">
            <div className="side-head"><span className="lbl">Saved turns</span></div>
            {turns.length === 0 && <p className="empty" style={{ padding: 16 }}>No saved turn: a snapshot is kept at the end of each turn.</p>}
            {turns.map((t) => (
              <a key={t} href={href({ page: 'back', run: route.run, turn: t })} style={{ textDecoration: 'none', color: 'inherit' }}>
                <span className="side-item" aria-pressed={t === turn} style={{ flexDirection: 'row' }}>
                  <span className="mono">T{t}</span>
                  <span style={{ flex: '1 1 auto' }} />
                  <span className="mono dim" style={{ fontSize: 10 }}>after turn {t}</span>
                </span>
              </a>
            ))}
          </nav>
          <main className="col-main">
            {found.state === 'error' && <p className="error-box">{found.message}</p>}
            {data && (
              <>
                <header>
                  <div className="lbl">carry on from the end of turn {data.turn}</div>
                  <h1 className="mono" style={{ margin: '6px 0 0', fontSize: 26, fontWeight: 500 }}>at {data.step}{data.pending ? ` · waiting on ${data.pending}` : ''}</h1>
                </header>
                <div className="panel">
                  <div className="panel-h"><span className="lbl">What was said up to here</span></div>
                  <div className="panel-b">
                    {data.said.map((line, i) => (
                      <div key={i} className="said">
                        <span className="said-who mono" style={{ color: line.who === 'caller' ? 'var(--caller)' : 'var(--bot)' }}>{line.who === 'caller' ? `T${line.turn} caller` : 'bot'}</span>
                        <span className="said-text">{line.text}{line.who === 'bot' && <span className="said-mark mono">{line.heard ? ' ✓ heard' : ' · not heard'}</span>}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <StateForm facts={data.facts} draft={draft} setDraft={setDraft} refused={refused} schema={schema.state === 'ok' ? schema.data : undefined} />
                {error && <div className="problem">{error}</div>}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <button type="button" className="btn btn-y" disabled={busy} onClick={() => void carryOn('text')}>Carry on by text</button>
                  <button type="button" className="btn" disabled={busy} onClick={() => void carryOn('voice')}>Carry on by voice</button>
                  <span className="hint" style={{ alignSelf: 'center' }}>On the config as saved now. The new run names this one as where it came from.</span>
                </div>
              </>
            )}
          </main>
          {data && <Consequences data={data} />}
        </div>
      }
    />
  );
}

function StateForm({ facts, draft, setDraft, refused, schema }: { facts: Record<string, unknown>; draft: Draft; setDraft: (d: Draft) => void; refused: Record<string, string>; schema?: StateSchema }) {
  const props = schema?.state.properties ?? {};
  return (
    <div className="panel">
      <div className="panel-h">
        <span className="lbl">The state to carry on with</span>
        <span className="hint">checked whole by the agent's own types before anything opens</span>
      </div>
      <div className="panel-b" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 14 }}>
        {Object.keys(facts).map((key) => {
          const value = draft[key] ?? '';
          const before = facts[key];
          const about = props[key]?.description;
          return (
            <div key={key} className="field" style={typeof before === 'object' && before !== null ? { gridColumn: '1 / -1' } : undefined}>
              <label htmlFor={`fact-${key}`}>{key}</label>
              {typeof value === 'boolean' ? (
                <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <input id={`fact-${key}`} type="checkbox" checked={value} onChange={(e) => setDraft({ ...draft, [key]: e.target.checked })} />
                  <span className="mono" style={{ fontSize: 13 }}>{String(value)}</span>
                </label>
              ) : typeof before === 'object' && before !== null ? (
                <textarea id={`fact-${key}`} className={`textarea ${refused[key] ? 'input-bad' : ''}`} rows={3} value={value} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} spellCheck={false} />
              ) : (
                <input id={`fact-${key}`} className={`input mono ${refused[key] ? 'input-bad' : ''}`} value={value} placeholder="none" onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} />
              )}
              {refused[key] && <div className="problem">{refused[key]}</div>}
              {about && <span className="hint">{about}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Consequences({ data }: { data: GoBackData }) {
  return (
    <aside className="col-try" aria-label="What carrying on means">
      <div className="panel-h"><span className="lbl">What carrying on means</span></div>
      <div className="panel-b">
        <div className="field">
          <span className="lbl" style={{ color: 'var(--ok)' }}>Comes along</span>
          <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--soft)', fontSize: 14, lineHeight: 1.6 }}>
            <li>The history the model sees, and what was said and heard</li>
            <li>The state above, with your edits</li>
            <li>The call's clock: "Thursday" means the same day</li>
            <li>{data.receipts.length} receipt(s), and every action that was settled</li>
          </ul>
        </div>
        <div className="field">
          <span className="lbl" style={{ color: 'var(--yellow)' }}>Starts fresh</span>
          <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--soft)', fontSize: 14, lineHeight: 1.6 }}>
            <li>Offers: the step offers again for the state as it is now</li>
            <li>A waiting read-back: asked again; a yes never carries across</li>
            <li>An action only read back, never sent: dropped, so its step asks again</li>
          </ul>
        </div>
        <div className="field">
          <span className="lbl" style={{ color: 'var(--bad)' }}>Done after T{data.turn}, and still done</span>
          {data.done_after.length === 0 ? (
            <span className="hint">Nothing: no business change was made after this turn.</span>
          ) : (
            data.done_after.map((d, i) => (
              <div key={i} className="warn">T{d.turn} · {d.operation} · {JSON.stringify(d.receipt)}</div>
            ))
          )}
          <span className="hint">Going back does not undo the business. The replay may meet these.</span>
        </div>
      </div>
    </aside>
  );
}
