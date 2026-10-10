import { lazy, Suspense, useState } from 'react';
import { api } from '../api/client';
import { useApi } from '../api/useApi';
import { CallBar } from '../components/CallBar';
import { FocusView } from '../components/FocusView';
import { PathStrip } from '../components/PathStrip';
import { Section } from '../components/Section';
import { Shell } from '../components/Shell';
import { StepDetail } from '../components/StepDetail';
import { href, type Route } from '../router';

type View = 'focus' | 'map';

/** The map pulls in React Flow and ELK, the page's two largest libraries, so it loads when first opened. */
const MapView = lazy(() => import('../components/MapView').then((m) => ({ default: m.MapView })));

/**
 * The live view, as someone testing an agent by voice uses it: the mic first, then the step the
 * call is in (Focus) or the whole agent (Map), the path over turns to go back from, and the
 * inspector. Until the engine runs calls (docs/studio/README.md, phase 2) there is no run, so the
 * focus is the step picked, the path is empty and the run sections say what they will show.
 */
export function Live({ route }: { route: Extract<Route, { page: 'live' }> }) {
  const agents = useApi(api.agents, 'agents');
  const id = route.agent ?? (agents.state === 'ok' ? agents.data[0]?.id : undefined);
  const agent = useApi(() => (id ? api.agent(id) : Promise.reject(new Error('no agent'))), id ?? '');
  const [view, setView] = useState<View>('focus');
  const data = agent.state === 'ok' ? agent.data : undefined;
  const graph = data?.graph ?? undefined;
  const focus = route.step && graph?.nodes.some((n) => n.id === route.step) ? route.step : graph?.entry;
  const node = graph?.nodes.find((n) => n.id === focus);
  const pick = (step: string) => {
    if (step !== '$end') window.location.hash = href({ page: 'live', agent: id, step });
  };

  return (
    <Shell
      route={route}
      agent={id}
      who={null}
      body={
        <div className="live">
          <CallBar canCall={false} />
          <div className="live-body">
            <main className="main">
              <div className="toolbar">
                <div className="segs" role="tablist" aria-label="Graph view">
                  {(['focus', 'map'] as const).map((v) => (
                    <button key={v} type="button" role="tab" aria-selected={view === v} className={`seg ${view === v ? 'seg-on' : ''}`} onClick={() => setView(v)}>
                      {v}
                    </button>
                  ))}
                </div>
                <span className="mono dim" style={{ fontSize: 12 }}>
                  {view === 'focus' ? 'the step in focus, how callers get here, every move from here' : 'the whole agent · steps that hand over to each other drawn as one group'}
                </span>
                <span style={{ flex: '1 1 auto' }} />
                <span className="mono" style={{ fontSize: 11, color: 'var(--faint)' }}>moves from the engine's own graph</span>
              </div>
              <div className={`stage ${view === 'focus' ? 'dots' : ''}`}>
                {agent.state === 'loading' && <p className="empty">Loading the graph…</p>}
                {agent.state === 'error' && <p className="error-box">{agent.message}</p>}
                {data?.status === 'error' && <p className="error-box">{data.error}</p>}
                {graph && focus && view === 'focus' && <FocusView graph={graph} step={focus} path={[]} onPick={pick} />}
                {graph && view === 'map' && (
                  <Suspense fallback={<p className="empty">Loading the map…</p>}>
                    <MapView graph={graph} focus={focus} onPick={pick} />
                  </Suspense>
                )}
              </div>
              <PathStrip turns={[]} onPick={() => {}} onFork={() => {}} />
            </main>
            <aside className="inspector" aria-label="Inspector">
              <Section title="Conversation" note={<span className="tag">no run</span>}>
                <p className="empty">What you and the bot say, line by line, and whether each line was heard.</p>
              </Section>
              <Section title="State" note={<span className="tag">no run</span>}>
                <p className="empty">The state after each turn, with what changed marked. Saved per turn, so going back is instant.</p>
              </Section>
              <Section title="Model" note={<span className="tag">idle</span>} open={false}>
                <p className="empty">The step's request as it streams, and the outputs it may choose.</p>
              </Section>
              <Section title={<>Step <span className="mono" style={{ textTransform: 'none', letterSpacing: 0 }}>{focus}</span></>} note={node && <span className="tag">{node.kind}</span>} open={false}>
                {graph && node ? <StepDetail graph={graph} node={node} /> : <p className="empty">Pick a step.</p>}
                {id && <a className="btn btn-y" href={href({ page: 'config', agent: id, step: focus })} style={{ alignSelf: 'flex-start' }}>Edit step</a>}
              </Section>
            </aside>
          </div>
        </div>
      }
    />
  );
}
