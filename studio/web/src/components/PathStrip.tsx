export interface TurnMark {
  turn: number;
  step: string;
  live?: boolean;
}

const COL = 120;
const ROW = 32;
const LEFT = 120;
const TOP = 34;

/**
 * Option E: the call's path over turns. One row per step it has been in, one column per turn,
 * so a move back is the line climbing. Picking a turn shows the facts after it; `onFork` starts
 * a new run from that turn's saved facts, so the person testing can keep talking from there.
 */
export function PathStrip({ turns, selected, onPick, onFork }: {
  turns: TurnMark[];
  selected?: number;
  onPick: (turn: number) => void;
  onFork: (turn: number) => void;
}) {
  if (!turns.length) {
    return (
      <section className="path" aria-label="Path over turns">
        <div className="path-head">
          <span className="lbl">Path over turns</span>
          <span className="empty">No run yet. Each turn shows up here, and you can go back to any of them and keep talking.</span>
        </div>
      </section>
    );
  }
  const rows = [...new Set(turns.map((t) => t.step))];
  const y = (step: string) => TOP + rows.indexOf(step) * ROW + ROW / 2;
  const x = (i: number) => LEFT + 40 + i * COL;
  const d = turns
    .map((t, i) => {
      if (i === 0) return `M ${x(0)} ${y(t.step)}`;
      const px = x(i - 1);
      const py = y((turns[i - 1] as TurnMark).step);
      const mid = (px + x(i)) / 2;
      return `C ${mid} ${py}, ${mid} ${y(t.step)}, ${x(i)} ${y(t.step)}`;
    })
    .join(' ');
  const width = x(turns.length - 1) + 80;
  const height = TOP + rows.length * ROW + 24;
  const picked = turns.find((t) => t.turn === selected);
  return (
    <section className="path" aria-label="Path over turns">
      <div className="path-head">
        <span className="lbl">Path over turns</span>
        <span className="empty">Pick a turn to look at it. Every turn is saved, so you can go back to it.</span>
        <span style={{ flex: '1 1 auto' }} />
        {picked && !picked.live && (
          <button type="button" className="btn btn-y" onClick={() => onFork(picked.turn)}>
            Go back to T{picked.turn} and keep talking
          </button>
        )}
      </div>
      <div className="path-box">
        <svg width={width} height={height} aria-hidden="true">
          {rows.map((r) => (
            <g key={r}>
              <text x={12} y={y(r) + 4} className="path-row">{r}</text>
              <line x1={LEFT} x2={width} y1={y(r)} y2={y(r)} stroke="#211f1b" />
            </g>
          ))}
          <path d={d} fill="none" stroke="#d9d7d1" strokeWidth={3} />
        </svg>
        {turns.map((t, i) => (
          <button
            key={t.turn}
            type="button"
            className={`path-col ${t.turn === selected ? 'path-col-on' : ''}`}
            style={{ left: x(i) - COL / 2, width: COL }}
            onClick={() => onPick(t.turn)}
            aria-label={`Turn ${t.turn}, ${t.step}`}
          >
            <span className="path-t" style={{ color: t.live ? 'var(--yellow)' : undefined }}>T{t.turn}{t.live ? ' · live' : ''}</span>
            <span className={`path-dot ${t.live ? 'pulse' : ''}`} style={{ top: y(t.step), background: t.live ? 'var(--yellow)' : undefined }} />
          </button>
        ))}
      </div>
    </section>
  );
}
