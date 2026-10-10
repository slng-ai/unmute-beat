import ELK, { type ElkExtendedEdge, type ElkNode } from 'elkjs/lib/elk.bundled.js';
import type { Graph } from '../api/types';
import type { MapEdge } from './collapse';

export const STEP = { w: 220, h: 84 };
export const END = { w: 96, h: 40 };
const HEADER = 40;

export interface Placed {
  x: number;
  y: number;
  w: number;
  h: number;
  parent?: string;
}

export interface Point {
  x: number;
  y: number;
}

export interface Laid {
  at: Record<string, Placed>;
  routes: Record<string, Point[]>;
}

const elk = new ELK();

/**
 * Places every step, group and the end node with ELK's layered algorithm, left to right, and
 * routes every line with right angles. Groups are compound nodes laid out with their members
 * (`INCLUDE_CHILDREN`); members' positions are relative to their group, as React Flow wants, and
 * routes are in the root's coordinates (`edgeCoords: ROOT`), so the page draws them as given.
 * The entry step is pinned to the first layer and the end node to the last; a flow of more than eight
 * boxes wraps into rows near the stage's shape (`wrapping`, `aspectRatio`) instead of one line too wide to read.
 */
export async function place(graph: Graph, edges: MapEdge[]): Promise<Laid> {
  const inGroup = new Set(graph.groups.flatMap((g) => g.steps));
  const wide = graph.nodes.filter((n) => !inGroup.has(n.id)).length + graph.groups.length > 8;
  const box = (id: string, virtual: boolean): ElkNode => ({
    id,
    width: virtual ? END.w : STEP.w,
    height: virtual ? END.h : STEP.h,
    layoutOptions:
      id === graph.entry
        ? { 'elk.layered.layering.layerConstraint': 'FIRST' }
        : virtual
          ? { 'elk.layered.layering.layerConstraint': 'LAST' }
          : {},
  });
  const root: ElkNode = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.json.edgeCoords': 'ROOT',
      'elk.layered.spacing.nodeNodeBetweenLayers': '120',
      'elk.layered.spacing.edgeNodeBetweenLayers': '30',
      'elk.spacing.nodeNode': '40',
      'elk.spacing.edgeEdge': '14',
      'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
      'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
      ...(wide ? { 'elk.layered.wrapping.strategy': 'MULTI_EDGE', 'elk.aspectRatio': '1.6' } : {}),
    },
    children: [
      ...graph.nodes.filter((n) => !inGroup.has(n.id)).map((n) => box(n.id, n.virtual)),
      ...graph.groups.map((g) => ({
        id: g.id,
        layoutOptions: { 'elk.padding': `[top=${HEADER + 16},left=16,bottom=16,right=16]` },
        children: g.steps.map((s) => box(s, false)),
      })),
    ],
    edges: edges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  };
  const laid = await elk.layout(root);
  const at: Record<string, Placed> = {};
  const routes: Record<string, Point[]> = {};
  const collect = (edgesHere: ElkExtendedEdge[] | undefined) => {
    for (const edge of edgesHere ?? []) {
      const section = edge.sections?.[0];
      if (section) routes[edge.id] = [section.startPoint, ...(section.bendPoints ?? []), section.endPoint];
    }
  };
  collect(laid.edges);
  for (const child of laid.children ?? []) {
    at[child.id] = { x: child.x ?? 0, y: child.y ?? 0, w: child.width ?? 0, h: child.height ?? 0 };
    collect(child.edges);
    for (const inner of child.children ?? []) {
      at[inner.id] = { x: inner.x ?? 0, y: inner.y ?? 0, w: inner.width ?? 0, h: inner.height ?? 0, parent: child.id };
    }
  }
  return { at, routes };
}
