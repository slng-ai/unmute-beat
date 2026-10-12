import { api } from '../api/client';
import { useApi } from '../api/useApi';
import { useSchemas } from '../api/useSchemas';
import { PathStrip } from '../components/PathStrip';
import { Section } from '../components/Section';
import { Shell } from '../components/Shell';
import { IntentsPanel, ModelPanel, StatePanel, statusTag, Transcript } from '../live/panels';
import { useRun } from '../live/useRun';
import { href, go, type Route } from '../router';
import { Placeholder } from '../components/Placeholder';

/**
 * One run, whole: what was said and heard, each model request as it streamed and what it decided,
 * the facts after every change, each business action and its receipt, and the path over turns to go
 * back from. Live while the run goes on, read back from the store once it is over: the same events.
 */
export function RunView({ route }: { route: Extract<Route, { page: 'run' }> }) {
  const view = useRun(route.run);
  const detail = useApi(() => api.run(route.run), `run-${route.run}-${view.status}-${view.turns.length}`);
  const saved = detail.state === 'ok' ? detail.data.turns : view.snapshots;
  const tag = statusTag(view);
  const schemas = useSchemas(view.agent);
  const turns = view.turns.map((t) => ({ turn: t.turn, step: t.step, live: view.status === 'live' && t.turn === view.turns.at(-1)?.turn }));
  return (
    <Shell
      route={route}
      agent={view.agent}
      who={view.status === 'live' ? (view.thinking ? 'engine' : null) : undefined}
      children={
        <>
          <span className="mono" style={{ fontSize: 13 }}>{route.run}</span>
          <span className={`tag ${tag.tone}`}>{tag.label}</span>
          {view.agent && <a className="mono" style={{ fontSize: 12 }} href={href({ page: 'agents', agent: view.agent })}>{view.agent}</a>}
          {view.channel && <span className="mono dim" style={{ fontSize: 12 }}>{view.channel} · config {view.config || '—'}</span>}
          {view.forked && (
            <a className="mono" style={{ fontSize: 12 }} href={href({ page: 'run', run: view.forked.run })}>carried on from {view.forked.run} · T{view.forked.turn}</a>
          )}
        </>
      }
      actions={
        <>
          {view.status === 'live' && view.agent && (
            <a className="btn" href={href({ page: 'agents', agent: view.agent, tab: 'build', run: route.run })}>Talk in Try it</a>
          )}
          {view.status === 'live' && <button type="button" className="btn btn-q" onClick={() => void api.hangUp(route.run)}>Hang up</button>}
          <a className="btn btn-q" href={href({ page: 'metrics', run: route.run })}>Its timings</a>
        </>
      }
      body={
        <div className="live">
          <div className="live-body">
            <main className="main" style={{ padding: 20, gap: 16 }}>
              {view.status === 'missing' && <p className="error-box">No run {route.run} in the store.</p>}
              {view.status === 'connecting' && <Placeholder what="the run" kind="rows" rows={6} />}
              <div className="lbl">Conversation</div>
              <Transcript lines={view.lines} thinking={view.thinking} />
              <div className="lbl" style={{ marginTop: 12 }}>What the model did, request by request</div>
              <ModelPanel requests={view.requests} last={50} />
            </main>
            <aside className="inspector" aria-label="Inspector">
              <Section title="Where the caller is" note={<span className="tag tag-y">{view.step || '—'}</span>}>
                <div className="chips">
                  {view.path.map((p, i) => (
                    <span key={i} className="chip" title={p.via}>T{p.turn} {p.step}</span>
                  ))}
                </div>
              </Section>
              <Section title="Facts" note={view.changed.length ? <span className="tag tag-y">changed {view.changed.join(', ')}</span> : undefined}>
                <StatePanel state={view.state} changed={view.changed} schema={schemas.state} />
              </Section>
              <Section title="Business actions" note={<span className="tag">{view.intents.length}</span>}>
                <IntentsPanel intents={view.intents} receipts={schemas.receipts} />
              </Section>
              <Section title="Turns" note={<span className="tag">{view.turns.length}</span>}>
                {view.turns.length === 0 && <p className="empty">No finished turn yet.</p>}
                {view.turns.map((t) => (
                  <div key={t.turn} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
                    <span className="mono">T{t.turn}</span>
                    <span className="mono dim">{t.step} · {t.status} · {t.requests} request(s)</span>
                    <span style={{ flex: '1 1 auto' }} />
                    {saved.includes(t.turn) && <a className="mono" style={{ fontSize: 12 }} href={href({ page: 'back', run: route.run, turn: t.turn })}>go back here</a>}
                  </div>
                ))}
              </Section>
            </aside>
          </div>
          <PathStrip turns={turns} onPick={(turn) => go({ page: 'back', run: route.run, turn })} onFork={(turn) => go({ page: 'back', run: route.run, turn })} />
        </div>
      }
    />
  );
}
