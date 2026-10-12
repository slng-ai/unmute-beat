import { api } from '../api/client';
import type { Case, ConfigEdit, EditorOperation, LastReceipts, Words } from '../api/types';
import { useApi } from '../api/useApi';
import { Line, Results } from './lines';

/** What a line would sound like, filled from a real receipt when there is one. */
function SoundsLike({ line, sample, from }: { line: string; sample?: Record<string, unknown>; from: string }) {
  return (
    <div className="hint">
      Sounds like: <span style={{ color: 'var(--bot)' }}>“{new Line(line).fill(sample) || '(nothing)'}”</span>
      {sample ? ` · from ${from}` : ' · no real receipt of this result yet: fields read as gaps'}
    </div>
  );
}

/** The fields a line may name, each a chip that adds it. */
function FieldChips({ fields, onPick }: { fields: string[]; onPick: (field: string) => void }) {
  if (!fields.length) return null;
  return (
    <div className="chips" aria-label="Receipt fields">
      <span className="hint" style={{ alignSelf: 'center' }}>fields this result has:</span>
      {fields.map((field) => (
        <button key={field} type="button" className="fieldchip" onClick={() => onPick(field)} title={`add {${field}} to the line`}>{`{${field}}`}</button>
      ))}
    </div>
  );
}

/** One case of lines by result: which result it is for, its line, what it sounds like, and the fields that result has. */
function CaseRow({ c, n, results, last, cases, update }: { c: Case; n: number; results: Results; last?: LastReceipts; cases: Case[]; update: (cases: Case[]) => void }) {
  const kind = Results.kind(c);
  const { label, fields } = results.of(c);
  const all = results.fields;
  const problems = new Line(c.line).problems(fields, label, all.length > 0);
  const sample = Results.sample(c, last);
  const [field, wanted] = c.when && c.when !== 'refused' ? (Object.entries(c.when)[0] ?? ['', '']) : ['', ''];
  const closed = field ? results.closed(field) : undefined;
  const put = (next: Case) => update(Results.ordered(cases.map((each, at) => (at === n ? next : each))));
  const pick = (name: string) => put({ line: c.line, when: { [name]: results.closed(name)?.[0] ?? '' } });
  const setKind = (next: string) => {
    if (next === 'done') put({ line: c.line });
    else if (next === 'refused') put({ line: c.line, when: 'refused' });
    else pick(Object.keys(results.op.values)[0] ?? all[0] ?? '');
  };
  const taken = new Set(cases.filter((_, at) => at !== n).map(Results.kind));
  return (
    <div className="case" data-case={n}>
      <div className="case-when">
        <span className="lbl">when</span>
        <select className="select" aria-label="Which result" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="field">a receipt field is…</option>
          <option value="refused" disabled={taken.has('refused')}>it is refused (any other refusal)</option>
          <option value="done" disabled={taken.has('done')}>it works (any other success)</option>
        </select>
        {kind === 'field' && (
          <>
            <select className="select mono" aria-label="Receipt field" value={field} onChange={(e) => pick(e.target.value)}>
              {all.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
            <span className="dim">=</span>
            {closed ? (
              <select
                className="select mono"
                aria-label="Value"
                value={Results.show(wanted)}
                onChange={(e) => put({ line: c.line, when: { [field]: closed.find((value) => Results.show(value) === e.target.value) ?? e.target.value } })}
              >
                {closed.map((value) => <option key={Results.show(value)} value={Results.show(value)}>{Results.show(value)}</option>)}
              </select>
            ) : (
              <input className="input mono" aria-label="Value" style={{ width: 140 }} value={Results.show(wanted ?? '')} onChange={(e) => put({ line: c.line, when: { [field]: e.target.value } })} />
            )}
          </>
        )}
        <span style={{ flex: '1 1 auto' }} />
        <button type="button" className="btn btn-q" onClick={() => update(cases.filter((_, at) => at !== n))} aria-label="Remove this case">remove</button>
      </div>
      <input
        className={`input mono ${problems.length ? 'input-bad' : ''}`}
        aria-label="Line said for this result"
        value={c.line}
        placeholder="empty: say nothing for this result"
        onChange={(e) => put({ ...c, line: e.target.value })}
      />
      {problems.map((problem) => <div key={problem} className="problem">{problem}</div>)}
      {c.line && !problems.length && <SoundsLike line={c.line} sample={sample} from="a real receipt of this result" />}
      <FieldChips fields={fields} onPick={(name) => put({ ...c, line: `${c.line}{${name}}` })} />
    </div>
  );
}

/** One line for any success: its input, its problems, what it sounds like, and the fields a success has. */
function OneLine({ id, value, fields, sample, placeholder, disabled, onChange }: { id: string; value: string; fields: string[]; sample?: Record<string, unknown>; placeholder: string; disabled?: boolean; onChange: (value: string) => void }) {
  const problems = new Line(value).problems(fields, 'a success');
  return (
    <>
      <input id={id} className={`input mono ${problems.length ? 'input-bad' : ''}`} value={value} disabled={disabled} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      {problems.map((problem) => <div key={problem} className="problem">{problem}</div>)}
      {value && !problems.length && <SoundsLike line={value} sample={sample} from="its last real receipt" />}
      {!disabled && <FieldChips fields={fields} onPick={(name) => onChange(`${value}{${name}}`)} />}
    </>
  );
}

/** The closing lines of one operation: one line, or a line for each result, with what each would sound like. */
export function ClosingEditor({ op, draft, setDraft, agent }: { op: EditorOperation; draft: ConfigEdit; setDraft: (d: ConfigEdit) => void; agent: string }) {
  const receipts = useApi(() => api.receipts(agent), `receipts-${agent}`);
  const results = new Results(op);
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
  const firstCase = (): Case => {
    const first = Object.keys(op.values)[0] ?? op.fields[0] ?? '';
    return { when: { [first]: results.closed(first)?.[0] ?? '' }, line: '' };
  };
  const toCases = () => {
    const refusal: Case[] = op.refused_fields.length ? [{ when: 'refused', line: '' }] : [];
    set('say', [firstCase(), ...refusal, { line: typeof say === 'string' ? say : '' }]);
  };
  const toLine = () => set('say', (Array.isArray(say) ? say.find((c) => !c.when)?.line : say) ?? '');
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
              <button type="button" className={`seg ${Array.isArray(say) ? '' : 'seg-on'}`} aria-pressed={!Array.isArray(say)} onClick={() => Array.isArray(say) && toLine()}>one line</button>
              <button type="button" className={`seg ${Array.isArray(say) ? 'seg-on' : ''}`} aria-pressed={Array.isArray(say)} onClick={() => !Array.isArray(say) && toCases()}>by result</button>
            </div>
          </div>
          {Array.isArray(say) ? (
            <>
              <span className="hint">Tried in order: the first case that fits the receipt is said. A refusal is said only when a case names it; otherwise the agent answers it.</span>
              {say.map((c, n) => <CaseRow key={n} c={c} n={n} results={results} last={last} cases={say} update={(cases) => set('say', cases.length ? cases : '')} />)}
              <div>
                <button type="button" className="btn btn-q" onClick={() => set('say', Results.ordered([...say, firstCase()]))}>add a case</button>
              </div>
            </>
          ) : (
            <OneLine id={`${op.name}-say`} value={say} fields={op.fields} sample={last?.done} placeholder="empty: it says nothing" onChange={(value) => set('say', value)} />
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
              onChange={(value) => set('recap', value)}
            />
          )}
        </div>
      </div>
    </div>
  );
}
