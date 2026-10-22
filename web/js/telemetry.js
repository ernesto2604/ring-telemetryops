/**
 * Telemetry display: Digital Twin sliders (simulated) or Ring Partner API status (measured only)
 * Controls real-time continuous sliders, backend synchronization,
 * and reactive device health badge updates.
 */

let telemetryDebounceTimer = null;

// Severity levels (GREEN / DEGRADED / CRITICAL) come from GET /api/thresholds, the backend's single
// table (src/config/severity_thresholds.json). The page keeps no copy of the numbers: the heartbeat,
// the badges, the metric cards and the signal pill all classify through classifyMetric().
let SEVERITY = null;
const LEVEL_RANK = { GREEN: 0, DEGRADED: 1, CRITICAL: 2 };
const OP_SYMBOLS = { lt: '<', lte: '≤', gt: '>', gte: '≥' };
const SEVERITY_OPS = {
  lt: (v, x) => v < x, lte: (v, x) => v <= x, gt: (v, x) => v > x, gte: (v, x) => v >= x,
};

/** Installs a severity table (the /api/thresholds body); false when it is not usable. */
function applySeverityTable(table) {
  if (!table || typeof table.metrics !== 'object' || !table.heartbeat || !table.domain_labels) return false;
  SEVERITY = table;
  return true;
}

async function loadSeverityTable() {
  try {
    const r = await fetch('/api/thresholds');
    return r.ok && applySeverityTable(await r.json());
  } catch {
    return false;
  }
}

function levelRank(level) { return LEVEL_RANK[level] ?? -1; }

function conditionMatches(cond, ctx) {
  return Object.entries(cond || {}).every(([k, v]) => ctx[k] === v);
}

/** `when`: all keys must equal the context; `unless`: not all of them may. Same rule as severity.py. */
function ruleApplies(rule, ctx) {
  if (rule.when && !conditionMatches(rule.when, ctx)) return false;
  if (rule.unless && conditionMatches(rule.unless, ctx)) return false;
  return true;
}

/** Level of one value, or null when unmeasured, not applicable (e.g. a transformer on battery) or no table. */
function classifyMetric(name, value, ctx = {}) {
  const rule = SEVERITY && SEVERITY.metrics[name];
  if (!rule || typeof value !== 'number' || !Number.isFinite(value) || !ruleApplies(rule, ctx)) return null;
  const band = rule.bands.find(b => ruleApplies(b, ctx) && SEVERITY_OPS[b.op](value, b.value));
  return band
    ? { metric: name, value, domain: rule.domain, unit: rule.unit, level: band.level, label: band.label,
        reason: band.reason, op: band.op, limit: band.value }
    : { metric: name, value, domain: rule.domain, unit: rule.unit, level: 'GREEN', label: rule.green_label,
        reason: null, op: null, limit: null };
}

/** The boundary of a band, by label (e.g. bandLimit('battery', 'Critical') -> 10). */
function bandLimit(name, label) {
  const band = SEVERITY && SEVERITY.metrics[name].bands.find(b => b.label === label);
  return band ? band.value : null;
}

/** "< 10%", "≤ 0.5 V AC", "< -88 dBm": a band boundary as text, straight from the table. */
function limitText(item) {
  return `${OP_SYMBOLS[item.op]} ${item.limit}${item.unit === '%' ? '' : ' '}${item.unit}`;
}

function domainLabel(domain) {
  return (SEVERITY && SEVERITY.domain_labels[domain]) || domain;
}

function heartbeatPulsesRequired() {
  return SEVERITY ? SEVERITY.heartbeat.degraded_pulses : 0;
}

/** Digital Twin values in the backend's metric names (src/agent/concierge.py _telemetry_metrics). */
function twinMetrics() {
  return {
    rssi: state.rssi,
    battery: state.battery,
    transformer: state.powerMode === 'wired' ? state.transformer : null,
    jitter_ms: state.jitter,
    packet_loss_pct: state.loss,
    fps: state.fps,
    bitrate: state.bitrate,
  };
}

function twinContext() {
  // The Twin has no in-home chime setting, so the mechanical chime rule never applies here.
  return { power_mode: state.powerMode, chime_type: null };
}

/**
 * Every Twin metric with its level, the worst level and the dominant anomaly (first metric with the
 * worst level, in table order: power, network, stream). Same algorithm as severity.classify_telemetry.
 */
function classifyTwinTelemetry() {
  if (!SEVERITY) return null;
  const values = twinMetrics();
  const ctx = twinContext();
  const items = Object.entries(SEVERITY.metrics)
    .map(([name, rule]) => classifyMetric(name, values[rule.value_key || name], ctx))
    .filter(Boolean);
  const anomalies = items.filter(i => i.level !== 'GREEN');
  const level = anomalies.reduce((worst, i) => (levelRank(i.level) > levelRank(worst) ? i.level : worst), 'GREEN');
  return { level, items, anomalies, dominant: anomalies.find(i => i.level === level) || null };
}

const LEVEL_METRIC_CLASS = { GREEN: 'metric-good', DEGRADED: 'metric-fair', CRITICAL: 'metric-poor' };
const LEVEL_BADGE_CLASS = { GREEN: 'badge-green', DEGRADED: 'badge-yellow', CRITICAL: 'badge-red' };
const LEVEL_TEXT = { GREEN: 'OK', DEGRADED: 'Degraded', CRITICAL: 'Critical' };

// Shown for any metric the Ring Partner API did not report. Never replaced by slider values.
const METRIC_NOT_AVAILABLE = 'N/D';

// HTTP 429 without Retry-After: exponential backoff. Ring documents "exponential backoff
// starting at 1 second"; the 300 s cap is a prototype choice. Polls never run faster than
// the normal interval, so short backoff steps simply fall within it.
const RATE_LIMIT_BACKOFF_BASE_SECONDS = 1;
const RATE_LIMIT_BACKOFF_MAX_SECONDS = 300;

/** Seconds to pause polling after a failed read, or 0 for none. */
function pollPauseSeconds(status, retryAfterHeader) {
  const retryAfter = parseFloat(retryAfterHeader);
  if (Number.isFinite(retryAfter) && retryAfter > 0) return retryAfter;   // always honour Retry-After
  if (status !== 429) return 0;
  state.rateLimitStreak++;
  return Math.min(
    RATE_LIMIT_BACKOFF_MAX_SECONDS,
    RATE_LIMIT_BACKOFF_BASE_SECONDS * 2 ** (state.rateLimitStreak - 1),
  );
}

function isPartnerSource() {
  return state.ringSource === 'partner';
}

/** Only the Digital Twin (MOCK) may show slider values; any other source shows measured data only. */
function showsMeasuredDataOnly() {
  return isPartnerSource() || state.ringMode !== 'MOCK';
}

/** A measured number, or null when absent/not numeric (a measured 0 stays 0). */
function measuredNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

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

  if (key === 'transformer') {
    // The Cut button mirrors the slider: active only inside the table's power-cut band (CRITICAL).
    const cut = classifyMetric('transformer', v, { power_mode: 'wired' })?.level === 'CRITICAL';
    state.hardwareFaults.powerCut = cut;
    document.getElementById('hw-btn-power')?.classList.toggle('active-warn', cut);
  }

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
  if (showsMeasuredDataOnly()) {
    // Partner mode: badges reflect official telemetry only, never the Digital Twin sliders.
    updatePartnerBadges(state.telemetry);
    return;
  }
  // Badge colour = severity level, and the band name is always written next to it (not colour alone).
  const ctx = twinContext();
  const wifiEl = document.getElementById('badge-wifi');
  const wifi = classifyMetric('rssi', state.rssi, ctx);
  if (wifiEl && wifi) {
    wifiEl.textContent = `📶 Wi-Fi ${wifi.label}`;
    wifiEl.className = 'badge ' + LEVEL_BADGE_CLASS[wifi.level];
  }

  const batEl = document.getElementById('badge-battery');
  if (batEl) {
    if (state.powerMode === 'battery') {
      const bat = classifyMetric('battery', state.battery, ctx);
      if (bat) {
        const icon = bat.level === 'CRITICAL' ? '🪫' : '🔋';
        batEl.textContent = `${icon} ${state.battery}% (${bat.level === 'GREEN' ? 'Battery' : bat.label})`;
        batEl.className = 'badge ' + LEVEL_BADGE_CLASS[bat.level];
      }
    } else {
      // Continuous hardwired power: the transformer band decides (the battery is a trickle-charged backup).
      const tr = classifyMetric('transformer', state.transformer, ctx);
      if (tr) {
        const text = {
          GREEN: `⚡ ${state.transformer.toFixed(1)}V (Hardwired)`,
          DEGRADED: `⚡ ${tr.label} (${state.transformer}V)`,
          CRITICAL: `🪫 ${tr.label} (${state.transformer}V)`,
        };
        batEl.textContent = text[tr.level];
        batEl.className = 'badge ' + LEVEL_BADGE_CLASS[tr.level];
      }
    }
  }

  updateLedHaloState();
}

let telemetryRefreshInFlight = false;

async function refreshTelemetry() {
  // Partner: one refresh at a time, so a slow Ring response never stacks extra requests.
  if (isPartnerSource() && telemetryRefreshInFlight) return;
  telemetryRefreshInFlight = isPartnerSource();
  try {
    if (state.ringMode !== 'MOCK' && state.ringMode !== 'LIVE') {
      // ERROR or not yet known: no data source. Replace what is on screen (static HTML or old values) with N/D.
      state.telemetry = null;
      updateMetricCards(null);
      throw new Error('Ring credentials are configured but the LIVE adapter failed to connect');
    }
    if (state.ringMode === 'LIVE') {
      const liveResponse = await fetch('/api/ring/telemetry');
      if (!liveResponse.ok) {
        if (isPartnerSource()) {
          // An API failure says nothing about the device: no telemetry is shown (not "offline").
          state.telemetry = null;
          const message = await responseErrorMessage(liveResponse);
          state.partnerApiError = { status: liveResponse.status, message };
          // Retry-After (or 429 backoff): the poll timer skips requests until it has elapsed.
          const pause = pollPauseSeconds(
            liveResponse.status, liveResponse.headers && liveResponse.headers.get('Retry-After'));
          state.pollPausedUntil = pause > 0 ? Date.now() + pause * 1000 : 0;
          updateMetricCards(null);
          const toast = `Ring telemetry unavailable: ${message}`;
          if (state.lastTelemetryError !== toast && typeof showToast === 'function') showToast(toast, 'error');
          state.lastTelemetryError = toast;
        }
        throw new Error(`Live telemetry request failed (${liveResponse.status})`);
      }
      const liveTelemetry = await liveResponse.json();
      state.telemetry = liveTelemetry;
      state.lastTelemetryError = null;
      state.partnerApiError = null;
      state.pollPausedUntil = 0;
      state.rateLimitStreak = 0;   // a successful read resets the backoff
      updateMetricCards(liveTelemetry);
      return;
    }

    const body = {
      mode:         'custom',
      power_mode:   state.powerMode,
      device_id:    document.getElementById('c-devid')?.value || 'a1b2c3d4e5f6',
      device_model: document.getElementById('c-model')?.value || 'Ring Video Doorbell 4',
      wifi_ssid:    document.getElementById('c-ssid')?.value  || 'HomeNetwork_2G',
      rssi:         state.rssi,
      battery_percentage: state.battery,
      transformer_voltage_vac: state.powerMode === 'wired' ? state.transformer : null,
      battery_saver_low_voltage: state.battery < bandLimit('battery', 'Critical'),
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
  } finally {
    telemetryRefreshInFlight = false;
  }
}

/** Readable error text from a failed API response (FastAPI `detail` may be a string or an object). */
async function responseErrorMessage(response) {
  try {
    const body = await response.json();
    const detail = body && body.detail;
    if (typeof detail === 'string') return detail;
    if (detail && detail.message) return detail.message;
  } catch {
    // Non-JSON error body: fall back to the HTTP status.
  }
  return `HTTP ${response.status}`;
}

function updateMetricCards(t) {
  if (showsMeasuredDataOnly()) {
    updatePartnerMetricCards(t);
    return;
  }
  const dh  = t.device_health  || {};
  const rtp = (t.webrtc_stats  || {}).inbound_rtp || {};

  const rssi    = dh.rssi ?? state.rssi;
  const battery = dh.battery_percentage ?? state.battery;
  const jitter  = rtp.jitter != null ? (rtp.jitter * 1000).toFixed(1) : state.jitter.toFixed(1);
  const loss    = rtp.packet_loss_fraction != null ? (rtp.packet_loss_fraction * 100).toFixed(1) : state.loss.toFixed(1);

  const ctx = twinContext();
  setLevelMetric('m-rssi',    rssi,    classifyMetric('rssi', rssi, ctx));
  setLevelMetric('m-battery', battery, classifyMetric('battery', battery, ctx));
  setLevelMetric('m-jitter',  jitter,  classifyMetric('jitter_ms', parseFloat(jitter), ctx));
  setLevelMetric('m-loss',    loss,    classifyMetric('packet_loss_pct', parseFloat(loss), ctx));
}

/** Twin metric card: value, colour from the level, and the band name as text. */
function setLevelMetric(id, value, item) {
  setMetric(id, value, item ? LEVEL_METRIC_CLASS[item.level] : 'metric-normal');
  const levelEl = document.getElementById(id + '-level');
  if (levelEl) levelEl.textContent = item ? (item.level === 'GREEN' ? item.label : `${item.label} · ${LEVEL_TEXT[item.level]}`) : '';
}

function updatePartnerMetricCards(t) {
  const dh  = (t && t.device_health) || {};
  const rtp = ((t && t.webrtc_stats) || {}).inbound_rtp || {};

  const rssi     = measuredNumber(dh.rssi);
  const battery  = measuredNumber(dh.battery_percentage);
  const jitterS  = measuredNumber(rtp.jitter);
  const lossFrac = measuredNumber(rtp.packet_loss_fraction);
  const jitterMs = jitterS === null ? null : jitterS * 1000;
  const lossPct  = lossFrac === null ? null : lossFrac * 100;

  setMeasuredMetric('m-rssi',    rssi,     v => v,             rssiClass);
  setMeasuredMetric('m-battery', battery,  v => v,             battClass);
  setMeasuredMetric('m-jitter',  jitterMs, v => v.toFixed(1),  jitterClass);
  setMeasuredMetric('m-loss',    lossPct,  v => v.toFixed(1),  lossClass);
  updatePartnerBadges(t);
}

function setMeasuredMetric(id, value, format, classify) {
  if (value === null) {
    setMetric(id, METRIC_NOT_AVAILABLE, 'metric-unavailable');
  } else {
    setMetric(id, format(value), classify(value));
  }
}

/** Ring-reported reachability: true / false / anything else is unknown (never inferred). */
function onlineLabel(online) {
  if (online === true)  return { text: '🟢 Online',      cls: 'badge badge-green' };
  if (online === false) return { text: '🔴 Offline',     cls: 'badge badge-red' };
  return                       { text: `⚪ Online ${METRIC_NOT_AVAILABLE}`, cls: 'badge' };
}

/**
 * 'supported' | 'unsupported' | 'unknown' for one capability value, using documented markers only:
 * a `supported` boolean, or a non-empty list (codecs, configurations). Bare booleans, numbers
 * (0 or not), strings, empty lists and null say nothing about support.
 */
function capabilityState(value) {
  if (Array.isArray(value)) return value.length ? 'supported' : 'unknown';
  if (!value || typeof value !== 'object') return 'unknown';
  if (typeof value.supported === 'boolean') return value.supported ? 'supported' : 'unsupported';
  const states = Object.values(value).map(capabilityState);
  return states.includes('supported') ? 'supported' : states.includes('unsupported') ? 'unsupported' : 'unknown';
}

function capabilitiesText(caps) {
  const names = state => Object.keys(caps).filter(name =>
    // Ring documents battery_status: null as "not a battery-powered device".
    (name === 'battery_status' && caps[name] === null ? 'unsupported' : capabilityState(caps[name])) === state);
  const supported = names('supported');
  const unsupported = names('unsupported');
  if (!supported.length && !unsupported.length) return 'Ring capabilities: none reported';
  return `Ring capabilities: ${supported.join(', ') || 'none'}`
    + (unsupported.length ? ` · Not supported: ${unsupported.join(', ')}` : '');
}

function updatePartnerBadges(t) {
  const dh = (t && t.device_health) || {};

  // Identity as returned by Ring discovery; the Digital Twin's model and ID are never shown here.
  const nameEl = document.getElementById('dev-name');
  if (nameEl) nameEl.textContent = (t && t.device_name) || `Device name ${METRIC_NOT_AVAILABLE}`;
  const idEl = document.getElementById('dev-id');
  if (idEl) { idEl.textContent = `ID: ${(t && t.device_id) || METRIC_NOT_AVAILABLE}`; idEl.style.display = ''; }
  const capsEl = document.getElementById('partner-capabilities');
  if (capsEl) {
    const caps = t && t.capabilities;
    capsEl.style.display = '';
    capsEl.textContent = caps && typeof caps === 'object' && !Array.isArray(caps)
      ? capabilitiesText(caps) : `Ring capabilities ${METRIC_NOT_AVAILABLE}`;
  }

  const onlineEl = document.getElementById('badge-online');
  if (onlineEl) {
    const label = onlineLabel(dh.online);
    onlineEl.textContent = label.text;
    onlineEl.className = label.cls;
    // reported_at is the device's last check-in (uplink), not the current time.
    onlineEl.title = dh.updated_at
      ? `Last device check-in reported by Ring: ${dh.updated_at}`
      : 'Last device check-in not reported by Ring';
  }
  const apiErrEl = document.getElementById('badge-api-error');
  if (apiErrEl) {
    const err = state.partnerApiError;
    apiErrEl.style.display = err ? '' : 'none';
    apiErrEl.textContent = err ? `⚠️ Ring API error (HTTP ${err.status})` : '';
    apiErrEl.title = err ? err.message : '';
  }

  const rssi = measuredNumber(dh.rssi);
  const battery = measuredNumber(dh.battery_percentage);
  const transformer = measuredNumber(dh.transformer_voltage_vac);

  // Partner display keeps its colour bands (unchanged Partner behaviour); the numbers come from the table.
  const wifiEl = document.getElementById('badge-wifi');
  if (wifiEl) {
    if (rssi === null)      { wifiEl.textContent = `📶 Wi-Fi ${METRIC_NOT_AVAILABLE}`; wifiEl.className = 'badge'; }
    else if (rssi >= bandLimit('rssi', 'Weak')) { wifiEl.textContent = '📶 Wi-Fi OK';    wifiEl.className = 'badge badge-green'; }
    else if (rssi >= bandLimit('rssi', 'Poor')) { wifiEl.textContent = '📶 Wi-Fi Weak';  wifiEl.className = 'badge badge-yellow'; }
    else                    { wifiEl.textContent = '📶 Wi-Fi Poor';  wifiEl.className = 'badge badge-red'; }
  }

  const batEl = document.getElementById('badge-battery');
  if (batEl) {
    // Only measured values are shown; the power setup is never inferred from missing data.
    if (battery !== null) {
      batEl.textContent = `🔋 ${battery}%`;
      batEl.className = 'badge ' + partnerBattBadge(battery);
    } else if (transformer !== null) {
      batEl.textContent = `⚡ ${transformer.toFixed(1)}V`;
      batEl.className = 'badge ' + (transformer === 0 ? 'badge-red'
        : transformer < bandLimit('transformer', 'Undervoltage') ? 'badge-yellow' : 'badge-green');
    } else {
      batEl.textContent = `🔋 Power ${METRIC_NOT_AVAILABLE}`;
      batEl.className = 'badge';
    }
  }
}

function setMetric(id, val, cls) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = val;
    el.className = 'metric-value ' + cls;
  }
}

// Partner metric cards. Ring reports no RSSI, jitter or packet loss, so in practice only the battery
// card is classified; it keeps its 30/10 % display bands (battery-device context) as before.
const PARTNER_DISPLAY_CTX = { power_mode: 'battery' };
function levelClassOf(name, v) {
  const item = classifyMetric(name, v, PARTNER_DISPLAY_CTX);
  return item ? LEVEL_METRIC_CLASS[item.level] : 'metric-normal';
}
function partnerBattBadge(v) {
  const item = classifyMetric('battery', v, PARTNER_DISPLAY_CTX);
  return item ? LEVEL_BADGE_CLASS[item.level] : '';
}
function rssiClass(v)    { return levelClassOf('rssi', v); }
function battClass(v)    { return levelClassOf('battery', v); }
function jitterClass(v)  { return levelClassOf('jitter_ms', v); }
function lossClass(v)    { return levelClassOf('packet_loss_pct', v); }

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

  if (typeof resetHeartbeatIncident === 'function') resetHeartbeatIncident();
  if (typeof dismissDiagnosis === 'function') dismissDiagnosis();   // also resets the engine label
  if (typeof updateHeartbeatUI === 'function') updateHeartbeatUI();

  refreshTelemetry();
  if (typeof showToast === 'function') {
    showToast('🔄 Telemetry reset to optimal factory defaults', 'success');
  }
}
