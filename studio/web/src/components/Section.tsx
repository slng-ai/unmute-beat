import { useState, type ReactNode } from 'react';

/** One inspector section: a header that opens and closes it, and a note on the right. */
export function Section({ title, note, open: initial = true, children }: {
  title: ReactNode;
  note?: ReactNode;
  open?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(initial);
  return (
    <div className="sec">
      <button className="sec-h" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="dim" style={{ width: 10 }}>{open ? '▾' : '▸'}</span>
        {title}
        <span style={{ flex: '1 1 auto' }} />
        {note}
      </button>
      {open && <div className="sec-b">{children}</div>}
    </div>
  );
}
