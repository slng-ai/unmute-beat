/**
 * A browser call from the studio: core's own client (Pipecat's PipecatClient and SmallWebRTCTransport,
 * with core's playback reports), loaded from the engine at /voice-sdk/ rather than bundled, so the
 * studio's call reports playback exactly as core's /voice/ page does and a read-back can be agreed to.
 *
 * The offer and its ICE candidates go to the studio (`/api/runs/<run>/offer`), which hands them to the
 * runner holding the call; the audio itself goes straight between this page and that runner.
 */

export interface VoiceCall {
  hangUp: () => Promise<void>;
  reports: boolean;
}

export interface VoiceEvents {
  onStatus: (status: string) => void;
  onReport: (count: number) => void;
  onEnded: () => void;
}

interface Sdk {
  PipecatClient: new (options: unknown) => {
    connect: (options: unknown) => Promise<void>;
    disconnect: () => Promise<void>;
  };
  SmallWebRTCTransport: new (options?: unknown) => unknown;
}

const SDK = '/voice-sdk/sdk.js';
const PLAYOUT = '/voice-sdk/playout.js';

export async function startVoice(run: string, audio: HTMLAudioElement, on: VoiceEvents): Promise<VoiceCall> {
  const sdk = (await import(/* @vite-ignore */ SDK)) as Sdk;
  const playout = (await import(/* @vite-ignore */ PLAYOUT)) as {
    reportPlayout: (client: unknown, audio: HTMLAudioElement, onReport: () => void) => boolean;
  };
  let reported = 0;
  const client = new sdk.PipecatClient({
    transport: new sdk.SmallWebRTCTransport(),
    enableMic: true,
    enableCam: false,
    callbacks: {
      onTrackStarted: (track: MediaStreamTrack, participant?: { local?: boolean }) => {
        if (track.kind === 'audio' && !participant?.local) audio.srcObject = new MediaStream([track]);
      },
      onBotReady: () => on.onStatus('connected'),
      onBotStartedSpeaking: () => on.onStatus('bot speaking'),
      onBotStoppedSpeaking: () => on.onStatus('listening'),
      onUserStartedSpeaking: () => on.onStatus('you are speaking'),
      onDisconnected: () => {
        on.onStatus('call ended');
        on.onEnded();
      },
    },
  });
  const reports = playout.reportPlayout(client, audio, () => on.onReport(++reported));
  on.onStatus('connecting');
  await client.connect({ webrtcRequestParams: { endpoint: `/api/runs/${encodeURIComponent(run)}/offer` } });
  return { hangUp: () => client.disconnect(), reports };
}
