/*
 * Playback evidence for a bot built on slng-voice-core, added to Pipecat's own web client.
 *
 * Pipecat's PipecatClient and SmallWebRTCTransport own the connection, the microphone, the devices and
 * reconnection. The page plays the bot's track in its own <audio> element, as Pipecat's vanilla examples
 * do. This module only watches, and reports through the client's own messages:
 *
 *   bot  -> page  server-message  playout-mark {mark, source, start, rtp}
 *   page -> bot   client-message  playout      {mark, source, rtp, since}
 *
 * `source` is the bot's audio stream (its SSRC). `start` is an RTP timestamp at or before the segment's first
 * audio; `rtp` is one past its last; `since` is where the page's unbroken playing began. The page reports a
 * mark only when both hold, and the bot checks `since` and `rtp` again:
 *
 *   1. the audio element played the bot's track, with no break, from a playout position at or before
 *      `start`: attached to the page, playing, not muted, volume above zero, holding the live track;
 *   2. the browser's playout position for that source (RTCRtpReceiver.getSynchronizationSources()
 *      rtpTimestamp, W3C webrtc-pc) passed `rtp`.
 *
 * Any break (pause, a blocked autoplay, a detached or replaced source, a muted or ended track) ends the run.
 * Playing again starts a new run at the position then, so a resumed segment whose start was already past is
 * never reported: resuming cannot approve what did not play whole. The receiver's position alone is not
 * enough, since it may advance with no output attached (W3C). What a report proves: this page's audio element
 * was playing the bot's stream, unmuted, from before the segment began until the browser's playout passed its
 * end. Not that a speaker or a person heard it: system volume, the output device and the room are beyond it.
 *
 * ponytail: SmallWebRTCTransport 1.10.8 declares no accessor for its RTCPeerConnection, so the receiver is
 * read from its `pc` member. If a version drops it, `reportPlayout` declares nothing, the bot refuses the call
 * before its greeting, and nothing false is ever reported. The upgrade is a public receiver or playout event in
 * the SDK.
 */
import { RTVIEvent, learnAboutClient, setAboutClient } from './sdk.js';

export const PLAYOUT = 'rtp-timestamp/2';
const SAMPLE_MS = 20;
const BREAKS = ['pause', 'emptied', 'loadstart', 'waiting', 'ended', 'volumechange'];

/** RTP timestamps wrap at 2^32: `a` is at or after `b` (RFC 1982 serial comparison). */
function atOrAfter(a, b) {
  return ((a - b) >>> 0) < 2 ** 31;
}

/**
 * Watch `audio` play the bot's track on `client`, and report each segment that played whole.
 * Call before `client.connect()`: it declares the capability in the client's `client-ready`.
 * Returns whether it could (false: this transport gives no receiver to watch, and nothing is declared).
 */
export function reportPlayout(client, audio, onReport = () => {}) {
  const transport = client.transport;
  if (!Object.hasOwn(transport, 'pc') || !('getSynchronizationSources' in RTCRtpReceiver.prototype)) return false;
  const about = learnAboutClient();
  setAboutClient({ ...about, platform_details: { ...about.platform_details, playout: PLAYOUT } });

  const pending = new Map();
  let run = null;
  const broken = () => { run = null; };
  for (const name of BREAKS) audio.addEventListener(name, broken);

  function receiver() {
    const pc = transport.pc;
    const audioIn = pc && pc.getTransceivers().find((t) => t.mid === '0');
    return audioIn ? audioIn.receiver : null;
  }

  function playing(track) {
    const stream = audio.srcObject;
    return audio.isConnected && !audio.paused && !audio.ended && !audio.muted && audio.volume > 0
      && audio.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      && stream instanceof MediaStream && stream.getAudioTracks().includes(track)
      && track.readyState === 'live' && track.enabled && !track.muted;
  }

  function sample() {
    const input = receiver();
    const [position] = input && playing(input.track) ? input.getSynchronizationSources() : [];
    if (!position) { run = null; return; }
    if (!run || run.source !== position.source) run = { source: position.source, from: position.rtpTimestamp };
    for (const [mark, segment] of pending) {
      if (segment.source !== run.source) continue;
      if (!atOrAfter(segment.start, run.from)) { pending.delete(mark); continue; }
      if (atOrAfter(position.rtpTimestamp, segment.rtp)) {
        pending.delete(mark);
        client.sendClientMessage('playout', { mark, source: position.source, rtp: position.rtpTimestamp, since: run.from });
        onReport(mark);
      }
    }
  }

  client.on(RTVIEvent.ServerMessage, (message) => {
    const d = message && message.t === 'playout-mark' ? message.d : null;
    if (d && typeof d.mark === 'string' && [d.source, d.start, d.rtp].every(Number.isInteger)) {
      pending.set(d.mark, { source: d.source, start: d.start, rtp: d.rtp });
    }
  });
  client.on(RTVIEvent.TrackStarted, broken);
  client.on(RTVIEvent.TrackStopped, broken);
  client.on(RTVIEvent.Disconnected, () => {
    pending.clear();
    run = null;
  });
  setInterval(sample, SAMPLE_MS);
  return true;
}
