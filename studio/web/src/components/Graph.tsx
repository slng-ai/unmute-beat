import type { Graph as GraphData, GraphNode } from '../api/types';
import { layout } from '../graph/layout';

const KIND: Record<string, string> = { conversation: 'Agent', action: 'Action', end: 'End' };

function header(node: GraphNode): string {
  const roles = node.roles.filter((r) => r !== 'consulted');
  if (node.roles.includes('consulted')) return 'Consult';
  return [KIND[node.kind] ?? node.kind, ...roles].join(' · ');
}

function summary(node: GraphNode): string {
  const parts = [
    node.fields.length && `${node.fields.length} field${node.fields.length === 1 ? '' : 's'}`,
    node.offers.length && `offers ${node.offers.join(', ')}`,
    node.operations.length && `${node.operations.length} op${node.operations.length === 1 ? '' : 's'}`,
    node.opens_with && `opens_with ${node.opens_with}`,
  ].filter(Boolean);
  return parts.join(' · ') || (node.roles.includes('consulted') ? 'answers, hands back' : '—');
}

/**
 * The step graph as declared. `active` is where the caller is; `selected` is what the inspector
 * shows; the moves out of the active step (or the selected one, with no run) are lit. Only moves
 * that go forward are drawn for the rest, faintly: every exit at once is a hairball, not a map.
 */
export function Graph({ graph, selected, active, onSelect }: {
  graph: GraphData;
  selected?: string;
  active?: string;
  onSelect: (id: string) => void;
}) {
  const { at, width, height } = layout(graph);
  const from = active ?? selected;
  const next = new Set(graph.edges.filter((e) => e.source === from).map((e) => e.target));
  return (
    <div className="graph" style={{ width, height }}>
      <svg width={width} height={height} aria-hidden="true">
        {graph.edges.map((edge, i) => {
          const a = at[edge.source];
          const b = at[edge.target];
          if (!a || !b) return null;
          const lit = edge.source === from;
          const forward = b.x > a.x;
          if (!lit && (!forward || edge.kind === 'goodbye')) return null;
          const sy = a.y + a.h / 2;
          const ty = b.y + b.h / 2;
          let d: string;
          if (forward) {
            const sx = a.x + a.w;
            const bend = Math.max(40, (b.x - sx) / 2);
            d = `M ${sx} ${sy} C ${sx + bend} ${sy}, ${b.x - bend} ${ty}, ${b.x} ${ty}`;
          } else {
            const reach = 48 + Math.abs(ty - sy) / 6;
            d = `M ${a.x} ${sy} C ${a.x - reach} ${sy}, ${b.x - reach} ${ty}, ${b.x} ${ty}`;
          }
          return <path key={i} className={`edge ${edge.kind}${lit ? ' lit' : ''}`} d={d} />;
        })}
      </svg>
      {graph.nodes.map((node) => {
        const p = at[node.id];
        if (!p) return null;
        const cls = [
          'node',
          node.virtual && 'virtual',
          node.roles.includes('consulted') && 'consulted',
          node.id === active && 'active',
          node.id !== active && next.has(node.id) && 'next',
          node.id === selected && 'selected',
        ]
          .filter(Boolean)
          .join(' ');
        return (
          <button key={node.id} type="button" className={cls} style={{ left: p.x, top: p.y }} onClick={() => onSelect(node.id)}>
            {node.virtual ? (
              'End'
            ) : (
              <>
                <span className="node-h">
                  {header(node)}
                  <span style={{ flex: '1 1 auto' }} />
                  {node.id === active ? '● Active' : next.has(node.id) ? 'Can go' : ''}
                </span>
                <span className="node-t">{node.id}</span>
                <span className="node-m">{summary(node)}</span>
              </>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function Legend() {
  return (
    <div className="legend">
      <span className="lbl">Graph</span>
      <span style={{ flex: '1 1 auto' }} />
      <span className="lbl"><i style={{ borderColor: 'var(--yellow)' }} />can go next</span>
      <span className="lbl"><i style={{ borderColor: '#34312c' }} />exit</span>
      <span className="lbl"><i style={{ borderTopStyle: 'dashed', borderColor: 'var(--muted)' }} />consult</span>
      <span className="lbl"><i style={{ borderTopStyle: 'dotted', borderColor: 'var(--yellow-dim)' }} />prerequisite</span>
    </div>
  );
}
