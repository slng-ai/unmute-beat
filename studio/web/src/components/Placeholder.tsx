/**
 * The shape of what is loading, in still blocks: never a blank wait, and never a word that flashes.
 * `cards` for a list of cards, `rows` for a table or lines of text. A screen reader hears "Loading <what>" once.
 */
export function Placeholder({ what, kind = 'cards', rows = 3 }: { what: string; kind?: 'cards' | 'rows'; rows?: number }) {
  return (
    <div className={`ph ph-${kind}`} role="status" aria-busy="true">
      <span className="sr">Loading {what}</span>
      <span className="ph-title" aria-hidden="true" />
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="ph-item" aria-hidden="true" />
      ))}
    </div>
  );
}
