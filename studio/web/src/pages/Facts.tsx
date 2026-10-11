import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, ApiError } from '../api/client';
import type { Fact, FactChecked, FactEdit, FactKind, FactLimits, FactsView, FactUse, FactUseKind, NewKind } from '../api/types';
import { useApi } from '../api/useApi';
import { ModelView } from '../model';
import { go, href } from '../router';

const GROUPS: { id: Fact['group']; title: string; about: string }[] = [
  { id: 'caller', title: 'Filled in by the caller', about: 'Steps record these from what the caller says.' },
  { id: 'tools', title: 'Set by tools', about: "A tool's result sets these; no step can." },
  { id: 'open', title: 'Set when the call opens', about: 'No step or tool sets these: the application fills them in when a call starts, or they stay empty.' },
];

/** How each kind of use reads on a fact, in the order the page lists them. */
const USES: { kind: FactUseKind; label: string; note?: string }[] = [
  { kind: 'fills', label: 'Filled in by', note: 'the model may record it on these steps' },
  { kind: 'asks', label: 'Asked for by' },
  { kind: 'identity', label: 'Who the caller is', note: 'a change of it makes every offer start over' },
  { kind: 'set_by', label: 'Set by' },
  { kind: 'needed', label: 'Needed before', note: 'these steps wait until it is filled in' },
  { kind: 'input', label: 'Input to' },
  { kind: 'offered', label: 'Offered by' },
  { kind: 'rests_on', label: 'Choices that rest on it', note: 'a change to it takes that offer away' },
  { kind: 'read_back', label: 'Read back by' },
  { kind: 'carried_out', label: 'Carried out on' },
  { kind: 'sees', label: 'Seen by', note: 'these steps are shown its value' },
  { kind: 'prompt', label: 'Named in a prompt' },
  { kind: 'code', label: 'Named in code', note: "the studio can't prove this code doesn't read it" },
];

const KINDS: { kind: NewKind; label: string; eg: string }[] = [
  { kind: 'text', label: 'Text', eg: 'a name, a note' },
  { kind: 'whole_number', label: 'Whole number', eg: '2 guests' },
  { kind: 'number', label: 'Number', eg: '4.5' },
  { kind: 'yes_no', label: 'Yes/No', eg: 'wants a refund' },
  { kind: 'date', label: 'Date', eg: '2026-10-14' },
  { kind: 'not_past', label: 'A date not in the past', eg: 'a booking day' },
  { kind: 'time', label: 'Time', eg: '14:30' },
  { kind: 'choice', label: 'One of a list', eg: 'short, medium, long' },
  { kind: 'phone', label: 'Phone number', eg: '+34 612 345 678' },
  { kind: 'quoted', label: "The caller's own words", eg: 'a complaint' },
];

/** A fact name in plain words, as the engine's report does: `hair_length` is "Hair length". */
function label(name: string): string {
  const spaced = name.replace(/_/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** A use's place, in a few words: the step, the operation, or the file and line. */
function place(use: FactUse, fact = ''): string {
  switch (use.kind) {
    case 'offered':
      return `${use.function.split(':').pop()} in ${use.step}`;
    case 'rests_on':
      return `${use.choice} in ${use.step}`;
    case 'read_back':
    case 'carried_out':
      return `${use.operation} in ${use.step}`;
    case 'input':
      return use.argument && use.argument !== fact ? `${use.operation} (as ${use.argument})` : use.operation;
    case 'set_by':
      return use.operation;
    case 'prompt':
    case 'code':
      return `${use.file}:${use.line}`;
    case 'identity':
      return '';
    default:
      return use.step;
  }
}

/** One line under a card: what fills it in and what it feeds, the uses a person looks for first. */
function summary(fact: Fact): string {
  const of = (kind: FactUseKind) => [...new Set(fact.uses.filter((u) => u.kind === kind).map((u) => place(u, fact.name)))];
  const parts: string[] = [];
  const fills = of('fills');
  const set = of('set_by');
  const input = of('input');
  const rests = of('rests_on');
  if (fills.length) parts.push(fills.length > 2 ? `filled in by ${fills.length} steps` : `filled in by ${fills.join(', ')}`);
  if (set.length) parts.push(`set by ${set.join(', ')}`);
  if (input.length) parts.push(input.length > 2 ? `input to ${input.length} tools` : `input to ${input.join(', ')}`);
  if (rests.length) parts.push(`${rests.length} ${rests.length === 1 ? 'choice rests' : 'choices rest'} on it`);
  const prompts = [...new Set(fact.uses.filter((u) => u.kind === 'prompt').map((u) => u.file))];
  if (prompts.length) parts.push(`named in ${prompts.length} ${prompts.length === 1 ? 'prompt' : 'prompts'}`);
  return parts.join(' · ') || 'not used by any step, tool or prompt';
}

/** A value as the page shows a scalar: empty as a word, text as it is. */
function shown(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'empty';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

/** What a fact accepts, beyond its kind: its limits and the kind's own checks, in words. */
function accepts(fact: Fact): string[] {
  const words: string[] = [];
  const l = fact.limits;
  if (l.min_length !== undefined) words.push(`at least ${l.min_length} characters`);
  if (l.max_length !== undefined) words.push(`at most ${l.max_length} characters`);
  if (l.ge !== undefined) words.push(`at least ${l.ge}`);
  if (l.gt !== undefined) words.push(`more than ${l.gt}`);
  if (l.le !== undefined) words.push(`at most ${l.le}`);
  if (l.lt !== undefined) words.push(`less than ${l.lt}`);
  const codes = [fact.kind.code, ...fact.kind.members.map((m) => m.code)];
  if (codes.includes('not_past')) words.push("never before the call's own today");
  if (codes.includes('quoted')) words.push('only words the caller said on this call');
  if (codes.includes('phone')) words.push('a real number with its country code, checked by libphonenumber');
  return words;
}

/**
 * The Facts tab: what an agent keeps track of on a call, each fact as a card, and one fact's detail, uses and
 * the changes the studio can prove. A change is checked on a copy first (the module still reads as Python and
 * changed only there, the agent still loads, the facts' schema changed by exactly that) and only then saved.
 */
export function FactsTab({ agent, picked, onSaved }: { agent: string; picked?: string; onSaved: () => void }) {
  const [reload, setReload] = useState(0);
  const loaded = useApi(() => api.facts(agent), `facts-${agent}-${reload}`);
  const [adding, setAdding] = useState(false);
  const [find, setFind] = useState('');
  const [result, setResult] = useState<{ name: string; checked: FactChecked; message: string } | null>(null);
  const view = loaded.state === 'ok' ? loaded.data : undefined;
  const fact = view?.facts.find((f) => f.name === picked) ?? (adding ? undefined : view?.facts[0]);

  useEffect(() => {
    if (!view?.runners) return;
    const timer = window.setTimeout(() => setReload((n) => n + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [view?.runners]);

  const pick = (name?: string) => {
    setAdding(false);
    go({ page: 'agents', agent, tab: 'facts', step: name });
  };
  const saved = (name: string, checked: FactChecked, message: string) => {
    setResult({ name, checked, message });
    if (checked.saved) {
      setReload((n) => n + 1);
      onSaved();
    }
  };

  if (loaded.state === 'loading' && !view) return <p className="empty" style={{ padding: 24 }}>Reading the facts…</p>;
  if (loaded.state === 'error') return <p className="error-box" style={{ margin: 24 }}>{loaded.message}</p>;
  if (!view) return null;
  if (view.problems.length) {
    return (
      <main className="col-main" style={{ maxWidth: 820 }}>
        <h1 className="facts-title">The facts can't be read right now</h1>
        <p className="facts-lead">The agent doesn't load, so the studio can't tell what it keeps track of. Fix what is listed below first, on Full config or in code; this page reads it again when you come back.</p>
        {view.problems.map((p, i) => <div key={i} className="problem">{p.what}</div>)}
      </main>
    );
  }
  const needle = find.trim().toLowerCase();
  const matching = view.facts.filter((f) => !needle || `${f.name} ${f.label} ${f.description}`.toLowerCase().includes(needle));
  const narrowOpen = Boolean(picked) || adding;

  return (
    <div className="facts" data-open={narrowOpen}>
      <aside className="facts-list" aria-label="Facts">
        <div className="facts-head">
          <h1 className="facts-title">Facts</h1>
          <p className="facts-lead">What the agent keeps track of on a call. Every call starts with them empty; steps fill them in from what the caller says, and tools set them from what the business answers.</p>
          <span className="hint">defined in {view.file} · {view.class_name} · {view.facts.length} facts</span>
        </div>
        {view.runners && <div className="warn">{view.runners}</div>}
        {view.rules.length > 0 && (
          <div className="facts-rule">
            <span className="lbl">All the facts together have a rule in code</span>
            {view.rules.map((r) => <span key={r.name} className="hint">{r.name} · {r.where}</span>)}
          </div>
        )}
        <button type="button" className="btn btn-y" style={{ justifyContent: 'center' }} onClick={() => { setAdding(true); setResult(null); }}>+ Add a fact</button>
        <div className="field">
          <label htmlFor="fact-find">Find a fact</label>
          <input id="fact-find" className="input" type="search" placeholder="a name, or a word in its meaning" value={find} onChange={(e) => setFind(e.target.value)} />
        </div>
        {GROUPS.map((g) => {
          const own = matching.filter((f) => f.group === g.id);
          return (
            <section key={g.id} className="facts-group" aria-label={g.title}>
              <div className="facts-group-h">
                <h2 className="lbl">{g.title}</h2>
                <span className="mono dim">{own.length}</span>
              </div>
              <span className="hint">{g.about}</span>
              {own.length === 0 && <span className="facts-none">{needle ? 'None match.' : 'None in this agent.'}</span>}
              {own.map((f) => (
                <a key={f.name} className={`fact-card${fact?.name === f.name && !adding ? ' fact-card-on' : ''}`} href={href({ page: 'agents', agent, tab: 'facts', step: f.name })} aria-current={fact?.name === f.name && !adding ? 'true' : undefined} onClick={() => setAdding(false)}>
                  <span className="fact-card-top">
                    <span className="fact-card-name">{f.label}</span>
                    <span className="mono dim">{f.name}</span>
                  </span>
                  <span className="fact-kind">{f.kind.words}</span>
                  {f.description ? <span className="fact-card-about">{f.description}</span> : <span className="fact-card-about dim">No meaning written for the agent.</span>}
                  <span className="mono dim fact-card-uses">{summary(f)}</span>
                </a>
              ))}
            </section>
          );
        })}
      </aside>
      <main className="facts-main">
        <button type="button" className="facts-back mono" onClick={() => pick(undefined)}>← All facts</button>
        {adding ? (
          <AddFact view={view} agent={agent} onCancel={() => setAdding(false)} onDone={(name, checked, message) => { saved(name, checked, message); if (checked.saved) pick(name); }} />
        ) : fact ? (
          <FactDetail key={`${fact.name}-${view.version}`} fact={fact} view={view} agent={agent} result={result?.name === fact.name ? result : null} onStart={() => setResult(null)} onResult={saved} />
        ) : (
          <div className="facts-empty">
            <h2>No facts yet</h2>
            <p className="facts-lead">This agent keeps track of nothing between turns: every step works only from what the caller just said. Add a fact for anything a later step, a tool or a closing line needs to know.</p>
            <button type="button" className="btn btn-y" onClick={() => setAdding(true)}>+ Add the first fact</button>
          </div>
        )}
      </main>
    </div>
  );
}

/** A kind in words, with its choices, and what is inside a group or each item of a list. */
function KindBlock({ kind, editing = false }: { kind: FactKind; editing?: boolean }) {
  return (
    <div className="fact-kind-block">
      <span>{kind.words}</span>
      {kind.code === 'choice' && kind.choices.length > 0 && !editing && (
        <div className="chips">{kind.choices.map((c) => <span key={String(c)} className="chip">{String(c)}</span>)}</div>
      )}
      {kind.code === 'group' && kind.fields.length > 0 && (
        <dl className="fact-nested">
          {kind.fields.map((f) => (
            <div key={f.name}>
              <dt>{f.label} <span className="mono dim">{f.name}</span></dt>
              <dd>{f.kind.words}{f.kind.choices.length > 0 && `: ${f.kind.choices.join(', ')}`}{f.description && <span className="hint"> · {f.description}</span>}</dd>
            </div>
          ))}
        </dl>
      )}
      {kind.code === 'list' && kind.item?.code === 'group' && <KindBlock kind={kind.item} />}
      {kind.defined_in && <span className="hint">defined in {kind.defined_in}{kind.model ? ` as ${kind.model}` : ''}</span>}
    </div>
  );
}

/** Every use of a fact, by kind, each place a link to where it is when the studio can show it. */
function UsesTable({ fact, agent, latest, latestRun }: { fact: Fact; agent: string; latest: ReactNode; latestRun: string }) {
  const rows = USES.map((u) => ({ ...u, found: fact.uses.filter((use) => use.kind === u.kind) })).filter((u) => u.found.length);
  return (
    <section className="panel" aria-label="Where it is used">
      <div className="panel-h">
        <span className="lbl" style={{ color: 'var(--text)' }}>Where it is used</span>
        <span style={{ flex: '1 1 auto' }} />
        <span className="hint">from the engine's own reading of agent.yaml and the code</span>
      </div>
      {rows.length === 0 && <div className="fact-use"><span className="hint">Not used by any step, tool or prompt yet.</span></div>}
      {rows.map((u) => (
        <div key={u.kind} className="fact-use">
          <span className="mono fact-use-k">{u.label}</span>
          <span className="fact-use-v">
            {[...new Map(u.found.map((use) => [place(use, fact.name), use])).values()].map((use) => {
              const text = place(use, fact.name);
              if (!text) return null;
              return use.step && ['fills', 'sees', 'asks', 'needed'].includes(use.kind) ? (
                <a key={text} className="chip" href={href({ page: 'agents', agent, tab: 'build', step: use.step })}>{text}</a>
              ) : (
                <span key={text} className="chip">{text}</span>
              );
            })}
            {u.kind === 'asks' && u.found[0]?.detail && <span className="hint">"{u.found[0].detail}"</span>}
            {u.kind === 'needed' && u.found[0]?.detail && <span className="hint">{u.found[0].detail} fills it in first</span>}
            {u.note && <span className="hint">{u.note}</span>}
          </span>
        </div>
      ))}
      <div className="fact-use">
        <span className="mono fact-use-k">In the latest run</span>
        <span className="fact-use-v">
          {latestRun ? <>{latest}<a className="mono" style={{ fontSize: 12 }} href={href({ page: 'run', run: latestRun })}>{latestRun}</a></> : <span className="hint">no run of this agent yet</span>}
        </span>
      </div>
    </section>
  );
}

/** What checking or saving found: each thing proved, each problem, and the change itself as a diff. */
function Proof({ result }: { result: { checked: FactChecked; message: string } }) {
  const { checked, message } = result;
  return (
    <section className="fact-proof" aria-label="What the check found" aria-live="polite">
      <div className={checked.ok ? 'good' : 'problem'}>{message}</div>
      {checked.proved.map((p) => <div key={p} className="good">✓ {p}</div>)}
      {checked.problems.map((p, i) => <div key={i} className={p.level === 'warning' ? 'warn' : 'problem'}>{p.where}: {p.what}</div>)}
      {checked.runners && <div className="warn">{checked.runners}</div>}
      {checked.diff && (
        <pre className="fact-diff">
          {checked.diff.split('\n').map((line, i) => (
            <span key={i} className={line.startsWith('+') && !line.startsWith('+++') ? 'diff-add' : line.startsWith('-') && !line.startsWith('---') ? 'diff-del' : line.startsWith('@@') ? 'dim' : ''}>{line}{'\n'}</span>
          ))}
        </pre>
      )}
    </section>
  );
}

/** Send an edit to be checked or saved, and say what came of it. */
async function submit(agent: string, edit: FactEdit, save: boolean): Promise<{ checked: FactChecked; message: string }> {
  try {
    const checked = save ? await api.saveFact(agent, edit) : await api.checkFact(agent, edit);
    const message = checked.saved
      ? `Saved as config ${checked.version}. The next run uses it.`
      : checked.ok
        ? `The check passes. Saving makes config ${checked.version}.`
        : 'Not saved: fix what is listed first.';
    return { checked, message };
  } catch (failure) {
    const message = failure instanceof ApiError ? failure.message : String(failure);
    return { checked: { ok: false, version: '', problems: [], proved: [], diff: '', changed: [] }, message };
  }
}

type Draft = { description?: string; choices?: string[]; limits?: FactLimits; may_be_empty?: boolean; start?: unknown; fill_steps?: string[] };

/** One fact: what it is, what it accepts, its rules, where it is used, and the changes the studio can prove. */
function FactDetail({ fact, view, agent, result, onStart, onResult }: { fact: Fact; view: FactsView; agent: string; result: { checked: FactChecked; message: string } | null; onStart: () => void; onResult: (name: string, checked: FactChecked, message: string) => void }) {
  const [draft, setDraft] = useState<Draft>({});
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [choice, setChoice] = useState('');
  const fills = useMemo(() => [...new Set(fact.uses.filter((u) => u.kind === 'fills').map((u) => u.step))], [fact]);
  const dirty = Object.keys(draft).length > 0;
  const group = GROUPS.find((g) => g.id === fact.group)?.title ?? '';
  const can = fact.editable;
  const choices = draft.choices ?? fact.kind.choices.map(String);
  const steps = draft.fill_steps ?? fills;
  const empty = draft.may_be_empty ?? fact.starts_empty;
  const plainKind = fact.kind.code;
  const limits = { ...fact.limits, ...draft.limits } as FactLimits;
  const isText = plainKind === 'text';

  const run = async (save: boolean, edit: FactEdit = { action: 'change', name: fact.name, ...draft }) => {
    setBusy(true);
    onStart();
    const done = await submit(agent, { ...edit, base: view.version }, save);
    setBusy(false);
    onResult(fact.name, done.checked, done.message);
    if (done.checked.saved) setDraft({});
  };
  const addChoice = () => {
    const value = choice.trim();
    if (value && !choices.includes(value)) setDraft({ ...draft, choices: [...choices, value] });
    setChoice('');
  };
  const latest = fact.latest !== null && typeof fact.latest === 'object' ? <ModelView value={fact.latest} /> : <span className="mono" style={{ color: fact.latest == null ? 'var(--dim)' : 'var(--ok)' }}>{shown(fact.latest)}</span>;

  return (
    <div className="fact-detail">
      <header className="fact-detail-h">
        <div style={{ flex: '1 1 320px', minWidth: 0 }}>
          <div className="lbl">{group}</div>
          <h2 className="fact-detail-name">{fact.label}</h2>
          <div className="mono dim" style={{ fontSize: 13, marginTop: 4 }}>{fact.name}{fact.line ? ` · ${view.file} line ${fact.line}` : ''}</div>
        </div>
        <span className="tag tag-y">{fact.kind.words}</span>
      </header>

      <section className="field">
        <label htmlFor="fact-meaning">What the agent is told it means</label>
        {can.description ? (
          <textarea id="fact-meaning" className="textarea fact-prose" rows={3} value={draft.description ?? fact.description} placeholder="Nothing yet: the agent only has the name to go on." onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
        ) : (
          <div className="facts-ro">{fact.description || 'Nothing written.'} <br />{fact.why_not.description}</div>
        )}
        <div className="warn">The model reads this to decide what to record. Changing it changes how the agent behaves, like a prompt: try a real run after saving.</div>
        {fact.note && <span className="hint">Note in code, for developers (the model never reads it): {fact.note}</span>}
      </section>

      <section className="fact-facts">
        <div className="field">
          <span className="lbl">Kind</span>
          <KindBlock kind={fact.kind} editing={Boolean(can.choices)} />
          {can.choices ? (
            <div className="fact-edit">
              <span className="lbl">Choices</span>
              <div className="chips">
                {choices.map((c) => (
                  <span key={c} className="chip chip-edit">{c}<button type="button" className="chip-x" aria-label={`Remove ${c}`} onClick={() => setDraft({ ...draft, choices: choices.filter((x) => x !== c) })}>×</button></span>
                ))}
              </div>
              <div className="fact-inline">
                <label className="sr" htmlFor="fact-choice">Add a choice</label>
                <input id="fact-choice" className="input mono" placeholder="add a choice" value={choice} onChange={(e) => setChoice(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addChoice(); } }} />
                <button type="button" className="btn btn-q" onClick={addChoice}>Add</button>
              </div>
            </div>
          ) : fact.kind.choices.length > 0 && fact.why_not.choices ? (
            <span className="facts-ro">{fact.why_not.choices}</span>
          ) : null}
        </div>
        <div className="field">
          <span className="lbl">When the call opens</span>
          <span>{fact.starts_empty ? 'Starts empty' : `Starts as ${shown(fact.start)}`}</span>
          {can.empty && (
            <div className="fact-edit">
              <label className="check"><input type="checkbox" checked={empty} onChange={(e) => setDraft({ ...draft, may_be_empty: e.target.checked, start: e.target.checked ? undefined : draft.start ?? (plainKind === 'yes_no' ? false : plainKind === 'choice' ? choices[0] : plainKind === 'text' ? '' : 0) })} /> Can be empty (starts empty, and can be cleared)</label>
              {!empty && (
                <div className="fact-inline">
                  <label htmlFor="fact-start" className="lbl">Starts as</label>
                  {plainKind === 'yes_no' ? (
                    <select id="fact-start" className="select" value={String(draft.start ?? fact.start ?? false)} onChange={(e) => setDraft({ ...draft, may_be_empty: false, start: e.target.value === 'true' })}><option value="true">Yes</option><option value="false">No</option></select>
                  ) : plainKind === 'choice' ? (
                    <select id="fact-start" className="select" value={String(draft.start ?? fact.start ?? choices[0])} onChange={(e) => setDraft({ ...draft, may_be_empty: false, start: e.target.value })}>{choices.map((c) => <option key={c} value={c}>{c}</option>)}</select>
                  ) : (
                    <input id="fact-start" className="input" type={isText ? 'text' : 'number'} value={String(draft.start ?? fact.start ?? '')} onChange={(e) => setDraft({ ...draft, may_be_empty: false, start: isText ? e.target.value : Number(e.target.value) })} />
                  )}
                </div>
              )}
            </div>
          )}
          <span className="lbl" style={{ marginTop: 8 }}>What it accepts</span>
          <span>{accepts(fact).join('; ') || 'Anything of its kind'}</span>
          {can.limits && (
            <div className="fact-inline">
              {(isText ? (['min_length', 'max_length'] as const) : (['ge', 'le'] as const)).map((key) => (
                <div key={key} className="field" style={{ flex: '1 1 120px' }}>
                  <label htmlFor={`fact-${key}`}>{key === 'min_length' ? 'At least (characters)' : key === 'max_length' ? 'At most (characters)' : key === 'ge' ? 'At least' : 'At most'}</label>
                  <input id={`fact-${key}`} className="input" type="number" value={limits[key] ?? ''} onChange={(e) => setDraft({ ...draft, limits: { ...draft.limits, [key]: e.target.value === '' ? null : Number(e.target.value) } })} />
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      {fact.rules.length > 0 && (
        <section className="field">
          <span className="lbl">Has a rule in code</span>
          {fact.rules.map((r) => (
            <div key={r.name} className="facts-ro fact-rule">
              <span style={{ color: 'var(--text)' }}>{r.name}</span>
              {r.says.map((s) => <span key={s}>Its error says: "{s}"</span>)}
              {r.heard.map((h) => <span key={h}>The caller hears: "{h}"</span>)}
              <span>Edit in code: {r.where}</span>
            </div>
          ))}
        </section>
      )}

      <section className="field">
        <span className="lbl">Steps that fill it in</span>
        <div className="chips">
          {steps.length === 0 && <span className="hint">none: no step's model records it</span>}
          {steps.map((s) => (
            <span key={s} className="chip chip-edit">{s}<button type="button" className="chip-x" aria-label={`Stop ${s} filling it in`} onClick={() => setDraft({ ...draft, fill_steps: steps.filter((x) => x !== s) })}>×</button></span>
          ))}
          {view.steps.filter((s) => !steps.includes(s)).length > 0 && (
            <select className="select fact-add-step" aria-label="Add a step that fills it in" value="" onChange={(e) => e.target.value && setDraft({ ...draft, fill_steps: [...steps, e.target.value] })}>
              <option value="">+ a step</option>
              {view.steps.filter((s) => !steps.includes(s)).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          )}
        </div>
        <span className="hint">A step that fills it in also sees it; taking a step away stops both. Written to agent.yaml line by line, checked like every other edit.</span>
      </section>

      <UsesTable fact={fact} agent={agent} latest={latest} latestRun={view.latest_run} />

      <section className="field">
        <span className="lbl">Remove or rename</span>
        {can.remove ? (
          removing ? (
            <div className="fact-inline">
              <span className="warn" style={{ flex: '1 1 260px' }}>Remove {fact.name} from {view.file}? Runs saved with it can no longer go back.</span>
              <button type="button" className="btn btn-q" onClick={() => setRemoving(false)}>Keep it</button>
              <button type="button" className="btn" disabled={busy} onClick={() => void run(true, { action: 'remove', name: fact.name })}>Remove it</button>
            </div>
          ) : (
            <button type="button" className="btn btn-q" style={{ alignSelf: 'flex-start' }} onClick={() => setRemoving(true)}>Remove this fact</button>
          )
        ) : (
          <span className="facts-ro">It can't be removed while it is used, as listed above. Take it out of each use first.</span>
        )}
        <span className="facts-ro">Renaming isn't done here: runs saved under the old name could no longer go back, and code that reads it by name can't all be found. Add a new fact, move its uses, then remove this one.</span>
      </section>

      <div className="fact-actions">
        {dirty && <span className="tag tag-y">unsaved</span>}
        <button type="button" className="btn btn-q" disabled={!dirty || busy} onClick={() => setDraft({})}>Discard</button>
        <button type="button" className="btn btn-q" disabled={!dirty || busy} onClick={() => void run(false)}>Check</button>
        <button type="button" className="btn btn-y" disabled={!dirty || busy} onClick={() => void run(true)}>{busy ? 'Checking…' : 'Save'}</button>
        <span className="hint">each change is proved on a copy before anything is written</span>
      </div>
      {result && <Proof result={result} />}
    </div>
  );
}

/** A new fact: its name, its kind from a small palette, what it means, and the steps that fill it in. */
function AddFact({ view, agent, onCancel, onDone }: { view: FactsView; agent: string; onCancel: () => void; onDone: (name: string, checked: FactChecked, message: string) => void }) {
  const [name, setName] = useState('');
  const [kind, setKind] = useState<NewKind>('text');
  const [choices, setChoices] = useState<string[]>([]);
  const [choice, setChoice] = useState('');
  const [description, setDescription] = useState('');
  const [steps, setSteps] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ checked: FactChecked; message: string } | null>(null);
  const taken = view.facts.some((f) => f.name === name);
  const badName = name !== '' && !/^[a-z][a-z0-9_]*$/.test(name);
  const ready = name !== '' && !badName && !taken && (kind !== 'choice' || choices.length > 0);
  const addChoice = () => {
    const value = choice.trim();
    if (value && !choices.includes(value)) setChoices([...choices, value]);
    setChoice('');
  };
  const send = async (save: boolean) => {
    setBusy(true);
    setResult(null);
    const edit: FactEdit = { base: view.version, action: 'add', name, kind, description, fill_steps: steps, ...(kind === 'choice' ? { choices } : {}) };
    const done = await submit(agent, edit, save);
    setBusy(false);
    setResult(done);
    if (done.checked.saved) onDone(name, done.checked, done.message);
  };
  return (
    <div className="fact-detail">
      <header>
        <div className="lbl">New fact</div>
        <h2 className="fact-detail-name">Add a fact</h2>
        <p className="facts-lead">It starts empty on every call. Pick the steps that can fill it in, and the agent is told about it there.</p>
      </header>
      <div className="field">
        <label htmlFor="new-name">Name in code</label>
        <input id="new-name" className={`input mono${badName || taken ? ' input-bad' : ''}`} value={name} placeholder="hair_length" autoComplete="off" spellCheck={false} onChange={(e) => setName(e.target.value)} />
        <span className={badName || taken ? 'problem' : 'hint'}>{taken ? `${name} is taken` : badName ? 'lower case letters, digits and underscores, starting with a letter' : name ? `shown as "${label(name)}"` : 'lower case and underscores, like hair_length'}</span>
      </div>
      <fieldset className="fact-kinds">
        <legend className="lbl">Kind</legend>
        {KINDS.map((k) => (
          <button key={k.kind} type="button" className={`fact-kind-pick${kind === k.kind ? ' on' : ''}`} aria-pressed={kind === k.kind} onClick={() => setKind(k.kind)}>
            <span className="fact-kind-pick-l">{k.label}</span>
            <span className="mono dim">{k.eg}</span>
          </button>
        ))}
      </fieldset>
      {kind === 'choice' && (
        <div className="field">
          <span className="lbl">Choices</span>
          <div className="chips">
            {choices.map((c) => (
              <span key={c} className="chip chip-edit">{c}<button type="button" className="chip-x" aria-label={`Remove ${c}`} onClick={() => setChoices(choices.filter((x) => x !== c))}>×</button></span>
            ))}
            {choices.length === 0 && <span className="hint">none yet: add at least one</span>}
          </div>
          <div className="fact-inline">
            <label className="sr" htmlFor="new-choice">Add a choice</label>
            <input id="new-choice" className="input mono" placeholder="add a choice, then Enter" value={choice} onChange={(e) => setChoice(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addChoice(); } }} />
            <button type="button" className="btn btn-q" onClick={addChoice}>Add</button>
          </div>
        </div>
      )}
      <div className="field">
        <label htmlFor="new-meaning">What the agent is told it means</label>
        <textarea id="new-meaning" className="textarea fact-prose" rows={3} value={description} placeholder="How long the caller's hair is, as they describe it: short, medium or long." onChange={(e) => setDescription(e.target.value)} />
        <span className="hint">The model reads this to decide what to record. Without it, the agent only has the name to go on.</span>
      </div>
      <fieldset className="fact-steps">
        <legend className="lbl">Steps that can fill it in</legend>
        {view.steps.map((s) => (
          <label key={s} className="check"><input type="checkbox" checked={steps.includes(s)} onChange={(e) => setSteps(e.target.checked ? [...steps, s] : steps.filter((x) => x !== s))} /> {s}</label>
        ))}
        <span className="hint">A step that fills it in also sees it. None: it stays empty until code sets it.</span>
      </fieldset>
      <div className="fact-actions">
        <button type="button" className="btn btn-q" onClick={onCancel}>Cancel</button>
        <button type="button" className="btn btn-q" disabled={!ready || busy} onClick={() => void send(false)}>Check</button>
        <button type="button" className="btn btn-y" disabled={!ready || busy} onClick={() => void send(true)}>{busy ? 'Checking…' : 'Add the fact'}</button>
      </div>
      {result && <Proof result={result} />}
    </div>
  );
}
