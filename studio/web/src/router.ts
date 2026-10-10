import { useEffect, useState } from 'react';

/**
 * The studio's places. Three at the top (Agents, Runs, Metrics), and two that open from them: one
 * run, followed live or read back, and going back to one of its turns. A hash route needs no server
 * fallback: the engine serves one index.html.
 */
export type AgentTab = 'build' | 'operations' | 'graph' | 'runs' | 'metrics' | 'config';

export type Route =
  | { page: 'boot' }
  | { page: 'agents'; agent?: string; tab?: AgentTab; step?: string; run?: string }
  | { page: 'runs'; agent?: string }
  | { page: 'run'; run: string }
  | { page: 'back'; run: string; turn?: number }
  | { page: 'metrics'; agent?: string; run?: string };

const TABS: AgentTab[] = ['build', 'operations', 'graph', 'runs', 'metrics', 'config'];

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

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parse(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return route;
}
