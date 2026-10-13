/**
 * Official Ring Parametric Telemetry Management
 * Controls real-time continuous sliders, backend synchronization,
 * and reactive device health badge updates.
 */

let telemetryDebounceTimer = null;

function updateSlider(key, val) {
  const v = parseFloat(val);
  state[key] = v;

  const labels = {
    rssi:        `${v} dBm`,
    battery:     `${v}%`,
    transformer: v === 0 ? '0 V (Cut)' : `${v.toFixed(1)} V`,
    jitter:      `${v} ms`,
    loss:        `${v}%`,
    fps:         `${v} fps`,
    bitrate:     `${v} kbps`,
    rtt:         `${v} ms`,
  };
  const lbl = document.getElementById('lbl-' + key);
  if (lbl) lbl.textContent = labels[key];

  if (key === 'transformer' && state.powerMode === 'wired') {
    const batSl   = document.getElementById('sl-battery');
    const batItem = document.getElementById('item-battery');
    const batLbl  = document.getElementById('lbl-battery');
    if (v === 0) {
      if (batSl)   batSl.disabled = false;
      if (batItem) batItem.style.opacity = '1';
      if (batLbl)  batLbl.textContent = `${state.battery}%`;
    } else {
      state.battery = 100;
      if (batSl)   { batSl.value = 100; batSl.disabled = true; }
      if (batItem) batItem.style.opacity = '0.65';
      if (batLbl)  batLbl.textContent = '100% ⚡ (Continuous power)';
    }
  }

  // 1. Immediate visual canvas effect (0ms latency)
  updateCanvasEffects();

  // 2. Synchronize physical device badges
  updateDeviceLiveBadges();

  // 3. Server sync (debounce 300ms to avoid slider drag spam)
  if (telemetryDebounceTimer) clearTimeout(telemetryDebounceTimer);
  telemetryDebounceTimer = setTimeout(() => {
    refreshTelemetry();
  }, 300);
}

function updateDeviceLiveBadges() {
  const wifiEl = document.getElementById('badge-wifi');
  if (wifiEl) {
    if (state.rssi >= -60)      { wifiEl.textContent = '📶 Wi-Fi OK';    wifiEl.className = 'badge badge-green'; }
    else if (state.rssi >= -75) { wifiEl.textContent = '📶 Wi-Fi Weak';  wifiEl.className = 'badge badge-yellow'; }
    else                        { wifiEl.textContent = '📶 Wi-Fi Poor';  wifiEl.className = 'badge badge-red'; }
  }

  const batEl = document.getElementById('badge-battery');
  if (batEl) {
    if (state.powerMode === 'battery') {
      if (state.battery >= 30) {
        batEl.textContent = `🔋 ${state.battery}% (Battery)`;
        batEl.className = 'badge badge-green';
      } else if (state.battery >= 10) {
        batEl.textContent = `🔋 ${state.battery}% (Low)`;
        batEl.className = 'badge badge-yellow';
      } else {
        batEl.textContent = `🪫 ${state.battery}% (Critical)`;
        batEl.className = 'badge badge-red';
      }
    } else {
      // Continuous hardwired power
      if (state.transformer > 0 && state.transformer < 12) {
        batEl.textContent = `⚡ Undervoltage (${state.transformer}V)`;
        batEl.className = 'badge badge-yellow';
      } else if (state.transformer === 0 || state.hardwareFaults.powerCut) {
        batEl.textContent = `🪫 No Power (0V)`;
        batEl.className = 'badge badge-red';
      } else if (state.battery >= 30) {
        batEl.textContent = `⚡ ${state.transformer.toFixed(1)}V (Hardwired)`;
        batEl.className = 'badge badge-green';
      } else {
        batEl.textContent = `🔋 ${state.battery}%`;
        batEl.className = 'badge badge-yellow';
      }
    }
  }

  updateLedHaloState();
}

async function refreshTelemetry() {
  try {
    const body = {
      mode:         'custom',
      power_mode:   state.powerMode,
      device_id:    document.getElementById('c-devid')?.value || 'a1b2c3d4e5f6',
      device_model: document.getElementById('c-model')?.value || 'Ring Video Doorbell 4',
      wifi_ssid:    document.getElementById('c-ssid')?.value  || 'HomeNetwork_2G',
      rssi:         state.rssi,
      battery_percentage: state.battery,
      transformer_voltage_vac: state.powerMode === 'wired' ? state.transformer : null,
      battery_saver_low_voltage: state.battery < 10,
      jitter_ms:    state.jitter,
      packet_loss_pct: state.loss,
      frames_per_second: state.fps,
      bitrate_kbps: state.bitrate,
      rtt_ms:       state.rtt,
    };

    const r = await fetch('/api/telemetry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!r.ok) return;
    const t = await r.json();
    state.telemetry = t;
    updateMetricCards(t);
  } catch (e) {
    console.warn('refreshTelemetry error', e);
  }
}

function updateMetricCards(t) {
  const dh  = t.device_health  || {};
  const rtp = (t.webrtc_stats  || {}).inbound_rtp || {};

  const rssi    = dh.rssi ?? state.rssi;
  const battery = dh.battery_percentage ?? state.battery;
  const jitter  = rtp.jitter != null ? (rtp.jitter * 1000).toFixed(1) : state.jitter.toFixed(1);
  const loss    = rtp.packet_loss_fraction != null ? (rtp.packet_loss_fraction * 100).toFixed(1) : state.loss.toFixed(1);

  setMetric('m-rssi',    rssi,    rssiClass(rssi));
  setMetric('m-battery', battery, battClass(battery));
  setMetric('m-jitter',  jitter,  jitterClass(parseFloat(jitter)));
  setMetric('m-loss',    loss,    lossClass(parseFloat(loss)));
}

function setMetric(id, val, cls) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = val;
    el.className = 'metric-value ' + cls;
  }
}

function rssiClass(v)    { return v >= -60 ? 'metric-good' : v >= -75 ? 'metric-fair' : 'metric-poor'; }
function battClass(v)    { return v >= 30  ? 'metric-good' : v >= 10  ? 'metric-fair' : 'metric-poor'; }
function jitterClass(v)  { return v <= 15  ? 'metric-good' : v <= 45  ? 'metric-fair' : 'metric-poor'; }
function lossClass(v)    { return v <= 1   ? 'metric-good' : v <= 8   ? 'metric-fair' : 'metric-poor'; }

/**
 * Resets all telemetry and physical faults to optimal Ring factory defaults.
 */
function resetTelemetryDefaults() {
  state.rssi        = -48;
  state.battery     = 100;
  state.transformer = 18.0;
  state.jitter      = 2;
  state.loss        = 0;
  state.fps         = 30;
  state.bitrate     = 1200;
  state.rtt         = 18;

  state.hardwareFaults = {
    dirtyLens:  false,
    nightMode:  false,
    pinkFilter: false,
    flicker:    false,
    powerCut:   false,
  };

  const sliders = [
    { id: 'sl-rssi',        val: -48,  lbl: 'lbl-rssi',        txt: '-48 dBm' },
    { id: 'sl-battery',     val: 100,  lbl: 'lbl-battery',     txt: '100%' },
    { id: 'sl-transformer', val: 18.0, lbl: 'lbl-transformer', txt: '18.0 V' },
    { id: 'sl-jitter',      val: 2,    lbl: 'lbl-jitter',      txt: '2 ms' },
    { id: 'sl-loss',        val: 0,    lbl: 'lbl-loss',        txt: '0%' },
    { id: 'sl-fps',         val: 30,   lbl: 'lbl-fps',         txt: '30 fps' },
    { id: 'sl-bitrate',     val: 1200, lbl: 'lbl-bitrate',     txt: '1200 kbps' },
    { id: 'sl-rtt',         val: 18,   lbl: 'lbl-rtt',         txt: '18 ms' },
  ];

  sliders.forEach(s => {
    const el = document.getElementById(s.id);
    if (el) el.value = s.val;
    const lbl = document.getElementById(s.lbl);
    if (lbl) lbl.textContent = s.txt;
  });

  ['hw-btn-dirty', 'hw-btn-night', 'hw-btn-pink', 'hw-btn-flicker', 'hw-btn-power'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.classList.remove('active', 'active-warn');
  });

  ['lens-dirty-layer', 'lens-pink-layer'].forEach(id => {
    document.getElementById(id)?.classList.remove('active');
  });
  ['ir-top', 'ir-right', 'ir-bottom', 'ir-left'].forEach(id => {
    document.getElementById(id)?.classList.remove('active');
  });

  if (typeof setPowerMode === 'function') setPowerMode('wired');
  updateCanvasEffects();
  updateDeviceLiveBadges();

  state.anomalyConsecutiveCount = 0;
  state.lastAnomalyDomain = null;
  state.lastDiagnosedAnomalySignature = null;
  if (typeof updateHeartbeatUI === 'function') updateHeartbeatUI();

  refreshTelemetry();
  if (typeof showToast === 'function') {
    showToast('🔄 Telemetry reset to optimal factory defaults', 'success');
  }
}
