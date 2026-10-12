/** A JSON schema node, as Pydantic writes one for a model: properties, `$ref`s into `$defs`, unions, enums, formats. */
export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  prefixItems?: JsonSchema[];
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  enum?: unknown[];
  const?: unknown;
  format?: string;
  default?: unknown;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  discriminator?: { propertyName: string; mapping?: Record<string, string> };
  additionalProperties?: boolean | JsonSchema;
}

export type Kind = 'object' | 'array' | 'boolean' | 'integer' | 'number' | 'string' | 'choice' | 'union' | 'any';

/** One node of a model's JSON schema, its `$ref`s followed against the root's `$defs`: the shape of a state or a receipt. */
export class Schema {
  readonly node: JsonSchema;

  constructor(
    node: JsonSchema,
    readonly root: JsonSchema = node,
  ) {
    this.node = Schema.resolved(node, root);
  }

  /** A schema for a node, or none when there is no node to read. */
  static of(node: JsonSchema | undefined | null): Schema | undefined {
    return node && Object.keys(node).length ? new Schema(node) : undefined;
  }

  /** A node with its `$ref` (and a single-member `allOf`, as Pydantic writes a described reference) followed. */
  private static resolved(node: JsonSchema, root: JsonSchema): JsonSchema {
    let found = node;
    for (let depth = 0; depth < 8; depth += 1) {
      const ref = found.$ref ?? (found.allOf?.length === 1 ? found.allOf[0]?.$ref : undefined);
      const name = ref?.split('/').pop();
      const target = name ? root.$defs?.[name] : undefined;
      if (!target) break;
      const { $ref: _ref, allOf: _all, ...rest } = found;
      found = { ...target, ...rest };
    }
    return found;
  }

  /** A child node, read against the same root. */
  private child(node: JsonSchema): Schema {
    return new Schema(node, this.root);
  }

  get title(): string | undefined {
    return this.node.title;
  }

  get description(): string | undefined {
    return this.node.description;
  }

  get format(): string | undefined {
    return this.node.format ?? this.options().find((option) => option.node.format)?.node.format;
  }

  /** A union's members, `null` left out; any other node is its own one option. */
  options(): Schema[] {
    const members = this.node.anyOf ?? this.node.oneOf;
    if (!members) return [this];
    return members.filter((member) => member.type !== 'null').map((member) => this.child(member));
  }

  /** Whether `null` is one of the values it takes. */
  nullable(): boolean {
    const members = this.node.anyOf ?? this.node.oneOf ?? [];
    return members.some((member) => member.type === 'null') || this.node.type === 'null' || (Array.isArray(this.node.type) && this.node.type.includes('null'));
  }

  /** What kind of value it holds, a nullable's `null` aside. */
  kind(): Kind {
    const options = this.options();
    if (options.length > 1) return options.every((option) => option.isScalar()) ? 'choice' : 'union';
    const only = options[0] ?? this;
    if (only !== this) return only.kind();
    if (this.node.enum || this.node.const !== undefined) return 'choice';
    const type = Array.isArray(this.node.type) ? this.node.type.find((t) => t !== 'null') : this.node.type;
    if (type === 'object' || this.node.properties) return 'object';
    if (type === 'array') return 'array';
    if (type === 'boolean' || type === 'integer' || type === 'number' || type === 'string') return type;
    return 'any';
  }

  /** Whether every value it takes is a single value, not an object or a list. */
  isScalar(): boolean {
    return !['object', 'array', 'union'].includes(this.kind());
  }

  /** Its fields, in the model's order. */
  fields(): [string, Schema][] {
    const only = this.options().length === 1 ? (this.options()[0] ?? this) : this;
    return Object.entries(only.node.properties ?? {}).map(([name, node]) => [name, this.child(node)]);
  }

  /** A field by name, if it has one. */
  field(name: string): Schema | undefined {
    return this.fields().find(([key]) => key === name)?.[1];
  }

  /** The schema of a list's items. */
  item(): Schema | undefined {
    const only = this.options().length === 1 ? (this.options()[0] ?? this) : this;
    const node = only.node.items ?? only.node.prefixItems?.[0];
    return node ? this.child(node) : undefined;
  }

  /** The values it can only ever hold (enums and constants, over every option); none when it is open. */
  choices(): unknown[] | undefined {
    const found = this.options().flatMap((option) => option.node.enum ?? (option.node.const !== undefined ? [option.node.const] : []));
    const open = this.options().some((option) => !option.node.enum && option.node.const === undefined);
    return found.length && !open ? found : undefined;
  }

  /** Suggested values for an open choice: the enums among its options. */
  suggestions(): unknown[] {
    return this.options().flatMap((option) => option.node.enum ?? (option.node.const !== undefined ? [option.node.const] : []));
  }

  /** Whether a field must be given. */
  requires(name: string): boolean {
    return (this.node.required ?? []).includes(name);
  }

  /** A union member that fits a value: by its discriminator, else the one whose fields the value has most of. */
  fitting(value: unknown): Schema {
    const members = this.options();
    if (members.length < 2 || value === null || typeof value !== 'object') return members[0] ?? this;
    const held = value as Record<string, unknown>;
    const tag = this.node.discriminator?.propertyName;
    const tagged = tag ? members.find((member) => member.fields().some(([name, field]) => name === tag && field.choices()?.includes(held[tag]))) : undefined;
    if (tagged) return tagged;
    const score = (member: Schema) => member.fields().filter(([name, field]) => name in held && (!field.choices() || field.choices()?.includes(held[name]))).length;
    return members.reduce((best, member) => (score(member) > score(best) ? member : best));
  }

  /** An empty value of this shape: what a form starts a new object or list item from. */
  blank(): unknown {
    if (this.node.default !== undefined) return structuredClone(this.node.default);
    switch (this.kind()) {
      case 'object':
        return Object.fromEntries(this.fields().map(([name, field]) => [name, field.nullable() ? null : field.blank()]));
      case 'array':
        return [];
      case 'boolean':
        return false;
      case 'integer':
      case 'number':
        return 0;
      case 'choice':
        return this.choices()?.[0] ?? '';
      default:
        return '';
    }
  }
}

/** A path into a value, as a validation error's `loc` gives it: `['customer', 'customer_phone']` is `customer.customer_phone`. */
export function pathKey(path: (string | number)[]): string {
  return path.map(String).join('.');
}
