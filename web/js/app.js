/**
 * Ring TelemetryOps — Autonomous Orchestrator
 * Heartbeat cycle (15s pulses), autonomous diagnosis of sustained faults,
 * and smart Ring App notifications (zero manual intervention needed).
 */

const state = {
  provider:     'bedrock',
  selectedVideo: null,
  telemetry:    null,

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
  lastDiagnosedAnomalySignature: null,
};

// ------------------------------------------------------------------
// Initialization
// ------------------------------------------------------------------
window.addEventListener('DOMContentLoaded', async () => {
  await checkServer();
  initCanvasEngine();
  await refreshTelemetry();
  startHeartbeatEngine();
});

async function checkServer() {
  try {
    const r = await fetch('/api/scenarios');
    if (r.ok) {
      document.getElementById('server-dot').style.background = 'var(--ring-green)';
      document.getElementById('server-status').textContent = 'Server online';
    }
  } catch {
    document.getElementById('server-dot').style.background = 'var(--ring-red)';
    document.getElementById('server-status').textContent = 'Offline';
  }
}

function selectProvider(p) {
  state.provider = p;
  document.getElementById('prov-groq')?.classList.toggle('active', p === 'groq');
  document.getElementById('prov-bedrock')?.classList.toggle('active', p === 'bedrock');
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
    updateHeartbeatUI();
  }

  showToast(isWired ? '⚡ Hardwired Installation: Continuous transformer active (16-24V)' : '🔋 Battery-Only Installation: Transformer not applicable', 'info');
}

// ------------------------------------------------------------------
// Heartbeat Engine (15-second cycles)
// ------------------------------------------------------------------
let heartbeatInterval = null;

function startHeartbeatEngine() {
  if (heartbeatInterval) clearInterval(heartbeatInterval);

  state.heartbeatRemaining = 15;
  updateHeartbeatUI();

  heartbeatInterval = setInterval(() => {
    state.heartbeatRemaining--;
    if (state.heartbeatRemaining <= 0) {
      state.heartbeatRemaining = 15;
      onHeartbeatPulse();
    }
    updateHeartbeatUI();
  }, 1000);
}

function updateHeartbeatUI() {
  const timerEl = document.getElementById('hb-timer');
  const countEl = document.getElementById('hb-persistency');
  if (timerEl) timerEl.textContent = `${state.heartbeatRemaining}s`;

  if (countEl) {
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
 * - Battery-Only: Transformer does not exist; only evaluates battery depletion (≤10%).
 */
function getHeartbeatAnomaly() {
  // A. Power Evaluation
  if (state.powerMode === 'wired') {
    if (state.hardwareFaults.powerCut || state.transformer === 0) {
      return {
        domain: 'HARDWARE_POWER',
        isCriticalImmediate: true,
        reason: 'Power cut / 0V AC transformer on hardwired doorbell',
      };
    }
    if (state.transformer > 0 && state.transformer < 12.0) {
      return {
        domain: 'HARDWARE_POWER',
        isCriticalImmediate: false,
        reason: 'Transformer undervoltage (<12V AC)',
      };
    }
  } else {
    // Battery-Only Installation
    if (state.battery <= 5) {
      return {
        domain: 'HARDWARE_POWER',
        isCriticalImmediate: true,
        reason: 'Rechargeable battery critically depleted (≤5%)',
      };
    }
    if (state.battery <= 15) {
      return {
        domain: 'HARDWARE_POWER',
        isCriticalImmediate: false,
        reason: 'Rechargeable battery low (≤15%)',
      };
    }
  }

  // B. Network Evaluation (Wi-Fi or IP link)
  if (state.rssi < -75 || state.loss > 8 || state.jitter > 40) {
    return {
      domain: 'NETWORK',
      isCriticalImmediate: false,
      reason: state.rssi < -75 ? 'Weak Wi-Fi signal' : 'Degraded IP link',
    };
  }

  return null;
}

/**
 * Executes official background diagnostic heartbeat every 15 seconds
 */
async function onHeartbeatPulse() {
  await refreshTelemetry();

  const anomaly = getHeartbeatAnomaly();

  if (anomaly) {
    // Immediate critical power failure: trigger on 1st pulse without waiting 3 pulses
    if (anomaly.isCriticalImmediate) {
      state.anomalyConsecutiveCount = 3;
      state.lastAnomalyDomain = anomaly.domain;
      updateHeartbeatUI();
      console.log(`[Heartbeat] CRITICAL power incident on 1st pulse! (${anomaly.reason})`);
      triggerAutonomousDiagnosis('background_immediate_power', 'power_cut');
      return;
    }

    // Persistence rule by domain:
    // If anomaly domain changes (e.g. NETWORK to HARDWARE_POWER), reset counter to 1
    if (state.lastAnomalyDomain && state.lastAnomalyDomain !== anomaly.domain) {
      console.log(`[Heartbeat] Anomaly domain changed from ${state.lastAnomalyDomain} to ${anomaly.domain}. Resetting counter to 1.`);
      state.anomalyConsecutiveCount = 1;
    } else {
      state.anomalyConsecutiveCount++;
    }
    state.lastAnomalyDomain = anomaly.domain;

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
  }

  updateHeartbeatUI();
}

/**
 * Checks if an active session event (ding, PIR, Live View) warrants diagnosis
 * evaluating active camera feed and streaming telemetry in real-time.
 */
function checkEventSessionDiagnostics(reason) {
  const hw = state.hardwareFaults || {};
  const isOpticalFault = hw.dirtyLens || hw.pinkFilter || hw.flicker;
  const isPowerFault   = hw.powerCut || state.transformer === 0 || state.battery <= 10;
  const isNetworkFault = state.rssi < -75 || state.loss > 8 || state.jitter > 40 || state.fps < 15 || state.bitrate < 300;

  if (isOpticalFault || isPowerFault || isNetworkFault) {
    // User experiencing degraded session in real-time: trigger autonomous diagnosis
    setTimeout(() => {
      triggerAutonomousDiagnosis('event_session', reason);
    }, 1200);
  }
}

// ------------------------------------------------------------------
// Autonomous AI Diagnosis
// ------------------------------------------------------------------
let isAnalyzing = false;

async function triggerAutonomousDiagnosis(triggerOrigin, extraContext = '') {
  if (isAnalyzing) return;

  // Signature check to avoid repeating identical diagnoses in loop
  const currentSignature = `${state.rssi}_${state.battery}_${state.transformer}_${state.loss}_${JSON.stringify(state.hardwareFaults)}`;
  if (state.lastDiagnosedAnomalySignature === currentSignature && (triggerOrigin === 'background_persistent' || triggerOrigin === 'background_immediate_power')) {
    return;
  }
  state.lastDiagnosedAnomalySignature = currentSignature;

  isAnalyzing = true;
  showAnalyzingIndicator(true);

  // Capture canvas frame ONLY if camera is active
  let canvasFrameBase64 = null;
  const isCamActive = !!(typeof cx !== 'undefined' && cx.cameraActive);
  if (isCamActive) {
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
    const r = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        frame_base64:     canvasFrameBase64,
        video_path:       state.selectedVideo ? state.selectedVideo.path : 'data/samples/good_internet.mp4',
        telemetry:        state.telemetry || {},
        provider:         state.provider,
        keyframe_second:  1.0,
        camera_active:    isCamActive,
      }),
    });

    if (r.ok) {
      const data = await r.json();
      const diag = data.diagnostic_result;

      // Render full official report
      renderResult(diag);

      // Trigger Smart Ring App Push Notification
      showAppPushNotification(diag, triggerOrigin, extraContext);
    }
  } catch (err) {
    console.error('Error during autonomous diagnosis:', err);
  } finally {
    isAnalyzing = false;
    showAnalyzingIndicator(false);
  }
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
  iconEl.textContent = status === 'CRITICAL' ? '🚨' : '⚠️';

  if (origin === 'background_immediate_power') {
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
    metaEl.innerHTML = `<span>Domain: ${diag.root_cause_domain}</span> • <span>AI Confidence: ${Math.round((diag.confidence_score || 0) * 100)}%</span>`;
  }
}

function closePushBanner() {
  const banner = document.getElementById('app-push-banner');
  if (banner) banner.classList.remove('show');
}

function renderResult(diag) {
  const status    = diag.diagnostic_status || 'DEGRADED';
  const domain    = diag.root_cause_domain || 'NONE';
  const confidence= Math.round((diag.confidence_score || 0) * 100);

  const icons = { HEALTHY: '✅', DEGRADED: '⚠️', CRITICAL: '🚨' };
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
function clearDiagnosticConsole() {
  const resCont = document.getElementById('result-content');
  const emptyRes = document.getElementById('empty-result');
  const banner = document.getElementById('app-push-banner');
  if (resCont) resCont.style.display = 'none';
  if (emptyRes) emptyRes.style.display = '';
  if (banner) banner.classList.remove('show');

  state.lastDiagnosedAnomalySignature = null;
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
    showToast('👁️ WHEP connection established: Live View opened', 'info');
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
    const str = (a.title + ' ' + a.root_cause + ' ' + (a.symptoms || []).join(' ') + ' ' + (a.recommended_actions || []).join(' ')).toLowerCase();
    return str.includes(q);
  });
  renderKbModal(filtered);
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
    card.innerHTML = `
      <div class="kb-card-header">
        <div class="kb-card-title">${a.title}</div>
        <span class="kb-card-id">${a.id}</span>
      </div>
      <div class="kb-card-source">📖 Official source: <em>${a.official_ring_source || 'Ring Support Center'}</em></div>
      <div class="kb-card-cause"><strong>Root Cause:</strong> ${a.root_cause}</div>
      <div style="font-size:0.78rem;color:var(--text-secondary);margin-bottom:6px;">
        <strong>Detectable symptoms:</strong>
        <ul style="padding-left:16px;margin-top:4px;">
          ${(a.symptoms || []).map(s => `<li>${s}</li>`).join('')}
        </ul>
      </div>
      <div style="font-size:0.78rem;color:var(--ring-cyan);margin-top:6px;">
        <strong>Official recommended actions:</strong>
        <ul style="padding-left:16px;margin-top:4px;">
          ${(a.recommended_actions || []).slice(0, 3).map(act => `<li>${act}</li>`).join('')}
        </ul>
      </div>
      <div class="kb-tag-list">
        <span class="kb-tag">Domain: ${a.root_cause_domain}</span>
        <span class="kb-tag">Category: ${a.issue_category}</span>
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
