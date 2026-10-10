import { useState } from 'react';
import { api } from '../api/client';
import type { RunRow } from '../api/types';
import { useApi } from '../api/useApi';
import { Shell } from '../components/Shell';
import { href, go, type Route } from '../router';

function when(iso: string): string {
  const at = new Date(iso);
  return `${at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
}

/** Runs, newest first: every agent's, or one agent's when `agent` is given (an agent's own page passes it). */
export function RunsTable({ agent = '', channel = '', outcome = '' }: { agent?: string; channel?: string; outcome?: string }) {
  const [tick, setTick] = useState(0);
  const runs = useApi(() => api.runs(agent), `runs-${agent}-${tick}`);
  if (runs.state === 'loading') return <p className="empty">Loading runs…</p>;
  if (runs.state === 'error') return <p className="error-box">{runs.message}</p>;
  const rows = runs.data.filter((r) => (!channel || r.channel === channel) && (!outcome || (outcome === 'live' ? r.outcome === null : r.outcome === outcome)));
  return (
    <div className="scroll-x">
      <table className="table" aria-label="Runs">
        <thead>
          <tr>
            <th>Run</th>
            {!agent && <th>Agent</th>}
            <th>Talk by</th>
            <th>Started</th>
            <th>Turns</th>
            <th>Outcome</th>
            <th>Config</th>
            <th>Carried on from</th>
            <th>
              <button type="button" className="btn btn-q" style={{ height: 26 }} onClick={() => setTick((t) => t + 1)}>Refresh</button>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r: RunRow) => (
            <tr key={r.id} className="click" onClick={() => go({ page: 'run', run: r.id })}>
              <td className="mono"><a href={href({ page: 'run', run: r.id })} onClick={(e) => e.stopPropagation()}>{r.id}</a></td>
              {!agent && <td className="mono">{r.agent}</td>}
              <td className="mono dim">{r.channel === 'voice' ? 'voice · browser' : r.channel}</td>
              <td className="mono dim">{when(r.started_at)}</td>
              <td className="mono">{r.turns}</td>
              <td>{r.outcome === null ? <span className="tag tag-y">live{r.step ? ` · ${r.step}` : ''}</span> : <span className={`tag ${r.outcome === 'failed' ? 'tag-bad' : 'tag-ok'}`}>{r.outcome}</span>}</td>
              <td className="mono dim">{r.config || '—'}</td>
              <td className="mono dim">{r.forked_from ? `${r.forked_from} · T${r.forked_turn}` : '—'}</td>
              <td>{r.turns > 0 && <a className="mono" style={{ fontSize: 12 }} href={href({ page: 'back', run: r.id, turn: r.turns })} onClick={(e) => e.stopPropagation()}>go back</a>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p className="empty" style={{ padding: 16 }}>No runs{agent ? ` of ${agent}` : ''} yet. Start one from an agent's page.</p>}
    </div>
  );
}

/**
 * Every run of every agent, filterable by agent, by how it was talked to, and by how it ended. An
 * agent's own page shows the same table filtered to it; a run opens its full view, live or read back.
 */
export function Runs({ route }: { route: Extract<Route, { page: 'runs' }> }) {
  const agents = useApi(api.agents, 'agents');
  const runners = useApi(api.runners, 'runners');
  const [channel, setChannel] = useState('');
  const [outcome, setOutcome] = useState('');
  const list = agents.state === 'ok' ? agents.data : [];
  return (
    <Shell
      route={route}
      agent={route.agent}
      actions={<a className="btn btn-y" href={href({ page: 'agents', agent: route.agent })}>Start a call</a>}
      body={
        <main className="main">
          <div className="metrics-head">
            <div style={{ flex: '1 1 360px' }}>
              <div className="lbl" style={{ marginBottom: 8 }}>every agent · newest first</div>
              <h1 style={{ margin: 0, fontSize: 32, fontWeight: 800 }}>Runs</h1>
            </div>
            {runners.state === 'ok' && (
              <span className="mono dim" style={{ fontSize: 12 }}>
                {runners.data.filter((r) => r.connected).length} runner(s) linked · {runners.data.reduce((n, r) => n + r.active, 0)} call(s) live
              </span>
            )}
          </div>
          <div className="toolbar" role="group" aria-label="Filter">
            <div className="segs" aria-label="Agent">
              <a className={`seg ${!route.agent ? 'seg-on' : ''}`} href={href({ page: 'runs' })} style={{ display: 'inline-flex', alignItems: 'center', textDecoration: 'none' }}>All agents</a>
              {list.map((a) => (
                <a key={a.id} className={`seg ${route.agent === a.id ? 'seg-on' : ''}`} href={href({ page: 'runs', agent: a.id })} style={{ display: 'inline-flex', alignItems: 'center', textDecoration: 'none' }}>{a.id}</a>
              ))}
            </div>
            <select className="select" style={{ width: 'auto' }} aria-label="Talk by" value={channel} onChange={(e) => setChannel(e.target.value)}>
              <option value="">any channel</option>
              <option value="text">text</option>
              <option value="voice">voice</option>
            </select>
            <select className="select" style={{ width: 'auto' }} aria-label="Outcome" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
              <option value="">any outcome</option>
              <option value="live">live</option>
              <option value="goodbye">goodbye</option>
              <option value="hangup">hangup</option>
              <option value="failed">failed</option>
            </select>
          </div>
          <div style={{ padding: 16 }}>
            <RunsTable agent={route.agent} channel={channel} outcome={outcome} />
          </div>
        </main>
      }
    />
  );
}
