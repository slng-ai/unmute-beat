import type { Case, EditorOperation, LastReceipts } from '../api/types';

/** A field inside nested data, as a closing line names it: `customer.name`, `customer[name]`, `items[0].spoken` (`core.closing.FieldPath`). */
export class FieldPath {
  readonly parts: string[];

  constructor(readonly text: string) {
    this.parts = text.match(/[^.[\]]+/g) ?? [];
  }

  /** The path as the receipt's shape names it, list indexes left out: `items[0].spoken` is `items.spoken`. */
  get key(): string {
    return this.parts.filter((part) => !/^\d+$/.test(part)).join('.');
  }

  /** The value at this path, or undefined when any part of it is missing. */
  find(data: unknown): unknown {
    return this.parts.reduce<unknown>((at, part) => (at !== null && typeof at === 'object' ? (at as Record<string, unknown>)[part] : undefined), data);
  }
}

/** One closing line with `{field}` placeholders, filled as core fills it: nested fields by path, a missing one as nothing. */
export class Line {
  constructor(readonly text: string) {}

  /** The line filled from a receipt. */
  fill(receipt: Record<string, unknown> | undefined): string {
    return this.text.replace(/\{\{|\}\}|\{([^{}]*)\}/g, (match, name: string | undefined) => {
      if (match === '{{') return '{';
      if (match === '}}') return '}';
      const value = new FieldPath((name ?? '').split(/[!:]/)[0] ?? '').find(receipt);
      if (value === undefined || value === null) return '';
      return typeof value === 'object' ? JSON.stringify(value) : String(value);
    });
  }

  /** Every field the line names, by its path. */
  fields(): string[] {
    const named = [...this.text.replace(/\{\{|\}\}/g, '').matchAll(/\{([^{}]*)\}/g)].map((m) => (m[1] ?? '').split(/[!:]/)[0] ?? '');
    return [...new Set(named.filter(Boolean))];
  }

  /** What is wrong with it before the server says so: an unclosed brace, a field its result does not have. */
  problems(fields: string[], result = 'its receipt', known = fields.length > 0): string[] {
    const stripped = this.text.replace(/\{\{|\}\}/g, '');
    const open = (stripped.match(/\{/g)?.length ?? 0) !== (stripped.match(/\}/g)?.length ?? 0) || /\{[^}]*\{/.test(stripped);
    const missing = this.fields().filter((name) => known && !fields.includes(new FieldPath(name).key));
    return [
      ...(open ? ['a brace is not closed'] : []),
      ...(missing.length ? [`${result} has no ${missing.map((m) => `{${m}}`).join(', ')}: it would be said as a gap`] : []),
    ];
  }
}

type CaseKind = 'field' | 'done' | 'refused';

/** One operation's results, a success and a refusal, and how its closing cases read against them (`core.closing`). */
export class Results {
  constructor(readonly op: EditorOperation) {}

  /** Every field path of either result, for a `when` to name. */
  get fields(): string[] {
    return [...new Set([...this.op.fields, ...this.op.refused_fields])];
  }

  /** A receipt value and a `when` value compared as core compares them: equal, or equal as text. */
  static same(value: unknown, wanted: unknown): boolean {
    if (Array.isArray(wanted)) return wanted.some((each) => Results.same(value, each));
    if (value === undefined) return false;
    if (value === null || wanted === null) return value === wanted;
    return value === wanted || String(value).toLowerCase() === String(wanted).toLowerCase();
  }

  /** Whether a case is the line for this receipt. */
  static fits(c: Case, receipt: Record<string, unknown>, refused: boolean): boolean {
    if (!c.when) return !refused;
    if (c.when === 'refused') return refused;
    return Object.entries(c.when).every(([name, wanted]) => Results.same(new FieldPath(name).find(receipt), wanted));
  }

  /** What kind of case it is. */
  static kind(c: Case): CaseKind {
    return !c.when ? 'done' : c.when === 'refused' ? 'refused' : 'field';
  }

  /** Specific cases first, then the refusal line, then the line for any other success: the only order where each is reached. */
  static ordered(cases: Case[]): Case[] {
    const rank = { field: 0, refused: 1, done: 2 } as const;
    return [...cases].sort((a, b) => rank[Results.kind(a)] - rank[Results.kind(b)]);
  }

  /** Which result a case is for, in words, and the fields its line may name. */
  of(c: Case): { label: string; fields: string[] } {
    const op = this.op;
    if (!c.when) return { label: 'a success', fields: op.fields };
    if (c.when === 'refused') return { label: 'a refusal', fields: op.refused_fields };
    const when = Object.entries(c.when);
    const sides = [
      { label: 'a success', fields: op.fields, values: op.values },
      { label: 'a refusal', fields: op.refused_fields, values: op.refused_values },
    ].filter((side) =>
      side.fields.length > 0 &&
      when.every(([name, wanted]) => {
        const key = new FieldPath(name).key;
        return side.fields.includes(key) && (!side.values[key] || side.values[key].some((value) => Results.same(value, wanted)));
      }),
    );
    const labels = [...new Set(sides.map((side) => side.label))];
    return { label: labels.length === 1 ? (labels[0] ?? 'its receipt') : 'its receipt', fields: [...new Set(sides.flatMap((side) => side.fields))] };
  }

  /** Every closed value a field can hold, over both results: what a `when` picks from. */
  closed(field: string): unknown[] | undefined {
    const all = [...(this.op.values[field] ?? []), ...(this.op.refused_values[field] ?? [])];
    const unique = all.filter((value, at) => all.findIndex((other) => Results.show(other) === Results.show(value)) === at);
    return unique.length ? unique : undefined;
  }

  /** The case's own receipt from recent runs, when the latest receipt of its result fits it. */
  static sample(c: Case, last: LastReceipts | undefined): Record<string, unknown> | undefined {
    const tried = [
      { receipt: last?.done, refused: false },
      { receipt: last?.refused, refused: true },
    ];
    return tried.find(({ receipt, refused }) => receipt && Results.fits(c, receipt, refused))?.receipt;
  }

  /** A value as a select shows it. */
  static show(value: unknown): string {
    return typeof value === 'string' ? value : JSON.stringify(value);
  }
}
