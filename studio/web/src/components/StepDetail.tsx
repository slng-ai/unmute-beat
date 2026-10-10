import type { Graph, GraphNode } from '../api/types';

/** Everything the manifest says about one step, and where it can send the caller. */
export function StepDetail({ graph, node }: { graph: Graph; node: GraphNode }) {
  if (node.virtual) {
    return <p className="empty">Every conversation step can end the call with a goodbye. Code says the recaps, anything left undone, then the goodbye line.</p>;
  }
  const rows: [string, string][] = (
    [
      ['kind', node.kind + (node.roles.length ? ` · ${node.roles.join(', ')}` : '')],
      ['agent', node.agent],
      ['prompt', node.prompt],
      ['fields', node.fields.join(' · ')],
      ['facts', node.facts === null ? 'flow default' : node.facts.join(' · ') || 'none'],
      ['choices', node.choices.join(' · ')],
      ['offers', node.offers.join(' · ')],
      ['operations', node.operations.join(' · ')],
      ['opens_with', node.opens_with],
      ['requires', node.requires.join(' · ')],
      ['capabilities', node.capabilities.join(' · ')],
    ] as [string, string][]
  ).filter(([, v]) => v);
  const out = graph.edges.filter((e) => e.source === node.id && e.kind !== 'goodbye');
  return (
    <>
      <div className="kv">
        {rows.map(([k, v]) => (
          <div key={k}>
            <span>{k}</span>
            <span>{v}</span>
          </div>
        ))}
      </div>
      <span className="lbl">Can go to</span>
      {out.length === 0 && <p className="empty">Nowhere but a goodbye{node.roles.includes('consulted') ? ', or back to the step that consulted it' : ''}.</p>}
      {out.map((e) => (
        <div key={`${e.kind}-${e.target}`} style={{ display: 'grid', gridTemplateColumns: '130px minmax(0,1fr)', gap: 10, padding: '6px 0', borderBottom: '1px dashed var(--rule-soft)' }}>
          <span className="mono" style={{ fontSize: 12, color: 'var(--yellow)' }}>→ {e.target}</span>
          <span style={{ fontSize: 13, color: 'var(--muted)' }}>
            <span className="tag" style={{ marginRight: 6 }}>{e.kind}</span>
            {e.purpose}
          </span>
        </div>
      ))}
    </>
  );
}
