import type { Graph } from '../api/types';

export const NODE_W = 200;
export const NODE_H = 96;
const COL = NODE_W + 100;
const ROW = NODE_H + 40;
const PAD = 8;

export interface Placed {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * Columns by distance from the entry over the moves that hand the caller on (exits, `then`,
 * prerequisites, failure paths). A consulted step sits under the first step that consults it,
 * since the caller comes back; a step nothing reaches goes after the rest; the end node is last.
 */
export function layout(graph: Graph): { at: Record<string, Placed>; width: number; height: number } {
  const forward = new Set(['exit', 'then', 'prerequisite', 'error', 'unknown', 'take_back']);
  const depth = new Map<string, number>([[graph.entry, 0]]);
  const queue = [graph.entry];
  while (queue.length) {
    const from = queue.shift() as string;
    for (const edge of graph.edges) {
      if (edge.source === from && forward.has(edge.kind) && !depth.has(edge.target)) {
        depth.set(edge.target, (depth.get(from) ?? 0) + 1);
        queue.push(edge.target);
      }
    }
  }
  const consulter = new Map<string, string>();
  for (const edge of graph.edges) if (edge.kind === 'consult' && !consulter.has(edge.target)) consulter.set(edge.target, edge.source);

  const placedSteps = graph.nodes.filter((n) => !n.virtual);
  const maxDepth = Math.max(0, ...depth.values());
  const column = new Map<string, number>();
  for (const node of placedSteps) {
    const host = consulter.get(node.id);
    if (!depth.has(node.id) && host !== undefined && depth.has(host)) column.set(node.id, depth.get(host) as number);
    else column.set(node.id, depth.get(node.id) ?? maxDepth + 1);
  }
  const lastColumn = Math.max(0, ...column.values()) + 1;

  const rows = new Map<number, number>();
  const at: Record<string, Placed> = {};
  const consulted = (id: string) => consulter.has(id) && !depth.has(id);
  const ordered = [...placedSteps.filter((n) => !consulted(n.id)), ...placedSteps.filter((n) => consulted(n.id))];
  for (const node of ordered) {
    const col = column.get(node.id) ?? 0;
    const row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    at[node.id] = { x: PAD + col * COL, y: PAD + row * ROW, w: NODE_W, h: NODE_H };
  }
  const tallest = Math.max(1, ...rows.values());
  for (const node of graph.nodes.filter((n) => n.virtual)) {
    at[node.id] = { x: PAD + lastColumn * COL, y: PAD + ((tallest - 1) * ROW) / 2 + (NODE_H - 28) / 2, w: 88, h: 28 };
  }
  const width = Math.max(...Object.values(at).map((p) => p.x + p.w)) + PAD;
  const height = Math.max(...Object.values(at).map((p) => p.y + p.h)) + PAD;
  return { at, width, height };
}
