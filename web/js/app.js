/**
 * Ring TelemetryOps — Autonomous Orchestrator
 * Heartbeat cycle (15s pulses), autonomous diagnosis of sustained faults,
 * and smart Ring App notifications (zero manual intervention needed).
 */

const state = {
  selectedVideo: null,
  telemetry:    null,
  ringMode:     'UNKNOWN', // 'MOCK' | 'LIVE' | 'ERROR' once /api/ring/status answers
  ringSource:   'simulator', // 'simulator' | 'partner' (from /api/ring/status)
  lastTelemetryError: null,

  // Partner monitoring (status polling). Thresholds come from the server config and are
  // prototype design choices, not Ring parameters.
  pollIntervalSeconds: 15,
  offlineAlertReads:   3,
  offlineStreak:       0,     // consecutive successful reads with online === false
  offlineAlertActive:  false,
  partnerApiError:     null,  // last failed status read {status, message}; separate from device state
  pollPausedUntil:     0,     // epoch ms; set from Retry-After or the 429 backoff
  rateLimitStreak:     0,     // consecutive 429s without Retry-After (backoff exponent)
  offlineAlertMaxGapSeconds: 60,
  lastEvidenceAt:      0,     // epoch ms of the last read with online true/false
  liveWebrtcStats: null,

  // Parametric sliders
  powerMode:   'wired', // 'wired' | 'battery'
  rssi:        -48,
  battery:     100,
  transformer: 18.0,
  jitter:      2,
  loss:        0,
  fps:         30,
  bitrate:     1200,
  rtt:         18,

  // Hardware physical faults
  hardwareFaults: {
    dirtyLens:  false,
    nightMode:  false,
    pinkFilter: false,
    flicker:    false,
    powerCut:   false,
  },

  // Heartbeat monitor & persistence
  heartbeatRemaining: 15,
  anomalyConsecutiveCount: 0,
  lastAnomalyDomain: null, // 'HARDWARE_POWER' | 'NETWORK'
  lastAnomalyReason: null, // a new reason (even in the same domain) restarts the count
  lastDiagnosedAnomalySignature: null,
  heartbeatAlertActive: false, // the shown alert/report came from the heartbeat and is unresolved
  heartbeatIncident: 0,        // bumped when an incident ends (recovery, Reset): late answers are stale
};

// ------------------------------------------------------------------
// Initialization
// ------------------------------------------------------------------
window.addEventListener('DOMContentLoaded', async () => {
  await checkServer();
  initCanvasEngine();
  await refreshTelemetry();
  updateDeviceLiveBadges();   // replaces the neutral HTML placeholders
  startHeartbeatEngine();
});

const RING_MODES = ['MOCK', 'LIVE', 'ERROR'];
const RING_SOURCES = ['simulator', 'partner'];

/** The data source could not be determined: explicit error, never a fallback to the simulator. */
function enterUnknownSourceError(reason) {
  state.ringMode = 'ERROR';
  disableTwinControls();
  document.getElementById('server-dot').style.background = 'var(--ring-red)';
  document.getElementById('server-status').textContent = `Data source unknown: ${reason}`;
}

async function checkServer() {
  // The mode comes from /api/ring/status alone (/api/scenarios has no consumer in the UI).
  let status;
  try {
    const statusResponse = await fetch('/api/ring/status');
    if (!statusResponse.ok) {
      enterUnknownSourceError(`server returned HTTP ${statusResponse.status}`);
      return;
    }
    status = await statusResponse.json();
  } catch {
    enterUnknownSourceError('server unreachable');
    return;
  }
  if (!status || !RING_MODES.includes(status.mode) || !RING_SOURCES.includes(status.source)) {
    enterUnknownSourceError('unrecognized status response');
    return;
  }
  state.ringMode = status.mode;
  state.ringSource = status.source;
  if (status.monitoring) {
    state.pollIntervalSeconds = status.monitoring.poll_interval_seconds || state.pollIntervalSeconds;
    state.offlineAlertReads = status.monitoring.offline_alert_consecutive_reads || state.offlineAlertReads;
    state.offlineAlertMaxGapSeconds =
      status.monitoring.offline_alert_max_gap_seconds || state.offlineAlertMaxGapSeconds;
  }
  document.getElementById('server-dot').style.background = 'var(--ring-green)';
  const statusEl = document.getElementById('server-status');
  if (state.ringSource === 'partner') applyPartnerModeUI();
  else if (state.ringMode !== 'MOCK') disableTwinControls();   // no data source: Twin values are not data
  if (state.ringMode === 'LIVE' && state.ringSource === 'partner') {
    statusEl.textContent = 'Ring Partner API connected';
  } else if (state.ringMode === 'LIVE') {
    statusEl.textContent = 'Ring LIVE connected';
  } else if (state.ringMode === 'ERROR') {
    statusEl.textContent = 'Ring credentials error';
    document.getElementById('server-dot').style.background = 'var(--ring-red)';
  } else {
    statusEl.textContent = 'Digital Twin active';
  }
}

/**
 * Automatic diagnoses read the Digital Twin sliders, so they only run where those
 * sliders are the data source. Partner mode has no reliable trigger signal yet, and
 * ERROR mode has no data: both use the explicit "Analyze now" button instead.
 */
function autoDiagnosisEnabled() {
  // Only an explicitly reported simulator (MOCK) makes the sliders a data source.
  return state.ringSource !== 'partner' && state.ringMode === 'MOCK';
}

/**
 * Outside the Digital Twin the simulator controls, their simulated values and the Twin identity
 * are disabled and hidden, so nothing simulated is shown next to (or instead of) real data.
 */
function disableTwinControls() {
  document.querySelectorAll(
    'input[type="range"], .hw-tool-btn, #btn-pwr-wired, #btn-pwr-battery, .btn-reset-telemetry, '
    + '.cam-trigger-btn, #ring-bell-btn'
  ).forEach(el => { el.disabled = true; });
  ['twin-status-tag', 'badge-fw', 'twin-identity-fields', 'twin-power-selector', 'twin-sliders', 'twin-hw-controls']
    .forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
  ['btn-pwr-wired', 'btn-pwr-battery'].forEach(id => document.getElementById(id)?.classList.remove('active'));
}

/**
 * Partner mode: Digital Twin controls cannot change official data, so they are disabled
 * and the explicit analysis button replaces autonomous triggers.
 */
function applyPartnerModeUI() {
  disableTwinControls();
  const onlineBadge = document.getElementById('badge-online');
  if (onlineBadge) onlineBadge.style.display = '';
  const analyzeBtn = document.getElementById('btn-analyze-now');
  if (analyzeBtn) analyzeBtn.style.display = '';
  const modeLabel = document.getElementById('autonomous-mode-label');
  if (modeLabel) modeLabel.textContent = '● Manual analysis (Ring Partner API)';
  const sourceLabel = document.getElementById('telemetry-source-label');
  if (sourceLabel) sourceLabel.textContent = 'Ring Partner API (device status only)';
  const monitorLabel = document.getElementById('monitor-source-label');
  if (monitorLabel) monitorLabel.textContent = 'No Ring video: Partner image/stream retrieval not integrated';
  const powerPill = document.getElementById('power-mode-status-pill');
  if (powerPill) powerPill.textContent = 'Power setup N/D (not reported by Ring)';
  const engineDesc = document.getElementById('engine-mode-desc');
  if (engineDesc) {
    engineDesc.textContent = 'Ring Partner API mode: run a diagnosis with "Analyze now". The server fetches official telemetry.';
  }
}

/**
 * Changes Ring electrical installation mode (Hardwired transformer vs Battery only)
 */
function setPowerMode(mode) {
  state.powerMode = mode;
  const isWired = mode === 'wired';

  const btnWired = document.getElementById('btn-pwr-wired');
  const btnBat   = document.getElementById('btn-pwr-battery');
  const pill     = document.getElementById('power-mode-status-pill');
  const trItem   = document.getElementById('item-transformer');
  const trSl     = document.getElementById('sl-transformer');
  const trLbl    = document.getElementById('lbl-transformer');
  const batItem  = document.getElementById('item-battery');
  const batSl    = document.getElementById('sl-battery');
  const batLbl   = document.getElementById('lbl-battery');

  if (btnWired) btnWired.classList.toggle('active', isWired);
  if (btnBat)   btnBat.classList.toggle('active', !isWired);

  if (pill) {
    pill.textContent = isWired ? '⚡ Continuous Hardwired' : '🔋 Battery Only (No wires)';
    pill.style.color = isWired ? 'var(--ring-cyan)' : 'var(--ring-green)';
  }

  if (isWired) {
    if (trItem) trItem.style.opacity = '1';
    if (trSl)   trSl.disabled = false;
    if (trLbl)  trLbl.textContent = state.transformer === 0 ? '0 V (Cut)' : `${state.transformer.toFixed(1)} V`;

    // Active power -> battery is continuously trickle charged at 100%
    if (state.transformer > 0) {
      state.battery = 100;
      if (batSl)   { batSl.value = 100; batSl.disabled = true; }
      if (batItem) batItem.style.opacity = '0.65';
      if (batLbl)  batLbl.textContent = '100% ⚡ (Continuous power)';
    } else {
      // Power cut: allow controlling backup battery level
      if (batSl)   batSl.disabled = false;
      if (batItem) batItem.style.opacity = '1';
      if (batLbl)  batLbl.textContent = `${state.battery}%`;
    }
  } else {
    // Battery Only: transformer disabled, battery free
    if (trItem) trItem.style.opacity = '0.45';
    if (trSl)   trSl.disabled = true;
    if (trLbl)  trLbl.textContent = 'N/A (No wires)';

    if (batSl)   batSl.disabled = false;
    if (batItem) batItem.style.opacity = '1';
    if (batLbl)  batLbl.textContent = `${state.battery}%`;
  }

  updateCanvasEffects();
  updateDeviceLiveBadges();
  refreshTelemetry();

  // Reset power anomalies if transitioned to healthy state
  if (!getHeartbeatAnomaly()) {
    state.anomalyConsecutiveCount = 0;
    state.lastAnomalyDomain = null;
    state.lastAnomalyReason = null;
    state.lastDiagnosedAnomalySignature = null;
    state.heartbeatIncident++;
    updateHeartbeatUI();
  }

  showToast(isWired ? '⚡ Hardwired Installation: Continuous transformer active (required voltage depends on the device model)' : '🔋 Battery-Only Installation: Transformer not applicable', 'info');
}

// ------------------------------------------------------------------
// Heartbeat Engine (15-second cycles)
// ------------------------------------------------------------------
let heartbeatInterval = null;

/** Seconds between pulses: the configured Partner poll interval, or the simulator's 15 s. */
function heartbeatPeriod() {
  return state.ringSource === 'partner' ? state.pollIntervalSeconds : 15;
}

function startHeartbeatEngine() {
  if (heartbeatInterval) clearInterval(heartbeatInterval);

  state.heartbeatRemaining = heartbeatPeriod();
  updateHeartbeatUI();

  heartbeatInterval = setInterval(() => {
    state.heartbeatRemaining--;
    if (state.heartbeatRemaining <= 0) {
      state.heartbeatRemaining = heartbeatPeriod();
      onHeartbeatPulse();
    }
    updateHeartbeatUI();
  }, 1000);
}

function updateHeartbeatUI() {
  const timerEl = document.getElementById('hb-timer');
  const countEl = document.getElementById('hb-persistency');
  if (timerEl) timerEl.textContent = `${state.heartbeatRemaining}s`;

  if (countEl && state.ringSource === 'partner') {
    countEl.textContent = state.offlineAlertActive
      ? `Offline confirmed (${state.offlineStreak} reads)`
      : `Offline ${state.offlineStreak}/${state.offlineAlertReads} reads`;
    countEl.className = 'persistency-badge ' + (
      state.offlineAlertActive ? 'crit' : state.offlineStreak > 0 ? 'warn' : 'ok'
    );
  } else if (countEl) {
    const domainTag = state.lastAnomalyDomain ? ` [${state.lastAnomalyDomain === 'HARDWARE_POWER' ? 'Power' : 'Wi-Fi'}]` : '';
    countEl.textContent = `${state.anomalyConsecutiveCount}/3 heartbeats${domainTag}`;
    countEl.className = 'persistency-badge ' + (
      state.anomalyConsecutiveCount === 0 ? 'ok' :
      state.anomalyConsecutiveCount < 3 ? 'warn' : 'crit'
    );
  }
}

/**
 * Evaluates reportable anomalies in background standby heartbeats.
 * Accurately differentiates between hardwired and battery-only installations:
 * - Hardwired: 0V transformer or power cut is a critical hardware fault (HARDWARE_POWER).
 * - Battery-Only: Transformer does not exist; only evaluates battery depletion (<10%, immediate).
 */
function getHeartbeatAnomaly() {
  // A. Power Evaluation
  if (state.powerMode === 'wired') {
    if (state.transformer === 0) {
      return {
        domain: 'HARDWARE_POWER',
        isCriticalImmediate: true,
        reason: 'Power cut / 0V AC transformer on hardwired doorbell',
      };
    }
    if (state.transformer > 0 && state.transformer < TRANSFORMER_MIN_VAC) {
      return {
        domain: 'HARDWARE_POWER',
        isCriticalImmediate: false,
        reason: 'Transformer undervoltage (<12V AC)',
      };
    }
  } else {
    // Battery-Only Installation
    if (state.battery < BATTERY_CRITICAL_PCT) {
      return {
        domain: 'HARDWARE_POWER',
        isCriticalImmediate: true,
        reason: 'Rechargeable battery critically depleted (<10%)',
      };
    }
    // 10 % and above is not an anomaly; the badge's low/critical colours are display only.
  }

  // B. Network Evaluation (Wi-Fi or IP link)
  if (state.rssi < RSSI_DEGRADED_DBM || state.loss > PACKET_LOSS_DEGRADED_PCT || state.jitter > JITTER_DEGRADED_MS) {
    return {
      domain: 'NETWORK',
      isCriticalImmediate: false,
      reason: state.rssi < RSSI_DEGRADED_DBM ? 'Weak Wi-Fi signal' : 'Degraded IP link',
    };
  }

  return null;
}

/**
 * Executes the background diagnostic heartbeat every 15 seconds
 */
/**
 * Persistent-offline alert (prototype design choice; thresholds come from the server:
 * RING_PARTNER_OFFLINE_ALERT_READS and RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS).
 *
 * "Evidence" is a successful status read where Ring reports online as true or false.
 * The alert is confirmed when the last N evidence reads all reported online === false
 * AND no two successive evidence reads are more than MAX_GAP apart. So:
 *  - online === false                     -> streak + 1 (after the gap check below)
 *  - online === true                      -> streak and alert reset
 *  - online null or API error             -> no evidence: streak unchanged, gap clock keeps running
 *  - evidence after a gap > MAX_GAP       -> streak and alert reset first (old reads are stale)
 * The alert is separate from the Ring-reported state shown on the online badge.
 */
function applyOfflineAlertPolicy(online, nowMs) {
  if (online !== true && online !== false) return;
  const gapMs = state.offlineAlertMaxGapSeconds * 1000;
  if (state.lastEvidenceAt && nowMs - state.lastEvidenceAt > gapMs) {
    state.offlineStreak = 0;
    state.offlineAlertActive = false;
  }
  state.lastEvidenceAt = nowMs;
  if (online === true) {
    state.offlineStreak = 0;
    state.offlineAlertActive = false;
    return;
  }
  state.offlineStreak++;
  if (!state.offlineAlertActive && state.offlineStreak >= state.offlineAlertReads) {
    state.offlineAlertActive = true;
    showToast(`🔴 Ring reports the device offline in ${state.offlineStreak} consecutive reads`, 'error');
  }
}

/**
 * Partner monitoring pulse: one status refresh (skipped while a Retry-After / 429 backoff
 * pause is active), then the offline alert policy. It never calls the diagnostic agent;
 * analysis stays on "Analyze now".
 */
async function pollPartnerStatus() {
  if (Date.now() < state.pollPausedUntil) {
    updateHeartbeatUI();
    return;
  }
  await refreshTelemetry();
  const online = state.telemetry && state.telemetry.device_health
    ? state.telemetry.device_health.online : undefined;
  applyOfflineAlertPolicy(online, Date.now());
  updateHeartbeatUI();
}

async function onHeartbeatPulse() {
  if (state.ringSource === 'partner') {
    await pollPartnerStatus();
    return;
  }
  await refreshTelemetry();

  if (!autoDiagnosisEnabled()) {
    state.anomalyConsecutiveCount = 0;
    state.lastAnomalyDomain = null;
    state.lastAnomalyReason = null;
    updateHeartbeatUI();
    return;
  }

  const anomaly = getHeartbeatAnomaly();

  if (anomaly) {
    // Immediate critical power failure: trigger on 1st pulse without waiting 3 pulses
    if (anomaly.isCriticalImmediate) {
      state.anomalyConsecutiveCount = 3;
      state.lastAnomalyDomain = anomaly.domain;
      state.lastAnomalyReason = anomaly.reason;
      updateHeartbeatUI();
      console.log(`[Heartbeat] CRITICAL power incident on 1st pulse! (${anomaly.reason})`);
      triggerAutonomousDiagnosis('background_immediate_power', 'power_cut');
      return;
    }

    // Persistence rule by reason: a new reason (also within the same domain, e.g. critical ->
    // low battery) is a new anomaly and starts again at 1. The count stops at 3.
    if (state.lastAnomalyReason !== anomaly.reason) {
      if (state.lastAnomalyReason) {
        console.log(`[Heartbeat] Anomaly changed from "${state.lastAnomalyReason}" to "${anomaly.reason}". Resetting counter to 1.`);
      }
      state.anomalyConsecutiveCount = 1;
    } else {
      state.anomalyConsecutiveCount = Math.min(3, state.anomalyConsecutiveCount + 1);
    }
    state.lastAnomalyDomain = anomaly.domain;
    state.lastAnomalyReason = anomaly.reason;

    console.log(`[Heartbeat] Sustained anomaly [${anomaly.domain}]: ${state.anomalyConsecutiveCount}/3 heartbeats.`);

    // If persistent for 3 consecutive heartbeats (45s sustained in same domain)
    if (state.anomalyConsecutiveCount >= 3) {
      triggerAutonomousDiagnosis('background_persistent', anomaly.domain);
    }
  } else {
    // Normalized telemetry: reset counter (transient jitter filtered out)
    if (state.anomalyConsecutiveCount > 0) {
      console.log(`[Heartbeat] Telemetry normalized. Counter reset to 0 (micro-outage of ${state.lastAnomalyDomain} filtered).`);
    }
    state.anomalyConsecutiveCount = 0;
    state.lastAnomalyDomain = null;
    state.lastAnomalyReason = null;
    // The incident is over: a later anomaly is new even with identical values, whether or not
    // this one was rendered (skipped analysis, or replaced by an event-session report).
    state.lastDiagnosedAnomalySignature = null;
    state.heartbeatIncident++;
    if (state.heartbeatAlertActive) resolveHeartbeatAlert();
  }

  updateHeartbeatUI();
}

/**
 * Checks if an active session event (ding, PIR, Live View) warrants diagnosis
 * evaluating active camera feed and streaming telemetry in real-time.
 */
function checkEventSessionDiagnostics(reason) {
  if (!autoDiagnosisEnabled()) return;
  // Let the backend pre-check telemetry and the captured frame. Bedrock is
  // only invoked if that pre-check finds an anomaly.
  setTimeout(() => {
    triggerAutonomousDiagnosis('event_session', reason);
  }, 1200);
}

// ------------------------------------------------------------------
// Autonomous AI Diagnosis
// ------------------------------------------------------------------
let isAnalyzing = false;

async function triggerAutonomousDiagnosis(triggerOrigin, extraContext = '') {
  if (isAnalyzing) return;
  // Without a reported data source there is nothing to diagnose: never send Twin data as a fallback.
  if (state.ringSource !== 'partner' && state.ringMode !== 'MOCK') return;

  // Signature check to avoid repeating identical diagnoses in loop
  const currentSignature = `${state.rssi}_${state.battery}_${state.transformer}_${state.loss}`;
  if (state.lastDiagnosedAnomalySignature === currentSignature && (triggerOrigin === 'background_persistent' || triggerOrigin === 'background_immediate_power')) {
    return;
  }
  state.lastDiagnosedAnomalySignature = currentSignature;

  isAnalyzing = true;
  showAnalyzingIndicator(true);
  const incident = state.heartbeatIncident;

  const partner = state.ringSource === 'partner';
  // Capture canvas frame ONLY if camera is active (never in Partner mode: the canvas is simulated)
  let canvasFrameBase64 = null;
  const isCamActive = !!(typeof cx !== 'undefined' && cx.cameraActive);
  if (isCamActive && state.ringMode !== 'LIVE' && !partner) {
    const canvas = document.getElementById('video-canvas');
    if (canvas && canvas.width > 0 && canvas.height > 0) {
      try {
        const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
        canvasFrameBase64 = dataUrl.replace(/^data:image\/jpeg;base64,/, '');
      } catch (e) {
        console.warn('Canvas frame capture error:', e);
      }
    }
  }

  try {
    // Partner: the server fetches official telemetry itself; no browser metrics are sent.
    const body = partner ? {
      source_mode:      'ring',
      device_id:        (state.telemetry && state.telemetry.device_id) || null,
      camera_active:    false,
      telemetry:        null,
      frame_base64:     null,
    } : {
      frame_base64:     canvasFrameBase64,
      video_path:       state.selectedVideo ? state.selectedVideo.path : 'data/samples/good_internet.mp4',
      telemetry:        mergeLiveTelemetry(state.telemetry || {}),
      keyframe_second:  1.0,
      camera_active:    isCamActive,
      source_mode:      state.ringMode === 'MOCK' ? 'twin' : 'ring',
    };
    const r = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!r.ok) {
      // A failed request is reported as a failure, never as a diagnosis.
      state.lastDiagnosedAnomalySignature = null;
      showToast(`❌ Diagnosis failed: ${await responseErrorMessage(r)}`, 'error');
      return;
    }

    const data = await r.json();
    if (data.analysis_skipped) {
      const noVideo = data.video_analysis && data.video_analysis.available === false;
      showToast(noVideo
        ? '✅ No anomaly in measured telemetry (no video frame analyzed); Bedrock analysis skipped'
        : '✅ Telemetry and video metrics nominal; Bedrock analysis skipped', 'info');
      return;
    }

    // A heartbeat analysis that answers after its incident ended (recovery or Reset) is stale:
    // showing it would bring back a resolved alert. Event and manual analyses are not affected.
    if (triggerOrigin.startsWith('background_') && incident !== state.heartbeatIncident) {
      console.log('[Heartbeat] Discarding a late analysis answer: the incident already ended.');
      return;
    }

    const diag = data.diagnostic_result;

    // Render the full diagnostic report
    renderResult(diag);
    showReportProvider(data._provider);

    // Trigger Smart Ring App Push Notification
    showAppPushNotification(diag, triggerOrigin, extraContext);

    // A new report is current, not resolved. Only heartbeat alerts are resolved by a nominal pulse.
    setResolvedNote(false);
    state.heartbeatAlertActive = triggerOrigin.startsWith('background_');

  } catch (err) {
    console.error('Error during autonomous diagnosis:', err);
    state.lastDiagnosedAnomalySignature = null;
    showToast(`❌ Diagnosis failed: ${err.message || 'network error'}`, 'error');
  } finally {
    isAnalyzing = false;
    showAnalyzingIndicator(false);
  }
}

/** Explicit diagnosis from the UI (the only trigger in Partner mode). */
async function runManualDiagnosis() {
  const btn = document.getElementById('btn-analyze-now');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Analyzing…'; }
  try {
    await triggerAutonomousDiagnosis('manual');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🩺 Analyze now'; }
  }
}

function mergeLiveTelemetry(telemetry) {
  if (state.ringMode !== 'LIVE' || !state.liveWebrtcStats) return telemetry;
  return {
    ...telemetry,
    webrtc_stats: state.liveWebrtcStats,
    measurement_status: {
      ...(telemetry.measurement_status || {}),
      webrtc_stats: 'measured',
    },
  };
}

function showAnalyzingIndicator(active) {
  const pulseDot = document.querySelector('.hb-dot');
  if (pulseDot) {
    pulseDot.style.background = active ? 'var(--ring-yellow)' : 'var(--ring-cyan)';
    pulseDot.style.boxShadow  = active ? '0 0 10px var(--ring-yellow)' : '0 0 6px var(--ring-cyan)';
  }
}

function showAppPushNotification(diag, origin, extraContext) {
  const banner = document.getElementById('app-push-banner');
  const titleEl = document.getElementById('app-push-title');
  const descEl = document.getElementById('app-push-desc');
  const metaEl = document.getElementById('app-push-meta');
  const iconEl = document.getElementById('app-push-icon');
  if (!banner) return;

  const status = diag.diagnostic_status || 'DEGRADED';
  banner.className = 'app-push-banner show ' + (status === 'CRITICAL' ? '' : 'warning');
  iconEl.textContent = status === 'CRITICAL' ? '🚨' : status === 'INSUFFICIENT_DATA' ? '❔' : '⚠️';

  if (status === 'INSUFFICIENT_DATA') {
    titleEl.textContent = 'Ring: Not enough data to diagnose this device';
    descEl.textContent = diag.recommended_mitigation || 'Some essential metrics were not measured.';
  } else if (origin === 'manual') {
    titleEl.textContent = `Ring diagnosis: ${status}`;
    descEl.textContent = diag.recommended_mitigation || 'See the diagnostic console for details.';
  } else if (origin === 'background_immediate_power') {
    if (state.powerMode === 'battery') {
      titleEl.textContent = `Ring Alert: Critical Battery Depleted (≤5%)`;
    } else {
      titleEl.textContent = `Ring Alert: Critical Power Outage (0V AC)`;
    }
    descEl.textContent = diag.recommended_mitigation || 'A critical power loss was detected on your device.';
  } else if (origin === 'background_persistent') {
    const domainLabel = extraContext === 'HARDWARE_POWER' ? 'Power' : 'Wi-Fi Network';
    titleEl.textContent = `Ring Alert: Persistent anomaly in ${domainLabel} (${state.anomalyConsecutiveCount} heartbeats)`;
    descEl.textContent = diag.recommended_mitigation || 'We detected continuous issues with your device.';
  } else {
    const names = { ding: 'Doorbell Call', motion: 'Motion Detection', live: 'Live View' };
    titleEl.textContent = `Ring Alert: Issue during ${names[extraContext] || 'Streaming'}`;
    descEl.textContent = diag.recommended_mitigation || 'Quality or power issues were detected during the active session.';
  }

  if (metaEl) {
    // Model output is untrusted: built as text, never parsed as HTML.
    metaEl.innerHTML = '';
    const domainEl = document.createElement('span');
    domainEl.textContent = `Domain: ${diag.root_cause_domain}`;
    const confEl = document.createElement('span');
    confEl.textContent = ` • Confidence: ${Math.round((diag.confidence_score || 0) * 100)}%`;
    metaEl.appendChild(domainEl);
    metaEl.appendChild(confEl);
  }
}

function closePushBanner() {
  const banner = document.getElementById('app-push-banner');
  if (banner) banner.classList.remove('show');
}

// Names the engine that produced the shown report: a local fallback must not look like a Bedrock answer.
function showReportProvider(provider) {
  const el = document.getElementById('engine-provider');
  if (!el) return;
  const labels = {
    bedrock: '☁️ AWS Bedrock (Claude)',
    local_fallback: '⚙️ Local fallback (Bedrock unavailable)',
    precheck: '⚙️ Local pre-check (no Bedrock call)',
  };
  el.textContent = labels[provider] || '☁️ AWS Bedrock (Claude)';
}

function renderResult(diag) {
  const status    = diag.diagnostic_status || 'DEGRADED';
  const domain    = diag.root_cause_domain || 'NONE';
  const confidence= Math.round((diag.confidence_score || 0) * 100);

  const icons = { HEALTHY: '✅', DEGRADED: '⚠️', CRITICAL: '🚨', INSUFFICIENT_DATA: '❔' };
  const labels = {
    NETWORK:                  'Network Issue (Wi-Fi / Upstream)',
    NETWORK_WEBRTC:           'WebRTC Connection / Firewall Error',
    OPTICAL_PHYSICAL:         'Optical / Physical / Lens Issue',
    VIDEO_OPTICS:             'Optical Interference / LED Frequency (50Hz/60Hz)',
    HARDWARE_POWER:           'Power Supply / Battery Failure',
    ENVIRONMENT_POWER:        'Extreme Temperature / Cold Battery',
    AUDIO_NETWORK:            'Two-Way Audio / Echo / Jitter',
    ALGORITHM_CONFIGURATION:  'Configuration / Mounting Height or PIR Zones',
    NONE:                     'Healthy Device and Stream',
    UNKNOWN:                  'Undetermined (not enough measured data)',
  };

  const bar = document.getElementById('diag-status-bar');
  if (bar) bar.className = 'diagnostic-status ' + status;
  document.getElementById('diag-icon').textContent        = icons[status] || '❓';
  document.getElementById('diag-status-text').textContent = status;
  document.getElementById('diag-domain-text').textContent = labels[domain] || domain;

  document.getElementById('conf-fill').style.width = confidence + '%';
  document.getElementById('conf-pct').textContent  = confidence + '%';

  renderList('telemetry-findings', diag.telemetry_findings || []);
  renderList('visual-findings',    diag.visual_findings    || []);

  document.getElementById('mitigation-text').textContent = diag.recommended_mitigation || '—';

  const resCont = document.getElementById('result-content');
  const emptyRes = document.getElementById('empty-result');
  if (resCont) resCont.style.display = '';
  if (emptyRes) emptyRes.style.display = 'none';
}

function renderList(id, items) {
  const ul = document.getElementById(id);
  if (!ul) return;
  ul.innerHTML = '';
  items.forEach(item => {
    const li = document.createElement('li');
    li.textContent = item;
    ul.appendChild(li);
  });
  if (items.length === 0) {
    const li = document.createElement('li');
    li.textContent = 'No findings';
    ul.appendChild(li);
  }
}

// ------------------------------------------------------------------
// Diagnostic Console: Clear
// ------------------------------------------------------------------
function setResolvedNote(resolved) {
  const note = document.getElementById('diag-resolved-note');
  if (!note) return;
  note.style.display = resolved ? '' : 'none';
  note.textContent = resolved ? '✅ Resolved — telemetry back to nominal' : '';
}

/** Heartbeat anomaly recovered: the alert ends, the report stays as history marked resolved. */
function resolveHeartbeatAlert() {
  document.getElementById('app-push-banner')?.classList.remove('show');
  setResolvedNote(true);
  state.heartbeatAlertActive = false;
  state.lastDiagnosedAnomalySignature = null;   // a later incident is new, even with the same values
}

/** Removes the active alert and the report (Reset / Clear). */
function dismissDiagnosis() {
  const resCont = document.getElementById('result-content');
  const emptyRes = document.getElementById('empty-result');
  if (resCont) resCont.style.display = 'none';
  if (emptyRes) emptyRes.style.display = '';
  document.getElementById('app-push-banner')?.classList.remove('show');
  setResolvedNote(false);
  state.heartbeatAlertActive = false;
  state.lastDiagnosedAnomalySignature = null;
  state.heartbeatIncident++;
}

function clearDiagnosticConsole() {
  dismissDiagnosis();
  showToast('🧹 Diagnostic console cleared and ready', 'info');
}

// ------------------------------------------------------------------
// Trigger Simulation (Max duration 7s)
// ------------------------------------------------------------------
function simulateDing() {
  pressDoorbellButton();
  activateCamera('ding', 7);
}

function simulateMotion() {
  // PIR sensor triggers
  const dome = document.querySelector('.ring-pir-dome');
  if (dome) {
    dome.style.boxShadow = '0 0 10px rgba(0,212,255,0.8), inset 0 0 4px #00d4ff';
    setTimeout(() => { dome.style.boxShadow = ''; }, 3000);
  }
  showToast('🚶 PIR Sensor: Motion detected in front zone', 'info');
  activateCamera('motion', 7);
}

function toggleLiveView() {
  if (cx.cameraActive && cx.sessionReason === 'live') {
    deactivateCamera();
    showToast('👁️ Live View closed', 'info');
  } else {
    showToast('👁️ Simulated Live View opened (Digital Twin sample video)', 'info');
    activateCamera('live', 0); // manual
  }
}

// ------------------------------------------------------------------
// Ring Knowledge Base (RAG Modal)
// ------------------------------------------------------------------
let kbArticles = [];

async function openKbModal() {
  document.getElementById('kb-modal')?.classList.add('show');
  if (kbArticles.length === 0) {
    try {
      const r = await fetch('/api/kb');
      if (r.ok) {
        const data = await r.json();
        kbArticles = data.articles || [];
        renderKbModal(kbArticles);
      }
    } catch {
      document.getElementById('kb-list').innerHTML = '<p style="color:var(--ring-red)">Error loading knowledge base.</p>';
    }
  } else {
    renderKbModal(kbArticles);
  }
}

function closeKbModal() {
  document.getElementById('kb-modal')?.classList.remove('show');
}

function filterKbArticles(val) {
  const q = val.toLowerCase().trim();
  if (!q) {
    renderKbModal(kbArticles);
    return;
  }
  const filtered = kbArticles.filter(a => {
    const str = [a.title, a.issue_type, ...(a.symptoms || []), ...(a.recommended_actions || [])]
      .filter(Boolean).join(' ').toLowerCase();
    return str.includes(q);
  });
  renderKbModal(filtered);
}

/** Escapes text for interpolation into an HTML template (KB content is data, not markup). */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function renderKbModal(articles) {
  const container = document.getElementById('kb-list');
  if (!container) return;
  container.innerHTML = '';
  if (articles.length === 0) {
    container.innerHTML = '<p style="color:var(--text-muted);font-size:0.85rem;">No articles found matching that filter.</p>';
    return;
  }

  articles.forEach(a => {
    const card = document.createElement('div');
    card.className = 'kb-card';
    const sources = Array.isArray(a.official_sources) && a.official_sources.length
      ? a.official_sources.join('; ') : 'No source listed';
    card.innerHTML = `
      <div class="kb-card-header">
        <div class="kb-card-title">${escapeHtml(a.title)}</div>
        <span class="kb-card-id">${escapeHtml(a.id)}</span>
      </div>
      <div class="kb-card-source">📖 Cited sources (article titles, not verified by this app): <em>${escapeHtml(sources)}</em></div>
      <div class="kb-card-cause"><strong>Issue type:</strong> ${escapeHtml(a.issue_type || 'Not specified')}</div>
      <div style="font-size:0.78rem;color:var(--text-secondary);margin-bottom:6px;">
        <strong>Detectable symptoms:</strong>
        <ul style="padding-left:16px;margin-top:4px;">
          ${(a.symptoms || []).map(s => `<li>${escapeHtml(s)}</li>`).join('')}
        </ul>
      </div>
      <div style="font-size:0.78rem;color:var(--ring-cyan);margin-top:6px;">
        <strong>Recommended actions:</strong>
        <ul style="padding-left:16px;margin-top:4px;">
          ${(a.recommended_actions || []).slice(0, 3).map(act => `<li>${escapeHtml(act)}</li>`).join('')}
        </ul>
      </div>
      <div class="kb-tag-list">
        <span class="kb-tag">Domain: ${escapeHtml(a.root_cause_domain || 'Not specified')}</span>
        <span class="kb-tag">Category: ${escapeHtml(a.issue_category || 'Not specified')}</span>
      </div>
    `;
    container.appendChild(card);
  });
}

// ------------------------------------------------------------------
// Toast Notifications
// ------------------------------------------------------------------
let toastTimer;
function showToast(msg, type = 'info') {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.className   = 'show ' + (type === 'error' ? 'error' : type === 'success' ? 'success' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}
