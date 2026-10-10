import type { ReactNode } from 'react';
import { href, type Route } from '../router';

type Who = 'caller' | 'engine' | 'bot' | null;

/** The top bar every page shares: the mark, the three pages, the page's own cells, and who is active. */
export function Shell({ route, agent, who, children, actions, body }: {
  route: Route;
  agent?: string;
  who?: Who;
  children?: ReactNode;
  actions?: ReactNode;
  body: ReactNode;
}) {
  const pages = [
    { page: 'runs', label: 'Runs' },
    { page: 'live', label: 'Live' },
    { page: 'config', label: 'Config' },
  ] as const;
  return (
    <div className="app">
      <header className="bar">
        <a className="brand" href={href({ page: 'runs', agent })}>
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">SLNG</span>
          <span className="mono dim" style={{ fontSize: 13 }}>/ studio</span>
        </a>
        <nav className="nav" aria-label="Studio">
          {pages.map((p) => (
            <a key={p.page} href={href({ page: p.page, agent })} aria-current={route.page === p.page ? 'page' : undefined}>
              {p.label}
            </a>
          ))}
        </nav>
        <div className="bar-fill">{children}</div>
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
