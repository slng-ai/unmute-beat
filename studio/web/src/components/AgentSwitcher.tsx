import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { api } from '../api/client';
import { useApi } from '../api/useApi';
import { MOD, useKey } from '../keys';
import { go, type Route } from '../router';
import { Placeholder } from './Placeholder';

/** Where switching to `agent` goes from `route`: the same tab of the Agents page, or the same list filtered to it. */
function target(route: Route, agent: string): Route {
  switch (route.page) {
    case 'agents':
      return { page: 'agents', agent, tab: route.tab };
    case 'runs':
      return { page: 'runs', agent };
    case 'metrics':
      return { page: 'metrics', agent };
    default:
      return { page: 'agents', agent };
  }
}

/**
 * Every agent, to switch to by typing: Cmd/Ctrl+K on every page, or the agent button in the bar when `button`
 * is set. Arrows move, Enter opens the same tab on the other agent, Escape closes. A move that would lose an
 * unsaved edit is asked about first, by the router.
 */
export function AgentSwitcher({ route, agent, button }: { route: Route; agent?: string; button: boolean }) {
  const [open, setOpen] = useState(false);
  const [find, setFind] = useState('');
  const [at, setAt] = useState(0);
  const ref = useRef<HTMLDialogElement>(null);
  const agents = useApi(() => (open ? api.agents() : Promise.resolve([])), `switch-${open}`);
  useKey('k', () => setOpen(true));
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setFind('');
      setAt(0);
      dialog.showModal();
    }
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const needle = find.trim().toLowerCase();
  const list = agents.state === 'ok' ? agents.data.filter((a) => a.id.toLowerCase().includes(needle)) : [];
  const pick = (id: string) => {
    setOpen(false);
    if (id !== agent) go(target(route, id));
  };
  const keys = (event: KeyboardEvent) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setAt((now) => (list.length ? (now + (event.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length : 0));
    } else if (event.key === 'Enter' && list[at]) {
      event.preventDefault();
      pick(list[at].id);
    }
  };

  return (
    <>
      {button && (
        <button type="button" className="switch" aria-keyshortcuts="Meta+K Control+K" aria-label={`Switch agent, now ${agent ?? 'none'}`} onClick={() => setOpen(true)}>
          <span className="mono">{agent ?? 'Pick an agent'}</span>
          <span className="dim" aria-hidden="true">▾</span>
          <kbd className="switch-key" aria-hidden="true">{MOD}K</kbd>
        </button>
      )}
      <dialog ref={ref} className="dialog switcher" aria-label="Switch agent" onClose={() => setOpen(false)}>
        <div className="switcher-top">
          <input
            className="switcher-find mono"
            role="combobox"
            aria-expanded="true"
            aria-controls="switch-list"
            aria-activedescendant={list[at] ? `switch-${list[at].id}` : undefined}
            aria-label="Agent name"
            placeholder="Type an agent's name"
            autoFocus
            value={find}
            onChange={(event) => {
              setFind(event.target.value);
              setAt(0);
            }}
            onKeyDown={keys}
          />
          <kbd className="switch-key">esc</kbd>
        </div>
        <ul id="switch-list" className="switcher-list" role="listbox" aria-label="Agents">
          {agents.state !== 'ok' && (
            <li className="switcher-none">
              <Placeholder what="agents" kind="rows" rows={3} />
            </li>
          )}
          {agents.state === 'ok' && list.length === 0 && <li className="switcher-none">No agent matches.</li>}
          {list.map((a, i) => (
            <li key={a.id} id={`switch-${a.id}`} role="option" aria-selected={i === at} className="switcher-item" onClick={() => pick(a.id)} onMouseMove={() => setAt(i)}>
              <span className="switcher-name">{a.id}</span>
              <span className={`mono ${a.status === 'error' ? 'switcher-bad' : 'dim'}`}>{a.status === 'error' ? 'does not load' : `${a.steps} steps · compiled`}{a.id === agent ? ' · current' : ''}</span>
            </li>
          ))}
        </ul>
      </dialog>
    </>
  );
}
