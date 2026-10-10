import { useState } from 'react';
import { api } from '../api/client';
import type { Kind, Limits, Metrics, ModelStats } from '../api/types';
import { useApi } from '../api/useApi';
import { Shell } from '../components/Shell';
import { LEVEL_COLOR, ms } from '../graph/timing';
import { href, go, type Route } from '../router';

const RULES: { key: keyof Limits; what: string; count?: boolean }[] = [
  { key: 'voice_to_voice', what: 'Voice to voice' },
  { key: 'stt_first_byte', what: 'STT first byte' },
  { key: 'llm_first_token', what: 'LLM first token' },
  { key: 'llm_request', what: 'LLM request that did not stream' },
  { key: 'tts_first_byte', what: 'TTS first audio' },
  { key: 'requests_per_turn', what: 'LLM requests in one turn', count: true },
];

const KIND: Record<Kind, string> = { stt: 'STT', llm: 'LLM', tts: 'TTS' };

function level(value: number | null, limit: { slow: number; bad: number }): 'ok' | 'slow' | 'bad' {
  if (value === null) return 'ok';
  return value > limit.bad ? 'bad' : value > limit.slow ? 'slow' : 'ok';
}

function day(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/**
 * Timings from the local Jaeger. Two views: the overview of recent runs, which can be narrowed to one
 * agent (asked of Jaeger itself), and one run, which names its own agent and has no filter. Both end
 * with latency by model: each STT, LLM and TTS model on its own row, so a model swapped between runs
 * is two rows, each with the period it covers.
 */
export function MetricsPage({ route }: { route: Extract<Route, { page: 'metrics' }> }) {
  return (
    <Shell
      route={route}
      agent={route.agent}
      body={<MetricsBody agent={route.agent} run={route.run} />}
    />
  );
}

export function MetricsBody({ agent = '', run = '', fixed = false }: { agent?: string; run?: string; fixed?: boolean }) {
  const agents = useApi(api.agents, 'agents');
  const [lookback, setLookback] = useState('24h');
  const [trace, setTrace] = useState('');
  const scope: 'overview' | 'run' = run || trace ? 'run' : 'overview';
  const metrics = useApi(
    () => (run ? api.runMetrics(run) : trace ? api.traceMetrics(trace) : api.metrics(lookback, agent)),
    `metrics-${agent}-${run}-${trace}-${lookback}`,
  );
  const overview = useApi(() => api.metrics(lookback, fixed ? agent : ''), `metrics-runs-${fixed ? agent : ''}-${lookback}`);
  const runs = overview.state === 'ok' ? overview.data.runs : [];
  const list = agents.state === 'ok' ? agents.data : [];
  const setScope = (next: 'overview' | 'run') => {
    if (next === 'overview') {
      setTrace('');
      if (run && !fixed) go({ page: 'metrics', agent });
    } else if (runs[0]) {
      setTrace(runs[0].trace);
    }
  };
  const named = metrics.state === 'ok' && metrics.data.scope === 'run' ? metrics.data.runs[0] : undefined;

  return (
    <main className="main" style={{ padding: 0 }}>
      <div className="metrics-head">
        <div style={{ flex: '1 1 360px' }}>
          <div className="lbl" style={{ marginBottom: 8 }}>
            {scope === 'overview' ? `last ${lookback} · ${fixed ? `this agent, ${agent}` : agent ? `agent ${agent}` : 'every agent'}` : `one run${named?.agent ? ` · agent ${named.agent}` : ''}`} · from the local Jaeger
          </div>
          <h1 style={{ margin: 0, fontSize: 32, fontWeight: 800 }}>{scope === 'overview' ? 'Timings' : named?.run || named?.trace || run || 'One run'}</h1>
        </div>
        <div className="segs" role="tablist" aria-label="Scope">
          <button type="button" role="tab" aria-selected={scope === 'overview'} className={`seg ${scope === 'overview' ? 'seg-on' : ''}`} onClick={() => setScope('overview')}>Overview</button>
          <button type="button" role="tab" aria-selected={scope === 'run'} className={`seg ${scope === 'run' ? 'seg-on' : ''}`} onClick={() => setScope('run')} disabled={!runs.length && !run}>One run</button>
        </div>
        {scope === 'overview' && (
          <select className="select" style={{ width: 'auto' }} aria-label="Period" value={lookback} onChange={(e) => setLookback(e.target.value)}>
            <option value="1h">last hour</option>
            <option value="24h">last 24 h</option>
            <option value="168h">last 7 days</option>
          </select>
        )}
        {scope === 'run' && !run && (
          <select className="select" style={{ width: 'auto', maxWidth: 360 }} aria-label="Run" value={trace} onChange={(e) => setTrace(e.target.value)}>
            {runs.map((r) => (
              <option key={r.trace} value={r.trace}>{r.run || r.trace.slice(0, 12)} · {r.agent || 'unnamed agent'} · {r.turns} turns</option>
            ))}
          </select>
        )}
        {metrics.state === 'ok' && (
          <a className="btn btn-q" href={metrics.data.source.replace('127.0.0.1', 'localhost')} target="_blank" rel="noreferrer">Open in Jaeger</a>
        )}
      </div>
      {scope === 'overview' && !fixed && (
        <div className="toolbar" aria-label="Agent filter">
          <span className="lbl">Agent</span>
          <div className="segs">
            <a className={`seg ${!agent ? 'seg-on' : ''}`} href={href({ page: 'metrics' })} style={{ display: 'inline-flex', alignItems: 'center', textDecoration: 'none' }}>All agents</a>
            {list.map((a) => (
              <a key={a.id} className={`seg ${agent === a.id ? 'seg-on' : ''}`} href={href({ page: 'metrics', agent: a.id })} style={{ display: 'inline-flex', alignItems: 'center', textDecoration: 'none' }}>{a.id}</a>
            ))}
          </div>
        </div>
      )}
      {metrics.state === 'loading' && <p className="empty" style={{ padding: 24 }}>Reading traces…</p>}
      {metrics.state === 'error' && (
        <div style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontSize: 18, fontWeight: 600 }}>{metrics.status === 503 ? 'No traces to read yet.' : metrics.status === 404 ? 'No trace for this run yet.' : 'Traces could not be read.'}</div>
          <p className="empty" style={{ margin: 0 }}>{metrics.message}</p>
        </div>
      )}
      {metrics.state === 'ok' && <Loaded m={metrics.data} />}
    </main>
  );
}

function Loaded({ m }: { m: Metrics }) {
  const s = m.summary;
  const tiles = [
    { label: 'Voice to voice p90', value: ms(s.voice_to_voice_p90_ms), note: `target ≤ ${ms(m.limits.voice_to_voice.slow)}`, color: LEVEL_COLOR[level(s.voice_to_voice_p90_ms, m.limits.voice_to_voice)] },
    { label: 'LLM first token p90', value: ms(s.llm_first_token_p90_ms), note: `target ≤ ${ms(m.limits.llm_first_token.slow)}`, color: LEVEL_COLOR[level(s.llm_first_token_p90_ms, m.limits.llm_first_token)] },
    { label: 'TTS first audio p90', value: ms(s.tts_first_audio_p90_ms), note: `target ≤ ${ms(m.limits.tts_first_byte.slow)}`, color: LEVEL_COLOR[level(s.tts_first_audio_p90_ms, m.limits.tts_first_byte)] },
    { label: 'Errors', value: String(s.errors), note: `${s.turns} turns · ${s.runs} run(s)`, color: s.errors ? 'var(--bad)' : 'var(--text)' },
  ];
  return (
    <>
      <div className="tiles" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))' }}>
        {tiles.map((t) => (
          <div key={t.label} className="tile">
            <span className="lbl" style={{ fontSize: 10 }}>{t.label}</span>
            <span className="mono" style={{ fontSize: 26, color: t.color }}>{t.value}</span>
            <span className="mono dim" style={{ fontSize: 11 }}>{t.note}</span>
          </div>
        ))}
      </div>
      {s.mixed.length > 0 && m.scope === 'overview' && (
        <div className="banner" style={{ margin: '12px 16px' }}>
          {s.mixed.map((k) => KIND[k]).join(', ')}: more than one model in this period, so the tiles mix them. The table below splits them.
        </div>
      )}
      {s.turns === 0 && (
        <p className="empty" style={{ padding: 24 }}>
          Jaeger answers but has no turns here. Start a run from an agent's page; its turns show here a few seconds after each one ends.
        </p>
      )}
      <div className="metrics-body">
        <section style={{ flex: '999 1 640px', minWidth: 0 }}>
          <div className="lbl" style={{ padding: '14px 16px' }}>Turns, in order</div>
          <div className="moves">
            <table>
              <thead>
                <tr>
                  <th className="lbl">Turn</th>
                  {m.scope === 'overview' && <th className="lbl">Run</th>}
                  <th className="lbl">Voice to voice</th>
                  <th className="lbl">STT</th>
                  <th className="lbl">LLM</th>
                  <th className="lbl">TTS</th>
                  <th className="lbl">Flags</th>
                </tr>
              </thead>
              <tbody>
                {m.turns.map((t) => {
                  const cell = (kind: Kind) =>
                    t.requests
                      .filter((r) => r.kind === kind)
                      .map((r) => (
                        <div key={r.span} style={{ color: LEVEL_COLOR[r.level] }} title={r.first_byte_ms === null ? `${r.model}: no first token (did not stream): total ${ms(r.duration_ms)}` : `${r.model}: first ${ms(r.first_byte_ms)}, total ${ms(r.duration_ms)}`}>
                          {r.first_byte_ms === null ? <span className="dim">— · {ms(r.duration_ms)}</span> : ms(r.first_byte_ms)}
                          {r.error ? ' ✕' : ''}
                        </div>
                      ));
                  const v2v = level(t.voice_to_voice_ms, m.limits.voice_to_voice);
                  return (
                    <tr key={`${t.trace}-${t.turn}`}>
                      <th className="mono">T{t.turn}{t.interrupted ? ' ⤺' : ''}</th>
                      {m.scope === 'overview' && <td className="mono" style={{ fontSize: 11, textAlign: 'left', padding: '0 8px' }}>{t.run ? <a href={href({ page: 'run', run: t.run })}>{t.run}</a> : t.trace.slice(0, 8)}<div className="dim">{t.agent}</div></td>}
                      <td style={{ color: LEVEL_COLOR[v2v] }}>{t.voice_to_voice_ms === null ? <span className="dim">text</span> : ms(t.voice_to_voice_ms)}</td>
                      <td>{cell('stt')}</td>
                      <td>{cell('llm')}</td>
                      <td>{cell('tts')}</td>
                      <td style={{ textAlign: 'left', padding: '6px 10px', fontFamily: 'var(--sans)', fontSize: 13 }}>
                        {t.flags.length ? t.flags.map((f) => <div key={f.what} style={{ color: LEVEL_COLOR[f.level] }}>{f.what}</div>) : <span className="dim">—</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <ModelTable models={m.models} scope={m.scope} />
        </section>
        <aside className="rules">
          <div className="lbl" style={{ padding: '10px 12px', borderBottom: '1px solid var(--rule-soft)' }}>What counts as slow</div>
          {RULES.map((r) => (
            <div key={r.key} className="rule">
              <span>{r.what}</span>
              <span className="mono" style={{ color: 'var(--yellow)' }}>&gt; {r.count ? m.limits[r.key].slow : ms(m.limits[r.key].slow)}</span>
              <span className="mono" style={{ color: 'var(--bad)' }}>&gt; {r.count ? m.limits[r.key].bad : ms(m.limits[r.key].bad)}</span>
            </div>
          ))}
          <div className="mono" style={{ fontSize: 10, color: 'var(--faint)', padding: '8px 12px' }}>slow · over the limit · defaults for every agent · tiles are p90</div>
        </aside>
      </div>
    </>
  );
}

function ModelTable({ models, scope }: { models: ModelStats[]; scope: 'overview' | 'run' }) {
  return (
    <>
      <div className="lbl" style={{ padding: '18px 16px 10px' }}>Latency by model{scope === 'overview' ? ' · a model swapped between runs is two rows' : ''}</div>
      <div className="moves">
        <table aria-label="Latency by model">
          <thead>
            <tr>
              <th className="lbl">Kind</th>
              <th className="lbl">Model</th>
              <th className="lbl">Provider</th>
              <th className="lbl">Used</th>
              <th className="lbl">Requests</th>
              <th className="lbl">First byte p90</th>
              <th className="lbl">Total p90</th>
              <th className="lbl">Errors</th>
              <th className="lbl">Share over the limit</th>
            </tr>
          </thead>
          <tbody>
            {models.map((x) => (
              <tr key={`${x.kind}-${x.model}-${x.provider}`}>
                <th className="mono">{KIND[x.kind]}</th>
                <td style={{ textAlign: 'left', padding: '0 10px' }}>{x.model || '—'}</td>
                <td className="dim">{x.provider || '—'}</td>
                <td className="dim" style={{ fontSize: 11 }}>{x.first_seen === x.last_seen ? day(x.first_seen) : `${day(x.first_seen)} – ${day(x.last_seen)}`} · {x.runs} run(s)</td>
                <td>{x.requests}</td>
                <td title={x.first_byte_p90_ms === null ? 'no request of it streamed: no first byte to show' : undefined}>{x.first_byte_p90_ms === null ? <span className="dim">—</span> : ms(x.first_byte_p90_ms)}</td>
                <td>{ms(x.total_p90_ms)}</td>
                <td style={{ color: x.errors ? 'var(--bad)' : undefined }}>{x.errors}</td>
                <td style={{ color: x.over_limit ? 'var(--bad)' : undefined }}>{Math.round(x.over_limit * 100)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
