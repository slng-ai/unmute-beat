import { useEffect, useState } from 'react';

/** The studio's pages. A hash route needs no server fallback: the engine serves one index.html. */
export type Route =
  | { page: 'boot' }
  | { page: 'runs'; agent?: string }
  | { page: 'live'; agent?: string; step?: string }
  | { page: 'config'; agent?: string; step?: string }
  | { page: 'metrics'; agent?: string };

export function parse(hash: string): Route {
  const [page = '', agent, step] = hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  switch (page) {
    case 'boot':
      return { page: 'boot' };
    case 'live':
      return { page: 'live', agent, step };
    case 'config':
      return { page: 'config', agent, step };
    case 'metrics':
      return { page: 'metrics', agent };
    default:
      return { page: 'runs', agent: page === 'runs' ? agent : undefined };
  }
}

export function href(route: Route): string {
  const parts: string[] = [route.page];
  if (route.page !== 'boot' && route.agent) parts.push(route.agent);
  if ((route.page === 'config' || route.page === 'live') && route.agent && route.step) parts.push(route.step);
  return '#/' + parts.map(encodeURIComponent).join('/');
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
