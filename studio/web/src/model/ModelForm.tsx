import { useState, type ReactNode } from 'react';
import { pathKey, Schema } from './schema';

type Path = (string | number)[];

interface FieldProps {
  schema?: Schema;
  value: unknown;
  onChange: (value: unknown) => void;
  errors: Record<string, string>;
  path: Path;
}

/** The text a field shows: none as empty. */
function text(value: unknown): string {
  return value === null || value === undefined ? '' : String(value);
}

/** An input's text back as the value its field holds: empty is none when it may be, a number for a number. */
function parsed(raw: string, schema: Schema | undefined, numeric: boolean): unknown {
  if (raw === '' && (schema?.nullable() ?? true)) return null;
  if (numeric && raw.trim() !== '' && !Number.isNaN(Number(raw))) return Number(raw);
  return raw;
}

/** An input type for a string format: a date picker for a date, a time picker for a time. */
function inputType(format: string | undefined): string {
  return { date: 'date', time: 'time', 'date-time': 'datetime-local', email: 'email', uri: 'url' }[format ?? ''] ?? 'text';
}

/** A list or an open value with no schema to draw it from, edited as JSON. */
function JsonField({ value, onChange, path, bad }: { value: unknown; onChange: (value: unknown) => void; path: Path; bad: boolean }) {
  const [raw, setRaw] = useState(() => JSON.stringify(value ?? null, null, 1));
  const [wrong, setWrong] = useState(false);
  return (
    <>
      <textarea
        id={`field-${pathKey(path)}`}
        className={`textarea mono ${bad || wrong ? 'input-bad' : ''}`}
        rows={3}
        value={raw}
        spellCheck={false}
        onChange={(e) => {
          setRaw(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setWrong(false);
          } catch {
            setWrong(true);
          }
        }}
      />
      {wrong && <span className="hint">not JSON yet: the last valid value is kept</span>}
    </>
  );
}

/** One value's input, by what its schema says it holds. */
function Input({ schema, value, onChange, errors, path }: FieldProps): ReactNode {
  const id = `field-${pathKey(path)}`;
  const bad = Boolean(errors[pathKey(path)]);
  const kind = schema?.kind() ?? (typeof value === 'boolean' ? 'boolean' : typeof value === 'number' ? 'number' : value !== null && typeof value === 'object' ? 'any' : 'string');
  const nullable = schema?.nullable() ?? true;
  const cls = `input mono ${bad ? 'input-bad' : ''}`;
  if (kind === 'object' && schema) return <ObjectFields schema={schema} value={value} onChange={onChange} errors={errors} path={path} />;
  if (kind === 'union' && schema) return <UnionField schema={schema} value={value} onChange={onChange} errors={errors} path={path} />;
  if (kind === 'array' && schema?.item()) return <ListField schema={schema} value={value} onChange={onChange} errors={errors} path={path} />;
  if (kind === 'array' || kind === 'any') return <JsonField value={value} onChange={onChange} path={path} bad={bad} />;
  if (kind === 'boolean' && !nullable) {
    return (
      <label className="check">
        <input id={id} type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        <span className="mono">{String(Boolean(value))}</span>
      </label>
    );
  }
  const closed = kind === 'boolean' ? [true, false] : schema?.choices();
  if (closed) {
    const index = closed.findIndex((choice) => choice === value);
    return (
      <select id={id} className={`select mono ${bad ? 'input-bad' : ''}`} value={index < 0 ? '' : String(index)} onChange={(e) => onChange(e.target.value === '' ? null : closed[Number(e.target.value)])}>
        {(nullable || index < 0) && <option value="">—</option>}
        {closed.map((choice, n) => <option key={n} value={String(n)}>{String(choice)}</option>)}
      </select>
    );
  }
  const numeric = kind === 'integer' || kind === 'number';
  const suggestions = schema?.suggestions() ?? [];
  return (
    <>
      <input
        id={id}
        className={cls}
        type={numeric ? 'number' : kind === 'string' ? inputType(schema?.format) : 'text'}
        step={kind === 'integer' ? 1 : 'any'}
        value={text(value)}
        placeholder={nullable ? 'none' : ''}
        list={suggestions.length ? `${id}-list` : undefined}
        onChange={(e) => onChange(parsed(e.target.value, schema, numeric))}
      />
      {suggestions.length > 0 && (
        <datalist id={`${id}-list`}>
          {suggestions.map((choice) => <option key={String(choice)} value={String(choice)} />)}
        </datalist>
      )}
    </>
  );
}

/** A field with its name, its description, its input, and what the agent's types said was wrong with it. */
function Field({ name, schema, value, onChange, errors, path }: FieldProps & { name: string }) {
  const error = errors[pathKey(path)];
  const nested = schema ? ['object', 'union', 'array'].includes(schema.kind()) : value !== null && typeof value === 'object';
  return (
    <div className={`field ${nested ? 'field-wide' : ''}`}>
      <label htmlFor={`field-${pathKey(path)}`}>{name}</label>
      <Input schema={schema} value={value} onChange={onChange} errors={errors} path={path} />
      {error && <div className="problem">{error}</div>}
      {schema?.description && <span className="hint">{schema.description}</span>}
    </div>
  );
}

/** A model's fields, each its own input; a model that may be none can be added and cleared as a whole. */
function ObjectFields({ schema, value, onChange, errors, path }: FieldProps & { schema: Schema }) {
  const shape = schema.fitting(value);
  if (value === null || value === undefined || typeof value !== 'object') {
    return (
      <div className="model-none">
        <span className="dim mono">none</span>
        <button type="button" className="btn btn-q" onClick={() => onChange(shape.blank())}>add</button>
      </div>
    );
  }
  const held = value as Record<string, unknown>;
  const fields = shape.fields();
  const extra = Object.keys(held).filter((name) => !fields.some(([known]) => known === name));
  return (
    <fieldset className={path.length ? 'model-fields model-nested' : 'model-fields'}>
      {fields.map(([name, field]) => (
        <Field key={name} name={name} schema={field} value={held[name] ?? null} onChange={(next) => onChange({ ...held, [name]: next })} errors={errors} path={[...path, name]} />
      ))}
      {extra.map((name) => (
        <Field key={name} name={name} value={held[name]} onChange={(next) => onChange({ ...held, [name]: next })} errors={errors} path={[...path, name]} />
      ))}
      {path.length > 0 && schema.nullable() && (
        <div>
          <button type="button" className="btn btn-q" onClick={() => onChange(null)}>clear</button>
        </div>
      )}
    </fieldset>
  );
}

/** A value that is one of several models: pick which, then fill its fields. */
function UnionField({ schema, value, onChange, errors, path }: FieldProps & { schema: Schema }) {
  const members = schema.options();
  const current = value === null || value === undefined ? -1 : members.indexOf(schema.fitting(value));
  return (
    <div className="model-union">
      <select className="select mono" aria-label="Which kind" value={String(current)} onChange={(e) => onChange(Number(e.target.value) < 0 ? null : members[Number(e.target.value)]?.blank())}>
        {schema.nullable() && <option value="-1">—</option>}
        {members.map((member, n) => <option key={n} value={String(n)}>{member.title ?? `kind ${n + 1}`}</option>)}
      </select>
      {current >= 0 && members[current] && <Input schema={members[current]} value={value} onChange={onChange} errors={errors} path={path} />}
    </div>
  );
}

/** A list: each item its own input, removable, and a new item added from the item's shape. */
function ListField({ schema, value, onChange, errors, path }: FieldProps & { schema: Schema }) {
  const items = Array.isArray(value) ? value : [];
  const item = schema.item();
  return (
    <div className="model-list-edit">
      {items.map((each, n) => (
        <div key={n} className="model-item">
          <span className="mono dim">{n + 1}</span>
          <div className="model-item-body">
            <Input schema={item} value={each} onChange={(next) => onChange(items.map((old, at) => (at === n ? next : old)))} errors={errors} path={[...path, n]} />
          </div>
          <button type="button" className="btn btn-q" onClick={() => onChange(items.filter((_, at) => at !== n))} aria-label={`remove item ${n + 1}`}>remove</button>
        </div>
      ))}
      <div>
        <button type="button" className="btn btn-q" onClick={() => onChange([...items, item?.blank() ?? ''])}>add an item</button>
      </div>
    </div>
  );
}

/**
 * A model's value edited through its JSON schema, nested models and lists included: the go-back page's state. What the
 * agent's own types refused shows under the field it names, by its whole path (`customer.customer_phone`).
 */
export function ModelForm({ schema, value, onChange, errors = {} }: { schema?: Schema; value: Record<string, unknown>; onChange: (value: Record<string, unknown>) => void; errors?: Record<string, string> }) {
  if (!schema) {
    return (
      <fieldset className="model-fields">
        {Object.entries(value).map(([name, inner]) => (
          <Field key={name} name={name} value={inner} onChange={(next) => onChange({ ...value, [name]: next })} errors={errors} path={[name]} />
        ))}
      </fieldset>
    );
  }
  return <ObjectFields schema={schema} value={value} onChange={(next) => onChange(next as Record<string, unknown>)} errors={errors} path={[]} />;
}
