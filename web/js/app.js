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
  partnerApiError:     null,  // last failed status read {status, message, reason}; separate from device state
  ringErrorReason:     null,  // /api/ring/status error_reason (missing_token, token_rejected, ...)
  pollPausedUntil:     0,     // epoch ms; set from Retry-After or the 429 backoff
  eventsPausedUntil:   0,     // epoch ms; Retry-After of /api/ring/events
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

  // Heartbeat monitor & persistence (levels from the severity table, GET /api/thresholds)
  heartbeatRemaining: 15,
  anomalyConsecutiveCount: 0,
  lastAnomalyDomain: null, // dominant domain of the open incident: 'HARDWARE_POWER' | 'NETWORK'
  lastAnomalyReason: null, // dominant reason (a change within the same domain continues the incident)
  lastAnomalyLevel: null,  // 'DEGRADED' | 'CRITICAL' at the last pulse
  diagnosedLevel: null,    // highest level already diagnosed for the open incident (no duplicate reports)
  heartbeatAlertActive: false, // the shown report belongs to the open telemetry incident and is unresolved
  heartbeatIncident: 0,        // bumped when an incident ends (recovery, Reset): late answers are stale
  bedrockConfigured: null,     // from GET /api/engine: are AWS credentials available to the server?
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
  // Severity levels first: without them nothing can be classified, so the Twin stays disabled.
  if (!(await loadSeverityTable())) {
    enterUnknownSourceError('severity thresholds unavailable');
    return;
  }
  await loadEngineStatus();
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
  state.ringErrorReason = status.error_reason || null;
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
    renderRingErrorHint(state.ringErrorReason);
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
  if (monitorLabel) monitorLabel.textContent = 'Ring snapshot: none yet (run Analyze now)';
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
    resetHeartbeatIncident();
    state.heartbeatIncident++;
    updateHeartbeatUI();
  }

  showToast(isWired ? '⚡ Hardwired Installation: Continuous transformer active (required voltage depends on the device model)' : '🔋 Battery-Only Installation: Transformer not applicable', 'info');
}

// ------------------------------------------------------------------
// Heartbeat Engine (15-second cycles)
// ------------------------------------------------------------------
let heartbeatInterval = null;

/** Seconds between pulses: the configured Partner poll interval, or the severity table's heartbeat period. */
function heartbeatPeriod() {
  if (state.ringSource !== 'partner' && SEVERITY) return SEVERITY.heartbeat.period_seconds;
  return state.pollIntervalSeconds;
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
      : `Offline reads ${state.offlineStreak}/${state.offlineAlertReads}`;
    countEl.className = 'persistency-badge ' + (
      state.offlineAlertActive ? 'crit' : state.offlineStreak > 0 ? 'warn' : 'ok'
    );
  } else if (countEl) {
    // Text next to the colour: the problem family and its level (Degraded counts, Critical is immediate).
    const family = { HARDWARE_POWER: 'Power', NETWORK: 'Network' }[state.lastAnomalyDomain];
    const tag = family ? ` [${family} · ${LEVEL_TEXT[state.lastAnomalyLevel] || ''}]` : '';
    const required = heartbeatPulsesRequired();
    countEl.textContent = `${state.anomalyConsecutiveCount}/${required} heartbeats${tag}`;
    countEl.className = 'persistency-badge ' + (
      state.anomalyConsecutiveCount === 0 ? 'ok' :
      state.anomalyConsecutiveCount < required && state.lastAnomalyLevel !== 'CRITICAL' ? 'warn' : 'crit'
    );
  }
}

/**
 * The Twin's worst severity level and dominant anomaly, or null when every metric is GREEN.
 * Same table and algorithm as the backend pre-check (src/config/severity.py):
 * - Hardwired: the transformer band applies (critical at the power-cut band, degraded below the minimum).
 * - Battery-Only: the transformer does not exist; the battery bands apply (critical and low).
 * - Network and stream metrics (RSSI, packet loss, jitter, FPS, bitrate) apply to both.
 */
function getHeartbeatAnomaly() {
  const result = classifyTwinTelemetry();
  if (!result || !result.dominant) return null;
  const d = result.dominant;
  return {
    domain: d.domain, level: result.level, metric: d.metric, label: d.label, reason: d.reason,
    op: d.op, limit: d.limit, unit: d.unit, anomalies: result.anomalies,
    isCriticalImmediate: result.level === 'CRITICAL',
  };
}

/** Ends the open telemetry incident's bookkeeping (counter, family, level, dedup). */
function resetHeartbeatIncident() {
  state.anomalyConsecutiveCount = 0;
  state.lastAnomalyDomain = null;
  state.lastAnomalyReason = null;
  state.lastAnomalyLevel = null;
  state.diagnosedLevel = null;
}

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
  await pollPartnerEvents();
  updateHeartbeatUI();
}

const RING_EVENT_LABELS = { ding: 'Doorbell press', motion: 'Motion', on_demand: 'Live view' };

/**
 * Ring Event History, once per pulse (the server queries Ring at most once per poll interval).
 * New events are analyzed by the server; only reports with a problem come back (D11: silence
 * otherwise) and they are shown like a Digital Twin trigger (Ding / PIR / Live View).
 */
async function pollPartnerEvents() {
  // Ring's rate limit is per partner: a Retry-After on status reads pauses event queries too.
  if (Date.now() < state.eventsPausedUntil || Date.now() < state.pollPausedUntil) return;
  let data;
  try {
    const r = await fetch('/api/ring/events');
    if (!r.ok) {
      const wait = Number(r.headers && r.headers.get('Retry-After'));
      if (wait > 0) state.eventsPausedUntil = Date.now() + wait * 1000;
      return;
    }
    data = await r.json();
  } catch {
    return;   // events are best effort; status polling reports API failures
  }
  renderRingEvents(data.events || []);
  for (const report of data.reports || []) {
    const diag = report.diagnostic_result;
    if (!diag) continue;
    if (report.snapshot) showPartnerSnapshot(report.snapshot);
    renderResult(diag, report);
    showReportProvider(report._provider);
    showAppPushNotification(diag, 'event_session', report.trigger, report.checked_domains);
    setResolvedNote(false);
  }
}

function renderRingEvents(events) {
  const el = document.getElementById('partner-events');
  if (!el) return;
  el.style.display = '';
  el.innerHTML = '';
  const title = document.createElement('div');
  title.textContent = 'Ring events';
  el.appendChild(title);
  const list = document.createElement('ul');
  const lines = events.length ? events.map(e => {
    const when = Number.isFinite(e.start) ? new Date(e.start).toISOString().replace('T', ' ').slice(11, 19) + ' UTC' : 'time N/D';
    return `${RING_EVENT_LABELS[e.event_type] || e.event_type || 'Event'}${e.sub_type ? ` · ${e.sub_type}` : ''} · ${when}`;
  }) : ['No events yet'];
  lines.forEach(text => {
    const li = document.createElement('li');
    li.textContent = text;
    list.appendChild(li);
  });
  el.appendChild(list);
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
    // One incident per problem family (domain): weak -> poor Wi-Fi continues it, a new family restarts it.
    if (state.lastAnomalyDomain !== anomaly.domain) {
      if (state.lastAnomalyDomain) {
        console.log(`[Heartbeat] Dominant problem changed from ${state.lastAnomalyDomain} to ${anomaly.domain}: new incident.`);
      }
      state.anomalyConsecutiveCount = 0;
      state.diagnosedLevel = null;
    }
    const required = heartbeatPulsesRequired();
    // CRITICAL counts as confirmed on its first pulse; DEGRADED needs `required` consecutive pulses.
    state.anomalyConsecutiveCount = anomaly.level === 'CRITICAL'
      ? required : Math.min(required, state.anomalyConsecutiveCount + 1);
    state.lastAnomalyDomain = anomaly.domain;
    state.lastAnomalyReason = anomaly.reason;
    state.lastAnomalyLevel = anomaly.level;

    console.log(`[Heartbeat] ${anomaly.level} [${anomaly.domain}] ${anomaly.reason}: ${state.anomalyConsecutiveCount}/${required} heartbeats.`);

    // Diagnose once per incident and level: a confirmed incident, or an escalation DEGRADED -> CRITICAL.
    // A drop CRITICAL -> DEGRADED keeps the incident (and its report) open without a new diagnosis.
    if (state.anomalyConsecutiveCount >= required && levelRank(anomaly.level) > levelRank(state.diagnosedLevel)) {
      updateHeartbeatUI();
      triggerAutonomousDiagnosis(anomaly.level === 'CRITICAL' ? 'background_critical' : 'background_persistent', anomaly);
      return;
    }
  } else {
    // Everything back to GREEN: the incident is over (transient anomalies are filtered out here too).
    if (state.anomalyConsecutiveCount > 0) {
      console.log(`[Heartbeat] Telemetry back to GREEN. Counter reset to 0 (${state.lastAnomalyDomain} incident ended).`);
    }
    resetHeartbeatIncident();
    // A later anomaly is a new incident even with identical values, whether or not this one was
    // rendered (skipped analysis, or replaced by an event-session report).
    state.heartbeatIncident++;
    if (state.heartbeatAlertActive) resolveHeartbeatAlert();
  }

  updateHeartbeatUI();
}

// Delay before an event session is analyzed, so the canvas has rendered a frame to capture.
let EVENT_DIAGNOSIS_DELAY_MS = 1200;
let pendingEventDiagnosis = null;

/**
 * A trigger (Ring Chime, PIR Motion, Live View) analyzes the session at once, without the heartbeat
 * counter: the backend pre-check checks the telemetry and the captured frame, and Bedrock is only
 * invoked when it finds a problem (otherwise the UI stays silent: no alert, card or message). Triggers fired close together
 * share one analysis, so one event never produces two reports.
 */
function checkEventSessionDiagnostics(reason) {
  if (!autoDiagnosisEnabled()) return;
  if (pendingEventDiagnosis) clearTimeout(pendingEventDiagnosis);
  pendingEventDiagnosis = setTimeout(() => {
    pendingEventDiagnosis = null;
    triggerAutonomousDiagnosis('event_session', reason);
  }, EVENT_DIAGNOSIS_DELAY_MS);
}

// ------------------------------------------------------------------
// Autonomous AI Diagnosis
// ------------------------------------------------------------------
let isAnalyzing = false;

async function triggerAutonomousDiagnosis(triggerOrigin, extraContext = '') {
  if (isAnalyzing) return;
  // Without a reported data source there is nothing to diagnose: never send Twin data as a fallback.
  if (state.ringSource !== 'partner' && state.ringMode !== 'MOCK') return;

  // The open telemetry incident (if any) is diagnosed by this request, whoever started it: the
  // heartbeat will not report it again at this level, so a trigger and the counter never produce
  // two reports for one incident.
  const incidentAnomaly = state.ringSource !== 'partner' ? getHeartbeatAnomaly() : null;
  const previousDiagnosedLevel = state.diagnosedLevel;
  if (incidentAnomaly) {
    if (state.lastAnomalyDomain !== incidentAnomaly.domain) {
      state.anomalyConsecutiveCount = 0;
      state.lastAnomalyDomain = incidentAnomaly.domain;
    }
    state.lastAnomalyReason = incidentAnomaly.reason;
    state.lastAnomalyLevel = incidentAnomaly.level;
    if (levelRank(incidentAnomaly.level) > levelRank(state.diagnosedLevel)) state.diagnosedLevel = incidentAnomaly.level;
  }

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
      // A failed request is reported as a failure, never as a diagnosis; the incident can be diagnosed again.
      state.diagnosedLevel = previousDiagnosedLevel;
      showToast(`❌ Diagnosis failed: ${await responseErrorMessage(r)}`, 'error');
      return;
    }

    const data = await r.json();
    // A Ring snapshot is shown whatever the verdict (it is the image the diagnosis checked).
    if (partner && data.snapshot) showPartnerSnapshot(data.snapshot);

    // A heartbeat analysis that answers after its incident ended (recovery or Reset) is stale:
    // showing it would bring back a resolved alert. Event and manual analyses are not affected.
    if (triggerOrigin.startsWith('background_') && incident !== state.heartbeatIncident) {
      console.log('[Heartbeat] Discarding a late analysis answer: the incident already ended.');
      return;
    }

    const diag = data.diagnostic_result;
    if (data.analysis_skipped || !diag || diag.diagnostic_status === 'HEALTHY') {
      // D11: an automatic analysis that finds nothing stays silent; only "Analyze now" answers explicitly.
      if (triggerOrigin === 'manual') showNoIssues(data);
      return;
    }

    // Render the full diagnostic report (user view + technical details)
    renderResult(diag, data);
    showReportProvider(data._provider);

    // One push notification per incident: a repeat of the alert already shown only updates the report.
    const repeat = state.activeAlert && incidentAnomaly && state.heartbeatAlertActive
      && state.activeAlert.domain === diag.root_cause_domain && state.activeAlert.status === diag.diagnostic_status;
    if (!repeat) showAppPushNotification(diag, triggerOrigin, extraContext, data.checked_domains);

    // A new report is current, not resolved. It is resolved by the heartbeat only when it belongs to the
    // open telemetry incident (every metric back to GREEN); an optical-only event report stays as is.
    setResolvedNote(false);
    state.heartbeatAlertActive = triggerOrigin.startsWith('background_') || !!incidentAnomaly;

  } catch (err) {
    console.error('Error during autonomous diagnosis:', err);
    state.diagnosedLevel = previousDiagnosedLevel;
    showToast(`❌ Diagnosis failed: ${err.message || 'network error'}`, 'error');
  } finally {
    isAnalyzing = false;
    showAnalyzingIndicator(false);
  }
}

/** Partner monitor: the last Ring snapshot with Ring's media time and origin (recording or snapshot). */
function showPartnerSnapshot(meta) {
  const img = document.getElementById('partner-snapshot');
  if (img) {
    img.src = `/api/ring/snapshot?t=${Date.now()}`;
    img.style.display = '';
  }
  const standby = document.getElementById('camera-standby-screen');
  if (standby) standby.style.display = 'none';
  // A still image has no stream, signal or frame rate: hide the live HUD texts (they stay without a snapshot).
  for (const id of ['hud-cam-meta', 'signal-quality-pill', 'hud-fps-live']) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }
  const when = Number.isFinite(meta.media_timestamp)
    ? new Date(meta.media_timestamp).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : 'time N/D';
  const label = document.getElementById('monitor-source-label');
  if (label) label.textContent = `Ring snapshot (latest recording) · ${when} · ${meta.media_origin || 'origin N/D'}`;
}

/** Nominal answer to an explicit "Analyze now" (pre-check, no Bedrock call). */
function showNoIssues(data) {
  const noVideo = data.video_analysis && data.video_analysis.available === false;
  const message = noVideo
    ? '✅ No issue in the measured telemetry (no video frame analyzed). Bedrock was not called.'
    : '✅ No issues detected. Bedrock was not called.';
  showToast(message, 'success');
  // An open incident's report stays on screen until the heartbeat resolves it.
  if (state.heartbeatAlertActive) return;
  const diag = data.diagnostic_result || {
    diagnostic_status: 'HEALTHY', root_cause_domain: 'NONE', confidence_score: 1,
    telemetry_findings: [], visual_findings: [], recommended_mitigation: 'No action needed.',
  };
  renderResult(diag, data);
  showReportProvider(data._provider || 'precheck');
  setResolvedNote(false);
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

/**
 * Push notification as the Ring app would show it: a title and one short line. The full
 * mitigation lives in the report card below, so the banner never repeats it (no duplicate text).
 * Titles quote the severity table's boundaries (limitText), never hard-coded numbers.
 */
function showAppPushNotification(diag, origin, extraContext, checkedDomains) {
  const banner = document.getElementById('app-push-banner');
  const titleEl = document.getElementById('app-push-title');
  const descEl = document.getElementById('app-push-desc');
  const metaEl = document.getElementById('app-push-meta');
  const iconEl = document.getElementById('app-push-icon');
  if (!banner) return;

  const status = diag.diagnostic_status || 'DEGRADED';
  banner.className = 'app-push-banner show ' + (status === 'CRITICAL' ? '' : 'warning');
  iconEl.textContent = status === 'CRITICAL' ? '🚨' : status === 'INSUFFICIENT_DATA' ? '❔' : '⚠️';
  titleEl.textContent = alertTitle(diag, origin, extraContext, checkedDomains);
  descEl.textContent = status === 'INSUFFICIENT_DATA'
    ? 'Some essential metrics were not measured. See the report below.'
    : `${domainLabel(diag.root_cause_domain)}: see the report below for what to do.`;

  if (metaEl) {
    // Model output is untrusted: built as text, never parsed as HTML.
    metaEl.innerHTML = '';
    const domainEl = document.createElement('span');
    domainEl.textContent = `${status} • ${domainLabel(diag.root_cause_domain)}`;
    const confEl = document.createElement('span');
    confEl.textContent = ` • Confidence: ${Math.round((diag.confidence_score || 0) * 100)}%`;
    metaEl.appendChild(domainEl);
    metaEl.appendChild(confEl);
  }
  state.activeAlert = { domain: diag.root_cause_domain, status };
}

const TRIGGER_NAMES = { ding: 'Doorbell Press', motion: 'Motion Detection', live: 'Live View' };

const CHECKED_DOMAIN_NAMES = { image: 'image', network: 'Wi-Fi', power: 'power' };

/** Partner (checked_domains) customer label for INSUFFICIENT_DATA; null for the simulator or other levels. */
function partnerCheckLabel(status, domains) {
  if (!Array.isArray(domains) || status !== 'INSUFFICIENT_DATA') return null;
  return domains.some(d => d.status !== 'not_available') ? 'Partial check' : 'Not enough data';
}

/** "Ring: Partial check (image OK; Wi-Fi and power are not shared by Ring)". */
function partialCheckTitle(domains) {
  const name = d => CHECKED_DOMAIN_NAMES[d.domain] || d.domain;
  const checked = domains.filter(d => d.status !== 'not_available')
    .map(d => `${name(d)} ${d.status === 'ok' ? 'OK' : d.status}`);
  const missing = domains.filter(d => d.status === 'not_available').map(name);
  const notShared = missing.length
    ? `; ${missing.join(' and ')} ${missing.length > 1 ? 'are' : 'is'} not shared by Ring` : '';
  return `Ring: Partial check (${checked.join(', ')}${notShared})`;
}

function alertTitle(diag, origin, extraContext, checkedDomains) {
  const status = diag.diagnostic_status || 'DEGRADED';
  if (partnerCheckLabel(status, checkedDomains) === 'Partial check') return partialCheckTitle(checkedDomains);
  if (status === 'INSUFFICIENT_DATA') return 'Ring: Not enough data to diagnose this device';
  if (origin === 'manual') return `Ring diagnosis: ${status}`;
  if (origin === 'event_session') return `Ring Alert: Issue during ${TRIGGER_NAMES[extraContext] || 'Streaming'}`;
  const anomaly = extraContext && extraContext.reason ? extraContext : null;
  if (!anomaly) return `Ring Alert: ${status}`;
  if (origin === 'background_critical') return `Ring Alert: ${anomaly.reason} (${limitText(anomaly)})`;
  return `Ring Alert: ${anomaly.reason} for ${state.anomalyConsecutiveCount} heartbeats (${limitText(anomaly)})`;
}

function closePushBanner() {
  const banner = document.getElementById('app-push-banner');
  if (banner) banner.classList.remove('show');
}

const PROVIDER_LABELS = {
  bedrock: '☁️ AWS Bedrock (Claude)',
  local_fallback: '⚙️ Local fallback (mock-up)',
  precheck: '⚙️ Local pre-check (no Bedrock call)',
};

/** Engine panel before any diagnosis (and after Reset / Clear): nothing has answered yet. */
function engineIdleLabel() {
  return state.bedrockConfigured === false ? '⚙️ Not configured (using local fallback)' : '⏸️ No diagnosis yet';
}

async function loadEngineStatus() {
  try {
    const r = await fetch('/api/engine');
    if (r.ok) state.bedrockConfigured = !!(await r.json()).credentials_configured;
  } catch {
    // Unknown: the idle label simply says no diagnosis has run yet.
  }
  showReportProvider(null);
}

// Names the engine that produced the last report: a local fallback must not look like a Bedrock answer.
function showReportProvider(provider) {
  const el = document.getElementById('engine-provider');
  if (!el) return;
  el.textContent = provider ? (PROVIDER_LABELS[provider] || `❓ Unknown engine (${provider})`) : engineIdleLabel();
}

const MITIGATION_SOURCE_LABELS = {
  knowledge_base: 'Project knowledge base (Ring-informed, unverified)',
  generated_no_kb_match: 'Generated, no KB match',
  knowledge_base_deterministic: 'Deterministic KB message (Bedrock text rejected twice)',
  generic_deterministic: 'Deterministic generic message (Bedrock text rejected twice, no KB match)',
};

function renderResult(diag, data = {}) {
  const status    = diag.diagnostic_status || 'DEGRADED';
  const domain    = diag.root_cause_domain || 'NONE';
  const confidence= Math.round((diag.confidence_score || 0) * 100);
  const healthy   = status === 'HEALTHY';

  const icons = { HEALTHY: '✅', DEGRADED: '⚠️', CRITICAL: '🚨', INSUFFICIENT_DATA: '❔' };

  // User view (what the Ring app would show): status, problem type, confidence and the mitigation.
  const bar = document.getElementById('diag-status-bar');
  if (bar) bar.className = 'diagnostic-status ' + status;
  document.getElementById('diag-icon').textContent        = icons[status] || '❓';
  // Partner (checked_domains): customer wording for INSUFFICIENT_DATA; the technical value stays in Technical details.
  const partialLabel = partnerCheckLabel(status, data.checked_domains);
  document.getElementById('diag-status-text').textContent = healthy ? 'Healthy' : partialLabel || status;
  document.getElementById('diag-domain-text').textContent = healthy ? 'No issues detected' : domainLabel(domain);

  document.getElementById('conf-fill').style.width = confidence + '%';
  document.getElementById('conf-pct').textContent  = confidence + '%';

  document.getElementById('mitigation-text').textContent = diag.recommended_mitigation || '—';

  // Technical view (support ticket): findings, engine, deterministic level, KB articles, device ID.
  renderList('telemetry-findings', diag.telemetry_findings || []);
  renderList('visual-findings',    diag.visual_findings    || []);
  const articles = data.retrieved_articles || [];
  const tech = {
    'tech-provider': PROVIDER_LABELS[data._provider] || data._provider || '—',
    'tech-model': data._model || '—',
    'tech-level': data.severity_level
      ? data.severity_level + (data.severity_override
        ? ` (model said ${data.severity_override.model}; replaced by the severity table)` : '')
      : status,
    'tech-domain': domain + (data.domain_override
      ? ` (model said ${data.domain_override.model}; replaced by the pre-check)` : ''),
    'tech-sops': articles.length ? articles.map(a => a.id).join(', ') : 'None retrieved',
    'tech-mitigation-source': MITIGATION_SOURCE_LABELS[data.mitigation_source]
      || (data._provider === 'precheck' ? 'Pre-check (no mitigation needed)' : '—'),
    'tech-device-id': (data.telemetry && data.telemetry.device_id) || '—',
  };
  Object.entries(tech).forEach(([id, text]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  });
  // Partner reports only: the settings Ring reported for the device.
  const settings = data.telemetry && data.telemetry.device_context;
  ['tech-device-settings-label', 'tech-device-settings'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = settings ? '' : 'none';
  });
  const settingsEl = document.getElementById('tech-device-settings');
  if (settingsEl) settingsEl.textContent = settings ? deviceSettingsText(settings) : '—';
  state.lastReport = { diag, data };

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

/** Plain-text ticket for support: the technical view of the shown report. */
function ticketText() {
  const report = state.lastReport;
  if (!report) return '';
  const { diag, data } = report;
  const line = id => (document.getElementById(id) || {}).textContent || '—';
  return [
    `Status: ${diag.diagnostic_status} (${domainLabel(diag.root_cause_domain)})`,
    `Root cause domain: ${line('tech-domain')}`,
    `Confidence: ${Math.round((diag.confidence_score || 0) * 100)}%`,
    `Engine: ${line('tech-provider')} | Model: ${line('tech-model')}`,
    `Severity level: ${line('tech-level')}`,
    `KB articles: ${line('tech-sops')} | Mitigation source: ${line('tech-mitigation-source')}`,
    `Device ID: ${line('tech-device-id')}`,
    ...(data.telemetry && data.telemetry.device_context
      ? [`Device settings (from Ring): ${line('tech-device-settings')}`] : []),
    'Telemetry findings:', ...(diag.telemetry_findings || []).map(f => `- ${f}`),
    'Visual findings:', ...(diag.visual_findings || []).map(f => `- ${f}`),
    `Recommended mitigation: ${diag.recommended_mitigation || '—'}`,
    `Data source: ${(data.telemetry && data.telemetry._source) || '—'}`,
  ].join('\n');
}

async function copyTicketDetails() {
  const text = ticketText();
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast('📋 Ticket details copied', 'success');
  } catch {
    showToast('Copy failed: select the technical details and copy them manually', 'error');
  }
}

// ------------------------------------------------------------------
// Diagnostic Console: Clear
// ------------------------------------------------------------------
function setResolvedNote(resolved) {
  const note = document.getElementById('diag-resolved-note');
  const resCont = document.getElementById('result-content');
  // A resolved report is history: greyed out, with the severity it had as secondary text.
  if (resCont) resCont.classList.toggle('resolved', resolved);
  if (!note) return;
  note.style.display = resolved ? '' : 'none';
  const previous = state.lastReport ? state.lastReport.diag.diagnostic_status : null;
  note.textContent = resolved
    ? `✅ Resolved: every metric is back to GREEN${previous ? ` · was ${previous}` : ''}`
    : '';
}

/** The telemetry incident ended (every metric GREEN): the alert ends, the report stays as resolved history. */
function resolveHeartbeatAlert() {
  document.getElementById('app-push-banner')?.classList.remove('show');
  setResolvedNote(true);
  state.heartbeatAlertActive = false;
  state.activeAlert = null;
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
  state.activeAlert = null;
  state.lastReport = null;
  state.diagnosedLevel = null;
  state.heartbeatIncident++;
  showReportProvider(null);
}

function clearDiagnosticConsole() {
  dismissDiagnosis();
  showToast('🧹 Diagnostic console cleared and ready', 'info');
}

// ------------------------------------------------------------------
// Trigger Simulation (Max duration 7s)
// ------------------------------------------------------------------
function simulateDing() {
  pressDoorbellButton();   // chime, halo and a 7 s camera session (one activation, one analysis)
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
