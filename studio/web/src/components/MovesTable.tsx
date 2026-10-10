import type { EdgeKind, Graph } from '../api/types';
import { MOVE } from '../graph/moves';

const SYMBOL: Partial<Record<EdgeKind, string>> = { exit: '→', prerequisite: '◇', return: '↩', handback: '↩', consult: '⋯', goodbye: '■' };

/** Option D: every move the engine allows, rows from and columns to, whatever the agent's size. */
export function MovesTable({ graph, focus, onPick }: { graph: Graph; focus?: string; onPick: (step: string) => void }) {
  const steps = graph.nodes.filter((n) => !n.virtual).map((n) => n.id);
  const cols = [...steps, ...(graph.nodes.some((n) => n.virtual) ? ['$end'] : [])];
  const kind = new Map(graph.edges.map((e) => [`${e.source}>${e.target}`, e.kind]));
  return (
    <div className="moves">
      <table>
        <thead>
          <tr>
            <th className="lbl">from ↓ · to →</th>
            {cols.map((c) => <th key={c} className="mono">{c === '$end' ? 'end' : c}</th>)}
          </tr>
        </thead>
        <tbody>
          {steps.map((row) => (
            <tr key={row} className={row === focus ? 'moves-on' : undefined}>
              <th>
                <button type="button" className="mono" onClick={() => onPick(row)}>{row}</button>
              </th>
              {cols.map((col) => {
                const k = kind.get(`${row}>${col}`);
                return (
                  <td key={col} title={k ? `${row} → ${col}: ${MOVE[k].label}` : undefined} className={row === col ? 'moves-self' : undefined}>
                    {row === col ? '·' : k ? (SYMBOL[k] ?? MOVE[k].mark) : ''}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="moves-key mono">
        <span>→ hand over</span><span>◇ needs first</span><span>↩ goes back</span><span>⋯ consult</span><span>⇢ goes on by itself</span><span>■ goodbye</span>
      </div>
    </div>
  );
}
