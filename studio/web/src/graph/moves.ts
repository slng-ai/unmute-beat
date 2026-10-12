import type { EdgeKind, Graph, GraphEdge, GraphNode } from '../api/types';

/** How a move reads to a person, and the mark the page puts in front of it. */
export const MOVE: Record<EdgeKind, { mark: string; label: string }> = {
  exit: { mark: '→', label: 'hand over' },
  consult: { mark: '↺', label: 'consult, comes back' },
  handback: { mark: '↩', label: 'back to who asked' },
  prerequisite: { mark: '◇', label: 'needs this first' },
  return: { mark: '↩', label: 'back to the waiting step' },
  route: { mark: '⇢', label: 'a receipt moves on' },
  then: { mark: '⇢', label: 'then' },
  error: { mark: '✕', label: 'on error' },
  unknown: { mark: '?', label: 'outcome unknown' },
  take_back: { mark: '↶', label: 'take back' },
  goodbye: { mark: '■', label: 'goodbye' },
};

/** The groups the focus view lists a step's moves under, in reading order. */
const SECTIONS: { name: string; kinds: EdgeKind[] }[] = [
  { name: 'Hand over', kinds: ['exit'] },
  { name: 'Goes back', kinds: ['return', 'handback'] },
  { name: 'Needs first', kinds: ['prerequisite'] },
  { name: 'Goes on by itself', kinds: ['route', 'then', 'error', 'unknown', 'take_back'] },
  { name: 'Consult', kinds: ['consult'] },
  { name: 'End', kinds: ['goodbye'] },
];

export interface MoveSection {
  name: string;
  moves: GraphEdge[];
}

/** A step's moves out, grouped for reading. A move to the entry step reads as going back. */
export function movesFrom(graph: Graph, step: string): MoveSection[] {
  const out = graph.edges.filter((e) => e.source === step);
  return SECTIONS.map((s) => ({ name: s.name, moves: out.filter((e) => s.kinds.includes(e.kind)) })).filter(
    (s) => s.moves.length > 0,
  );
}

/** Where a caller can arrive at this step from, when there is no run to say where they did. */
export function movesInto(graph: Graph, step: string): GraphEdge[] {
  return graph.edges.filter((e) => e.target === step && e.kind !== 'goodbye');
}

export function nodeById(graph: Graph): Map<string, GraphNode> {
  return new Map(graph.nodes.map((n) => [n.id, n]));
}

/** The header a step card shows: its kind and roles, in the page's words. */
export function kindLabel(node: GraphNode): string {
  if (node.virtual) return 'End';
  if (node.roles.includes('consulted')) return 'Consult';
  const kind = { conversation: 'Agent', action: 'Action', end: 'End' }[node.kind];
  const roles = node.roles.map((r) => ({ entry: 'entry', gate: 'gate', consulted: 'consult' })[r]);
  return [kind, ...roles].join(' · ');
}
