// Frontend regression tests: run with `node --test tests/frontend`.
// Loads the page scripts (in index.html order) into a VM context with a minimal fake DOM
// and a mocked fetch. No browser, no network.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const ROOT = new URL('../../', import.meta.url);
const SOURCES = ['web/js/chime-audio.js', 'web/js/canvas-engine.js', 'web/js/telemetry.js', 'web/js/app.js']
  .map(p => readFileSync(new URL(p, ROOT), 'utf8'));
// The single severity table the server returns at GET /api/thresholds (no copy in the tests either).
const SEVERITY_TABLE = JSON.parse(readFileSync(new URL('src/config/severity_thresholds.json', ROOT), 'utf8'));

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.textContent = '';
    this.className = '';
    this.innerHTML = '';
    this.value = '';
    this.disabled = false;
    this.style = {};
    this.children = [];
    // classList reads and writes className, as in a real DOM.
    const classes = () => new Set(this.className.split(/\s+/).filter(Boolean));
    const write = set => { this.className = [...set].join(' '); };
    this.classList = {
      add: (...c) => { const s = classes(); c.forEach(x => s.add(x)); write(s); },
      remove: (...c) => { const s = classes(); c.forEach(x => s.delete(x)); write(s); },
      toggle: (c, on) => { const s = classes(); (on ?? !s.has(c)) ? s.add(c) : s.delete(c); write(s); },
      contains: c => classes().has(c),
    };
  }
  appendChild(child) { this.children.push(child); }
}

/** Builds an isolated page: `routes` maps URL -> {status, body}; returns helpers. */
function loadPage(routes = {}, { simulatorReady = true, severity = true } = {}) {
  const elements = new Map();
  const requests = [];
  const toasts = [];
  const byId = id => {
    if (!elements.has(id)) elements.set(id, new FakeElement(id));
    return elements.get(id);
  };
  // Static markup the tests query by selector (subset of web/index.html).
  const markup = [
    ...['sl-rssi', 'sl-battery', 'sl-transformer', 'sl-jitter'].map(id => ({ id, sel: 'input[type="range"]' })),
    ...['trigger-ding', 'trigger-motion', 'trigger-live'].map(id => ({ id, sel: '.cam-trigger-btn' })),
    { id: 'ring-bell-btn', sel: '#ring-bell-btn' },
    { id: 'hw-btn-dirty', sel: '.hw-tool-btn' },
    { id: 'btn-pwr-wired', sel: '#btn-pwr-wired' },
  ];
  const document = {
    getElementById: byId,
    querySelector: () => null,
    querySelectorAll: selector => {
      const parts = selector.split(',').map(x => x.trim());
      return markup.filter(m => parts.includes(m.sel)).map(m => byId(m.id));
    },
    createElement: () => new FakeElement(),
  };
  routes = {
    '/api/thresholds': { status: 200, body: SEVERITY_TABLE },
    '/api/engine': { status: 200, body: { primary: 'bedrock', credentials_configured: true } },
    ...routes,
  };
  const fetch = async (url, options = {}) => {
    requests.push({ url, body: options.body ? JSON.parse(options.body) : null });
    // A route function may return a promise, to hold a response until the test releases it.
    const route = (typeof routes[url] === 'function' ? await routes[url]() : routes[url])
      || { status: 404, body: { detail: 'not found' } };
    const headers = route.headers || {};
    return {
      ok: route.status >= 200 && route.status < 300,
      status: route.status,
      headers: { get: name => headers[name] ?? null },
      json: async () => route.body,
    };
  };
  const context = vm.createContext({
    document, fetch, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    window: { addEventListener: () => {} },
  });
  for (const src of SOURCES) vm.runInContext(src, context);
  // Capture toasts without touching the fake DOM timers.
  vm.runInContext('showToast = (msg, type) => __toasts.push({ msg, type });', Object.assign(context, { __toasts: toasts }));
  // Most tests start from a page whose /api/thresholds and /api/ring/status already answered.
  if (severity) vm.runInContext(`applySeverityTable(${JSON.stringify(SEVERITY_TABLE)});`, context);
  if (simulatorReady) vm.runInContext("state.ringMode = 'MOCK';", context);
  const run = code => vm.runInContext(code, context);
  return { run, el: byId, requests, toasts };
}

const NULL_PARTNER_TELEMETRY = {
  _source: 'ring_partner_api',
  device_id: 'ava1.ring.device.FRONT',
  device_health: { online: true, rssi: null, battery_percentage: null, transformer_voltage_vac: null },
  webrtc_stats: { inbound_rtp: { jitter: null, packet_loss_fraction: null }, candidate_pair: {} },
};

test('Partner nulls render as N/D and never as slider values', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.rssi = -90; state.battery = 3; state.jitter = 99; state.loss = 30;");

  page.run(`updateMetricCards(${JSON.stringify(NULL_PARTNER_TELEMETRY)})`);

  for (const id of ['m-rssi', 'm-battery', 'm-jitter', 'm-loss']) {
    assert.equal(page.el(id).textContent, 'N/D', id);
    assert.equal(page.el(id).className, 'metric-value metric-unavailable', id);
  }
  assert.equal(page.el('badge-wifi').textContent, '📶 Wi-Fi N/D');
  assert.equal(page.el('badge-battery').textContent, '🔋 Power N/D');
});

test('Partner measured zero is shown as zero, not N/D', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner';");

  page.run(`updateMetricCards(${JSON.stringify({
    device_health: { rssi: -50, battery_percentage: 0 },
    webrtc_stats: { inbound_rtp: { jitter: 0, packet_loss_fraction: 0 } },
  })})`);

  assert.equal(page.el('m-battery').textContent, 0);
  assert.equal(page.el('m-jitter').textContent, '0.0');
  assert.equal(page.el('m-loss').textContent, '0.0');
  assert.equal(page.el('badge-battery').textContent, '🔋 0%');
});

test('Partner badges ignore slider changes', () => {
  const page = loadPage();
  page.run(`state.ringSource = 'partner'; state.telemetry = ${JSON.stringify(NULL_PARTNER_TELEMETRY)};`);

  page.run("state.rssi = -48; state.battery = 100; updateDeviceLiveBadges();");

  assert.equal(page.el('badge-wifi').textContent, '📶 Wi-Fi N/D');
  assert.notEqual(page.el('badge-battery').textContent, '🔋 100%');
});

test('Simulator keeps slider fallback and sliders still drive state', () => {
  const page = loadPage();

  page.run("updateSlider('rssi', '-82');");
  assert.equal(page.run('state.rssi'), -82);
  assert.equal(page.el('lbl-rssi').textContent, '-82 dBm');
  assert.equal(page.el('badge-wifi').textContent, '📶 Wi-Fi Poor');

  page.run('updateMetricCards({ device_health: {}, webrtc_stats: {} })');
  assert.equal(page.el('m-rssi').textContent, -82);
  assert.equal(page.el('m-jitter').textContent, '2.0');
});

test('Partner heartbeat never auto-diagnoses from slider anomalies', async () => {
  const page = loadPage({ '/api/ring/telemetry': { status: 200, body: NULL_PARTNER_TELEMETRY } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; state.rssi = -95; state.powerMode = 'battery'; state.battery = 2;");

  for (let i = 0; i < 4; i++) await page.run('onHeartbeatPulse()');

  assert.equal(page.requests.filter(r => r.url === '/api/analyze').length, 0);
  assert.equal(page.run('state.anomalyConsecutiveCount'), 0);
  assert.equal(page.el('m-rssi').textContent, 'N/D');
});

test('Simulator heartbeat still auto-diagnoses sustained anomalies', async () => {
  const page = loadPage({
    '/api/telemetry': { status: 200, body: { device_health: {}, webrtc_stats: {} } },
    '/api/analyze': { status: 200, body: { analysis_skipped: true } },
  });
  page.run('state.rssi = -90;');

  for (let i = 0; i < 3; i++) await page.run('onHeartbeatPulse()');
  await new Promise(r => setTimeout(r, 0));

  const analyze = page.requests.filter(r => r.url === '/api/analyze');
  assert.equal(analyze.length, 1);
  assert.equal(analyze[0].body.source_mode, 'twin');
});

test('Partner manual diagnosis sends no browser metrics', async () => {
  const page = loadPage({ '/api/analyze': { status: 200, body: { analysis_skipped: true } } });
  page.run(`state.ringSource = 'partner'; state.ringMode = 'LIVE';
            state.telemetry = ${JSON.stringify({ ...NULL_PARTNER_TELEMETRY, device_health: { rssi: -99 } })};
            state.liveWebrtcStats = { inbound_rtp: { jitter: 0.9 } };`);

  await page.run('runManualDiagnosis()');

  const [request] = page.requests.filter(r => r.url === '/api/analyze');
  assert.equal(request.body.source_mode, 'ring');
  assert.equal(request.body.telemetry, null);
  assert.equal(request.body.frame_base64, null);
  assert.equal(request.body.device_id, 'ava1.ring.device.FRONT');
  assert.equal(page.el('btn-analyze-now').disabled, false);
});

test('Failed analysis is reported as an error, not rendered as a diagnosis', async () => {
  const page = loadPage({
    '/api/analyze': { status: 503, body: { detail: { message: 'Ring API GET /v1/devices failed with HTTP 503' } } },
  });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  await page.run('runManualDiagnosis()');

  assert.equal(page.toasts.length, 1);
  assert.equal(page.toasts[0].type, 'error');
  assert.match(page.toasts[0].msg, /Diagnosis failed: Ring API GET \/v1\/devices failed with HTTP 503/);
  assert.equal(page.el('diag-status-text').textContent, '');      // nothing rendered
  assert.notEqual(page.el('app-push-banner').className.includes('show'), true);
});

test('Failed Partner telemetry refresh clears cards instead of keeping old values', async () => {
  const page = loadPage({ '/api/ring/telemetry': { status: 504, body: { detail: { message: 'timed out' } } } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  page.el('m-rssi').textContent = '-48';

  await page.run('refreshTelemetry()');

  assert.equal(page.el('m-rssi').textContent, 'N/D');
  assert.equal(page.run('state.telemetry'), null);
  assert.equal(page.toasts[0].type, 'error');
});

test('INSUFFICIENT_DATA and UNKNOWN are rendered explicitly', () => {
  const page = loadPage();

  page.run(`renderResult({ diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'UNKNOWN',
                           confidence_score: 0, telemetry_findings: [], visual_findings: [] })`);
  page.run(`showAppPushNotification({ diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'UNKNOWN' }, 'manual', '')`);

  assert.equal(page.el('diag-icon').textContent, '❔');
  assert.equal(page.el('diag-status-text').textContent, 'INSUFFICIENT_DATA');
  assert.equal(page.el('diag-domain-text').textContent, 'Undetermined (not enough measured data)');
  assert.equal(page.el('app-push-title').textContent, 'Ring: Not enough data to diagnose this device');
});

test('Dirty Lens blurs the canvas pixels that diagnosis captures and sends', async () => {
  const page = loadPage({ '/api/analyze': { status: 200, body: { analysis_skipped: true } } });
  const draws = [];
  const ctx = { filter: 'none', drawImage() { draws.push(this.filter); } };
  Object.assign(page.el('video-canvas'), {
    width: 1920, height: 1080, getContext: () => ctx,
    toDataURL: (type, quality) => `data:${type};base64,CANVAS_${ctx.filter}_${quality}`,
  });
  Object.assign(page.el('live-video-player'), { paused: false, ended: false });
  page.run(`globalThis.requestAnimationFrame = () => 0;
            state.rssi = -48; state.bitrate = 1200; state.loss = 0; state.battery = 100; state.fps = 30;
            cx.cameraActive = true; toggleDirtyLens(); renderFrame(1e6);`);

  assert.deepEqual(draws, ['blur(3.8px)']);   // blur applied to the drawn frame, not only to a CSS overlay
  assert.equal(ctx.filter, 'none');

  await page.run("triggerAutonomousDiagnosis('manual')");
  const [request] = page.requests.filter(r => r.url === '/api/analyze');
  assert.equal(request.body.frame_base64, 'CANVAS_none_0.85');   // the canvas itself, already blurred
  assert.equal(request.body.camera_active, true);
  assert.equal(request.body.source_mode, 'twin');
});

test('Existing simulator statuses still render as before', () => {
  const page = loadPage();

  page.run(`renderResult({ diagnostic_status: 'DEGRADED', root_cause_domain: 'NETWORK', confidence_score: 0.9 })`);

  assert.equal(page.el('diag-icon').textContent, '⚠️');
  assert.equal(page.el('diag-domain-text').textContent, 'Network Issue (Wi-Fi / Upstream)');
});

// ---------------------------------------------------------------------------
// Partner monitoring (status polling, offline policy, controls)
// ---------------------------------------------------------------------------

const partnerTelemetry = (online, extra = {}) => ({
  ...NULL_PARTNER_TELEMETRY,
  device_health: { ...NULL_PARTNER_TELEMETRY.device_health, online, ...extra },
});

const PARTNER_STATUS = {
  mode: 'LIVE', source: 'partner',
  monitoring: { mechanism: 'polling', poll_interval_seconds: 42, offline_alert_consecutive_reads: 2 },
};

test('Partner setup uses the configured poll interval and disables simulated controls', async () => {
  const page = loadPage({
    '/api/scenarios': { status: 200, body: {} },
    '/api/ring/status': { status: 200, body: PARTNER_STATUS },
  });

  await page.run('checkServer()');

  assert.equal(page.run('heartbeatPeriod()'), 42);
  assert.equal(page.run('state.offlineAlertReads'), 2);
  for (const id of ['sl-rssi', 'trigger-ding', 'trigger-motion', 'trigger-live', 'ring-bell-btn', 'hw-btn-dirty']) {
    assert.equal(page.el(id).disabled, true, id);
  }
  assert.equal(page.el('badge-online').style.display, '');
  assert.equal(page.el('btn-analyze-now').style.display, '');
  assert.equal(page.el('btn-analyze-clip').style.display, '');
  assert.equal(page.el('btn-live-stats').style.display, '');
});

test('Simulator keeps its 15 s heartbeat and enabled controls', async () => {
  const page = loadPage({
    '/api/scenarios': { status: 200, body: {} },
    '/api/ring/status': { status: 200, body: { mode: 'MOCK', source: 'simulator' } },
  });

  await page.run('checkServer()');

  assert.equal(page.run('heartbeatPeriod()'), 15);
  for (const id of ['sl-rssi', 'trigger-ding', 'ring-bell-btn', 'hw-btn-dirty']) {
    assert.equal(page.el(id).disabled, false, id);
  }
});

test('Each Partner pulse makes exactly one status refresh and never diagnoses', async () => {
  const page = loadPage({ '/api/ring/telemetry': { status: 200, body: partnerTelemetry(true) } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; state.rssi = -95; state.loss = 30;");

  for (let i = 0; i < 3; i++) await page.run('onHeartbeatPulse()');

  // One status refresh and one Ring events query per pulse (T28); never a diagnosis request.
  assert.deepEqual(page.requests.map(r => r.url), Array(3).fill(['/api/ring/telemetry', '/api/ring/events']).flat());
});

test('online false shows Offline and confirms the alert only after N consecutive reads', async () => {
  const page = loadPage({ '/api/ring/telemetry': { status: 200, body: partnerTelemetry(false) } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; state.offlineAlertReads = 2;");

  await page.run('onHeartbeatPulse()');
  assert.equal(page.el('badge-online').textContent, '🔴 Offline');
  assert.equal(page.run('state.offlineAlertActive'), false);
  assert.equal(page.el('hb-persistency').textContent, 'Offline reads 1/2');

  await page.run('onHeartbeatPulse()');
  assert.equal(page.run('state.offlineAlertActive'), true);
  assert.equal(page.el('hb-persistency').textContent, 'Offline confirmed (2 reads)');
  assert.equal(page.toasts.filter(t => t.type === 'error').length, 1);
  assert.equal(page.requests.filter(r => r.url === '/api/analyze').length, 0);
});

test('online null shows unknown and neither confirms nor clears an offline streak', async () => {
  let online = false;
  const page = loadPage({ '/api/ring/telemetry': () => ({ status: 200, body: partnerTelemetry(online) }) });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; state.offlineAlertReads = 3;");

  await page.run('onHeartbeatPulse()');
  online = null;
  await page.run('onHeartbeatPulse()');

  assert.equal(page.el('badge-online').textContent, '⚪ Online N/D');
  assert.equal(page.run('state.offlineStreak'), 1);
});

test('A timeout or 5xx is an API error, not a disconnection', async () => {
  let fail = false;
  const page = loadPage({
    '/api/ring/telemetry': () => fail
      ? { status: 504, body: { detail: { message: 'Ring API GET /v1/devices/x/status timed out after 10.0s' } } }
      : { status: 200, body: partnerTelemetry(true) },
  });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  await page.run('onHeartbeatPulse()');

  fail = true;
  await page.run('onHeartbeatPulse()');

  assert.equal(page.el('badge-online').textContent, '⚪ Online N/D');   // unknown, not Offline
  assert.equal(page.el('badge-api-error').style.display, '');
  assert.equal(page.el('badge-api-error').textContent, '⚠️ Ring API error (HTTP 504)');
  assert.equal(page.run('state.offlineStreak'), 0);
  assert.equal(page.el('m-rssi').textContent, 'N/D');

  fail = false;
  await page.run('onHeartbeatPulse()');
  assert.equal(page.el('badge-api-error').style.display, 'none');
  assert.equal(page.el('badge-online').textContent, '🟢 Online');
});

test('Retry-After pauses polling instead of retrying', async () => {
  const page = loadPage({
    '/api/ring/telemetry': { status: 429, headers: { 'Retry-After': '30' }, body: { detail: { message: 'rate limited' } } },
  });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  await page.run('onHeartbeatPulse()');
  await page.run('onHeartbeatPulse()');
  await page.run('onHeartbeatPulse()');

  assert.equal(page.requests.length, 1);
  assert.ok(page.run('state.pollPausedUntil') > Date.now() + 25000);
});

test('Partner HUD and halo never use Digital Twin slider values', () => {
  const page = loadPage();
  page.el('ring-led-halo');  // halo element exists in the page
  const halo = () => page.el('ring-led-halo').className;

  page.run(`state.ringSource = 'partner'; state.rssi = -48; state.loss = 0; state.fps = 30; state.transformer = 0;`);
  page.run(`state.telemetry = ${JSON.stringify(partnerTelemetry(true))}; updateCanvasEffects(); updateLedHaloState();`);
  assert.equal(page.el('signal-quality-pill').textContent, '📡 Signal N/D');
  assert.equal(page.el('hud-fps-live').textContent, 'N/D FPS');
  assert.equal(halo(), 'ring-led-halo led-idle');   // from online: true, not the 0 V slider

  page.run(`state.telemetry = ${JSON.stringify(partnerTelemetry(false))}; updateLedHaloState();`);
  assert.equal(halo(), 'ring-led-halo led-offline');

  page.run(`state.telemetry = ${JSON.stringify(partnerTelemetry(null))}; updateLedHaloState();`);
  assert.equal(halo(), 'ring-led-halo');
});

test('Simulator HUD still follows the sliders', () => {
  const page = loadPage();
  page.run('state.rssi = -90; state.loss = 20; state.fps = 10; updateCanvasEffects();');

  assert.equal(page.el('signal-quality-pill').textContent, '❌ Critical Connection');
});

test('Last check-in is labelled as reported by Ring, not as the current time', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner';");

  page.run(`updateMetricCards(${JSON.stringify(partnerTelemetry(true, { updated_at: '2026-10-19T10:00:00Z' }))})`);
  assert.equal(page.el('badge-online').title, 'Last device check-in reported by Ring: 2026-10-19T10:00:00Z');

  page.run(`updateMetricCards(${JSON.stringify(partnerTelemetry(true, { updated_at: null }))})`);
  assert.equal(page.el('badge-online').title, 'Last device check-in not reported by Ring');
});

// ---------------------------------------------------------------------------
// 429 backoff and the offline alert time window (controlled clock)
// ---------------------------------------------------------------------------

/** Partner page whose /api/ring/telemetry answers come from `next()`; clock starts at T0. */
function partnerPage(next) {
  const page = loadPage({ '/api/ring/telemetry': next });
  page.run(`state.ringSource = 'partner'; state.ringMode = 'LIVE';
            state.offlineAlertReads = 3; state.offlineAlertMaxGapSeconds = 60;
            globalThis.__now = 1e12; Date.now = () => globalThis.__now;`);
  page.advance = seconds => page.run(`globalThis.__now += ${seconds * 1000}`);
  page.pulse = () => page.run('onHeartbeatPulse()');
  page.telemetryCalls = () => page.requests.filter(r => r.url === '/api/ring/telemetry').length;
  return page;
}

const RATE_LIMITED = { status: 429, body: { detail: { message: 'Too Many Requests' } } };
const ok = online => ({ status: 200, body: partnerTelemetry(online) });
const TIMEOUT = { status: 504, body: { detail: { message: 'timed out' } } };

test('429 without Retry-After backs off exponentially and the poll timer waits', async () => {
  let reply = RATE_LIMITED;
  const page = partnerPage(() => reply);

  await page.pulse();                                      // 429 #1 -> 1 s
  assert.equal(page.run('state.pollPausedUntil - Date.now()'), 1000);
  await page.pulse();                                      // still paused: no request
  assert.equal(page.telemetryCalls(), 1);

  page.advance(1); await page.pulse();                     // 429 #2 -> 2 s
  assert.equal(page.run('state.pollPausedUntil - Date.now()'), 2000);
  page.advance(1); await page.pulse();                     // 1 s into a 2 s pause: skipped
  assert.equal(page.telemetryCalls(), 2);
  page.advance(1); await page.pulse();                     // 429 #3 -> 4 s
  assert.equal(page.run('state.pollPausedUntil - Date.now()'), 4000);
  assert.equal(page.el('badge-online').textContent, '⚪ Online N/D');   // never "Offline"
  assert.equal(page.run('state.offlineStreak'), 0);

  reply = ok(true);
  page.advance(4); await page.pulse();                     // success resets the backoff
  assert.equal(page.run('state.rateLimitStreak'), 0);
  assert.equal(page.run('state.pollPausedUntil'), 0);
  reply = RATE_LIMITED;
  page.advance(15); await page.pulse();
  assert.equal(page.run('state.pollPausedUntil - Date.now()'), 1000);   // starts again at 1 s
});

test('429 backoff is capped at 300 s', async () => {
  const page = partnerPage(() => RATE_LIMITED);

  for (let i = 0; i < 12; i++) { await page.pulse(); page.advance(400); }

  assert.equal(page.telemetryCalls(), 12);
  page.advance(-400);
  assert.equal(page.run('state.pollPausedUntil - Date.now()'), 300000);
});

test('Retry-After is honoured as-is and does not advance the backoff', async () => {
  const page = partnerPage(() => ({ ...RATE_LIMITED, headers: { 'Retry-After': '45' } }));

  await page.pulse();

  assert.equal(page.run('state.pollPausedUntil - Date.now()'), 45000);
  assert.equal(page.run('state.rateLimitStreak'), 0);
});

test('Other API errors do not pause polling or count as offline', async () => {
  const page = partnerPage(() => TIMEOUT);

  await page.pulse();
  page.advance(15); await page.pulse();

  assert.equal(page.telemetryCalls(), 2);
  assert.equal(page.run('state.pollPausedUntil'), 0);
  assert.equal(page.run('state.offlineStreak'), 0);
});

test('Repeated offline reads within the window confirm the alert', async () => {
  const page = partnerPage(() => ok(false));

  for (let i = 0; i < 3; i++) { await page.pulse(); page.advance(15); }

  assert.equal(page.run('state.offlineAlertActive'), true);
  assert.equal(page.el('badge-online').textContent, '🔴 Offline');   // Ring state, shown separately
});

test('An online read resets the streak and clears the alert', async () => {
  let online = false;
  const page = partnerPage(() => ok(online));
  for (let i = 0; i < 3; i++) { await page.pulse(); page.advance(15); }

  online = true;
  await page.pulse();

  assert.equal(page.run('state.offlineAlertActive'), false);
  assert.equal(page.run('state.offlineStreak'), 0);
  assert.equal(page.el('hb-persistency').textContent, 'Offline reads 0/3');
});

test('Brief API errors between offline reads keep the streak', async () => {
  const replies = [ok(false), TIMEOUT, ok(false), TIMEOUT, ok(false)];
  const page = partnerPage(() => replies.shift());

  for (let i = 0; i < 5; i++) { await page.pulse(); page.advance(15); }   // evidence gaps: 30 s < 60 s

  assert.equal(page.run('state.offlineAlertActive'), true);
});

test('A long period of API errors discards earlier offline reads', async () => {
  let reply = ok(false);
  const page = partnerPage(() => reply);
  await page.pulse(); page.advance(15);
  await page.pulse();                                       // streak 2

  reply = TIMEOUT;
  for (let i = 0; i < 5; i++) { page.advance(15); await page.pulse(); }   // 75 s without evidence
  reply = ok(false);
  page.advance(15); await page.pulse();

  assert.equal(page.run('state.offlineStreak'), 1);         // restarted, not 3
  assert.equal(page.run('state.offlineAlertActive'), false);
});

test('A long period of online: null also discards earlier offline reads', async () => {
  let reply = ok(false);
  const page = partnerPage(() => reply);
  await page.pulse(); page.advance(15);
  await page.pulse();

  reply = ok(null);
  for (let i = 0; i < 5; i++) { page.advance(15); await page.pulse(); }
  reply = ok(false);
  page.advance(15); await page.pulse();

  assert.equal(page.run('state.offlineStreak'), 1);
  assert.equal(page.run('state.offlineAlertActive'), false);
});

test('A stale confirmed alert is cleared after a long gap', async () => {
  let reply = ok(false);
  const page = partnerPage(() => reply);
  for (let i = 0; i < 3; i++) { await page.pulse(); page.advance(15); }
  assert.equal(page.run('state.offlineAlertActive'), true);

  reply = TIMEOUT;
  for (let i = 0; i < 6; i++) { page.advance(15); await page.pulse(); }
  reply = ok(false);
  page.advance(15); await page.pulse();

  assert.equal(page.run('state.offlineAlertActive'), false);
  assert.equal(page.run('state.offlineStreak'), 1);
  assert.equal(page.el('badge-online').textContent, '🔴 Offline');   // Ring state still shown
});

test('Non-Twin modes never fill missing metrics with slider values (RING-014)', () => {
  for (const mode of ['LIVE', 'ERROR']) {
    const page = loadPage();
    page.run(`state.ringSource = 'simulator'; state.ringMode = '${mode}'; state.rssi = -90; state.jitter = 99;`);

    page.run('updateMetricCards({ device_health: {}, webrtc_stats: {} })');

    for (const id of ['m-rssi', 'm-battery', 'm-jitter', 'm-loss']) {
      assert.equal(page.el(id).textContent, 'N/D', `${mode} ${id}`);
    }
    page.run('updateDeviceLiveBadges()');
    assert.equal(page.el('badge-wifi').textContent, '📶 Wi-Fi N/D', mode);
  }
});

test('KB modal shows real KB fields and escapes HTML (RING-016)', () => {
  const page = loadPage();
  const article = {
    id: 'KB-X', title: '<img src=x onerror=alert(1)>Wi-Fi', issue_type: 'weak_wifi_signal',
    official_sources: ['Fixing offline devices', '<b>bold</b>'], symptoms: ['<script>s()</script>'],
    recommended_actions: ['Move router'], root_cause_domain: 'NETWORK', issue_category: 'network',
  };

  page.run(`renderKbModal([${JSON.stringify(article)}])`);
  const html = page.el('kb-list').children[0].innerHTML;

  assert.ok(!html.includes('<img'), html);
  assert.ok(!html.includes('<script>'), html);
  assert.ok(!html.includes('<b>bold</b>'), html);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;Wi-Fi'));
  assert.ok(html.includes('Fixing offline devices; &lt;b&gt;bold&lt;/b&gt;'));
  assert.ok(html.includes('weak_wifi_signal'));
  assert.ok(!html.includes('undefined'));
  assert.ok(!html.includes('Ring Support Center'));
});

test('KB modal tolerates missing optional fields without "undefined" (RING-016)', () => {
  const page = loadPage();

  page.run(`renderKbModal([{ id: 'KB-Y', title: 'Only a title' }])`);
  const html = page.el('kb-list').children[0].innerHTML;

  assert.ok(!html.includes('undefined'), html);
  assert.ok(html.includes('No source listed'));
});

test('Push banner renders model output as text, not HTML (RING-016)', () => {
  const page = loadPage();

  page.run(`showAppPushNotification({ diagnostic_status: 'DEGRADED', root_cause_domain: '<img src=x onerror=alert(1)>',
    confidence_score: 0.5, recommended_mitigation: 'x' }, 'manual')`);

  const meta = page.el('app-push-meta');
  assert.equal(meta.innerHTML, '');
  assert.equal(meta.children[0].textContent, 'DEGRADED • <img src=x onerror=alert(1)>');
});

const PLAYGROUND_TELEMETRY = {
  ...NULL_PARTNER_TELEMETRY,
  device_id: 'ava1.ring.device.PLAYGROUND',
  device_name: 'Playground Device',
  capabilities: {
    battery_status: null,
    audio: { customizable_slots: null, supported_actions: null },
    video: { codecs: ['AVC'], max_resolution: 1080 },
    motion_detection: { configurations: ['enabled', 'motion_zones'] },
    smoke_detection: null,
  },
};

test('Error mode shows N/D, never the static simulator defaults (P-04)', async () => {
  for (const source of ['partner', 'simulator']) {
    const page = loadPage();
    page.run(`state.ringSource = '${source}'; state.ringMode = 'ERROR'; state.rssi = -48; state.battery = 100;`);
    for (const [id, text] of [['m-rssi', '-48'], ['m-battery', '100'], ['m-jitter', '2.0'], ['m-loss', '0.0'],
      ['badge-wifi', '📶 Wi-Fi OK'], ['badge-battery', '🔋 100%']]) page.el(id).textContent = text;

    await page.run('refreshTelemetry()');

    for (const id of ['m-rssi', 'm-battery', 'm-jitter', 'm-loss']) {
      assert.equal(page.el(id).textContent, 'N/D', `${source} ${id}`);
    }
    assert.equal(page.el('badge-wifi').textContent, '📶 Wi-Fi N/D', source);
    assert.equal(page.el('badge-battery').textContent, '🔋 Power N/D', source);
    assert.equal(page.run('state.telemetry'), null, source);
    assert.equal(page.requests.length, 0, source);   // no simulated or live telemetry request
  }
});

test('Partner shows the Ring device name and ID, never the Twin identity (P-09)', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; applyPartnerModeUI();");

  page.run(`updateMetricCards(${JSON.stringify(PLAYGROUND_TELEMETRY)})`);

  assert.equal(page.el('dev-name').textContent, 'Playground Device');
  assert.equal(page.el('dev-id').textContent, 'ID: ava1.ring.device.PLAYGROUND');
  for (const id of ['twin-status-tag', 'badge-fw', 'twin-identity-fields']) {
    assert.equal(page.el(id).style.display, 'none', id);
  }
  assert.ok(!page.el('power-mode-status-pill').textContent.includes('Hardwired'));
});

test('Partner device without name or ID shows N/D (P-09)', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  page.run('updateMetricCards(null)');

  assert.equal(page.el('dev-name').textContent, 'Device name N/D');
  assert.equal(page.el('dev-id').textContent, 'ID: N/D');
});

test('Partner lists only the capabilities Ring reported (P-08)', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  page.run(`updateMetricCards(${JSON.stringify(PLAYGROUND_TELEMETRY)})`);
  const text = page.el('partner-capabilities').textContent;

  assert.equal(page.el('partner-capabilities').style.display, '');
  // battery_status: null is documented as "not a battery device"; other nulls are left out.
  assert.equal(text, 'Ring capabilities: video, motion_detection · Not supported: battery_status');

  page.run(`updateMetricCards(${JSON.stringify({ ...PLAYGROUND_TELEMETRY, capabilities: null })})`);
  assert.equal(page.el('partner-capabilities').textContent, 'Ring capabilities N/D');
});

test('Simulator keeps the Twin identity panel (P-09)', () => {
  const page = loadPage();
  page.el('dev-name').textContent = 'Ring Video Doorbell 4';
  page.run("state.ringSource = 'simulator'; state.ringMode = 'MOCK';");

  page.run(`updateMetricCards({ device_health: { rssi: -48, battery_percentage: 100 }, webrtc_stats: {} }); updateDeviceLiveBadges();`);

  assert.equal(page.el('dev-name').textContent, 'Ring Video Doorbell 4');
  assert.notEqual(page.el('twin-status-tag').style.display, 'none');
  assert.notEqual(page.el('partner-capabilities').style.display, '');
});

test('Capabilities use documented markers only: supported boolean and non-empty lists (D1)', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  const caps = {
    battery_status: { supported: false },       // documented boolean: not supported
    siren: false,                               // bare boolean: undocumented, not claimed either way
    smoke_detection: null,                      // undocumented null: not listed
    video: { codecs: [], configurations: [] },  // only empty lists: not claimed
    motion_detection: { configurations: ['enabled'] },
    chime_audio: { customizable_slots: 0 },     // numbers alone prove nothing
    chime_slots: { customizable_slots: 3 },
    night_vision: true,                         // bare true: undocumented, not claimed
    tamper_detection: { supported: true },
  };

  page.run(`updateMetricCards(${JSON.stringify({ ...PLAYGROUND_TELEMETRY, capabilities: caps })})`);

  assert.equal(page.el('partner-capabilities').textContent,
    'Ring capabilities: motion_detection, tamper_detection · Not supported: battery_status');
});

test('battery_status: null is shown as not supported, as Ring documents (D1)', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  page.run(`updateMetricCards(${JSON.stringify({ ...PLAYGROUND_TELEMETRY,
    capabilities: { battery_status: null, smoke_detection: null } })})`);

  assert.equal(page.el('partner-capabilities').textContent, 'Ring capabilities: none · Not supported: battery_status');
});

test('Capabilities with no reported entry say so instead of listing nulls (D1)', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  page.run(`updateMetricCards(${JSON.stringify({ ...PLAYGROUND_TELEMETRY, capabilities: { a: null, b: [], c: {} } })})`);

  assert.equal(page.el('partner-capabilities').textContent, 'Ring capabilities: none reported');
});

const TWIN_CONTAINERS = ['twin-power-selector', 'twin-sliders', 'twin-hw-controls'];

test('Partner hides simulated slider values and clears the active power button (D2)', () => {
  const page = loadPage();
  page.el('btn-pwr-wired').classList.add('active');
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; applyPartnerModeUI();");

  for (const id of TWIN_CONTAINERS) assert.equal(page.el(id).style.display, 'none', id);
  assert.equal(page.el('btn-pwr-wired').classList.contains('active'), false);
  assert.equal(page.el('btn-pwr-battery').classList.contains('active'), false);
});

test('Simulator keeps its sliders, power selector and active wired button (D2)', () => {
  const page = loadPage();
  page.run("setPowerMode('wired'); updateSlider('rssi', -60);");

  for (const id of TWIN_CONTAINERS) assert.notEqual(page.el(id).style.display, 'none', id);
  assert.equal(page.el('btn-pwr-wired').classList.contains('active'), true);
  assert.equal(page.el('lbl-rssi').textContent, '-60 dBm');
});

const STATUS_FAILURES = {
  'status 500': { '/api/scenarios': { status: 200, body: {} }, '/api/ring/status': { status: 500, body: {} } },
  'status unreachable': { '/api/scenarios': { status: 200, body: {} },
    '/api/ring/status': () => { throw new TypeError('Failed to fetch'); } },
  'mode missing': { '/api/scenarios': { status: 200, body: {} }, '/api/ring/status': { status: 200, body: { source: 'partner' } } },
  'unknown source': { '/api/scenarios': { status: 200, body: {} },
    '/api/ring/status': { status: 200, body: { source: 'cloud', mode: 'MOCK' } } },
};

test('Before /api/ring/status answers, the mode is unknown and nothing simulated runs (D3)', async () => {
  const page = loadPage({}, { simulatorReady: false });
  page.el('live-video-player');
  page.el('video-canvas');

  await page.run('refreshTelemetry()');
  page.run("simulateDing()");

  assert.notEqual(page.run('state.ringMode'), 'MOCK');
  assert.equal(page.run('cx.cameraActive'), false);
  assert.deepEqual(page.requests, []);
});

for (const [name, routes] of Object.entries(STATUS_FAILURES)) {
  test(`Status check failure (${name}) is an explicit error, never the simulator (D3)`, async () => {
    const page = loadPage(routes, { simulatorReady: false });
    page.el('live-video-player');
    page.el('video-canvas');

    await page.run('checkServer()');
    const afterCheck = page.requests.length;
    // Anomalous slider state that would trigger Twin telemetry and diagnosis in the simulator.
    page.run("state.powerMode = 'battery'; state.battery = 3; state.rssi = -90;");
    await page.run('onHeartbeatPulse()');
    await page.run("triggerAutonomousDiagnosis('background_immediate_power', 'power_cut')");
    page.run('simulateDing()');

    assert.equal(page.run('state.ringMode'), 'ERROR');
    assert.notEqual(page.el('server-status').textContent, 'Digital Twin active');
    assert.equal(page.requests.length, afterCheck, 'no /api/telemetry or /api/analyze after the failure');
    assert.equal(page.run('cx.cameraActive'), false);
    for (const id of ['m-rssi', 'm-battery']) assert.equal(page.el(id).textContent, 'N/D');
    for (const id of TWIN_CONTAINERS) assert.equal(page.el(id).style.display, 'none', id);
  });
}

test('Mode comes from /api/ring/status only: /api/scenarios is not consulted (D3)', async () => {
  const page = loadPage({
    '/api/scenarios': { status: 500, body: {} },
    '/api/ring/status': { status: 200, body: { source: 'partner', mode: 'LIVE' } },
  }, { simulatorReady: false });

  await page.run('checkServer()');

  assert.equal(page.run('state.ringMode'), 'LIVE');
  assert.equal(page.run('state.ringSource'), 'partner');
  assert.equal(page.el('server-status').textContent, 'Ring Partner API connected');
  assert.ok(!page.requests.some(r => r.url === '/api/scenarios'));
});

test('Outside MOCK the signal pill, FPS tag and halo never use slider values', () => {
  for (const mode of ['ERROR', 'UNKNOWN']) {
    const page = loadPage({}, { simulatorReady: false });
    page.el('ring-led-halo');
    page.run(`state.ringSource = 'simulator'; state.ringMode = '${mode}'; state.rssi = -48; state.loss = 0; state.fps = 30;`);

    page.run('updateCanvasEffects(); updateLedHaloState();');

    assert.equal(page.el('signal-quality-pill').textContent, '📡 Signal N/D', mode);
    assert.equal(page.el('hud-fps-live').textContent, 'N/D FPS', mode);
    assert.equal(page.el('ring-led-halo').className, 'ring-led-halo', mode);
  }
});

test('Explicit simulator status keeps the Digital Twin fully working (D3)', async () => {
  const page = loadPage({
    '/api/scenarios': { status: 200, body: {} },
    '/api/ring/status': { status: 200, body: { source: 'simulator', mode: 'MOCK' } },
    '/api/telemetry': { status: 200, body: { device_health: { rssi: -48, battery_percentage: 100 }, webrtc_stats: {} } },
  }, { simulatorReady: false });

  await page.run('checkServer()');
  await page.run('refreshTelemetry()');

  assert.equal(page.run('state.ringMode'), 'MOCK');
  assert.equal(page.el('server-status').textContent, 'Digital Twin active');
  assert.ok(page.requests.some(r => r.url === '/api/telemetry'));
  assert.equal(page.el('m-rssi').textContent, -48);
  assert.equal(page.run('autoDiagnosisEnabled()'), true);
  for (const id of TWIN_CONTAINERS) assert.notEqual(page.el(id).style.display, 'none', id);
});

// ---------------------------------------------------------------------------
// B1 / B2: alert and report lifecycle, heartbeat counter
// ---------------------------------------------------------------------------

const POWER_REPORT = {
  _provider: 'local_fallback',
  diagnostic_result: {
    diagnostic_status: 'CRITICAL', root_cause_domain: 'HARDWARE_POWER', confidence_score: 0.86,
    telemetry_findings: ['Battery level is critically low at 3%.'], visual_findings: [],
    recommended_mitigation: 'Charge the battery.',
  },
};
const settle = () => new Promise(r => setTimeout(r, 5));

function twinPage(analyzeBody = POWER_REPORT) {
  const page = loadPage({
    '/api/telemetry': { status: 200, body: { device_health: {}, webrtc_stats: {} } },
    '/api/analyze': { status: 200, body: analyzeBody },
  });
  page.pulse = async () => { await page.run('onHeartbeatPulse()'); await settle(); };
  page.analyzeCount = () => page.requests.filter(r => r.url === '/api/analyze').length;
  page.bannerShown = () => page.el('app-push-banner').classList.contains('show');
  page.counter = () => page.el('hb-persistency').textContent;
  return page;
}

test('Reset hides the active alert and clears the previous report (B1)', async () => {
  const page = twinPage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();
  assert.equal(page.bannerShown(), true);
  assert.equal(page.el('result-content').style.display, '');

  page.run('resetTelemetryDefaults()');

  assert.equal(page.bannerShown(), false);
  assert.equal(page.el('result-content').style.display, 'none');
  assert.equal(page.el('empty-result').style.display, '');
  assert.notEqual(page.el('diag-resolved-note').style.display, '');
});

test('Recovery hides the alert but keeps the report, marked resolved (B1)', async () => {
  const page = twinPage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();

  page.run('state.battery = 100;');
  await page.pulse();

  assert.equal(page.bannerShown(), false);
  assert.equal(page.el('result-content').style.display, '');            // report kept
  assert.equal(page.el('diag-status-text').textContent, 'CRITICAL');     // unchanged history
  assert.equal(page.el('diag-resolved-note').style.display, '');
  assert.match(page.el('diag-resolved-note').textContent, /Resolved/);
  assert.equal(page.counter(), '0/3 heartbeats');
});

test('A new anomaly after recovery is diagnosed again and is no longer marked resolved (B1)', async () => {
  const page = twinPage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();
  page.run('state.battery = 100;');
  await page.pulse();

  page.run('state.battery = 3;');   // same values as the first incident
  await page.pulse();

  assert.equal(page.analyzeCount(), 2);
  assert.equal(page.bannerShown(), true);
  assert.notEqual(page.el('diag-resolved-note').style.display, '');
});

test('A nominal pulse does not mark an event-session report as resolved (B1)', async () => {
  const page = twinPage();
  await page.run("triggerAutonomousDiagnosis('event_session', 'ding')");

  await page.pulse();   // heartbeat sees no anomaly; the report did not come from the heartbeat

  assert.equal(page.bannerShown(), true);
  assert.notEqual(page.el('diag-resolved-note').style.display, '');
});

test('Partner polls never clear or mark a manual Partner report (B1)', async () => {
  const page = loadPage({
    '/api/analyze': { status: 200, body: POWER_REPORT },
    '/api/ring/telemetry': { status: 200, body: { ...NULL_PARTNER_TELEMETRY } },
  });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  await page.run('runManualDiagnosis()');

  for (let i = 0; i < 3; i++) await page.run('onHeartbeatPulse()');

  assert.equal(page.el('result-content').style.display, '');
  assert.equal(page.el('diag-status-text').textContent, 'CRITICAL');
  assert.equal(page.el('app-push-banner').classList.contains('show'), true);
  assert.notEqual(page.el('diag-resolved-note').style.display, '');
});

test('The visible counter never exceeds 3/3 and a sustained anomaly is diagnosed once (B2)', async () => {
  const page = twinPage();
  page.run('state.rssi = -80;');                       // Poor Wi-Fi: DEGRADED

  for (let i = 0; i < 5; i++) await page.pulse();

  assert.equal(page.counter(), '3/3 heartbeats [Network · Degraded]');
  assert.equal(page.run('state.anomalyConsecutiveCount'), 3);
  assert.equal(page.analyzeCount(), 1);
});

test('A different metric in the same family continues the incident (A2)', async () => {
  const page = twinPage();
  page.run('state.rssi = -70;');                       // Weak Wi-Fi signal
  await page.pulse(); await page.pulse();
  assert.equal(page.counter(), '2/3 heartbeats [Network · Degraded]');

  page.run('state.rssi = -48; state.loss = 20;');      // High packet loss: same NETWORK family
  await page.pulse();

  assert.equal(page.counter(), '3/3 heartbeats [Network · Degraded]');
  assert.equal(page.analyzeCount(), 1);
});

test('A new problem family restarts the count (A2)', async () => {
  const page = twinPage();
  page.run('state.rssi = -70;');
  await page.pulse(); await page.pulse();

  page.run("state.rssi = -48; state.powerMode = 'battery'; state.battery = 20;");   // Low battery: power family
  await page.pulse();

  assert.equal(page.counter(), '1/3 heartbeats [Power · Degraded]');
  assert.equal(page.analyzeCount(), 0);
});

test('Battery-only from 3% to 35% resolves the incident; 3% to 12% keeps it open (B2/D4)', async () => {
  const page = twinPage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");   // below 10 %: CRITICAL, immediate
  await page.pulse();
  assert.equal(page.counter(), '3/3 heartbeats [Power · Critical]');
  assert.equal(page.analyzeCount(), 1);

  page.run('state.battery = 12;');                                // Low (DEGRADED): still an incident
  for (let i = 0; i < 3; i++) await page.pulse();
  assert.equal(page.analyzeCount(), 1);                           // no new diagnosis on the way down
  assert.equal(page.bannerShown(), true);
  assert.notEqual(page.el('diag-resolved-note').style.display, '');

  page.run('state.battery = 35;');                                // GREEN: resolved
  await page.pulse();
  assert.equal(page.counter(), '0/3 heartbeats');
  assert.equal(page.bannerShown(), false);
  assert.equal(page.el('diag-resolved-note').style.display, '');

  page.run('state.battery = 8;');                                 // new incident
  await page.pulse();
  assert.equal(page.analyzeCount(), 2);
  assert.equal(page.bannerShown(), true);
});

test('A recurring anomaly is diagnosed again after recovery when the first analysis was skipped (B2)', async () => {
  const page = twinPage({ analysis_skipped: true });   // nothing rendered, so no alert to resolve
  page.run('state.rssi = -90;');
  for (let i = 0; i < 3; i++) await page.pulse();
  assert.equal(page.analyzeCount(), 1);

  page.run('state.rssi = -48;');
  await page.pulse();
  page.run('state.rssi = -90;');                         // same values as the first incident
  for (let i = 0; i < 3; i++) await page.pulse();

  assert.equal(page.analyzeCount(), 2);
});

test('A recurring anomaly is diagnosed again after an event-session report replaced the alert (B2)', async () => {
  const page = twinPage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();                                                   // heartbeat alert
  await page.run("triggerAutonomousDiagnosis('event_session', 'ding')"); // replaces it
  page.run('state.battery = 100;');
  await page.pulse();                                                   // recovery

  page.run('state.battery = 3;');
  await page.pulse();

  assert.equal(page.analyzeCount(), 3);
});

test('Switching to a power mode with no anomaly also ends the incident for deduplication (B2)', async () => {
  const page = twinPage({ analysis_skipped: true });
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();
  assert.equal(page.analyzeCount(), 1);

  page.run("setPowerMode('wired');");   // 18 V, battery 100: no anomaly remains
  page.run("setPowerMode('battery'); state.battery = 3;");
  await page.pulse();

  assert.equal(page.analyzeCount(), 2);
});

/** Twin page whose first /api/analyze response is held until `release()`; later ones answer at once. */
function racePage() {
  let release;
  const held = new Promise(r => { release = r; });
  let calls = 0;
  const page = loadPage({
    '/api/telemetry': { status: 200, body: { device_health: {}, webrtc_stats: {} } },
    '/api/analyze': () => (++calls === 1 ? held : Promise.resolve())
      .then(() => ({ status: 200, body: POWER_REPORT })),
  });
  page.pulse = async () => { await page.run('onHeartbeatPulse()'); await settle(); };
  page.release = async () => { release(); await settle(); };
  page.analyzeCount = () => page.requests.filter(r => r.url === '/api/analyze').length;
  page.bannerShown = () => page.el('app-push-banner').classList.contains('show');
  return page;
}

test('A late analysis answer after recovery does not bring back a resolved alert (race)', async () => {
  const page = racePage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();                                 // analysis pending
  assert.equal(page.analyzeCount(), 1);

  page.run('state.battery = 100;');
  await page.pulse();                                 // telemetry recovered before the answer
  await page.release();                               // late answer for the old incident

  assert.equal(page.bannerShown(), false);
  assert.notEqual(page.el('result-content').style.display, '');
  assert.equal(page.el('diag-status-text').textContent, '');
  assert.equal(page.run('state.heartbeatAlertActive'), false);
  assert.equal(page.run('isAnalyzing'), false);
});

test('After a discarded late answer, a new legitimate analysis still shows its result (race)', async () => {
  const page = racePage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();
  page.run('state.battery = 100;');
  await page.pulse();
  await page.release();

  page.run('state.battery = 3;');
  await page.pulse();

  assert.equal(page.analyzeCount(), 2);
  assert.equal(page.bannerShown(), true);
  assert.equal(page.el('diag-status-text').textContent, 'CRITICAL');
  assert.equal(page.run('state.heartbeatAlertActive'), true);
});

test('A pending analysis is kept when the anomaly is still present at the answer (race)', async () => {
  const page = racePage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();
  await page.pulse();                                 // still anomalous: same incident
  await page.release();

  assert.equal(page.bannerShown(), true);
  assert.equal(page.el('diag-status-text').textContent, 'CRITICAL');
});

test('Reset while an analysis is pending discards its late answer (race)', async () => {
  const page = racePage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();

  page.run('resetTelemetryDefaults()');
  await page.release();

  assert.equal(page.bannerShown(), false);
  assert.equal(page.el('result-content').style.display, 'none');
});

test('An event-session analysis is not discarded by a nominal heartbeat pulse (race)', async () => {
  const page = racePage();
  page.run("triggerAutonomousDiagnosis('event_session', 'ding')");   // pending, not awaited
  await settle();

  await page.pulse();                                 // nominal heartbeat while it is pending
  await page.release();

  assert.equal(page.bannerShown(), true);
  assert.equal(page.el('diag-status-text').textContent, 'CRITICAL');
});

test('Non-Twin modes never start a fake camera stream (RING-013)', () => {
  const page = loadPage();
  page.el('live-video-player');
  page.el('video-canvas');
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; activateCamera('live', 0);");

  assert.equal(page.run('cx.cameraActive'), false);
  assert.ok(page.toasts.some(t => t.msg.includes('not implemented')));
});

// ---------------------------------------------------------------------------
// B3 / O1: battery-only bands from the severity table (critical < 10 %, low < 30 %)
// ---------------------------------------------------------------------------

for (const [battery, level] of [[9, 'CRITICAL'], [10, 'DEGRADED'], [11, 'DEGRADED'], [15, 'DEGRADED'], [29, 'DEGRADED'], [30, null]]) {
  test(`Battery-only at ${battery}% is ${level || 'GREEN'} (B3/O1)`, async () => {
    const page = twinPage();
    page.run(`state.powerMode = 'battery'; state.battery = ${battery};`);

    assert.equal(page.run('getHeartbeatAnomaly()?.level ?? null'), level);
    await page.pulse();
    assert.equal(page.analyzeCount(), level === 'CRITICAL' ? 1 : 0);   // critical: first pulse
    await page.pulse(); await page.pulse();
    assert.equal(page.analyzeCount(), level ? 1 : 0);                   // degraded: third pulse
  });
}

// A5: every yellow or red indicator is a level that counts; the band name is always written.
for (const [setup, badgeId, text, cls, level] of [
  ['state.rssi = -60;', 'badge-wifi', '📶 Wi-Fi Excellent', 'badge badge-green', null],
  ['state.rssi = -61;', 'badge-wifi', '📶 Wi-Fi Weak', 'badge badge-yellow', 'DEGRADED'],
  ['state.rssi = -75;', 'badge-wifi', '📶 Wi-Fi Weak', 'badge badge-yellow', 'DEGRADED'],
  ['state.rssi = -76;', 'badge-wifi', '📶 Wi-Fi Poor', 'badge badge-yellow', 'DEGRADED'],
  ['state.rssi = -88;', 'badge-wifi', '📶 Wi-Fi Poor', 'badge badge-yellow', 'DEGRADED'],
  ['state.rssi = -89;', 'badge-wifi', '📶 Wi-Fi Critical', 'badge badge-red', 'CRITICAL'],
  ["state.powerMode = 'battery'; state.battery = 30;", 'badge-battery', '🔋 30% (Battery)', 'badge badge-green', null],
  ["state.powerMode = 'battery'; state.battery = 29;", 'badge-battery', '🔋 29% (Low)', 'badge badge-yellow', 'DEGRADED'],
  ["state.powerMode = 'battery'; state.battery = 10;", 'badge-battery', '🔋 10% (Low)', 'badge badge-yellow', 'DEGRADED'],
  ["state.powerMode = 'battery'; state.battery = 9;", 'badge-battery', '🪫 9% (Critical)', 'badge badge-red', 'CRITICAL'],
  ['state.transformer = 12;', 'badge-battery', '⚡ 12.0V (Hardwired)', 'badge badge-green', null],
  ['state.transformer = 11.5;', 'badge-battery', '⚡ Undervoltage (11.5V)', 'badge badge-yellow', 'DEGRADED'],
  ['state.transformer = 1;', 'badge-battery', '⚡ Undervoltage (1V)', 'badge badge-yellow', 'DEGRADED'],
  ['state.transformer = 0.5;', 'badge-battery', '🪫 No power (0.5V)', 'badge badge-red', 'CRITICAL'],
  ['state.transformer = 0;', 'badge-battery', '🪫 No power (0V)', 'badge badge-red', 'CRITICAL'],
]) {
  test(`Indicator and heartbeat agree at the boundary: ${setup} (A5)`, () => {
    const page = twinPage();
    page.run(`${setup} updateDeviceLiveBadges();`);

    assert.equal(page.el(badgeId).textContent, text);
    assert.equal(page.el(badgeId).className, cls);
    assert.equal(page.run('getHeartbeatAnomaly()?.level ?? null'), level);
  });
}

test('Metric cards show the band name next to the colour, from the table (A5/E10)', () => {
  const page = twinPage();
  page.run('state.rssi = -80; state.jitter = 41; state.loss = 8; updateMetricCards({ device_health: {}, webrtc_stats: {} });');

  assert.equal(page.el('m-rssi').className, 'metric-value metric-fair');
  assert.equal(page.el('m-rssi-level').textContent, 'Poor · Degraded');
  assert.equal(page.el('m-jitter-level').textContent, 'High · Degraded');
  assert.equal(page.el('m-loss').className, 'metric-value metric-good');    // 8 % is not above 8 %
  assert.equal(page.el('m-loss-level').textContent, 'OK');
});

test('Without the severity table nothing is classified and the Twin stays disabled', async () => {
  const page = loadPage({ '/api/thresholds': { status: 500, body: {} } }, { simulatorReady: false, severity: false });

  await page.run('checkServer()');

  assert.equal(page.run('state.ringMode'), 'ERROR');
  assert.equal(page.run('getHeartbeatAnomaly()'), null);
  assert.match(page.el('server-status').textContent, /severity thresholds unavailable/);
});

// The engine label names the provider that produced the shown report
// ---------------------------------------------------------------------------

for (const [provider, label] of [['local_fallback', 'Local fallback'], ['bedrock', 'AWS Bedrock'], ['precheck', 'pre-check']]) {
  test(`A ${provider} report is labelled as such, not as a Bedrock answer`, async () => {
    const page = twinPage({ ...POWER_REPORT, _provider: provider });
    page.run("state.powerMode = 'battery'; state.battery = 3;");
    await page.pulse();

    assert.equal(page.analyzeCount(), 1);
    assert.match(page.el('engine-provider').textContent, new RegExp(label));
  });
}

// ---------------------------------------------------------------------------
// Section 7.1: heartbeat sequences S1-S9 (critical = first pulse, degraded = 3 pulses)
// ---------------------------------------------------------------------------

const NETWORK_REPORT = {
  _provider: 'bedrock',
  severity_level: 'DEGRADED',
  retrieved_articles: [{ id: 'KB-RING-001' }],
  mitigation_source: 'knowledge_base',
  telemetry: { device_id: 'a1b2c3d4e5f6', _source: 'client_supplied' },
  diagnostic_result: {
    diagnostic_status: 'DEGRADED', root_cause_domain: 'NETWORK', confidence_score: 0.85,
    telemetry_findings: ['Wi-Fi signal (RSSI) is -84 dBm (Poor).'], visual_findings: [],
    recommended_mitigation: 'Your doorbell has a weak Wi-Fi connection. Move your router closer.',
  },
};
const NOMINAL_REPORT = {
  _provider: 'precheck', analysis_skipped: true, retrieved_articles: [],
  diagnostic_result: {
    diagnostic_status: 'HEALTHY', root_cause_domain: 'NONE', confidence_score: 1,
    telemetry_findings: ['All measured device and transport metrics are within the GREEN band.'],
    visual_findings: [], recommended_mitigation: 'No action needed: your doorbell is working normally.',
  },
};

/** Twin page that answers /api/analyze with `body` and records the analyses per pulse. */
function seqPage(body = NETWORK_REPORT) {
  const page = twinPage(body);
  page.title = () => page.el('app-push-title').textContent;
  page.pulses = async (n) => { const counts = []; for (let i = 0; i < n; i++) { await page.pulse(); counts.push(page.analyzeCount()); } return counts; };
  return page;
}

for (const [name, rssi, reason, limit] of [['S1', -84, 'Poor Wi-Fi signal', '< -75 dBm'], ['S2', -70, 'Weak Wi-Fi signal', '< -60 dBm']]) {
  test(`${name}: RSSI ${rssi} (degraded) counts 1/3, 2/3, 3/3 and is diagnosed on pulse 3`, async () => {
    const page = seqPage();
    page.run(`state.rssi = ${rssi};`);

    await page.pulse(); assert.equal(page.counter(), '1/3 heartbeats [Network · Degraded]');
    await page.pulse(); assert.equal(page.counter(), '2/3 heartbeats [Network · Degraded]');
    assert.equal(page.analyzeCount(), 0);
    await page.pulse(); assert.equal(page.counter(), '3/3 heartbeats [Network · Degraded]');

    assert.equal(page.analyzeCount(), 1);
    assert.equal(page.title(), `Ring Alert: ${reason} for 3 heartbeats (${limit})`);
    assert.equal(page.el('diag-status-text').textContent, 'DEGRADED');
  });
}

test('S3: RSSI -90 (critical) is diagnosed on the first pulse', async () => {
  const page = seqPage({ ...NETWORK_REPORT, diagnostic_result: { ...NETWORK_REPORT.diagnostic_result, diagnostic_status: 'CRITICAL' } });
  page.run('state.rssi = -90;');

  assert.deepEqual(await page.pulses(1), [1]);
  assert.equal(page.counter(), '3/3 heartbeats [Network · Critical]');
  assert.equal(page.title(), 'Ring Alert: Critical Wi-Fi signal (< -88 dBm)');
});

test('S4: Battery Only 10% counts 3 pulses; 9% is diagnosed on the first pulse', async () => {
  const low = seqPage(POWER_REPORT);
  low.run("state.powerMode = 'battery'; state.battery = 10;");
  assert.deepEqual(await low.pulses(3), [0, 0, 1]);

  const critical = seqPage(POWER_REPORT);
  critical.run("state.powerMode = 'battery'; state.battery = 9;");
  assert.deepEqual(await critical.pulses(1), [1]);
  assert.equal(critical.title(), 'Ring Alert: Critical battery level (< 10%)');
});

test('S5: wired 13 V is nominal, 11 V needs 3 pulses, 0 V is immediate', async () => {
  const ok = seqPage(POWER_REPORT);
  ok.run('state.transformer = 13;');
  assert.deepEqual(await ok.pulses(3), [0, 0, 0]);
  assert.equal(ok.counter(), '0/3 heartbeats');

  const under = seqPage(POWER_REPORT);
  under.run('state.transformer = 11;');
  assert.deepEqual(await under.pulses(3), [0, 0, 1]);
  assert.equal(under.title(), 'Ring Alert: Transformer undervoltage for 3 heartbeats (< 12 V AC)');

  const cut = seqPage(POWER_REPORT);
  cut.run('state.transformer = 0;');
  assert.deepEqual(await cut.pulses(1), [1]);
  assert.equal(cut.title(), 'Ring Alert: Transformer power cut (≤ 0.5 V AC)');
});

test('S6: RSSI -90 -> -70 keeps the incident open; -50 resolves it; Reset clears everything', async () => {
  const page = seqPage();
  page.run('state.rssi = -90;');
  await page.pulse();
  assert.equal(page.analyzeCount(), 1);

  page.run('state.rssi = -70;');                      // CRITICAL -> DEGRADED: still the same incident
  await page.pulse(); await page.pulse();
  assert.equal(page.analyzeCount(), 1);
  assert.equal(page.bannerShown(), true);
  assert.notEqual(page.el('diag-resolved-note').style.display, '');
  assert.equal(page.counter(), '3/3 heartbeats [Network · Degraded]');

  page.run('state.rssi = -50;');                      // every metric GREEN: resolved
  await page.pulse();
  assert.equal(page.counter(), '0/3 heartbeats');
  assert.equal(page.bannerShown(), false);
  assert.match(page.el('diag-resolved-note').textContent, /Resolved/);
  assert.equal(page.el('result-content').classList.contains('resolved'), true);

  page.run('resetTelemetryDefaults()');
  assert.equal(page.el('result-content').style.display, 'none');
  assert.equal(page.counter(), '0/3 heartbeats');
  assert.match(page.el('engine-provider').textContent, /No diagnosis yet/);
});

test('S7: an escalation from degraded (2/3) to critical is diagnosed on that pulse', async () => {
  const page = seqPage();
  page.run('state.rssi = -70;');
  assert.deepEqual(await page.pulses(2), [0, 0]);

  page.run('state.rssi = -90;');
  assert.deepEqual(await page.pulses(1), [1]);
  assert.equal(page.title(), 'Ring Alert: Critical Wi-Fi signal (< -88 dBm)');
});

test('S7b: an escalation after a degraded diagnosis is diagnosed again, a drop is not', async () => {
  const page = seqPage();
  page.run('state.rssi = -80;');
  assert.deepEqual(await page.pulses(3), [0, 0, 1]);
  page.run('state.rssi = -90;');
  assert.deepEqual(await page.pulses(1), [2]);       // DEGRADED -> CRITICAL
  page.run('state.rssi = -80;');
  assert.deepEqual(await page.pulses(2), [2, 2]);    // CRITICAL -> DEGRADED: no new diagnosis
});

/** Twin page where activateCamera runs against the fake DOM and the event delay is 0 ms. */
function triggerPage(body) {
  const page = seqPage(body);
  Object.assign(page.el('live-video-player'), { play: () => Promise.resolve(), pause() {} });
  page.run(`globalThis.requestAnimationFrame = () => 0; globalThis.cancelAnimationFrame = () => {};
            EVENT_DIAGNOSIS_DELAY_MS = 0;`);
  return page;
}

for (const [label, call, name] of [
  ['Ring Chime (Ding)', 'simulateDing()', 'Doorbell Press'],
  ['PIR Motion', 'simulateMotion()', 'Motion Detection'],
  ['Live View', 'toggleLiveView()', 'Live View'],
]) {
  test(`S8 / B1: ${label} with a problem is diagnosed at once, titled "Issue during ${name}"`, async () => {
    const page = triggerPage();
    page.run('state.rssi = -70;');
    await page.pulse();                                // heartbeat counting 1/3

    page.run(call);
    await settle(); await settle();

    const analyses = page.requests.filter(r => r.url === '/api/analyze');
    assert.equal(analyses.length, 1);                  // immediate, without waiting for 3/3
    assert.equal(analyses[0].body.camera_active, true);
    assert.equal(page.title(), `Ring Alert: Issue during ${name}`);

    // B3 / D10: the heartbeat reaching 3/3 does not report the same incident a second time.
    await page.pulse(); await page.pulse();
    assert.equal(page.analyzeCount(), 1);
  });

  test(`S9 / B2 / D11: ${label} without a problem shows and notifies nothing, and calls no Bedrock`, async () => {
    const page = triggerPage(NOMINAL_REPORT);
    await page.run(call);
    await settle(); await settle();
    assertSilentNominal(page);
  });
}

/** D11: a nominal automatic analysis leaves no alert, card, toast or engine label behind. */
function assertSilentNominal(page) {
  assert.equal(page.analyzeCount(), 1);                // the backend pre-check answers; no Bedrock call
  assert.equal(page.bannerShown(), false);
  assert.notEqual(page.el('result-content').style.display, '');
  assert.equal(page.el('diag-status-text').textContent, '');
  assert.ok(!page.toasts.some(t => /All good|No issue|Bedrock/i.test(t.msg)));
  assert.doesNotMatch(page.el('engine-provider').textContent, /pre-check/);
}

test('E9 / D11: Night mode plus Live View without a problem shows and notifies nothing', async () => {
  const page = triggerPage(NOMINAL_REPORT);
  page.run('toggleNightMode();');
  await page.run('toggleLiveView()');
  await settle(); await settle();
  assertSilentNominal(page);
});

test('D11: an explicit "Analyze now" still answers a nominal result (Partner path unchanged)', async () => {
  const page = loadPage({ '/api/analyze': { status: 200, body: NOMINAL_REPORT } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  await page.run("triggerAutonomousDiagnosis('manual')");

  assert.ok(page.toasts.some(t => /No issues detected/.test(t.msg)));
  assert.ok(!page.toasts.some(t => /All good/.test(t.msg)));
  assert.equal(page.el('diag-status-text').textContent, 'Healthy');
  assert.equal(page.el('result-content').style.display, '');
});

test('B3: a Ding while the camera is starting shares one analysis (no duplicate report)', async () => {
  const page = triggerPage();
  page.run('state.rssi = -70; EVENT_DIAGNOSIS_DELAY_MS = 5; simulateDing(); simulateMotion();');
  await new Promise(r => setTimeout(r, 30));

  assert.equal(page.analyzeCount(), 1);
});

test('B3: a trigger after the heartbeat already reported the incident updates the report, not the alert', async () => {
  const page = triggerPage();
  page.run('state.rssi = -80;');
  await page.pulses(3);
  const title = page.title();

  page.run("toggleLiveView();");
  await settle(); await settle();

  assert.equal(page.analyzeCount(), 2);                // new evidence (the frame) is analyzed
  assert.equal(page.title(), title);                   // same incident, same notification
});

// ---------------------------------------------------------------------------
// A4: alert titles quote the severity table, never stale numbers
// ---------------------------------------------------------------------------

test('Alert titles quote the boundaries of the severity table (A4)', async () => {
  for (const [setup, metric, label] of [
    ["state.powerMode = 'battery'; state.battery = 5;", 'battery', 'Critical'],
    ["state.powerMode = 'battery'; state.battery = 20;", 'battery', 'Low'],
    ['state.transformer = 0;', 'transformer', 'No power'],
    ['state.transformer = 11;', 'transformer', 'Undervoltage'],
    ['state.rssi = -95;', 'rssi', 'Critical'],
    ['state.rssi = -80;', 'rssi', 'Poor'],
    ['state.jitter = 90;', 'jitter_ms', 'High'],
    ['state.fps = 10;', 'fps', 'Low'],
  ]) {
    const page = seqPage(POWER_REPORT);
    page.run(setup);
    await page.pulses(3);
    const limit = SEVERITY_TABLE.metrics[metric].bands.find(b => b.label === label).value;
    const numbers = (page.title().match(/-?\d+(\.\d+)?/g) || []).map(Number).filter(n => n !== 3);
    assert.deepEqual(numbers, [limit], `${setup} -> ${page.title()}`);
  }
});

// ---------------------------------------------------------------------------
// E: user view vs technical view, engine label, standby HUD
// ---------------------------------------------------------------------------

test('E1/E5: the user view has no device ID or findings; the technical view has them', async () => {
  const page = seqPage();
  page.run('state.rssi = -80;');
  await page.pulses(3);

  const userView = ['diag-status-text', 'diag-domain-text', 'mitigation-text', 'app-push-title', 'app-push-desc']
    .map(id => page.el(id).textContent).join(' ');
  assert.ok(!userView.includes('a1b2c3d4e5f6'));
  assert.ok(!userView.includes('RSSI'));
  assert.equal(page.el('diag-domain-text').textContent, 'Network Issue (Wi-Fi / Upstream)');
  assert.equal(page.el('tech-device-id').textContent, 'a1b2c3d4e5f6');
  assert.equal(page.el('tech-sops').textContent, 'KB-RING-001');
  assert.equal(page.el('tech-provider').textContent, '☁️ AWS Bedrock (Claude)');
  assert.equal(page.el('tech-mitigation-source').textContent, 'Project knowledge base (Ring-informed, unverified)');
  assert.match(page.run('ticketText()'), /Device ID: a1b2c3d4e5f6/);
});

test('E2: the push banner shows a short line, never the full mitigation', async () => {
  const page = seqPage();
  page.run('state.rssi = -80;');
  await page.pulses(3);

  const desc = page.el('app-push-desc').textContent;
  assert.ok(!desc.includes(NETWORK_REPORT.diagnostic_result.recommended_mitigation));
  assert.ok(desc.split(/\s+/).length <= 15, desc);
  assert.equal(page.el('mitigation-text').textContent, NETWORK_REPORT.diagnostic_result.recommended_mitigation);
});

test('E3: the engine label follows the last diagnosis and starts as "No diagnosis yet"', async () => {
  const page = seqPage({ ...POWER_REPORT, _provider: 'local_fallback' });
  page.run('showReportProvider(null)');
  assert.match(page.el('engine-provider').textContent, /No diagnosis yet/);

  page.run("state.powerMode = 'battery'; state.battery = 3;");
  await page.pulse();
  assert.equal(page.el('engine-provider').textContent, '⚙️ Local fallback (mock-up)');

  page.run("showReportProvider('something_else')");
  assert.doesNotMatch(page.el('engine-provider').textContent, /Bedrock/);

  page.run('clearDiagnosticConsole()');
  assert.match(page.el('engine-provider').textContent, /No diagnosis yet/);
});

test('E3: without AWS credentials the idle label says the local fallback is used', async () => {
  const page = loadPage({ '/api/engine': { status: 200, body: { credentials_configured: false } } });
  await page.run('loadEngineStatus()');
  assert.equal(page.el('engine-provider').textContent, '⚙️ Not configured (using local fallback)');
});

test('E7: in standby the Stop button is hidden and the HUD shows no frame rate', () => {
  const page = triggerPage();
  page.run('deactivateCamera(); updateCanvasEffects();');
  assert.equal(page.el('btn-stop-camera').style.display, 'none');
  assert.equal(page.el('hud-fps-live').textContent, 'Standby');
  assert.match(page.el('hud-cam-meta').textContent, /standby/i);

  page.run("activateCamera('live', 0); clearTimeout(pendingEventDiagnosis); updateCanvasEffects();");
  assert.equal(page.el('btn-stop-camera').style.display, '');
  assert.equal(page.el('hud-fps-live').textContent, '30 FPS');
});

test('E8: the page declares a favicon served by the API', () => {
  const html = readFileSync(new URL('web/index.html', ROOT), 'utf8');
  assert.match(html, /<link rel="icon"[^>]+href="\/favicon\.ico"/);
});

test('E6: Night mode is a normal condition, outside the fault injection grid', () => {
  const html = readFileSync(new URL('web/index.html', ROOT), 'utf8');
  const faults = html.indexOf('Physical Fault Injection');
  const normal = html.indexOf('Normal operating conditions');
  const night = html.indexOf('id="hw-btn-night"');
  assert.ok(faults < normal && normal < night);
  assert.match(html, /Night mode \(IR LEDs\)/);
});

test('T2 / C7: the Transformer Cut button follows the transformer slider and Reset', () => {
  const page = loadPage();
  const active = () => page.el('hw-btn-power').classList.contains('active-warn');

  page.run('togglePowerCut();');
  assert.equal(active(), true);
  assert.equal(page.run('state.transformer'), 0);
  assert.equal(page.el('sl-transformer').value, 0);

  page.run("updateSlider('transformer', '18');");
  assert.equal(active(), false);
  assert.equal(page.run('state.hardwareFaults.powerCut'), false);

  page.run("updateSlider('transformer', '0.5');");    // inside the table's power-cut band (<= 0.5 V)
  assert.equal(active(), true);
  page.run("updateSlider('transformer', '1');");
  assert.equal(active(), false);

  page.run('togglePowerCut(); resetTelemetryDefaults();');
  assert.equal(active(), false);
  assert.equal(page.run('state.hardwareFaults.powerCut'), false);
});

test('T3 / C2: the simulator device card hides the ID; Partner shows it', () => {
  const html = readFileSync(new URL('web/index.html', ROOT), 'utf8');
  assert.match(html, /id="dev-id" style="display:none;"/);
  assert.match(html, /id="c-devid" value="a1b2c3d4e5f6"/);   // kept in the configuration field

  const page = loadPage();
  page.el('dev-id').style.display = 'none';
  page.run("state.ringSource = 'simulator'; state.ringMode = 'MOCK';");
  page.run('updateMetricCards({ device_health: { rssi: -48, battery_percentage: 100 }, webrtc_stats: {} }); updateDeviceLiveBadges();');
  assert.equal(page.el('dev-id').style.display, 'none');

  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  page.run(`updateMetricCards(${JSON.stringify(PLAYGROUND_TELEMETRY)})`);
  assert.equal(page.el('dev-id').style.display, '');
});

test('T16: Technical details show a root-cause domain replaced by the pre-check', () => {
  const page = loadPage();
  page.run(`renderResult(${JSON.stringify(NETWORK_REPORT.diagnostic_result)},
    ${JSON.stringify({ ...NETWORK_REPORT, domain_source: 'precheck_override',
                       domain_override: { model: 'ENVIRONMENT_POWER', deterministic: 'NETWORK' } })})`);
  assert.equal(page.el('diag-domain-text').textContent, 'Network Issue (Wi-Fi / Upstream)');
  assert.equal(page.el('tech-domain').textContent, 'NETWORK (model said ENVIRONMENT_POWER; replaced by the pre-check)');

  page.run(`renderResult(${JSON.stringify(NETWORK_REPORT.diagnostic_result)}, ${JSON.stringify(NETWORK_REPORT)})`);
  assert.equal(page.el('tech-domain').textContent, 'NETWORK');
});

// ---------------------------------------------------------------------------
// T21: plain-language Ring error reasons under the header (never the token)
// ---------------------------------------------------------------------------

const RING_REASON_HINTS = {
  missing_token: 'No Ring token configured.',
  token_rejected: 'Ring rejected the token. Playground tokens expire after 30 minutes: '
    + 'generate a new one and restart the server.',
  blocked_406: 'Ring blocked the request: required headers are missing, the request came from a datacenter IP, '
    + 'or Ring Security blocked this IP (not retryable; contact Ring developer support).',
  time_range_not_authorized: 'Ring only allows access to recordings made after you connected this app; '
    + 'the requested time is earlier.',
  rate_limited: 'Ring rate limit reached: requests resume after the wait Ring asked for.',
  device_offline_503: 'Ring answered 503: the device is unreachable or Ring is busy.',
  network_error: 'Ring could not be reached (timeout or network error).',
};

for (const [reason, hint] of Object.entries(RING_REASON_HINTS)) {
  test(`T21: /api/ring/status error_reason ${reason} keeps the header and shows its hint`, async () => {
    const page = loadPage({
      '/api/ring/status': { status: 200, body: { mode: 'ERROR', source: 'partner', error_reason: reason } },
    }, { simulatorReady: false });

    await page.run('checkServer()');

    assert.equal(page.el('server-status').textContent, 'Ring credentials error');
    assert.equal(page.el('partner-error-hint').textContent, hint);
    assert.equal(page.el('partner-error-hint').style.display, '');
  });
}

test('T21: a failed telemetry read shows the hint for its reason; a good read clears it', async () => {
  let reply = { status: 502, body: { detail: { message: 'HTTP 401', reason: 'token_rejected' } } };
  const page = loadPage({ '/api/ring/telemetry': () => reply });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  await page.run('refreshTelemetry().catch(() => {})');
  assert.equal(page.el('partner-error-hint').textContent, RING_REASON_HINTS.token_rejected);

  reply = { status: 200, body: NULL_PARTNER_TELEMETRY };
  await page.run('refreshTelemetry()');
  assert.equal(page.el('partner-error-hint').style.display, 'none');
});

test('T21: simulator mode never shows a Ring error hint', async () => {
  const page = loadPage({ '/api/ring/status': { status: 200, body: { mode: 'MOCK', source: 'simulator' } } },
    { simulatorReady: false });
  await page.run('checkServer()');
  assert.notEqual(page.el('partner-error-hint').style.display, '');
  assert.equal(page.el('server-status').textContent, 'Digital Twin active');
});

// ---------------------------------------------------------------------------
// T22: Ring-reported device settings on the Partner card and in Technical details
// ---------------------------------------------------------------------------

const RING_DEVICE_CONTEXT = {
  e2e_encryption_enabled: false,
  motion_detection: { enabled: true, zones: 1 },
  image_enhancements: { color_night_vision: false, hdr: false, ir_led_night_vision: false,
    auto_zoom_track: true, privacy_zones: 0 },
  video: { max_resolution: 1080, supported_resolutions: [1080] },
  location: { country: 'US', state: null },
  snooze: { active: null, until: null },
};
const SETTINGS_LINE = 'Ring settings: night vision color off · IR LED off · HDR off · privacy zones 0 · motion on (1 zone)';

test('T22: Partner card shows the Ring settings line; missing values are N/D', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  page.run(`updateMetricCards(${JSON.stringify({ ...NULL_PARTNER_TELEMETRY, device_context: RING_DEVICE_CONTEXT })})`);
  assert.equal(page.el('partner-settings').textContent, SETTINGS_LINE);

  const partial = { motion_detection: { enabled: null, zones: null }, image_enhancements: { hdr: true } };
  page.run(`updateMetricCards(${JSON.stringify({ ...NULL_PARTNER_TELEMETRY, device_context: partial })})`);
  assert.equal(page.el('partner-settings').textContent,
    'Ring settings: night vision color N/D · IR LED N/D · HDR on · privacy zones N/D · motion N/D');

  page.run(`updateMetricCards(${JSON.stringify(NULL_PARTNER_TELEMETRY)})`);
  assert.equal(page.el('partner-settings').textContent, 'Ring settings N/D');
});

test('T22: Technical details list Device settings (from Ring) only for Partner reports', () => {
  const page = loadPage();
  const diag = { diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'NONE', confidence_score: 0.5 };

  page.run(`renderResult(${JSON.stringify(diag)}, ${JSON.stringify({
    telemetry: { _source: 'ring_partner_api', device_context: RING_DEVICE_CONTEXT } })})`);
  assert.equal(page.el('tech-device-settings').style.display, '');
  assert.equal(page.el('tech-device-settings').textContent, SETTINGS_LINE);
  assert.ok(page.run('ticketText()').includes(`Device settings (from Ring): ${SETTINGS_LINE}`));

  page.run(`renderResult(${JSON.stringify(diag)}, ${JSON.stringify({ telemetry: { _source: 'mock' } })})`);
  assert.equal(page.el('tech-device-settings').style.display, 'none');
  assert.ok(!page.run('ticketText()').includes('Device settings'));
});

// ---------------------------------------------------------------------------
// T23: end-to-end encrypted video warning on the Partner card
// ---------------------------------------------------------------------------

const E2E_TEXT = 'Video is end-to-end encrypted: image analysis is not possible; diagnosis uses device status only';

for (const [label, mediaReadable, shown] of [['enabled', false, true], ['disabled', true, false], ['absent', null, false]]) {
  test(`T23: E2E encryption ${label} -> warning ${shown ? 'shown' : 'hidden'}`, () => {
    const page = loadPage();
    page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
    const context = { ...RING_DEVICE_CONTEXT, media_readable: mediaReadable };

    page.run(`updateMetricCards(${JSON.stringify({ ...NULL_PARTNER_TELEMETRY, device_context: context })})`);

    assert.equal(page.el('partner-e2e-warning').style.display, shown ? '' : 'none');
    assert.equal(page.el('partner-e2e-warning').textContent, shown ? E2E_TEXT : '');
  });
}

// ---------------------------------------------------------------------------
// T26: settings the device does not support read "n/a", not "off"
// ---------------------------------------------------------------------------

const LIVE_CONTEXT = {
  ...RING_DEVICE_CONTEXT,
  unsupported_settings: ['hdr', 'ir_led_night_vision', 'auto_zoom_track'],
  image_enhancements: { color_night_vision: true, hdr: null, ir_led_night_vision: null, auto_zoom_track: null,
    privacy_zones: 0 },
};
const LIVE_SETTINGS_LINE = 'Ring settings: night vision color on · IR LED n/a · HDR n/a · privacy zones 0 · motion on (1 zone)';

test('T26: unsupported settings show n/a on the card and in Device settings (from Ring)', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  page.run(`updateMetricCards(${JSON.stringify({ ...NULL_PARTNER_TELEMETRY, device_context: LIVE_CONTEXT })})`);
  assert.equal(page.el('partner-settings').textContent, LIVE_SETTINGS_LINE);

  const diag = { diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'NONE', confidence_score: 0 };
  page.run(`renderResult(${JSON.stringify(diag)}, ${JSON.stringify({
    telemetry: { _source: 'ring_partner_api', device_context: LIVE_CONTEXT } })})`);
  assert.equal(page.el('tech-device-settings').textContent, LIVE_SETTINGS_LINE);
});

test('T26: a supported setting reported off still reads off', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  const context = { ...LIVE_CONTEXT, unsupported_settings: [],
    image_enhancements: { ...LIVE_CONTEXT.image_enhancements, hdr: false } };
  page.run(`updateMetricCards(${JSON.stringify({ ...NULL_PARTNER_TELEMETRY, device_context: context })})`);
  assert.ok(page.el('partner-settings').textContent.includes('HDR off'));
});

// ---------------------------------------------------------------------------
// T27: the Partner monitor shows the Ring snapshot the diagnosis checked
// ---------------------------------------------------------------------------

test('T27: Partner monitor label no longer says retrieval is not integrated', () => {
  const page = loadPage();
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; applyPartnerModeUI();");
  assert.equal(page.el('monitor-source-label').textContent, 'Ring snapshot: none yet (run Analyze now)');
  assert.equal(page.el('monitor-title').textContent, 'Camera monitor — Ring');   // T51
});

test('T27: Analyze now with a snapshot shows it with Ring time and origin', async () => {
  const page = loadPage({ '/api/analyze': { status: 200, body: {
    analysis_skipped: false,
    snapshot: { media_timestamp: 1761210000000, media_origin: 'recording' },
    diagnostic_result: { diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'UNKNOWN', confidence_score: 0 },
  } } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  await page.run('runManualDiagnosis()');

  assert.match(page.el('partner-snapshot').src, /^\/api\/ring\/snapshot\?t=\d+$/);
  assert.equal(page.el('partner-snapshot').style.display, '');
  assert.equal(page.el('monitor-source-label').textContent,
    'Ring snapshot (latest recording) · 2025-10-23 09:00:00 UTC · recording');
});

test('T27: without a snapshot the monitor stays as it was', async () => {
  const page = loadPage({ '/api/analyze': { status: 200, body: { analysis_skipped: true } } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; applyPartnerModeUI();");
  await page.run('runManualDiagnosis()');
  assert.notEqual(page.el('partner-snapshot').style.display, '');
});

// ---------------------------------------------------------------------------
// T28: Ring events list and event-triggered reports
// ---------------------------------------------------------------------------

const BLURRY_EVENT_REPORT = trigger => ({
  event: { id: 'ev-9', event_type: trigger === 'live' ? 'on_demand' : trigger, start: 1761210000000 },
  trigger,
  _provider: 'local_fallback',
  diagnostic_result: { diagnostic_status: 'DEGRADED', root_cause_domain: 'OPTICAL_PHYSICAL', confidence_score: 0.86,
    telemetry_findings: [], visual_findings: ['Blur'], recommended_mitigation: 'Clean the lens.' },
});

test('T28: Ring events list shows type, subtype and time; empty list says No events yet', async () => {
  let reply = { status: 200, body: { events: [
    { id: 'a', event_type: 'motion', sub_type: 'human', start: 1761210000000 },
    { id: 'b', event_type: 'ding', sub_type: null, start: 1761210060000 },
  ], reports: [] } };
  const page = loadPage({ '/api/ring/events': () => reply });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

  await page.run('pollPartnerEvents()');
  const items = page.el('partner-events').children[1].children.map(li => li.textContent);
  assert.equal(page.el('partner-events').children[0].textContent, 'Ring events');
  assert.deepEqual(items, ['Motion · human · 09:00:00 UTC', 'Doorbell press · 09:01:00 UTC']);
  assert.equal(page.el('diag-status-text').textContent, '');   // no report: silence (D11)
  assert.equal(page.toasts.length, 0);

  reply = { status: 200, body: { events: [], reports: [] } };
  await page.run('pollPartnerEvents()');
  // The fake DOM keeps old children after innerHTML = '': the latest list is the last child.
  assert.deepEqual(page.el('partner-events').children.at(-1).children.map(li => li.textContent), ['No events yet']);
});

for (const [trigger, name] of [['ding', 'Doorbell Press'], ['motion', 'Motion Detection'], ['live', 'Live View']]) {
  test(`T28: a new ${trigger} event with a problem is reported as "Issue during ${name}"`, async () => {
    const page = loadPage({ '/api/ring/events': { status: 200, body: { events: [], reports: [BLURRY_EVENT_REPORT(trigger)] } } });
    page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");

    await page.run('pollPartnerEvents()');

    assert.equal(page.el('app-push-title').textContent, `Ring Alert: Issue during ${name}`);
    assert.equal(page.el('diag-status-text').textContent, 'DEGRADED');
  });
}

test('T28: events Retry-After pauses the next event queries', async () => {
  const page = loadPage({ '/api/ring/events': { status: 429, headers: { 'Retry-After': '30' }, body: {} } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  await page.run('pollPartnerEvents()');
  await page.run('pollPartnerEvents()');
  assert.equal(page.requests.filter(r => r.url === '/api/ring/events').length, 1);
});

// ---------------------------------------------------------------------------
// T29: Partner customer label from checked_domains
// ---------------------------------------------------------------------------

const PARTNER_DOMAINS = image => [
  { domain: 'image', status: image, detail: '' },
  { domain: 'network', status: 'not_available', detail: 'Ring does not share Wi-Fi or power data with partner apps' },
  { domain: 'power', status: 'not_available', detail: 'Ring does not share Wi-Fi or power data with partner apps' },
];

for (const [image, status, label] of [
  ['ok', 'INSUFFICIENT_DATA', 'Partial check'],
  ['not_available', 'INSUFFICIENT_DATA', 'Not enough data'],
  ['issue', 'DEGRADED', 'DEGRADED'],
]) {
  test(`T29: Partner image ${image} -> customer label "${label}", technical value kept`, () => {
    const page = loadPage();
    const diag = { diagnostic_status: status, root_cause_domain: 'UNKNOWN', confidence_score: 0 };
    page.run(`renderResult(${JSON.stringify(diag)}, ${JSON.stringify({ checked_domains: PARTNER_DOMAINS(image) })})`);
    assert.equal(page.el('diag-status-text').textContent, label);
    assert.equal(page.el('tech-level').textContent, status);
  });
}

test('T29: simulator reports keep INSUFFICIENT_DATA as the label', () => {
  const page = loadPage();
  const diag = { diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'UNKNOWN', confidence_score: 0 };
  page.run(`renderResult(${JSON.stringify(diag)}, {})`);
  assert.equal(page.el('diag-status-text').textContent, 'INSUFFICIENT_DATA');
});

// ---------------------------------------------------------------------------
// T33: Partner push title follows "Partial check"; a snapshot hides the live HUD texts
// ---------------------------------------------------------------------------

const PARTNER_REPORT = image => ({
  analysis_skipped: false,
  snapshot: { media_timestamp: 1761210000000, media_origin: 'recording' },
  checked_domains: PARTNER_DOMAINS(image),
  diagnostic_result: { diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'UNKNOWN', confidence_score: 0 },
});

for (const [image, title] of [
  ['ok', 'Ring: Partial check (image OK; Wi-Fi and power are not shared by Ring)'],
  ['not_available', 'Ring: Not enough data to diagnose this device'],
]) {
  test(`T33: Partner image ${image} -> push title "${title}"`, async () => {
    const page = loadPage({ '/api/analyze': { status: 200, body: PARTNER_REPORT(image) } });
    page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
    await page.run('runManualDiagnosis()');
    assert.equal(page.el('app-push-title').textContent, title);
  });
}

test('T33: an event report with "Partial check" gets the same push title', async () => {
  const page = loadPage({ '/api/ring/events': { status: 200, body: { events: [], reports: [PARTNER_REPORT('ok')] } } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  await page.run('pollPartnerEvents()');
  assert.equal(page.el('app-push-title').textContent, 'Ring: Partial check (image OK; Wi-Fi and power are not shared by Ring)');
});

test('T33: a Partner snapshot hides the standby, signal and FPS overlays', async () => {
  const page = loadPage({ '/api/analyze': { status: 200, body: PARTNER_REPORT('ok') } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; applyPartnerModeUI(); updateCanvasEffects();");
  assert.equal(page.el('signal-quality-pill').textContent, '📡 Signal N/D');
  await page.run('runManualDiagnosis()');
  for (const id of ['hud-cam-meta', 'signal-quality-pill', 'hud-fps-live']) {
    assert.equal(page.el(id).style.display, 'none', id);
  }
});

test('T33: without a snapshot the overlays stay visible', async () => {
  const page = loadPage({ '/api/analyze': { status: 200, body: { analysis_skipped: true } } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; applyPartnerModeUI(); updateCanvasEffects(); updateCameraStateControls();");
  await page.run('runManualDiagnosis()');
  assert.match(page.el('hud-cam-meta').textContent, /standby/i);
  assert.equal(page.el('signal-quality-pill').textContent, '📡 Signal N/D');
  for (const id of ['hud-cam-meta', 'signal-quality-pill', 'hud-fps-live']) {
    assert.notEqual(page.el(id).style.display, 'none', id);
  }
});

// ---------------------------------------------------------------------------
// T38: funnel line in the engine panel
// ---------------------------------------------------------------------------

const METRICS = {
  analyses_total: 12, precheck_nominal: 7, precheck_anomaly: 5, bedrock_calls: 3, bedrock_retries: 0,
  local_fallbacks: 0, insufficient_data: 0, feedback_up: 2, feedback_down: 0,
};

test('T38: the engine panel shows the funnel line from /api/metrics', async () => {
  const page = loadPage({ '/api/metrics': { status: 200, body: METRICS } });
  await page.run('loadEngineStatus()');
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(page.el('funnel-line').textContent,
    'Funnel (this session): Analyses 12 · healthy frames kept on server 7 · Bedrock calls 3 · fallbacks 0 · helpful 2 / not helpful 0');
});

test('T38: the funnel line refreshes after each analysis', async () => {
  let calls = 0;
  const page = loadPage({
    '/api/analyze': { status: 200, body: { analysis_skipped: true } },
    '/api/metrics': () => ({ status: 200, body: { ...METRICS, analyses_total: ++calls } }),
  });
  await page.run('runManualDiagnosis()');
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.match(page.el('funnel-line').textContent, /^Funnel \(this session\): Analyses 1 ·/);
});

test('T50: the funnel line refreshes after an event poll, even when the background analysis was silent', async () => {
  const page = loadPage({
    '/api/ring/events': { status: 200, body: { events: [], reports: [] } },
    '/api/metrics': { status: 200, body: { ...METRICS, analyses_total: 4, precheck_nominal: 3 } },
  });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE';");
  await page.run('pollPartnerEvents()');
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(page.requests.filter(r => r.url === '/api/metrics').length, 1);
  assert.match(page.el('funnel-line').textContent, /^Funnel \(this session\): Analyses 4 · healthy frames kept on server 3 ·/);
});

test('T38: a failed metrics request keeps the previous line', async () => {
  const page = loadPage({ '/api/metrics': { status: 500, body: {} } });
  page.el('funnel-line').textContent = 'Funnel (this session): —';
  await page.run('loadFunnelMetrics()');
  assert.equal(page.el('funnel-line').textContent, 'Funnel (this session): —');
});

// ---------------------------------------------------------------------------
// T39: "Was this helpful?" buttons
// ---------------------------------------------------------------------------

const RATED_REPORT = {
  report_id: '11111111-2222-3333-4444-555555555555', _provider: 'bedrock',
  diagnostic_result: { diagnostic_status: 'DEGRADED', root_cause_domain: 'OPTICAL_PHYSICAL', confidence_score: 0.9,
    telemetry_findings: [], visual_findings: [], recommended_mitigation: 'Clean the lens.' },
};

function ratedPage(feedbackRoute = { status: 200, body: { ok: true } }) {
  return loadPage({ '/api/analyze': { status: 200, body: RATED_REPORT }, '/api/feedback': feedbackRoute });
}

const feedbackRequests = page => page.requests.filter(r => r.url === '/api/feedback');

test('T39: a report shows "Was this helpful?" with enabled buttons', async () => {
  const page = ratedPage();
  await page.run('runManualDiagnosis()');
  assert.equal(page.el('feedback-box').style.display, '');
  assert.equal(page.el('feedback-up').disabled, false);
  assert.equal(page.el('feedback-down').disabled, false);
  assert.equal(page.el('feedback-status').textContent, '');
});

test('T39: 👍 sends one vote, says thanks and disables the buttons', async () => {
  const page = ratedPage();
  await page.run('runManualDiagnosis()');
  await page.run("sendFeedback('up')");
  assert.deepEqual(feedbackRequests(page).map(r => r.body), [{ report_id: RATED_REPORT.report_id, rating: 'up' }]);
  assert.equal(page.el('feedback-status').textContent, 'Thanks for your feedback');
  assert.equal(page.el('feedback-up').disabled, true);
  assert.equal(page.el('feedback-down').disabled, true);
  await page.run("sendFeedback('down')");
  assert.equal(feedbackRequests(page).length, 1);   // one vote per report
});

test('T39: 👎 shows an optional comment, sent trimmed and at most 200 characters', async () => {
  const page = ratedPage();
  await page.run('runManualDiagnosis()');
  page.run('showFeedbackComment()');
  assert.equal(page.el('feedback-comment-row').style.display, '');
  page.el('feedback-comment').value = `  ${'y'.repeat(250)}  `;
  await page.run("sendFeedback('down')");
  const [{ body }] = feedbackRequests(page);
  assert.equal(body.rating, 'down');
  assert.equal(body.comment, 'y'.repeat(200));
  assert.equal(page.el('feedback-comment-row').style.display, 'none');
  assert.equal(page.el('feedback-send').disabled, true);
});

test('T39: 👎 without a comment sends no comment field', async () => {
  const page = ratedPage();
  await page.run('runManualDiagnosis()');
  page.run('showFeedbackComment()');
  await page.run("sendFeedback('down')");
  assert.deepEqual(feedbackRequests(page)[0].body, { report_id: RATED_REPORT.report_id, rating: 'down' });
});

test('T39: a failed vote keeps the buttons enabled and shows an error', async () => {
  const page = ratedPage({ status: 503, body: { detail: 'Feedback could not be saved on the server; try again later.' } });
  await page.run('runManualDiagnosis()');
  await page.run("sendFeedback('up')");
  assert.equal(page.el('feedback-up').disabled, false);
  assert.equal(page.el('feedback-status').textContent, '');
  assert.ok(page.toasts.some(t => t.type === 'error' && /Feedback not sent/.test(t.msg)));
});

test('T39: a new report enables the buttons again; the voted one stays disabled', async () => {
  const page = ratedPage();
  await page.run('runManualDiagnosis()');
  await page.run("sendFeedback('up')");
  page.run(`renderResult(${JSON.stringify(RATED_REPORT.diagnostic_result)}, { report_id: 'other-report' })`);
  assert.equal(page.el('feedback-up').disabled, false);
  assert.equal(page.el('feedback-status').textContent, '');
  page.run(`renderResult(${JSON.stringify(RATED_REPORT.diagnostic_result)}, ${JSON.stringify(RATED_REPORT)})`);
  assert.equal(page.el('feedback-up').disabled, true);
  assert.equal(page.el('feedback-status').textContent, 'Thanks for your feedback');
});

test('T39: a report without report_id hides the feedback prompt', () => {
  const page = loadPage();
  page.run(`renderResult(${JSON.stringify(RATED_REPORT.diagnostic_result)}, {})`);
  assert.equal(page.el('feedback-box').style.display, 'none');
});

// ---------------------------------------------------------------------------
// T43: "Analyze last recording (5 s)": recording stats, never live network values
// ---------------------------------------------------------------------------

const CLIP_REPORT = {
  _provider: 'precheck',
  diagnostic_result: { diagnostic_status: 'INSUFFICIENT_DATA', root_cause_domain: 'NONE', confidence_score: 1,
                       telemetry_findings: [], visual_findings: [], recommended_mitigation: 'Check Device Health.' },
  checked_domains: [{ domain: 'image', status: 'ok', detail: 'Ring recording checked: no image issue detected' }],
  clip: { status: 'ok', label: 'Recording stats (from Ring recording, not live network)', duration_seconds: 5,
          fps: 15, resolution: '1280x720', bitrate_mbps: 1.2, frames_checked: 3 },
};

test('T43: a clip shows recording stats and the image verdict, and leaves the network cards alone', async () => {
  const page = loadPage({ '/api/ring/clip-analysis': { status: 200, body: CLIP_REPORT },
                          '/api/metrics': { status: 404, body: {} } });
  page.run("state.ringSource = 'partner';");
  page.el('m-jitter').textContent = 'N/D';

  await page.run('runClipAnalysis()');

  assert.equal(page.el('ring-media-stats').textContent,
    'Recording stats (from Ring recording, not live network): 5.0 s · 15 fps · 1280x720 · 1.2 Mbps'
    + ' · Image: Ring recording checked: no image issue detected');
  assert.equal(page.el('m-jitter').textContent, 'N/D');
  assert.ok(page.requests.some(r => r.url === '/api/ring/clip-analysis'));
  assert.equal(page.el('btn-analyze-clip').disabled, false);
});

test('T43: a clip Ring could not provide says why', async () => {
  const page = loadPage({ '/api/ring/clip-analysis': { status: 200, body: {
    clip: { status: 'not_ready', reason: "Ring's recording was not ready in time: no clip was analyzed" } } } });
  await page.run('runClipAnalysis()');
  assert.equal(page.el('ring-media-stats').textContent,
    "Recording stats: not available (Ring's recording was not ready in time: no clip was analyzed)");
});

test('T43: a credential error on the clip shows its hint', async () => {
  const page = loadPage({ '/api/ring/clip-analysis': { status: 502, body: {
    detail: { message: 'HTTP 401', reason: 'token_rejected' } } } });
  await page.run('runClipAnalysis()');
  assert.equal(page.el('partner-error-hint').textContent, RING_REASON_HINTS.token_rejected);
  assert.equal(page.el('ring-media-stats').textContent, 'Recording stats: not available (HTTP 401)');
});

// ---------------------------------------------------------------------------
// T44: "Live stream stats (8 s)" (best effort; never shown as home-network jitter or loss)
// ---------------------------------------------------------------------------

test('T44: live stream stats are labelled as received by this server', async () => {
  const page = loadPage({ '/api/ring/live-stats': { status: 200, body: { live: {
    status: 'ok', reason: null, label: 'Live stream as received by this server', seconds: 8, fps: 14.9,
    resolution: '1280x720', frame_count: 120, width: 1280, height: 720 } } } });
  page.el('m-loss').textContent = 'N/D';

  await page.run('runLiveStats()');

  assert.equal(page.el('ring-live-stats').textContent,
    'Live stream as received by this server: 8.0 s · 14.9 fps · 1280x720 · 120 frames');
  assert.equal(page.el('m-loss').textContent, 'N/D');
  assert.equal(page.el('btn-live-stats').disabled, false);
});

test('T44: an unavailable stream says why, as information and not an error', async () => {
  const page = loadPage({ '/api/ring/live-stats': { status: 200, body: { live: {
    status: 'not_available', reason: 'rtsps_unavailable', label: 'Live stream as received by this server' } } } });
  await page.run('runLiveStats()');
  assert.equal(page.el('ring-live-stats').textContent, 'Live stream stats: not available (rtsps_unavailable)');
  assert.equal(page.toasts.length, 0);
});
