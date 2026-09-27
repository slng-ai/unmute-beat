/*
 * What core's browser page takes from Pipecat's official web client SDK: the client, its transport, and
 * `setAboutClient` / `learnAboutClient`, which declare the playback capability in the client's own
 * `client-ready`. Re-exported as published; `npm run build` bundles them into core/src/core/client/sdk.js.
 */
export { PipecatClient, RTVIEvent, learnAboutClient, setAboutClient } from '@pipecat-ai/client-js';
export { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport';
