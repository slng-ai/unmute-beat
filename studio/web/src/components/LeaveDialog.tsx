import { useEffect, useRef } from 'react';
import { title, type Route } from '../router';

/**
 * Asked before a move that would lose an unsaved edit: keep editing, or let the edit go and open `to`.
 * A native dialog, so focus stays inside it and Escape keeps editing.
 */
export function LeaveDialog({ to, onStay, onLeave }: { to: Route | null; onStay: () => void; onLeave: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (to && !dialog.open) dialog.showModal();
    if (!to && dialog.open) dialog.close();
  }, [to]);
  const page = to ? title(to).replace(/ · SLNG Studio$/, '') : '';
  const where = to?.page === 'agents' && to.step ? `${to.step} (${page})` : page;
  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby="leave-h"
      onCancel={(event) => {
        event.preventDefault();
        onStay();
      }}
    >
      <h2 id="leave-h" className="dialog-h">Leave without saving?</h2>
      <p className="dialog-p">Your edit is not saved. It is lost if you open {where}.</p>
      <div className="dialog-actions">
        <button type="button" className="btn btn-q" autoFocus onClick={onStay}>Keep editing</button>
        <button type="button" className="btn" onClick={onLeave}>Discard and open</button>
      </div>
    </dialog>
  );
}
