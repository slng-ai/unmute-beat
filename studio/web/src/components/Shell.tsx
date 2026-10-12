import type { ReactNode } from 'react';
import { href, type Route } from '../router';
import { AgentSwitcher } from './AgentSwitcher';

type Who = 'caller' | 'engine' | 'bot' | null;

/**
 * The top bar every page shares: the mark, the three places (Agents, Runs, Metrics), the agent switch
 * (`switcher`; Cmd/Ctrl+K opens it on every page), the page's own cells, and who is active. At phone width
 * it is one row: the page's cells scroll sideways and its actions sit in a bar at the bottom.
 */
export function Shell({ route, agent, who, children, actions, body, switcher = false }: {
  route: Route;
  agent?: string;
  who?: Who;
  children?: ReactNode;
  actions?: ReactNode;
  body: ReactNode;
  switcher?: boolean;
}) {
  const pages = [
    { page: 'agents', label: 'Agents', to: href({ page: 'agents', agent }) },
    { page: 'runs', label: 'Runs', to: href({ page: 'runs' }) },
    { page: 'metrics', label: 'Metrics', to: href({ page: 'metrics' }) },
  ] as const;
  const here = route.page === 'run' || route.page === 'back' ? 'runs' : route.page;
  return (
    <div className="app" data-actions={actions ? 'true' : undefined}>
      <header className="bar">
        <a className="brand" href={href({ page: 'agents', agent })} aria-label="SLNG studio">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name" aria-hidden="true">SLNG</span>
          <span className="brand-sub mono dim" aria-hidden="true">/ studio</span>
        </a>
        <nav className="nav" aria-label="Studio">
          {pages.map((p) => (
            <a key={p.page} href={p.to} aria-current={here === p.page ? 'page' : undefined}>
              {p.label}
            </a>
          ))}
        </nav>
        <div className="bar-fill">
          <AgentSwitcher route={route} agent={agent} button={switcher} />
          {children}
        </div>
        {who !== undefined && <WhoIsActive who={who} />}
        {actions && <div className="bar-cell">{actions}</div>}
      </header>
      <div className="body">{body}</div>
    </div>
  );
}

function WhoIsActive({ who }: { who: Who }) {
  const cells = [
    { id: 'caller', label: 'CALLER' },
    { id: 'engine', label: 'ENGINE' },
    { id: 'bot', label: 'BOT' },
  ] as const;
  return (
    <div className="who" role="status" aria-label="Who is active">
      {cells.map((c) => (
        <span key={c.id} className={who === c.id ? `on-${c.id}` : undefined}>
          {c.label}
        </span>
      ))}
    </div>
  );
}
