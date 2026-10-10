import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { BootState, BootStatus } from '../api/types';
import { href } from '../router';

/** Lines the loading screen rotates through: each one is a real thing start-up does. */
const SENTENCES = [
  'Looking for Docker on this machine…',
  'Teaching postgres to remember every turn…',
  'Opening a redis stream for the live view…',
  'Checking that every exit in agent.yaml points at a real step…',
  'Compiling each prompt against its call type, before any call runs…',
  'Counting the read-backs twice…',
  'Making sure nothing gets booked without a heard yes…',
];

const MARK: Record<BootStatus, { icon: string; color: string }> = {
  waiting: { icon: '□', color: 'var(--faint)' },
  starting: { icon: '◰', color: 'var(--yellow)' },
  healthy: { icon: '■', color: 'var(--ok)' },
  skipped: { icon: '–', color: 'var(--dim)' },
  failed: { icon: '✕', color: 'var(--bad)' },
};

export function Boot() {
  const [boot, setBoot] = useState<BootState | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [line, setLine] = useState(0);

  useEffect(() => {
    let live = true;
    let timer = 0;
    const poll = () =>
      api.boot().then(
        (state) => {
          if (!live) return;
          setBoot(state);
          setUnreachable(false);
          if (!state.ready && !state.failed) timer = window.setTimeout(poll, 500);
        },
        () => {
          if (!live) return;
          setUnreachable(true);
          timer = window.setTimeout(poll, 1000);
        },
      );
    poll();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setLine((n) => n + 1), 2400);
    return () => window.clearInterval(timer);
  }, []);

  const steps = boot?.steps ?? [];
  const done = steps.filter((s) => s.status === 'healthy' || s.status === 'skipped').length;
  const pct = steps.length ? Math.round((done / steps.length) * 100) : 0;
  const lit = Math.round((pct / 100) * 24);
  const sentence = boot?.failed
    ? 'Start-up stopped. The failed step says what to do.'
    : boot?.ready
      ? 'Pick an agent to see its runs, or start a call.'
      : unreachable
        ? 'Waiting for the engine…'
        : SENTENCES[line % SENTENCES.length];

  return (
    <div className="app dots">
      <header className="bar" style={{ background: 'var(--ground)' }}>
        <span className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">SLNG</span>
          <span className="mono dim" style={{ fontSize: 13 }}>/ studio</span>
        </span>
      </header>
      <main style={{ flex: '1 1 auto', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '48px 16px' }}>
        <div style={{ width: '100%', maxWidth: 760, display: 'flex', flexDirection: 'column', gap: 28 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div className="mono dim" style={{ fontSize: 13 }}>
              $ slng studio up<span className="blink" style={{ color: 'var(--yellow)' }}>▍</span>
            </div>
            <h1 style={{ margin: 0, fontSize: 56, lineHeight: 1, fontWeight: 800, letterSpacing: '-0.02em' }}>
              {boot?.ready ? 'Studio is up.' : 'Starting studio'}
            </h1>
            <p key={sentence} className="fade" style={{ margin: 0, fontSize: 22, lineHeight: 1.35, color: boot?.failed ? 'var(--bad)' : 'var(--yellow)', minHeight: 60 }} aria-live="polite">
              {sentence}
            </p>
          </div>

          <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label="Start-up progress" style={{ display: 'grid', gridTemplateColumns: 'repeat(24, minmax(0, 1fr))', gap: 3 }}>
            {Array.from({ length: 24 }, (_, i) => (
              <div key={i} style={{ height: 14, background: i < lit ? 'var(--yellow)' : 'var(--rule-soft)' }} />
            ))}
          </div>

          <section style={{ background: 'var(--ground)', border: '1px solid var(--rule)' }} aria-label="Services">
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 16px', borderBottom: '1px solid var(--rule-soft)' }}>
              <span className="lbl">docker compose · slng-studio</span>
              <span style={{ flex: '1 1 auto' }} />
              <span className="mono dim" style={{ fontSize: 12 }}>{pct}%</span>
            </div>
            {steps.map((step) => (
              <div key={step.id} style={{ display: 'grid', gridTemplateColumns: '24px 140px minmax(0, 1fr) 90px', gap: 12, alignItems: 'center', padding: '12px 16px', borderBottom: '1px solid var(--hover)' }}>
                <span className={`mono ${step.status === 'starting' ? 'pulse' : ''}`} style={{ color: MARK[step.status].color, textAlign: 'center' }}>{MARK[step.status].icon}</span>
                <span className="mono" style={{ fontSize: 13 }}>{step.label}</span>
                <span style={{ fontSize: 14, color: step.status === 'failed' ? 'var(--bad)' : 'var(--muted)', overflowWrap: 'anywhere' }}>{step.message || step.detail}</span>
                <span className="mono" style={{ fontSize: 11, letterSpacing: '.08em', textTransform: 'uppercase', color: MARK[step.status].color, textAlign: 'right' }}>{step.status}</span>
              </div>
            ))}
            {!steps.length && <div className="empty" style={{ padding: 16 }}>Waiting for the engine on this port…</div>}
          </section>

          {boot?.ready && (
            <div className="fade" style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap', border: '1px solid var(--yellow)', padding: 16 }}>
              <div style={{ flex: '1 1 280px' }}>
                <div style={{ fontSize: 18, fontWeight: 600 }}>{boot.agents.length} agents found.</div>
                <div className="mono" style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4 }}>{boot.agents.join(' · ')}</div>
              </div>
              <a className="btn btn-y" href={href({ page: 'runs' })}>Open studio →</a>
            </div>
          )}
          <p className="mono" style={{ margin: 0, fontSize: 11, color: 'var(--faint)' }}>
            ctrl+c stops every container · runs stay in the slng-studio volume · slng studio down --volumes clears it
          </p>
        </div>
      </main>
    </div>
  );
}
