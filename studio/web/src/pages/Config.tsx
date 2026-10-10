import { useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../api/useApi';
import { Shell } from '../components/Shell';
import { MovesTable } from '../components/MovesTable';
import { StepDetail } from '../components/StepDetail';
import { href, type Route } from '../router';

/**
 * The agent's config, step by step, read from agent.yaml as the loader sees it. Editing (prompt,
 * fields, exits, context) and saving through the loader's checks is phase 3 of the plan.
 */
export function Config({ route }: { route: Extract<Route, { page: 'config' }> }) {
  const agents = useApi(api.agents, 'agents');
  const id = route.agent ?? (agents.state === 'ok' ? agents.data[0]?.id : undefined);
  const agent = useApi(() => (id ? api.agent(id) : Promise.reject(new Error('no agent'))), id ?? '');
  const data = agent.state === 'ok' ? agent.data : undefined;
  const graph = data?.graph ?? undefined;
  const steps = graph?.nodes.filter((n) => !n.virtual) ?? [];
  const step = steps.find((n) => n.id === route.step) ?? steps[0];
  const [tab, setTab] = useState<'step' | 'moves'>('step');
  const pick = (to: string) => (window.location.hash = href({ page: 'config', agent: id, step: to }));

  return (
    <Shell
      route={route}
      agent={id}
      actions={<button className="btn btn-y" type="button" disabled title="Editing lands in phase 3">Save to agent.yaml</button>}
      body={
        <>
          <nav className="side" aria-label="Steps">
            <div className="side-head">
              <span className="lbl">Steps</span>
              <span style={{ flex: '1 1 auto' }} />
              {graph && <span className="mono dim" style={{ fontSize: 11 }}>entry {graph.entry}</span>}
            </div>
            {steps.map((n) => (
              <a key={n.id} href={href({ page: 'config', agent: id, step: n.id })} style={{ textDecoration: 'none', color: 'inherit' }}>
                <span className="side-item" aria-pressed={n.id === step?.id} style={{ flexDirection: 'row', alignItems: 'center' }}>
                  <span className="mono" style={{ fontSize: 13 }}>{n.id}</span>
                  <span style={{ flex: '1 1 auto' }} />
                  <span className="mono dim" style={{ fontSize: 10 }}>{n.roles.join(' ') || n.kind}</span>
                </span>
              </a>
            ))}
          </nav>
          <main className="main" style={{ padding: 24, gap: 20 }}>
            {agent.state === 'error' && <p className="error-box">{agent.message}</p>}
            {data?.status === 'error' && (
              <>
                <h1 style={{ margin: 0, fontSize: 26 }}>{data.id} does not compile</h1>
                <p className="error-box">{data.error}</p>
              </>
            )}
            {graph && step && (
              <>
                <div className="segs" role="tablist" aria-label="Config view">
                  <button type="button" role="tab" aria-selected={tab === 'step'} className={`seg ${tab === 'step' ? 'seg-on' : ''}`} onClick={() => setTab('step')}>Step</button>
                  <button type="button" role="tab" aria-selected={tab === 'moves'} className={`seg ${tab === 'moves' ? 'seg-on' : ''}`} onClick={() => setTab('moves')}>Moves</button>
                </div>
                {tab === 'step' ? (
                  <>
                    <div>
                      <div className="lbl" style={{ marginBottom: 6 }}>{data?.path}</div>
                      <h1 className="mono" style={{ margin: 0, fontSize: 30, fontWeight: 500 }}>{step.id}</h1>
                    </div>
                    <div style={{ border: '1px solid var(--yellow-dim)', padding: '10px 14px', fontSize: 14, color: 'var(--soft)' }}>
                      Read only for now. Editing the prompt, fields, exits and context, checked by the loader before saving, comes next.
                    </div>
                    <StepDetail graph={graph} node={step} />
                  </>
                ) : (
                  <>
                    <div style={{ fontSize: 14, color: 'var(--muted)' }}>Every move the engine allows, read from its compiled graph. Rows: from. Columns: to.</div>
                    <MovesTable graph={graph} focus={step.id} onPick={pick} />
                  </>
                )}
              </>
            )}
          </main>
        </>
      }
    />
  );
}
