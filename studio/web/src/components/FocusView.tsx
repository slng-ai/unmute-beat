import type { Graph } from '../api/types';
import { kindLabel, MOVE, movesFrom, movesInto, nodeById } from '../graph/moves';

export interface Arrival {
  step: string;
  turn: number;
  how: string;
}

/**
 * Option B: the step in focus, where the caller came from, and every move from here, back moves
 * included. `path` is the run's own route; with no run, the arrivals the graph allows stand in.
 * `status` is what the engine is doing there right now (listening, thinking, speaking), if live.
 */
export function FocusView({ graph, step, path, status, onPick }: {
  graph: Graph;
  step: string;
  path: Arrival[];
  status?: string;
  onPick: (step: string) => void;
}) {
  const nodes = nodeById(graph);
  const node = nodes.get(step);
  if (!node) return <p className="empty">No step {step} in this agent.</p>;
  const sections = movesFrom(graph, step);
  const visited = new Map(path.map((p) => [p.step, p.turn]));
  const into = movesInto(graph, step);
  return (
    <div className="focus">
      <div className="focus-col">
        <span className="lbl">{path.length ? 'Came from' : 'Can arrive from'}</span>
        {path.length > 0
          ? [...path].reverse().map((p, i) => (
              <button key={`${p.step}-${p.turn}`} type="button" className={`card ${i === 0 ? 'card-seen' : 'card-old'}`} onClick={() => onPick(p.step)}>
                <span className="card-h">T{p.turn} · {p.how}</span>
                <span className="card-t">{p.step}</span>
              </button>
            ))
          : into.map((e) => (
              <button key={`${e.source}-${e.kind}`} type="button" className="card card-old" onClick={() => onPick(e.source)}>
                <span className="card-h">{MOVE[e.kind].label}</span>
                <span className="card-t">{e.source}</span>
              </button>
            ))}
        {!path.length && !into.length && <span className="empty">The call starts here.</span>}
      </div>

      <div className="focus-main">
        <div className="focus-status">{status ?? kindLabel(node)}</div>
        <div className="focus-body">
          <span className="mono" style={{ fontSize: 26, fontWeight: 500 }}>{node.id}</span>
          {node.prompt && <span className="mono dim" style={{ fontSize: 11 }}>{node.prompt}</span>}
          {node.fields.length > 0 && (
            <div className="chips">
              {node.fields.map((f) => <span key={f} className="chip">{f}</span>)}
            </div>
          )}
          {node.operations.length > 0 && (
            <div className="mono" style={{ fontSize: 11, color: 'var(--yellow)', borderTop: '1px dashed var(--rule-strong)', paddingTop: 10 }}>
              {node.operations.join(' · ')}
              {node.opens_with ? ` · opens with ${node.opens_with}` : ''}
            </div>
          )}
        </div>
      </div>

      <div className="focus-col">
        <span className="lbl">Can go next</span>
        {node.operations.length > 0 && (
          <div>
            <div className="grp">Stays here</div>
            {node.operations.map((op) => (
              <div key={op} className="go go-stay">
                ✓ {op}
                <small>on a yes</small>
              </div>
            ))}
          </div>
        )}
        {sections.map((s) => (
          <div key={s.name}>
            <div className="grp">{s.name}</div>
            {s.moves.map((e) => {
              const back = visited.has(e.target) || e.kind === 'return' || e.kind === 'handback';
              const mark = back && e.kind === 'exit' ? '↩' : MOVE[e.kind].mark;
              const why = visited.has(e.target) ? `was here T${visited.get(e.target)}` : e.purpose || MOVE[e.kind].label;
              const target = e.target === '$end' ? 'goodbye' : e.target;
              return (
                <button
                  key={`${e.kind}-${e.target}`}
                  type="button"
                  className={`go ${e.kind === 'consult' ? 'go-consult' : ''}`}
                  disabled={e.target === '$end'}
                  onClick={() => onPick(e.target)}
                  title={e.purpose}
                >
                  {mark} {target}
                  <small>{why}</small>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
