import type { ReactNode } from 'react';
import { Schema } from './schema';

/** A scalar as the page shows it: none as a dash, everything else as text that wraps inside its box. */
function Scalar({ value, tone }: { value: unknown; tone?: string }) {
  if (value === null || value === undefined || value === '') return <span className="dim">—</span>;
  return <span style={{ color: tone }}>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</span>;
}

/**
 * A state (the facts) or a receipt drawn from its model's JSON schema, the same way for both: each field by name, a nested model
 * as its own fields under it, a list as its numbered items. With no schema the value's own keys are drawn. `changed`
 * marks top-level fields the last change touched.
 */
export function ModelView({ value, schema, tone, changed = [] }: { value: unknown; schema?: Schema; tone?: string; changed?: string[] }): ReactNode {
  if (Array.isArray(value)) {
    if (!value.length) return <span className="dim">none</span>;
    const item = schema?.item();
    return (
      <ol className="model-list">
        {value.map((each, n) => (
          <li key={n}>
            <ModelView value={each} schema={item} tone={tone} />
          </li>
        ))}
      </ol>
    );
  }
  if (value === null || typeof value !== 'object') return <Scalar value={value} tone={tone} />;
  const held = value as Record<string, unknown>;
  const shape = schema?.fitting(held);
  const known = shape?.fields().map(([name]) => name) ?? [];
  const names = [...known.filter((name) => name in held), ...Object.keys(held).filter((name) => !known.includes(name))];
  if (!names.length) return <span className="dim">empty</span>;
  return (
    <dl className="fields">
      {names.map((name) => {
        const field = shape?.field(name);
        const inner = held[name];
        const nested = inner !== null && typeof inner === 'object';
        const marked = changed.includes(name);
        return (
          <div key={name} className={`field-row${marked ? ' field-changed' : ''}${nested ? ' field-nested' : ''}`} title={field?.description}>
            <dt>{name}</dt>
            <dd>
              <ModelView value={inner} schema={field} tone={tone} />
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
