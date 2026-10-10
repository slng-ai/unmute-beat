import { api } from '../api/client';
import type { Case, ConfigEdit, EditorOperation, LastReceipts, Words } from '../api/types';
import { useApi } from '../api/useApi';

/** `str.format_map` as core says a closing line: each `{field}` from the receipt, a missing one as nothing. */
export function fill(line: string, receipt: Record<string, unknown> | undefined): string {
  return line.replace(/\{\{|\}\}|\{([^{}]*)\}/g, (match, name: string | undefined) => {
    if (match === '{{') return '{';
    if (match === '}}') return '}';
    const key = (name ?? '').split(/[.[]/)[0] ?? '';
    const value = receipt?.[key];
    return value === undefined || value === null ? '' : String(value);
  });
}

/** What is wrong with a closing line before the server says so: an unclosed brace, a field its result does not have. */
export function lint(line: string, fields: string[], result = 'its receipt', known = fields.length > 0): string[] {
  const problems: string[] = [];
  const stripped = line.replace(/\{\{|\}\}/g, '');
  if ((stripped.match(/\{/g)?.length ?? 0) !== (stripped.match(/\}/g)?.length ?? 0) || /\{[^}]*\{/.test(stripped)) problems.push('a brace is not closed');
  const named = [...stripped.matchAll(/\{([^{}]*)\}/g)].map((m) => (m[1] ?? '').split(/[.[]/)[0] ?? '');
  const missing = named.filter((n) => n && known && !fields.includes(n));
  if (missing.length) problems.push(`${result} has no ${missing.map((m) => `{${m}}`).join(', ')}: it would be said as a gap`);
  return problems;
}

/** A receipt value and a `when` value compared as core compares them: equal, or equal as text. */
function same(value: unknown, wanted: unknown): boolean {
  if (Array.isArray(wanted)) return wanted.some((w) => same(value, w));
  if (value === null || value === undefined || wanted === null) return value === wanted || (value === undefined && wanted === null);
  return value === wanted || String(value).toLowerCase() === String(wanted).toLowerCase();
}

/** Whether a case is the line for this receipt: `core.closing.Case.fits`. */
export function fits(c: Case, receipt: Record<string, unknown>, refused: boolean): boolean {
  if (!c.when) return !refused;
  if (c.when === 'refused') return refused;
  return Object.entries(c.when).every(([name, wanted]) => name in receipt && same(receipt[name], wanted));
}

/** Which result a case is for, in words, and the fields its line may name: core's own sides, by fields and closed values. */
function resultOf(c: Case, op: EditorOperation): { label: string; fields: string[] } {
  if (!c.when) return { label: 'a success', fields: op.fields };
  if (c.when === 'refused') return { label: 'a refusal', fields: op.refused_fields };
  const when = c.when;
  const sides = [
    { label: 'a success', fields: op.fields, values: op.values },
    { label: 'a refusal', fields: op.refused_fields, values: op.refused_values },
  ].filter(
    (side) =>
      side.fields.length &&
      Object.entries(when).every(([name, wanted]) => side.fields.includes(name) && (!side.values[name] || side.values[name].some((v) => same(v, wanted)))),
  );
  const labels = new Set(sides.map((s) => s.label));
  return { label: labels.size === 1 ? [...labels][0]! : 'its receipt', fields: [...new Set(sides.flatMap((s) => s.fields))] };
}

/** Every closed value a field can hold, over both results: what a `when` picks from. */
function closedValues(op: EditorOperation, field: string): unknown[] | undefined {
  const all = [...(op.values[field] ?? []), ...(op.refused_values[field] ?? [])];
  const unique = all.filter((v, i) => all.findIndex((w) => show(w) === show(v)) === i);
  return unique.length ? unique : undefined;
}

/** The case's own receipt from recent runs, when the latest receipt of its result fits it. */
function sampleFor(c: Case, last: LastReceipts | undefined): Record<string, unknown> | undefined {
  for (const [result, receipt] of [['done', last?.done], ['refused', last?.refused]] as const) {
    if (receipt && fits(c, receipt, result === 'refused')) return receipt;
  }
  return undefined;
}

function show(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/** A typed `when` value back from the select's text: the closed value it names, or the text as typed. */
function valueOf(text: string, closed: unknown[] | undefined): unknown {
  return closed?.find((v) => show(v) === text) ?? text;
}

type Kind = 'field' | 'done' | 'refused';

function kindOf(c: Case): Kind {
  return !c.when ? 'done' : c.when === 'refused' ? 'refused' : 'field';
}

/** Specific cases first, then the refusal line, then the line for any other success: the only order where each is reached. */
function ordered(cases: Case[]): Case[] {
  const rank = { field: 0, refused: 1, done: 2 } as const;
  return [...cases].sort((a, b) => rank[kindOf(a)] - rank[kindOf(b)]);
}

function CaseRow({ c, n, op, last, cases, update }: { c: Case; n: number; op: EditorOperation; last: LastReceipts | undefined; cases: Case[]; update: (cases: Case[]) => void }) {
  const kind = kindOf(c);
  const { label, fields } = resultOf(c, op);
  const allFields = [...new Set([...op.fields, ...op.refused_fields])];
  const problems = lint(c.line, fields, label, allFields.length > 0);
  const sample = sampleFor(c, last);
  const [field, wanted] = kind === 'field' && c.when && c.when !== 'refused' ? (Object.entries(c.when)[0] ?? ['', '']) : ['', ''];
  const closed = field ? closedValues(op, field) : undefined;
  const put = (next: Case) => update(ordered(cases.map((each, i) => (i === n ? next : each))));
  const setKind = (k: Kind) => {
    if (k === 'done') put({ line: c.line });
    else if (k === 'refused') put({ line: c.line, when: 'refused' });
    else {
      const first = Object.keys(op.values)[0] ?? allFields[0] ?? '';
      put({ line: c.line, when: { [first]: closedValues(op, first)?.[0] ?? '' } });
    }
  };
  const taken = new Set(cases.filter((_, i) => i !== n).map(kindOf));
  const id = `${op.name}-case-${n}`;
  return (
    <div className="case" data-case={n}>
      <div className="case-when">
        <span className="lbl">when</span>
        <select className="select" aria-label="Which result" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          <option value="field">a receipt field is…</option>
          <option value="refused" disabled={taken.has('refused')}>it is refused (any other refusal)</option>
          <option value="done" disabled={taken.has('done')}>it works (any other success)</option>
        </select>
        {kind === 'field' && (
          <>
            <select className="select mono" aria-label="Receipt field" value={field} onChange={(e) => put({ line: c.line, when: { [e.target.value]: closedValues(op, e.target.value)?.[0] ?? '' } })}>
              {allFields.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
            <span className="dim">=</span>
            {closed ? (
              <select className="select mono" aria-label="Value" value={show(wanted)} onChange={(e) => put({ line: c.line, when: { [field]: valueOf(e.target.value, closed) } })}>
                {closed.map((v) => <option key={show(v)} value={show(v)}>{show(v)}</option>)}
              </select>
            ) : (
              <input className="input mono" aria-label="Value" style={{ width: 140 }} value={show(wanted ?? '')} onChange={(e) => put({ line: c.line, when: { [field]: e.target.value } })} />
            )}
          </>
        )}
        <span style={{ flex: '1 1 auto' }} />
        <button type="button" className="btn btn-q" onClick={() => update(cases.filter((_, i) => i !== n))} aria-label="Remove this case">remove</button>
      </div>
      <input
        id={id}
        className={`input mono ${problems.length ? 'input-bad' : ''}`}
        aria-label="Line said for this result"
        value={c.line}
        placeholder="empty: say nothing for this result"
        onChange={(e) => put({ ...c, line: e.target.value })}
      />
      {problems.map((p) => <div key={p} className="problem">{p}</div>)}
      {c.line && !problems.length && (
        <div className="hint">
          Sounds like: <span style={{ color: 'var(--bot)' }}>“{fill(c.line, sample) || '(nothing)'}”</span>
          {sample ? ' · from a real receipt of this result' : ' · no real receipt of this result yet: fields read as gaps'}
        </div>
      )}
      <FieldChips fields={fields} onPick={(f) => put({ ...c, line: `${c.line}{${f}}` })} />
    </div>
  );
}

function FieldChips({ fields, onPick }: { fields: string[]; onPick: (field: string) => void }) {
  if (!fields.length) return null;
  return (
    <div className="chips" aria-label="Receipt fields">
      <span className="hint" style={{ alignSelf: 'center' }}>fields this result has:</span>
      {fields.map((f) => (
        <button key={f} type="button" className="fieldchip" onClick={() => onPick(f)} title={`add {${f}} to the line`}>{`{${f}}`}</button>
      ))}
    </div>
  );
}

/** The closing lines of one operation: one line, or a line for each result, with what each would sound like. */
export function ClosingEditor({ op, draft, setDraft, agent }: { op: EditorOperation; draft: ConfigEdit; setDraft: (d: ConfigEdit) => void; agent: string }) {
  const receipts = useApi(() => api.receipts(agent), `receipts-${agent}`);
  const edited = draft.closing?.[op.name];
  const say: Words = edited?.say ?? op.say;
  const recap: Words = edited?.recap ?? op.recap;
  const last = receipts.state === 'ok' ? receipts.data[op.name] : undefined;
  const set = (key: 'say' | 'recap', value: Words) => {
    const mine = { ...draft.closing?.[op.name], [key]: value };
    if (JSON.stringify(value) === JSON.stringify(op[key])) delete mine[key];
    const closing = { ...draft.closing, [op.name]: mine };
    if (!Object.keys(mine).length) delete closing[op.name];
    setDraft({ ...draft, closing });
  };
  const byResult = Array.isArray(say);
  const toCases = () => {
    const first = Object.keys(op.values)[0];
    const cases: Case[] = first ? [{ when: { [first]: op.values[first]?.[0] }, line: '' }] : [];
    if (op.refused_fields.length) cases.push({ when: 'refused', line: '' });
    set('say', [...cases, { line: typeof say === 'string' ? say : '' }]);
  };
  const toLine = () => set('say', (Array.isArray(say) ? say.find((c) => !c.when)?.line : say) ?? '');
  const addCase = () => {
    if (!Array.isArray(say)) return;
    const first = Object.keys(op.values)[0] ?? op.fields[0] ?? '';
    set('say', ordered([...say, { when: { [first]: op.values[first]?.[0] ?? '' }, line: '' }]));
  };
  return (
    <div className="panel" data-operation={op.name}>
      <div className="panel-h">
        <span className="mono" style={{ fontSize: 14 }}>{op.name}</span>
        <span className="tag">{op.changes ? 'changes' : 'reads'}</span>
        <span className="tag">{op.approval === 'readback' ? 'read back first' : 'no read-back'}</span>
        <span style={{ flex: '1 1 auto' }} />
        <span className="hint">{op.form === 'class' ? 'a class: the line is saved as config beside it' : 'declared in the manifest'}</span>
      </div>
      <div className="panel-b">
        <div className="field">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <label htmlFor={`${op.name}-say`} style={{ flex: '1 1 auto' }}>Closing line · said after it runs</label>
            <div className="segs" role="group" aria-label="Closing line form">
              <button type="button" className={`seg ${byResult ? '' : 'seg-on'}`} aria-pressed={!byResult} onClick={() => byResult && toLine()}>one line</button>
              <button type="button" className={`seg ${byResult ? 'seg-on' : ''}`} aria-pressed={byResult} onClick={() => !byResult && toCases()}>by result</button>
            </div>
          </div>
          {Array.isArray(say) ? (
            <>
              <span className="hint">Tried in order: the first case that fits the receipt is said. A refusal is said only when a case names it; otherwise the agent answers it.</span>
              {say.map((c, n) => <CaseRow key={n} c={c} n={n} op={op} last={last} cases={say} update={(cases) => set('say', cases.length ? cases : '')} />)}
              <div>
                <button type="button" className="btn btn-q" onClick={addCase}>add a case</button>
              </div>
            </>
          ) : (
            <OneLine id={`${op.name}-say`} value={say} fields={op.fields} sample={last?.done} placeholder="empty: it says nothing" onChange={(v) => set('say', v)} />
          )}
        </div>
        <div className="field">
          <label htmlFor={`${op.name}-recap`}>Goodbye line · what a goodbye says of it</label>
          {Array.isArray(recap) ? (
            <div className="hint">Lines by result: edit them on the Full config tab.</div>
          ) : (
            <OneLine
              id={`${op.name}-recap`}
              value={recap}
              fields={op.fields}
              sample={last?.done}
              disabled={!op.changes}
              placeholder={op.changes ? 'empty: a goodbye cannot recap it' : 'a read changes nothing: a goodbye has nothing of it to say'}
              onChange={(v) => set('recap', v)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function OneLine({ id, value, fields, sample, placeholder, disabled, onChange }: { id: string; value: string; fields: string[]; sample: Record<string, unknown> | undefined; placeholder: string; disabled?: boolean; onChange: (value: string) => void }) {
  const problems = lint(value, fields, 'a success');
  return (
    <>
      <input id={id} className={`input mono ${problems.length ? 'input-bad' : ''}`} value={value} disabled={disabled} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      {problems.map((p) => <div key={p} className="problem">{p}</div>)}
      {value && !problems.length && (
        <div className="hint">
          Sounds like: <span style={{ color: 'var(--bot)' }}>“{fill(value, sample) || '(nothing)'}”</span>
          {sample ? ' · from its last real receipt' : ' · no real receipt yet: fields read as gaps'}
        </div>
      )}
      {!disabled && <FieldChips fields={fields} onPick={(f) => onChange(`${value}{${f}}`)} />}
    </>
  );
}
