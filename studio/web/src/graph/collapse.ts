import type { Graph, GraphEdge } from '../api/types';

/** One line on the map: between two steps, a step and a group, or two groups, with how many moves it stands for. */
export interface MapEdge {
  id: string;
  source: string;
  target: string;
  kind: GraphEdge['kind'];
  count: number;
}

/**
 * The map's lines. Moves inside a group are implied by the group itself ("any to any"); a step
 * that makes the same kind of move to every member of a group gets one line to the group, and
 * every member making the same move to one step gives one line from it. Goodbyes are left out:
 * every conversation step can end the call, and the end node says so.
 */
export function collapse(graph: Graph): MapEdge[] {
  const groupOf = new Map<string, string>();
  for (const g of graph.groups) for (const s of g.steps) groupOf.set(s, g.id);
  const members = new Map(graph.groups.map((g) => [g.id, g.steps]));
  const has = new Set(graph.edges.map((e) => `${e.source}>${e.target}>${e.kind}`));
  const lines = new Map<string, MapEdge>();

  const add = (source: string, target: string, kind: GraphEdge['kind']) => {
    const id = `${source}>${target}>${kind}`;
    const line = lines.get(id);
    if (line) line.count += 1;
    else lines.set(id, { id, source, target, kind, count: 1 });
  };
  const toAll = (source: string, group: string, kind: GraphEdge['kind']) =>
    (members.get(group) ?? []).every((m) => m === source || has.has(`${source}>${m}>${kind}`));
  const fromAll = (group: string, target: string, kind: GraphEdge['kind']) =>
    (members.get(group) ?? []).every((m) => m === target || has.has(`${m}>${target}>${kind}`));

  for (const e of graph.edges) {
    if (e.kind === 'goodbye') continue;
    const sg = groupOf.get(e.source);
    const tg = groupOf.get(e.target);
    if (sg && sg === tg) continue;
    const source = sg && fromAll(sg, e.target, e.kind) ? sg : e.source;
    const target = tg && toAll(e.source, tg, e.kind) ? tg : e.target;
    add(source, target, e.kind);
  }
  return [...lines.values()];
}
