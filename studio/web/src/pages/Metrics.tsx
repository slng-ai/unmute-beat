import { api } from '../api/client';
import type { Limits, Metrics } from '../api/types';
import { useApi } from '../api/useApi';
import { Shell } from '../components/Shell';
import { LEVEL_COLOR, ms } from '../graph/timing';
import type { Route } from '../router';

const RULES: { key: keyof Limits; what: string; count?: boolean }[] = [
  { key: 'voice_to_voice', what: 'Voice to voice' },
  { key: 'stt_first_byte', what: 'STT first byte' },
  { key: 'llm_first_token', what: 'LLM first token' },
  { key: 'llm_request', what: 'LLM request, with no first-token time' },
  { key: 'tts_first_byte', what: 'TTS first audio' },
  { key: 'requests_per_turn', what: 'LLM requests in one turn', count: true },
];

/**
 * Timings from the local Jaeger: every recent turn with its STT, LLM and TTS requests, what was
 * over its limit, and each model's p50 and p95. When Jaeger does not answer, the page says how to
 * get traces there rather than showing an empty, healthy-looking table.
 */
export function MetricsPage({ route }: { route: Extract<Route, { page: 'metrics' }> }) {
  const metrics = useApi(() => api.metrics('1h'), 'metrics');
  return (
    <Shell
      route={route}
      agent={route.agent}
      body={
        <main className="main" style={{ padding: 0 }}>
          <div className="metrics-head">
            <div style={{ flex: '1 1 360px' }}>
              <div className="lbl" style={{ marginBottom: 8 }}>last hour · from the local Jaeger</div>
              <h1 style={{ margin: 0, fontSize: 32, fontWeight: 800 }}>Turns and requests</h1>
            </div>
            {metrics.state === 'ok' && (
              <a className="btn btn-q" href={metrics.data.source.replace('127.0.0.1', 'localhost')} target="_blank" rel="noreferrer">
                Open in Jaeger
              </a>
            )}
          </div>
          {metrics.state === 'loading' && <p className="empty" style={{ padding: 24 }}>Reading traces…</p>}
          {metrics.state === 'error' && (
            <div style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: 18, fontWeight: 600 }}>{metrics.status === 503 ? 'No traces to read yet.' : 'Traces could not be read.'}</div>
              <p className="empty" style={{ margin: 0 }}>{metrics.message}</p>
            </div>
          )}
          {metrics.state === 'ok' && <Loaded m={metrics.data} />}
        </main>
      }
    />
  );
}

function Loaded({ m }: { m: Metrics }) {
  const s = m.summary;
  const v2vLevel = s.voice_to_voice_p95_ms === null ? 'ok' : s.voice_to_voice_p95_ms > m.limits.voice_to_voice.bad ? 'bad' : s.voice_to_voice_p95_ms > m.limits.voice_to_voice.slow ? 'slow' : 'ok';
  const tiles = [
    { label: 'Turns', value: String(s.turns), color: 'var(--text)' },
    { label: 'Voice to voice', value: ms(s.voice_to_voice_avg_ms), note: 'average', color: 'var(--text)' },
    { label: 'p95', value: ms(s.voice_to_voice_p95_ms), note: `target ≤ ${ms(m.limits.voice_to_voice.slow)}`, color: LEVEL_COLOR[v2vLevel] },
    { label: 'Over the limit', value: `${Math.round(s.over_limit * 100)}%`, note: 'of turns', color: s.over_limit ? 'var(--bad)' : 'var(--text)' },
    { label: 'Errors', value: String(s.errors), color: s.errors ? 'var(--bad)' : 'var(--text)' },
  ];
  return (
    <>
      <div className="tiles">
        {tiles.map((t) => (
          <div key={t.label} className="tile">
            <span className="lbl" style={{ fontSize: 10 }}>{t.label}</span>
            <span className="mono" style={{ fontSize: 24, color: t.color }}>{t.value}</span>
            {t.note && <span className="mono dim" style={{ fontSize: 11 }}>{t.note}</span>}
          </div>
        ))}
      </div>
      {s.turns === 0 && (
        <p className="empty" style={{ padding: 24 }}>
          Jaeger answers but has no turns from the last hour. Start a bot with slng studio bot salon (any use case), make a call, and its turns show here.
        </p>
      )}
      <div className="metrics-body">
        <section style={{ flex: '999 1 640px', minWidth: 0 }}>
          <div className="lbl" style={{ padding: '14px 16px' }}>Turns, in order</div>
          <div className="moves">
            <table>
              <thead>
                <tr>
                  <th className="lbl">Turn</th><th className="lbl">Voice to voice</th><th className="lbl">STT</th><th className="lbl">LLM</th><th className="lbl">TTS</th><th className="lbl">Flags</th>
                </tr>
              </thead>
              <tbody>
                {m.turns.map((t) => {
                  const of = (kind: 'stt' | 'llm' | 'tts') => t.requests.filter((r) => r.kind === kind);
                  const cell = (kind: 'stt' | 'llm' | 'tts') =>
                    of(kind).map((r) => (
                      <div key={r.span} style={{ color: LEVEL_COLOR[r.level] }}>{ms(r.first_byte_ms ?? r.duration_ms)}{r.error ? ' ✕' : ''}</div>
                    ));
                  const v = t.voice_to_voice_ms;
                  const v2v = v === null ? 'ok' : v > m.limits.voice_to_voice.bad ? 'bad' : v > m.limits.voice_to_voice.slow ? 'slow' : 'ok';
                  return (
                    <tr key={`${t.trace}-${t.turn}`}>
                      <th className="mono">T{t.turn}{t.interrupted ? ' ⤺' : ''}</th>
                      <td style={{ color: LEVEL_COLOR[v2v] }}>{ms(v)}</td>
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
          <div className="lbl" style={{ padding: '18px 16px 10px' }}>Every model</div>
          <div className="moves">
            <table>
              <thead>
                <tr>
                  <th className="lbl">Kind</th><th className="lbl">Model</th><th className="lbl">Calls</th><th className="lbl">p50</th><th className="lbl">p95</th><th className="lbl">Errors</th><th className="lbl">Over the limit</th>
                </tr>
              </thead>
              <tbody>
                {m.models.map((x) => (
                  <tr key={`${x.kind}-${x.model}-${x.provider}`}>
                    <th className="mono">{x.kind.toUpperCase()}</th>
                    <td style={{ textAlign: 'left', padding: '0 10px' }}>{x.model || '—'} <span className="dim">{x.provider}</span></td>
                    <td>{x.calls}</td>
                    <td>{ms(x.p50_ms)}</td>
                    <td>{ms(x.p95_ms)}</td>
                    <td style={{ color: x.errors ? 'var(--bad)' : undefined }}>{x.errors}</td>
                    <td style={{ color: x.over_limit ? 'var(--bad)' : undefined }}>{Math.round(x.over_limit * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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
          <div className="mono" style={{ fontSize: 10, color: 'var(--faint)', padding: '8px 12px' }}>slow · over the limit · defaults for every agent</div>
        </aside>
      </div>
    </>
  );
}
