import { api } from '../api/client';
import { useApi } from '../api/useApi';
import { Graph, Legend } from '../components/Graph';
import { Section } from '../components/Section';
import { Shell } from '../components/Shell';
import { StepDetail } from '../components/StepDetail';
import { href, type Route } from '../router';

/**
 * The live view: the graph on the left, the inspector on the right. Until the engine runs calls
 * (docs/studio/README.md, phase 2) there is no run to follow, so the graph is the agent's as
 * declared and the run sections say what they will show.
 */
export function Live({ route }: { route: Extract<Route, { page: 'live' }> }) {
  const agents = useApi(api.agents, 'agents');
  const id = route.agent ?? (agents.state === 'ok' ? agents.data[0]?.id : undefined);
  const agent = useApi(() => (id ? api.agent(id) : Promise.reject(new Error('no agent'))), id ?? '');
  const data = agent.state === 'ok' ? agent.data : undefined;
  const graph = data?.graph ?? undefined;
  const step = graph?.nodes.find((n) => n.id === (route.step ?? graph.entry));

  return (
    <Shell
      route={route}
      agent={id}
      who={null}
      actions={<button className="btn btn-q" type="button" disabled>Break at next turn</button>}
      body={
        <>
          <main className="main">
            <Legend />
            <div className="graph-wrap dots">
              {agent.state === 'loading' && <p className="empty">Loading the graph…</p>}
              {agent.state === 'error' && <p className="error-box">{agent.message}</p>}
              {data?.status === 'error' && <p className="error-box">{data.error}</p>}
              {graph && <Graph graph={graph} selected={step?.id} onSelect={(next) => (window.location.hash = href({ page: 'live', agent: id, step: next }))} />}
            </div>
            <section style={{ borderTop: '1px solid var(--rule-soft)', padding: '12px 20px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
              <span className="lbl">Turns</span>
              <span className="empty">No run. Each caller turn appears here, and picking one shows the state after it.</span>
            </section>
          </main>
          <aside className="inspector" aria-label="Inspector">
            <Section title="Conversation" note={<span className="tag">no run</span>}>
              <p className="empty">What the caller and the bot say, line by line, with how each line was heard.</p>
            </Section>
            <Section title="State" note={<span className="tag">no run</span>}>
              <p className="empty">The application state after each commit, with what changed marked.</p>
            </Section>
            <Section title="Model" note={<span className="tag">idle</span>} open={false}>
              <p className="empty">The active step's request as it streams, and the outputs it may choose.</p>
            </Section>
            <Section title={<>Step <span className="mono" style={{ textTransform: 'none', letterSpacing: 0 }}>{step?.id}</span></>} note={step && <span className="tag">{step.kind}</span>}>
              {graph && step ? <StepDetail graph={graph} node={step} /> : <p className="empty">Pick a step in the graph.</p>}
              {id && <a className="btn btn-y" href={href({ page: 'config', agent: id, step: step?.id })} style={{ alignSelf: 'flex-start' }}>Edit step</a>}
            </Section>
          </aside>
        </>
      }
    />
  );
}
