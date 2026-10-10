import { useEffect, useRef, useState } from 'react';

const BARS = 20;

/**
 * The microphone, first: a person testing a voice agent checks their mic before anything else.
 * "Test mic" opens it in this page and shows its level from the Web Audio analyser; nothing is
 * sent anywhere. Starting a call lands with the engine's run loop, so its button says so.
 */
export function CallBar({ canCall }: { canCall: boolean }) {
  const [levels, setLevels] = useState<number[]>(() => Array(BARS).fill(0));
  const [state, setState] = useState<'off' | 'on' | 'denied'>('off');
  const [device, setDevice] = useState('');
  const stop = useRef<() => void>(() => {});

  useEffect(() => () => stop.current(), []);

  const test = async () => {
    if (state === 'on') {
      stop.current();
      setState('off');
      setLevels(Array(BARS).fill(0));
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const audio = new AudioContext();
      const analyser = audio.createAnalyser();
      analyser.fftSize = 512;
      audio.createMediaStreamSource(stream).connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      let frame = 0;
      const tick = () => {
        analyser.getFloatTimeDomainData(samples);
        const rms = Math.sqrt(samples.reduce((sum, v) => sum + v * v, 0) / samples.length);
        setLevels((old) => [...old.slice(1), Math.min(1, rms * 8)]);
        frame = requestAnimationFrame(tick);
      };
      tick();
      setDevice(stream.getAudioTracks()[0]?.label ?? '');
      setState('on');
      stop.current = () => {
        cancelAnimationFrame(frame);
        stream.getTracks().forEach((t) => t.stop());
        void audio.close();
      };
    } catch {
      setState('denied');
    }
  };

  const line = { off: 'Mic off', on: 'Mic on · speak to check the level', denied: 'The browser did not allow the mic' }[state];
  return (
    <section className="callbar" aria-label="Call">
      <button type="button" className={`mic ${state === 'on' ? 'mic-on' : ''}`} onClick={test} aria-pressed={state === 'on'} aria-label={state === 'on' ? 'Stop the mic test' : 'Test the mic'}>
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
          <rect x="9" y="3" width="6" height="11" />
          <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
        </svg>
      </button>
      <div className="callbar-level">
        <span style={{ fontSize: 15, fontWeight: 600 }}>{line}</span>
        <div className="meter" aria-hidden="true">
          {levels.map((v, i) => (
            <span key={i} style={{ height: 3 + v * 15, background: state !== 'on' ? 'var(--rule-strong)' : v > 0.25 ? 'var(--caller)' : '#2e4a63' }} />
          ))}
        </div>
      </div>
      <div className="mono dim" style={{ fontSize: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span>{device ? `input · ${device}` : 'input · not opened yet'}</span>
        <span>a line counts as heard only on the page's playback report</span>
      </div>
      <span style={{ flex: '1 1 auto' }} />
      <button type="button" className="btn btn-q" disabled>Pause after this turn</button>
      <button type="button" className="btn btn-y" disabled={!canCall} title={canCall ? undefined : 'Calls start from here once the engine runs them (phase 2)'}>
        Start a call
      </button>
    </section>
  );
}
