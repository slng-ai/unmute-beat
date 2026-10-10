import { api } from '../api/client';
import type { Agent } from '../api/types';
import { useApi } from '../api/useApi';
import { Shell } from '../components/Shell';
import { href, type Route } from '../router';

export function Runs({ route }: { route: Extract<Route, { page: 'runs' }> }) {
  const agents = useApi(api.agents, 'agents');
  const runs = useApi(api.runs, `runs-${route.agent ?? ''}`);
  const list = agents.state === 'ok' ? agents.data : [];
  const agent = list.find((a) => a.id === route.agent) ?? list[0];

  return (
    <Shell
      route={route}
      agent={agent?.id}
      body={
        <>
          <nav className="side" aria-label="Agents">
            <div className="side-head">
              <span className="lbl">Agents in this repo</span>
              <span style={{ flex: '1 1 auto' }} />
              <span className="mono dim" style={{ fontSize: 11 }}>{list.length}</span>
            </div>
            {agents.state === 'loading' && <p className="empty" style={{ padding: 16 }}>Reading every agent.yaml…</p>}
            {agents.state === 'error' && <p className="error-box" style={{ margin: 16 }}>{agents.message}</p>}
            {list.map((a) => (
              <a key={a.id} href={href({ page: 'runs', agent: a.id })} style={{ textDecoration: 'none', color: 'inherit' }}>
                <span className="side-item" aria-pressed={a.id === agent?.id}>
                  <span style={{ display: 'flex', width: '100%' }}>
                    <span className="mono" style={{ fontSize: 14, fontWeight: 500 }}>{a.id}</span>
                    <span style={{ flex: '1 1 auto' }} />
                    <span className="mono" style={{ fontSize: 10, color: a.status === 'error' ? 'var(--bad)' : 'var(--ok)' }}>
                      {a.status === 'error' ? '✕ error' : '■ compiled'}
                    </span>
                  </span>
                  <span className="mono dim" style={{ fontSize: 11 }}>{a.path}</span>
                </span>
              </a>
            ))}
            <p className="empty" style={{ padding: 16, color: 'var(--faint)' }}>
              Found by scanning use-cases/*/*/agent.yaml. Add a folder with an agent.yaml and it shows up here.
            </p>
          </nav>
          <main className="main">{agent ? <AgentHeader agent={agent} /> : agents.state === 'ok' && <p className="empty" style={{ padding: 24 }}>No agent.yaml found.</p>}
            <div style={{ padding: '14px 24px', display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="lbl">Runs</span>
            </div>
            <div style={{ borderTop: '1px solid var(--rule-soft)', padding: '32px 24px' }}>
              {runs.state === 'loading' && <p className="empty">Loading runs…</p>}
              {runs.state === 'error' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <div style={{ fontSize: 18, fontWeight: 600 }}>{runs.status === 501 ? 'Runs are not recorded yet.' : 'Runs could not be loaded.'}</div>
                  <p className="empty" style={{ margin: 0 }}>
                    {runs.status === 501
                      ? 'The engine reads agents and draws their graphs today. Recording runs is the next phase: docs/studio/README.md.'
                      : runs.message}
                  </p>
                </div>
              )}
              {runs.state === 'ok' && runs.data.length === 0 && <p className="empty">No runs yet. Start a text run, or a call.</p>}
            </div>
          </main>
        </>
      }
    />
  );
}

function AgentHeader({ agent }: { agent: Agent }) {
  return (
    <div style={{ padding: '24px 24px 20px', borderBottom: '1px solid var(--rule-soft)', display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'flex-end' }}>
        <div style={{ flex: '1 1 360px', minWidth: 0 }}>
          <div className="lbl" style={{ marginBottom: 8 }}>{agent.path}</div>
          <h1 style={{ margin: 0, fontSize: 36, lineHeight: 1.1, fontWeight: 800 }}>{agent.id}</h1>
          {agent.status === 'compiled' && (
            <div className="mono" style={{ fontSize: 12, color: 'var(--muted)', marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 16 }}>
              <span>app_version {agent.app_version || '—'}</span>
              <span>{agent.steps} steps · {agent.operations} operations</span>
              <span>entry {agent.entry}</span>
              <span>{agent.model}</span>
            </div>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <a className="btn btn-q" href={href({ page: 'config', agent: agent.id })}>Open config</a>
          <a className="btn" href={href({ page: 'live', agent: agent.id })}>Open graph</a>
          <button className="btn btn-y" type="button" disabled title="The engine does not run calls in this build">Start a call</button>
        </div>
      </div>
      {agent.status === 'error' && <div className="error-box">{agent.error}</div>}
    </div>
  );
}
