import { useEffect, useRef, useState } from 'react';

/**
 * The studio's places. Three at the top (Agents, Runs, Metrics), and two that open from them: one
 * run, followed live or read back, and going back to one of its turns. A hash route needs no server
 * fallback: the engine serves one index.html.
 */
export type AgentTab = 'build' | 'operations' | 'facts' | 'graph' | 'runs' | 'metrics' | 'config';

export type Route =
  | { page: 'boot' }
  | { page: 'agents'; agent?: string; tab?: AgentTab; step?: string; run?: string }
  | { page: 'runs'; agent?: string }
  | { page: 'run'; run: string }
  | { page: 'back'; run: string; turn?: number }
  | { page: 'metrics'; agent?: string; run?: string };

/** An agent's tabs, in the order the page shows them, with their names. */
export const AGENT_TABS: { tab: AgentTab; label: string }[] = [
  { tab: 'build', label: 'Build and try' },
  { tab: 'operations', label: 'Operations' },
  { tab: 'facts', label: 'Facts' },
  { tab: 'graph', label: 'Graph' },
  { tab: 'runs', label: 'Its runs' },
  { tab: 'metrics', label: 'Its metrics' },
  { tab: 'config', label: 'Full config' },
];

const TABS = AGENT_TABS.map((t) => t.tab);

export function parse(hash: string): Route {
  const [path = '', search = ''] = hash.replace(/^#\/?/, '').split('?');
  const query = new URLSearchParams(search);
  const [page = '', a, b, c] = path.split('/').filter(Boolean).map(decodeURIComponent);
  switch (page) {
    case 'boot':
      return { page: 'boot' };
    case 'agents':
    case 'live':
    case 'config': {
      const tab = page === 'live' ? 'graph' : page === 'config' ? 'build' : TABS.includes(b as AgentTab) ? (b as AgentTab) : undefined;
      const step = page === 'agents' ? c : b;
      return { page: 'agents', agent: a, tab, step, run: query.get('run') ?? undefined };
    }
    case 'run':
      return a ? { page: 'run', run: a } : { page: 'runs' };
    case 'back':
      return a ? { page: 'back', run: a, turn: b ? Number(b) : undefined } : { page: 'runs' };
    case 'metrics':
      return { page: 'metrics', agent: query.get('agent') ?? a ?? undefined, run: query.get('run') ?? undefined };
    case 'runs':
      return a ? { page: 'run', run: a } : { page: 'runs', agent: query.get('agent') ?? undefined };
    default:
      return { page: 'agents' };
  }
}

export function href(route: Route): string {
  const enc = encodeURIComponent;
  switch (route.page) {
    case 'boot':
      return '#/boot';
    case 'agents': {
      const parts = ['agents', route.agent, route.agent && (route.tab ?? (route.step ? 'build' : undefined)), route.agent && route.step];
      const path = parts.filter(Boolean).map((p) => enc(p as string)).join('/');
      return `#/${path}${route.run ? `?run=${enc(route.run)}` : ''}`;
    }
    case 'runs':
      return `#/runs${route.agent ? `?agent=${enc(route.agent)}` : ''}`;
    case 'run':
      return `#/run/${enc(route.run)}`;
    case 'back':
      return `#/back/${enc(route.run)}${route.turn !== undefined ? `/${route.turn}` : ''}`;
    case 'metrics': {
      const query = new URLSearchParams();
      if (route.agent) query.set('agent', route.agent);
      if (route.run) query.set('run', route.run);
      const q = query.toString();
      return `#/metrics${q ? `?${q}` : ''}`;
    }
  }
}

export function go(route: Route): void {
  window.location.hash = href(route);
}

/** A page's name for the window, the tab and the Dock: the place first, the studio last. */
export function title(route: Route): string {
  const short = (run: string) => `Run ${run.slice(0, 8)}`;
  const parts = (() => {
    switch (route.page) {
      case 'boot':
        return ['Starting'];
      case 'agents':
        return route.agent ? [route.agent, AGENT_TABS.find((t) => t.tab === (route.tab ?? 'build'))?.label ?? ''] : ['Agents'];
      case 'runs':
        return ['Runs'];
      case 'run':
        return [short(route.run)];
      case 'back':
        return [route.turn !== undefined ? `Go back to T${route.turn}` : 'Go back', short(route.run)];
      case 'metrics':
        return route.agent ? [route.agent, 'Metrics'] : ['Metrics'];
    }
  })();
  return [...parts, 'SLNG Studio'].join(' · ');
}

/** Whether going to a route would lose an edit that is not saved; null when nothing is unsaved. */
type Loses = ((next: Route) => boolean) | null;

const guards = new Set<{ current: Loses }>();

/**
 * An editor's unsaved edit, kept from being lost by accident: the router asks before a move that would lose
 * it (`useRoute`'s `leaving`), and the browser before the window closes or reloads. `loses` says which moves
 * lose it: a tab switch keeps the Agents page's draft, but not a fact's.
 */
export function useUnsaved(loses: Loses): void {
  const ref = useRef<Loses>(loses);
  useEffect(() => {
    ref.current = loses;
  });
  useEffect(() => {
    guards.add(ref);
    return () => {
      guards.delete(ref);
    };
  }, []);
  const unsaved = loses !== null;
  useEffect(() => {
    if (!unsaved) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [unsaved]);
}

/**
 * The route the address shows. A move that would lose an unsaved edit is held: the address goes back, and
 * `leaving` is the route asked for until the person stays (`stay`) or lets the edit go (`leave`).
 */
export function useRoute(): { route: Route; leaving: Route | null; stay: () => void; leave: () => void } {
  const [route, setRoute] = useState(() => parse(window.location.hash));
  const [held, setHeld] = useState<string | null>(null);
  const shown = useRef(window.location.hash);
  useEffect(() => {
    const onChange = () => {
      const hash = window.location.hash;
      const next = parse(hash);
      if (hash !== shown.current && [...guards].some((guard) => guard.current?.(next))) {
        window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}${shown.current}`);
        setHeld(hash);
        return;
      }
      shown.current = hash;
      setRoute(next);
    };
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return {
    route,
    leaving: held === null ? null : parse(held),
    stay: () => setHeld(null),
    leave: () => {
      if (held === null) return;
      shown.current = held;
      setHeld(null);
      window.location.hash = held;
    },
  };
}
