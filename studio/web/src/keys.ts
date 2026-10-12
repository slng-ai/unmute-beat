import { useEffect, useRef } from 'react';

/** The modifier as this machine writes it: ⌘ on a Mac, Ctrl elsewhere. */
export const MOD = /Mac|iPhone|iPad/.test(navigator.userAgent) ? '⌘' : 'Ctrl+';

/**
 * Run `act` on Cmd+`key` (Mac) or Ctrl+`key` (elsewhere), in place of the browser's own use of the key.
 * Nothing is listened for while `act` is null, so the browser keeps the key where a page has no use for it.
 */
export function useKey(key: string, act: (() => void) | null): void {
  const ref = useRef(act);
  useEffect(() => {
    ref.current = act;
  });
  const on = act !== null;
  useEffect(() => {
    if (!on) return;
    const down = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === key) {
        event.preventDefault();
        ref.current?.();
      }
    };
    window.addEventListener('keydown', down);
    return () => window.removeEventListener('keydown', down);
  }, [key, on]);
}
