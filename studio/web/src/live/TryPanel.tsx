import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { RunRow } from '../api/types';
import { useApi } from '../api/useApi';
import { href, go } from '../router';
import { IntentsPanel, ModelPanel, StatePanel, statusTag, Transcript } from './panels';
import type { RunView } from './useRun';
import { startVoice, type VoiceCall } from './voice';

/**
 * The right half of an agent's page: a real run on the config as saved, by text or by voice from this
 * browser. A run is placed on a runner by the studio; its events stream back here as they happen. A
 * config saved while the run goes on makes the run stale: carry on from its last turn on the new
 * config, or keep it as it is.
 */
export function TryPanel({ agent, version, run, onRun, view }: {
  agent: string;
  version: string;
  run?: string;
  onRun: (run: string | undefined) => void;
  view: RunView;
}) {
  const [channel, setChannel] = useState<'text' | 'voice'>('text');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [voice, setVoice] = useState<{ status: string; reports: number; call?: VoiceCall; capable?: boolean }>({ status: '', reports: 0 });
  const audio = useRef<HTMLAudioElement>(null);
  const recent = useApi(() => api.runs(agent), `try-runs-${agent}-${run ?? ''}-${view.status}-${view.turns.length}`);
  const row = useApi(() => (run ? api.run(run) : Promise.reject(new Error('no run'))), `try-row-${run ?? ''}-${view.status}`);
  const waitingForVoice = row.state === 'ok' && row.data.run.channel === 'voice' && row.data.run.outcome === null && view.status !== 'live' && !voice.call;
  const live = view.status === 'live' || waitingForVoice;
  const lastTurn = view.turns.at(-1)?.turn ?? 0;
  const stale = Boolean(run && live && view.config && version && view.config !== version);

  useEffect(() => () => void voice.call?.hangUp(), [voice.call]);

  const join = async (id: string) => {
    if (!audio.current) return;
    const call = await startVoice(id, audio.current, {
      onStatus: (status) => setVoice((v) => ({ ...v, status })),
      onReport: (reports) => setVoice((v) => ({ ...v, reports })),
      onEnded: () => setVoice((v) => ({ ...v, call: undefined })),
    });
    setVoice({ status: 'connected', reports: 0, call, capable: call.reports });
  };

  const start = async (from?: { run: string; turn: number }) => {
    setBusy(true);
    setError('');
    try {
      const started = await api.start({ agent, channel, ...(from ? { resume_from: from } : {}) });
      onRun(started.run);
      if (started.channel === 'voice') await join(started.run);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    if (!run || !text.trim()) return;
    const words = text;
    setText('');
    setBusy(true);
    setError('');
    try {
      await api.turn(run, words);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const hangUp = async () => {
    if (voice.call) await voice.call.hangUp();
    if (run) await api.hangUp(run).catch(() => undefined);
  };

  const tag = statusTag(view);
  return (
    <aside className="col-try" aria-label="Try it">
      <div className="panel-h">
        <span className="lbl">Try it</span>
        <span style={{ flex: '1 1 auto' }} />
        <div className="segs" role="tablist" aria-label="Talk by">
          {(['text', 'voice'] as const).map((c) => (
            <button key={c} type="button" role="tab" aria-selected={channel === c} className={`seg ${channel === c ? 'seg-on' : ''}`} onClick={() => setChannel(c)} disabled={live}>
              {c === 'voice' ? 'Voice · browser' : 'Text'}
            </button>
          ))}
        </div>
      </div>
      <div className="panel-b">
        {!run || !live ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <button type="button" className="btn btn-y" onClick={() => void start()} disabled={busy} style={{ alignSelf: 'flex-start' }}>
              {busy ? 'Starting…' : channel === 'voice' ? 'Start a call' : 'Start a text run'}
            </button>
            <span className="hint">
              {channel === 'voice'
                ? 'Your mic goes straight to the runner over WebRTC. A line counts as heard only when this page reports it played.'
                : 'Each reply counts as read once it reaches this page.'}{' '}
              Runs on config {version || '…'}.
            </span>
          </div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
            <span className={`tag ${tag.tone}`}>{tag.label}</span>
            <span className="mono" style={{ fontSize: 12 }}>{run}</span>
            <span className="mono dim" style={{ fontSize: 12 }}>at {view.step || '…'}</span>
            <span style={{ flex: '1 1 auto' }} />
            <button type="button" className="btn btn-q" onClick={() => void hangUp()}>Hang up</button>
          </div>
        )}
        {error && <div className="problem">{error}</div>}
        {waitingForVoice && run && (
          <div className="banner">
            <span>This call carries on by voice: join it from this browser.</span>
            <button type="button" className="btn btn-y" onClick={() => void join(run).catch((e: unknown) => setError(String(e)))}>Join by voice</button>
          </div>
        )}
        {stale && (
          <div className="banner">
            <span>The config changed since this run started ({view.config} → {version}).</span>
            <span style={{ flex: '1 1 auto' }} />
            <button type="button" className="btn btn-y" onClick={() => void hangUp().then(() => start({ run: run as string, turn: lastTurn }))} disabled={!lastTurn}>
              Carry on from T{lastTurn} on the new config
            </button>
          </div>
        )}
        {channel === 'voice' && run && live && (
          <div className="hint">
            {voice.status || 'waiting for the browser'} · {voice.capable === false ? 'this browser cannot report playback: nothing can be agreed to' : `${voice.reports} lines reported played`}
          </div>
        )}
        <audio ref={audio} autoPlay />
        {run && (
          <>
            <Transcript lines={view.lines} thinking={view.thinking} />
            {live && view.channel === 'text' && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void send();
                }}
                style={{ display: 'flex', gap: 8 }}
              >
                <input className="input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Say something as the caller…" aria-label="Caller's words" disabled={busy} autoFocus />
                <button type="submit" className="btn btn-y" disabled={busy || !text.trim()}>Send</button>
              </form>
            )}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              <a className="btn btn-q" href={href({ page: 'run', run })}>Full live view</a>
              <button type="button" className="btn btn-q" disabled={lastTurn < 1} onClick={() => go({ page: 'back', run, turn: Math.max(1, lastTurn - 1) })}>
                Go back a turn
              </button>
              {!live && <button type="button" className="btn btn-q" onClick={() => onRun(undefined)}>New run</button>}
            </div>
            <details open>
              <summary className="lbl" style={{ cursor: 'pointer' }}>What the model is doing</summary>
              <div style={{ marginTop: 8 }}><ModelPanel requests={view.requests} last={4} /></div>
            </details>
            <details>
              <summary className="lbl" style={{ cursor: 'pointer' }}>State</summary>
              <div style={{ marginTop: 8 }}><StatePanel state={view.state} changed={view.changed} /></div>
            </details>
            <details>
              <summary className="lbl" style={{ cursor: 'pointer' }}>Business actions</summary>
              <div style={{ marginTop: 8 }}><IntentsPanel intents={view.intents} /></div>
            </details>
          </>
        )}
        <div style={{ borderTop: '1px solid var(--rule-soft)', paddingTop: 12, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span className="lbl">Its last runs · start from a saved turn</span>
          {recent.state === 'ok' && recent.data.length === 0 && <span className="hint">None yet.</span>}
          {recent.state === 'ok' &&
            recent.data.slice(0, 5).map((r: RunRow) => (
              <div key={r.id} style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
                <a className="mono" href={href({ page: 'run', run: r.id })} style={{ fontSize: 12 }}>{r.id}</a>
                <span className="mono dim" style={{ fontSize: 11 }}>{r.channel} · {r.turns} turns · {r.outcome ?? 'live'}</span>
                <span style={{ flex: '1 1 auto' }} />
                {r.turns > 0 && <a className="mono" style={{ fontSize: 11 }} href={href({ page: 'back', run: r.id, turn: r.turns })}>go back</a>}
              </div>
            ))}
        </div>
      </div>
    </aside>
  );
}
