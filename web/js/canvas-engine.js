/**
 * Canvas Engine — Real-Time Degradation & Ring Camera Lifecycle
 * Manages Standby state, event-driven activation (Ding, Motion, Live View),
 * and reactive rendering with compression and physical optical degradation.
 */

const cx = {
  cameraActive: false,        // false = standby, true = active stream
  sessionReason: 'standby',   // 'ding' | 'motion' | 'live' | 'standby'
  sessionTimer: null,
  pixelBlockSize: 1,          // 1 = sharp, >4 = H.264 macroblocking
  frameSkip: 0,               // Freeze frames count (packet loss)
  noiseAmount: 0,             // Digital sensor noise
  blurRadius: 0,              // Optical blur
  pinkTint: false,            // Stuck IR-cut filter
  nightMode: false,           // Monochromatic IR
  flickerActive: false,       // 50/60 Hz power grid flicker
  powerCut: false,            // Power cut: black screen
  brightness: 1.0,            // Overall brightness
  frameCounter: 0,
  flickerPhase: 0,
  isFrozen: false,
  frozenFrames: 0,
  displayFps: 30,
  livePeerConnection: null,
  liveStream: null,
  statsTimer: null,
  previousStats: null,
};

function initCanvasEngine() {
  const video = document.getElementById('live-video-player');
  const canvas = document.getElementById('video-canvas');
  if (!video || !canvas) return;

  // Load official base sample video
  video.src = '/data/samples/good_internet.mp4';
  video.loop = true;
  video.muted = true;
  video.playsInline = true;
  state.selectedVideo = { name: 'good_internet.mp4', path: 'data/samples/good_internet.mp4' };

  video.addEventListener('loadedmetadata', () => {
    canvas.width  = video.videoWidth  || 1280;
    canvas.height = video.videoHeight || 720;
  });

  video.addEventListener('canplay', () => {
    renderLoop();
  });

  updateCanvasEffects();
}

async function prepareLiveRingStream() {
  const video = document.getElementById('live-video-player');
  if (!video || !window.RTCPeerConnection) {
    throw new Error('This browser does not support WebRTC live playback.');
  }
  if (cx.livePeerConnection) return;

  const peer = new RTCPeerConnection();
  cx.livePeerConnection = peer;
  peer.addTransceiver('video', { direction: 'recvonly' });
  peer.ontrack = event => {
    cx.liveStream = event.streams[0];
    video.srcObject = cx.liveStream;
    video.play().catch(() => {});
    renderLoop();
  };

  const offer = await peer.createOffer();
  await peer.setLocalDescription(offer);
  const response = await fetch('/api/ring/whep-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/sdp' },
    body: offer.sdp,
  });
  if (!response.ok) {
    peer.close();
    cx.livePeerConnection = null;
    throw new Error(`Ring WHEP negotiation failed (${response.status})`);
  }
  const session = await response.json();
  await peer.setRemoteDescription({ type: 'answer', sdp: session.sdp_answer });
  cx.previousStats = null;
  cx.statsTimer = setInterval(collectLiveWebrtcStats, 1000);
}

async function collectLiveWebrtcStats() {
  const peer = cx.livePeerConnection;
  if (!peer || typeof peer.getStats !== 'function') return;

  const report = await peer.getStats();
  let inbound = null;
  let candidatePair = null;
  report.forEach(stat => {
    if (stat.type === 'inbound-rtp' && stat.kind === 'video') inbound = stat;
    if (stat.type === 'candidate-pair' && stat.state === 'succeeded' && stat.nominated) {
      candidatePair = stat;
    }
  });
  if (!inbound) return;

  const previous = cx.previousStats;
  const elapsedSeconds = previous ? Math.max(0.001, (inbound.timestamp - previous.timestamp) / 1000) : null;
  const bytesDelta = previous && elapsedSeconds ? Math.max(0, inbound.bytesReceived - previous.bytesReceived) : null;
  const framesDelta = previous && elapsedSeconds ? Math.max(0, inbound.framesReceived - previous.framesReceived) : null;
  const lostDelta = previous && Number.isFinite(inbound.packetsLost) && Number.isFinite(previous.packetsLost)
    ? Math.max(0, inbound.packetsLost - previous.packetsLost)
    : null;
  const receivedDelta = previous && Number.isFinite(inbound.packetsReceived) && Number.isFinite(previous.packetsReceived)
    ? Math.max(0, inbound.packetsReceived - previous.packetsReceived)
    : null;
  const packetCount = lostDelta !== null && receivedDelta !== null
    ? lostDelta + receivedDelta
    : null;
  state.liveWebrtcStats = {
    inbound_rtp: {
      kind: 'video',
      jitter: inbound.jitter ?? null,
      packets_lost: inbound.packetsLost ?? null,
      packet_loss_fraction: packetCount > 0 ? lostDelta / packetCount : (packetCount === 0 ? 0 : null),
      frames_dropped: inbound.framesDropped ?? null,
      frames_received: inbound.framesReceived ?? null,
      frames_per_second: framesDelta !== null ? framesDelta / elapsedSeconds : null,
      bytes_received: inbound.bytesReceived ?? null,
      bitrate_kbps: bytesDelta !== null ? (bytesDelta * 8) / elapsedSeconds / 1000 : null,
    },
    candidate_pair: {
      current_round_trip_time: candidatePair?.currentRoundTripTime ?? null,
      available_outgoing_bitrate: candidatePair?.availableOutgoingBitrate ?? null,
      state: candidatePair?.state ?? null,
    },
  };
  cx.previousStats = inbound;
}

/**
 * Wakes camera on physical or user trigger
 * @param {'ding' | 'motion' | 'live'} reason 
 * @param {number} durationSec (0 = manual until user closes)
 */
function activateCamera(reason, durationSec = 30) {
  const video = document.getElementById('live-video-player');
  const canvas = document.getElementById('video-canvas');
  const standby = document.getElementById('camera-standby-screen');
  const hudLive = document.getElementById('hud-live-indicator');
  const hudMeta = document.getElementById('hud-cam-meta');

  if (!video || !canvas) return;
  if (state.ringMode !== 'MOCK') {
    // Only the Digital Twin has a camera feed (a sample MP4). Ring Partner API live video is not
    // implemented, so nothing is ever shown as a Ring stream.
    showToast('Live video is not available: Ring Partner API streaming is not implemented.', 'error');
    return;
  }

  cx.cameraActive = true;
  cx.sessionReason = reason;

  // UI Updates
  if (standby) standby.classList.add('hidden');
  canvas.classList.add('active');

  // Update activation trigger buttons
  document.querySelectorAll('.cam-trigger-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('trigger-' + reason);
  if (btn) btn.classList.add('active');

  // HUD Labels and Countdown Timer
  const tagNames = { ding: 'LIVE CALL', motion: 'PIR MOTION', live: 'LIVE VIEW' };
  const baseTag = tagNames[reason] || 'STREAM';

  if (hudMeta) {
    hudMeta.textContent = `Ring 1080p HDR • ${Math.round(cx.displayFps)} FPS`;
  }

  video.play().catch(() => {});
  updateCanvasEffects();
  renderLoop();

  // Clear prior session timer
  if (cx.sessionTimer) {
    clearInterval(cx.sessionTimer);
    cx.sessionTimer = null;
  }

  if (durationSec > 0) {
    let remaining = durationSec;
    if (hudLive) {
      hudLive.style.display = 'flex';
      hudLive.innerHTML = `<span class="rec-dot"></span> SIMULATED • ${baseTag} (${remaining}s)`;
    }

    cx.sessionTimer = setInterval(() => {
      remaining--;
      if (remaining <= 0) {
        clearInterval(cx.sessionTimer);
        cx.sessionTimer = null;
        deactivateCamera();
        showToast(`⏱️ Camera session ended (${durationSec}s elapsed - Return to Standby)`, 'info');
      } else {
        if (hudLive) {
          hudLive.innerHTML = `<span class="rec-dot"></span> SIMULATED • ${baseTag} (${remaining}s)`;
        }
      }
    }, 1000);
  } else {
    if (hudLive) {
      hudLive.style.display = 'flex';
      hudLive.innerHTML = `<span class="rec-dot"></span> SIMULATED • ${baseTag}`;
    }
  }

  // Check if session starts under critical degraded condition for automatic diagnostics
  checkEventSessionDiagnostics(reason);
}

function deactivateCamera() {
  const video = document.getElementById('live-video-player');
  const canvas = document.getElementById('video-canvas');
  const standby = document.getElementById('camera-standby-screen');
  const hudLive = document.getElementById('hud-live-indicator');

  cx.cameraActive = false;
  cx.sessionReason = 'standby';
  if (cx.sessionTimer) {
    clearInterval(cx.sessionTimer);
    cx.sessionTimer = null;
  }

  if (video) video.pause();
  if (cx.statsTimer) {
    clearInterval(cx.statsTimer);
    cx.statsTimer = null;
  }
  if (cx.livePeerConnection) {
    cx.livePeerConnection.close();
    cx.livePeerConnection = null;
    cx.liveStream = null;
    if (video) video.srcObject = null;
  }
  state.liveWebrtcStats = null;
  if (canvas) canvas.classList.remove('active');
  if (standby) standby.classList.remove('hidden');
  if (hudLive) hudLive.style.display = 'none';

  document.querySelectorAll('.cam-trigger-btn').forEach(b => b.classList.remove('active'));
  updateLedHaloState();
}

/**
 * Updates canvas effects reactively according to sliders and physical faults
 */
function updateCanvasEffects() {
  const rssi    = state.rssi;
  const loss    = state.loss;
  const fps     = state.fps;
  const bitrate = state.bitrate;
  const battery = state.battery;
  const hw      = state.hardwareFaults || {};

  // Adaptive H.264 pixelation
  const bitrateNorm = Math.max(0, Math.min(1, (bitrate - 50) / (1200 - 50)));
  const rssiNorm    = Math.max(0, Math.min(1, (rssi - (-95)) / ((-45) - (-95))));
  const qualityScore = (bitrateNorm * 0.65 + rssiNorm * 0.35);

  cx.pixelBlockSize = qualityScore > 0.85 ? 1
                    : qualityScore > 0.65 ? 2
                    : qualityScore > 0.45 ? 4
                    : qualityScore > 0.25 ? 8
                    : qualityScore > 0.10 ? 16
                    : 32;

  // Packet loss: freeze probability
  cx.frameSkip = Math.round((loss / 100) * 90);

  // Render FPS
  cx.displayFps = Math.max(1, fps);

  // Physical faults
  cx.nightMode     = hw.nightMode   || false;
  cx.pinkTint      = hw.pinkFilter  || false;
  cx.blurRadius    = hw.dirtyLens   ? 3.8 : 0;
  cx.flickerActive = hw.flicker     || false;
  cx.powerCut      = hw.powerCut    || (state.transformer === 0);
  cx.noiseAmount   = cx.nightMode   ? 0.14 : 0;

  // Brightness based on battery level
  cx.brightness = battery < 5 ? 0.25 : battery < 15 ? 0.6 : 1.0;
  if (cx.powerCut) cx.brightness = 0;

  // Update signal HUD
  updateSignalHUD(rssi, loss, fps);
}

function updateSignalHUD(rssi, loss, fps) {
  const pill = document.getElementById('signal-quality-pill');
  const fpsPill = document.getElementById('hud-fps-live');
  const hudLive = document.getElementById('hud-live-indicator');
  if (!pill) return;

  if (typeof showsMeasuredDataOnly === 'function' && showsMeasuredDataOnly()) {
    // Partner, ERROR or unknown mode: never derive signal quality from Digital Twin sliders.
    pill.textContent = '📡 Signal N/D';
    pill.className = 'signal-quality-pill';
    if (fpsPill) fpsPill.textContent = 'N/D FPS';
    return;
  }

  let label, cls;
  if (cx.powerCut) {
    label = '⚫ NO POWER (0V)'; cls = 'sq-offline';
  } else if (rssi >= -60 && loss < 1 && fps >= 25) {
    label = '✅ Excellent Signal'; cls = 'sq-good';
  } else if (rssi >= -75 && loss < 5 && fps >= 15) {
    label = '⚠️ Weak Signal'; cls = 'sq-fair';
  } else if (rssi >= -88 && loss < 15) {
    label = '🔴 Poor Signal'; cls = 'sq-poor';
  } else {
    label = '❌ Critical Connection'; cls = 'sq-offline';
  }
  pill.textContent = label;
  pill.className = `signal-quality-pill ${cls}`;

  if (fpsPill) fpsPill.textContent = `${Math.max(1, Math.round(fps))} FPS`;
  if (hudLive && cx.powerCut) {
    hudLive.style.color = '#ff3d57';
    hudLive.innerHTML = `<span class="rec-dot" style="background:#ff3d57;"></span> POWER OUTAGE`;
  }
}

let _rafId = null;
let _lastFrameTime = 0;

function renderLoop() {
  if (_rafId) cancelAnimationFrame(_rafId);
  _rafId = requestAnimationFrame(renderFrame);
}

function renderFrame(timestamp) {
  const video  = document.getElementById('live-video-player');
  const canvas = document.getElementById('video-canvas');

  if (!canvas || !video || !cx.cameraActive || video.paused || video.ended) {
    _rafId = requestAnimationFrame(renderFrame);
    return;
  }

  // WebRTC FPS throttling
  const targetInterval = 1000 / cx.displayFps;
  if (timestamp - _lastFrameTime < targetInterval * 0.85) {
    _rafId = requestAnimationFrame(renderFrame);
    return;
  }
  _lastFrameTime = timestamp;

  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const W = canvas.width;
  const H = canvas.height;

  // POWER CUT (0 VAC or electrical outage): black screen
  if (cx.powerCut) {
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, W, H);
    _rafId = requestAnimationFrame(renderFrame);
    return;
  }

  // PACKET LOSS FREEZE ARTIFACTS
  cx.frameCounter++;
  if (cx.frameSkip > 0 && cx.frameCounter % Math.max(1, 90 - cx.frameSkip) === 0) {
    cx.isFrozen = true;
    cx.frozenFrames = Math.round(cx.frameSkip * 0.45);
  }
  if (cx.isFrozen) {
    cx.frozenFrames--;
    if (cx.frozenFrames <= 0) cx.isFrozen = false;
    if (cx.frozenFrames > 4) {
      applyBlockCorruption(ctx, W, H);
    }
    _rafId = requestAnimationFrame(renderFrame);
    return;
  }

  // BASE FRAME (with or without optical blur)
  if (cx.blurRadius > 0) {
    ctx.filter = `blur(${cx.blurRadius}px)`;
  } else {
    ctx.filter = 'none';
  }
  ctx.drawImage(video, 0, 0, W, H);
  ctx.filter = 'none';

  // 50/60 Hz EXTERNAL LIGHTING FLICKER (Rolling Shutter Stripes)
  if (cx.flickerActive) {
    cx.flickerPhase = (cx.flickerPhase + 0.45) % (Math.PI * 2);
    const bandSpacing = 36;
    const bandOffset = Math.round((Date.now() / 14) % bandSpacing);
    ctx.fillStyle = 'rgba(0, 0, 0, 0.45)';
    for (let y = -bandSpacing + bandOffset; y < H; y += bandSpacing) {
      ctx.fillRect(0, y, W, bandSpacing * 0.48);
    }
    const strobe = 0.5 + 0.5 * Math.sin(cx.flickerPhase * 6);
    ctx.fillStyle = `rgba(255, 255, 255, ${0.09 * strobe})`;
    ctx.fillRect(0, 0, W, H);
  }

  // BITRATE / RSSI PIXELATION
  if (cx.pixelBlockSize > 1) {
    applyPixelation(ctx, W, H, cx.pixelBlockSize);
  }

  // PIXEL SHADERS (Pink tint, B/W night vision, digital noise, brightness)
  if (cx.pinkTint || cx.nightMode || cx.noiseAmount > 0 || cx.brightness !== 1.0) {
    applyPixelShader(ctx, W, H);
  }

  // SIGNAL VIGNETTE
  if (cx.pixelBlockSize > 4) {
    applySignalVignette(ctx, W, H);
  }

  _rafId = requestAnimationFrame(renderFrame);
}

function applyPixelation(ctx, W, H, blockSize) {
  const sw = Math.ceil(W / blockSize);
  const sh = Math.ceil(H / blockSize);
  const tmp = document.createElement('canvas');
  tmp.width = sw; tmp.height = sh;
  const tctx = tmp.getContext('2d');
  tctx.drawImage(ctx.canvas, 0, 0, sw, sh);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(tmp, 0, 0, W, H);
  ctx.imageSmoothingEnabled = true;
}

function applyPixelShader(ctx, W, H) {
  const imageData = ctx.getImageData(0, 0, W, H);
  const d = imageData.data;
  const len = d.length;
  const noise = cx.noiseAmount;
  const bright = cx.brightness;

  for (let i = 0; i < len; i += 4) {
    let r = d[i], g = d[i+1], b = d[i+2];

    r *= bright; g *= bright; b *= bright;

    // IR Night Vision
    if (cx.nightMode) {
      const lum = 0.299 * r + 0.587 * g + 0.114 * b;
      const enhanced = Math.min(255, lum * 1.35);
      r = g = b = enhanced;
    }

    // Stuck IR-cut filter (magenta tint)
    if (cx.pinkTint) {
      r = Math.min(255, r * 1.45 + 25);
      g = Math.min(255, g * 0.55);
      b = Math.min(255, b * 1.30 + 40);
    }

    // Digital sensor noise
    if (noise > 0) {
      const n = (Math.random() - 0.5) * 255 * noise;
      r = Math.min(255, Math.max(0, r + n));
      g = Math.min(255, Math.max(0, g + n));
      b = Math.min(255, Math.max(0, b + n));
    }

    d[i] = r; d[i+1] = g; d[i+2] = b;
  }
  ctx.putImageData(imageData, 0, 0);
}

function applyBlockCorruption(ctx, W, H) {
  const numBlocks = Math.floor(Math.random() * 6) + 2;
  for (let b = 0; b < numBlocks; b++) {
    const bx = Math.floor(Math.random() * W);
    const by = Math.floor(Math.random() * H);
    const bw = Math.floor(Math.random() * 40 + 10);
    const bh = Math.floor(Math.random() * 20 + 5);
    const hue = Math.floor(Math.random() * 360);
    ctx.fillStyle = `hsla(${hue}, 80%, 50%, 0.6)`;
    ctx.fillRect(bx, by, bw, bh);
  }
}

function applySignalVignette(ctx, W, H) {
  const grad = ctx.createRadialGradient(W/2, H/2, H*0.25, W/2, H/2, H*0.85);
  const strength = Math.min(0.75, (cx.pixelBlockSize - 4) / 28 * 0.75);
  grad.addColorStop(0, 'rgba(0,0,0,0)');
  grad.addColorStop(1, `rgba(0,0,0,${strength})`);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);
}
