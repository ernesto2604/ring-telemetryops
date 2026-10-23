/**
 * whep.js — Ring live view (WHEP) in Partner mode.
 *
 * Ring's API only accepts server-to-server calls (browsers are blocked by CORS), so this page builds the
 * SDP offer and our server forwards it to Ring: the browser only ever calls /api/ring/whep/session and
 * never sees the Ring token or Ring's session URL. One attempt per click, no automatic reconnection.
 * Frames are only shown: they are never sent for analysis or stored.
 */

const RING_WHEP_ENDPOINT = '/api/ring/whep/session';
const RING_WHEP_STUN = 'stun:stun.l.google.com:19302';
const RING_WHEP_ICE_TIMEOUT_MS = 4000;   // after this the offer goes with the candidates gathered so far
const RING_WHEP_BUTTON_LABEL = '🔴 Ring live view (up to 60 s)';

// The running session, or null. Set to null first thing in stopRingLiveView, so it ends once.
let ringLive = null;

function ringLiveEl(id) {
  return document.getElementById(id);
}

function ringLiveStatus(text) {
  const line = ringLiveEl('ring-live-view-status');
  if (!line) return;
  line.textContent = text;
  line.style.display = text ? '' : 'none';
}

/** Resolves when ICE gathering completes or after `timeoutMs`, whichever comes first. */
function waitForIceGathering(session, timeoutMs) {
  if (session.pc.iceGatheringState === 'complete') return Promise.resolve(true);
  return new Promise(resolve => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    session.iceDone = () => { clearTimeout(timer); resolve(true); };
  });
}

function hhmmss(date) {
  return [date.getHours(), date.getMinutes(), date.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
}

/** The <video> replaces the snapshot on the Partner monitor while the session lasts. */
function showRingLiveVideo(session, stream) {
  const video = ringLiveEl('ring-live-video');
  if (video) {
    video.srcObject = stream;
    video.style.display = '';
  }
  const img = ringLiveEl('partner-snapshot');
  if (img) img.style.display = 'none';
  const standby = ringLiveEl('camera-standby-screen');
  if (standby) standby.style.display = 'none';
  const label = ringLiveEl('monitor-source-label');
  if (label) label.textContent = `Ring live view · started ${hhmmss(session.startedAt)}`;
}

function restoreRingMonitor(session) {
  const video = ringLiveEl('ring-live-video');
  if (video) {
    video.srcObject = null;
    video.style.display = 'none';
  }
  const { monitor } = session;
  const img = ringLiveEl('partner-snapshot');
  if (img) img.style.display = monitor.snapshotDisplay;
  const standby = ringLiveEl('camera-standby-screen');
  if (standby) standby.style.display = monitor.standbyDisplay;
  const label = ringLiveEl('monitor-source-label');
  if (label) label.textContent = monitor.label;
}

function ringLiveCountdown(session) {
  const left = Math.max(0, session.maxSeconds - session.elapsed);
  ringLiveStatus(`Ring live view: ends automatically within 30–60 s (about ${left} s left)`);
  if (left === 0) stopRingLiveView('session ended');
}

/** "Ring live view (up to 60 s)": one WHEP session through our server. */
async function startRingLiveView() {
  if (ringLive) return;
  const img = ringLiveEl('partner-snapshot');
  const standby = ringLiveEl('camera-standby-screen');
  const label = ringLiveEl('monitor-source-label');
  const pc = new RTCPeerConnection({ iceServers: [{ urls: RING_WHEP_STUN }] });
  const session = {
    pc, sessionId: null, startedAt: null, elapsed: 0, maxSeconds: 60, timers: [], iceDone: null,
    monitor: {
      snapshotDisplay: img ? img.style.display : 'none',
      standbyDisplay: standby ? standby.style.display : '',
      label: label ? label.textContent : '',
    },
  };
  ringLive = session;
  const btn = ringLiveEl('btn-ring-live');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Connecting to Ring…'; }
  const stopBtn = ringLiveEl('btn-ring-live-stop');
  if (stopBtn) stopBtn.style.display = '';
  ringLiveStatus('Ring live view: connecting…');

  // Handlers before the offer: attached later, Ring's video does not show up.
  pc.ontrack = event => {
    if (ringLive !== session) return;
    showRingLiveVideo(session, (event.streams && event.streams[0]) || new MediaStream([event.track]));
  };
  pc.onicecandidate = event => {
    if (!event.candidate && session.iceDone) session.iceDone();
  };
  pc.onconnectionstatechange = () => {
    if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) {
      stopRingLiveView(`connection ${pc.connectionState}`);
    }
  };

  try {
    pc.addTransceiver('video', { direction: 'recvonly' });   // no audio
    await pc.setLocalDescription(await pc.createOffer());
    await waitForIceGathering(session, RING_WHEP_ICE_TIMEOUT_MS);
    if (ringLive !== session) return;
    const response = await fetch(RING_WHEP_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: pc.localDescription.sdp }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = (data && data.detail) || {};
      const message = typeof detail === 'string' ? detail : (detail.message || `HTTP ${response.status}`);
      if (detail.reason && typeof renderRingErrorHint === 'function') renderRingErrorHint(detail.reason);
      stopRingLiveView(null, { error: detail.reason ? `${message} (${detail.reason})` : message });
      return;
    }
    if (ringLive !== session) {
      // Stopped while Ring was answering: close the session the server just opened.
      fetch(`${RING_WHEP_ENDPOINT}/${encodeURIComponent(data.session_id)}`, { method: 'DELETE' }).catch(() => {});
      return;
    }
    session.sessionId = data.session_id;
    session.maxSeconds = data.max_seconds_hint || 60;
    await pc.setRemoteDescription({ type: 'answer', sdp: data.sdp_answer });
    if (ringLive !== session) return;
    session.startedAt = new Date();
    if (btn) btn.textContent = '🔴 Ring live view running';
    ringLiveCountdown(session);
    session.timers.push(setInterval(() => {
      session.elapsed += 1;
      ringLiveCountdown(session);
    }, 1000));
  } catch (err) {
    stopRingLiveView(null, { error: (err && err.message) || 'the browser could not start WebRTC' });
  }
}

/**
 * Ends the live view (Stop, error, connection lost, end of the session or page unload): closes the
 * connection, stops the tracks, closes the session on the server once and restores the monitor.
 */
function stopRingLiveView(reason, { keepalive = false, error = null } = {}) {
  const session = ringLive;
  if (!session) return;
  ringLive = null;
  session.timers.forEach(clearInterval);
  const { pc } = session;
  pc.ontrack = pc.onicecandidate = pc.onconnectionstatechange = null;
  for (const receiver of (pc.getReceivers ? pc.getReceivers() : [])) {
    if (receiver.track) receiver.track.stop();
  }
  pc.close();
  if (session.sessionId) {
    fetch(`${RING_WHEP_ENDPOINT}/${encodeURIComponent(session.sessionId)}`, { method: 'DELETE', keepalive })
      .catch(() => {});
  }
  restoreRingMonitor(session);
  const btn = ringLiveEl('btn-ring-live');
  if (btn) { btn.disabled = false; btn.textContent = RING_WHEP_BUTTON_LABEL; }
  const stopBtn = ringLiveEl('btn-ring-live-stop');
  if (stopBtn) stopBtn.style.display = 'none';
  ringLiveStatus(error ? `Ring live view is not available: ${error}` : `Ring live view ended${reason ? ` (${reason})` : ''}`);
}

window.addEventListener('beforeunload', () => stopRingLiveView('page closed', { keepalive: true }));
