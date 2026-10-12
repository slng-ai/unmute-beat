/**
 * A browser call from the studio on LiveKit: LiveKit's own web client, loaded from the engine at
 * /voice-sdk/livekit.js rather than bundled, as core's Pipecat client is (`voice.ts`).
 *
 * The runner placed the call in a room of its own on the LiveKit server and joined it as the agent; this
 * page joins the same room with the caller's token the studio passed on, publishes the microphone and
 * plays the agent's audio. LiveKit reports only its own playout, never this page's, so such a call runs
 * with simulated delivery: LiveKit's playout counts as heard.
 */

import type { VoiceCall, VoiceEvents } from './voice';

export interface LiveKitRoom {
  url: string;
  room: string;
  token: string;
}

interface Track {
  kind: string;
  attach: (element: HTMLMediaElement) => HTMLMediaElement;
  detach: () => HTMLMediaElement[];
}

interface Participant {
  identity: string;
  isLocal?: boolean;
}

interface Room {
  on: (event: string, listener: (...args: never[]) => void) => Room;
  connect: (url: string, token: string) => Promise<void>;
  disconnect: () => Promise<void>;
  startAudio: () => Promise<void>;
  localParticipant: { setMicrophoneEnabled: (enabled: boolean) => Promise<unknown> };
}

interface Sdk {
  Room: new (options?: unknown) => Room;
  RoomEvent: {
    TrackSubscribed: string;
    TrackUnsubscribed: string;
    ParticipantConnected: string;
    ActiveSpeakersChanged: string;
    Disconnected: string;
  };
}

const SDK = '/voice-sdk/livekit.js';

export async function startLiveKit(found: LiveKitRoom, audio: HTMLAudioElement, on: VoiceEvents): Promise<VoiceCall> {
  const sdk = (await import(/* @vite-ignore */ SDK)) as Sdk;
  const events = sdk.RoomEvent;
  const room = new sdk.Room({ adaptiveStream: false, dynacast: false });
  room
    .on(events.TrackSubscribed, ((track: Track, _publication: unknown, participant: Participant) => {
      if (track.kind === 'audio' && !participant.isLocal) track.attach(audio);
    }) as (...args: never[]) => void)
    .on(events.TrackUnsubscribed, ((track: Track) => {
      track.detach();
    }) as (...args: never[]) => void)
    .on(events.ParticipantConnected, (() => on.onStatus('connected')) as (...args: never[]) => void)
    .on(events.ActiveSpeakersChanged, ((speakers: Participant[]) => {
      if (speakers.some((p) => p.isLocal)) on.onStatus('you are speaking');
      else if (speakers.length > 0) on.onStatus('bot speaking');
      else on.onStatus('listening');
    }) as (...args: never[]) => void)
    .on(events.Disconnected, (() => {
      on.onStatus('call ended');
      on.onEnded();
    }) as (...args: never[]) => void);
  on.onStatus('connecting');
  await room.connect(found.url, found.token);
  await room.startAudio().catch(() => undefined);
  await room.localParticipant.setMicrophoneEnabled(true);
  on.onStatus('waiting for the agent');
  return { hangUp: () => room.disconnect(), reports: false };
}
