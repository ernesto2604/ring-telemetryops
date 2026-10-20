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
function loadPage(routes = {}, { simulatorReady = true } = {}) {
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
  // Most tests start from a page whose /api/ring/status already reported the simulator.
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

  assert.deepEqual(page.requests.map(r => r.url), Array(3).fill('/api/ring/telemetry'));
});

test('online false shows Offline and confirms the alert only after N consecutive reads', async () => {
  const page = loadPage({ '/api/ring/telemetry': { status: 200, body: partnerTelemetry(false) } });
  page.run("state.ringSource = 'partner'; state.ringMode = 'LIVE'; state.offlineAlertReads = 2;");

  await page.run('onHeartbeatPulse()');
  assert.equal(page.el('badge-online').textContent, '🔴 Offline');
  assert.equal(page.run('state.offlineAlertActive'), false);
  assert.equal(page.el('hb-persistency').textContent, 'Offline 1/2 reads');

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
  assert.equal(page.el('hb-persistency').textContent, 'Offline 0/3 reads');
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
  assert.equal(meta.children[0].textContent, 'Domain: <img src=x onerror=alert(1)>');
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
  page.run('state.rssi = -90;');

  for (let i = 0; i < 5; i++) await page.pulse();

  assert.equal(page.counter(), '3/3 heartbeats [Wi-Fi]');
  assert.equal(page.run('state.anomalyConsecutiveCount'), 3);
  assert.equal(page.analyzeCount(), 1);
});

test('A different reason in the same domain restarts the count (B2)', async () => {
  const page = twinPage();
  page.run('state.rssi = -90;');                       // Weak Wi-Fi signal
  await page.pulse(); await page.pulse();
  assert.equal(page.counter(), '2/3 heartbeats [Wi-Fi]');

  page.run('state.rssi = -48; state.loss = 20;');      // Degraded IP link (same NETWORK domain)
  await page.pulse();

  assert.equal(page.counter(), '1/3 heartbeats [Wi-Fi]');
  assert.equal(page.analyzeCount(), 0);
});

test('Battery recovering from 3% to 12% resolves the incident; a new drop below 10% is a new one (B2/B3)', async () => {
  const page = twinPage();
  page.run("state.powerMode = 'battery'; state.battery = 3;");   // below 10 %: immediate
  await page.pulse();
  assert.equal(page.counter(), '3/3 heartbeats [Power]');
  assert.equal(page.analyzeCount(), 1);

  page.run('state.battery = 12;');                                // 12 % is not an anomaly
  for (let i = 0; i < 3; i++) await page.pulse();
  assert.equal(page.counter(), '0/3 heartbeats');
  assert.equal(page.analyzeCount(), 1);
  assert.equal(page.bannerShown(), false);
  assert.equal(page.el('diag-resolved-note').style.display, '');

  page.run('state.battery = 8;');                                 // new incident
  await page.pulse();
  assert.equal(page.counter(), '3/3 heartbeats [Power]');
  assert.equal(page.analyzeCount(), 2);
  assert.equal(page.bannerShown(), true);
  assert.notEqual(page.el('diag-resolved-note').style.display, '');
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
// B3: battery-only alert threshold (same value as the backend's BATTERY_CRITICAL_PCT)
// ---------------------------------------------------------------------------

for (const [battery, immediate] of [[9, true], [10, false], [11, false], [12, false], [15, false]]) {
  test(`Battery-only at ${battery}% ${immediate ? 'alerts on the first pulse' : 'is never an anomaly'} (B3)`, async () => {
    const page = twinPage();
    page.run(`state.powerMode = 'battery'; state.battery = ${battery};`);

    assert.equal(page.run('getHeartbeatAnomaly()?.isCriticalImmediate === true'), immediate);
    if (!immediate) assert.equal(page.run('getHeartbeatAnomaly()'), null);
    for (let i = 0; i < 3; i++) await page.pulse();
    assert.equal(page.analyzeCount(), immediate ? 1 : 0);
  });
}

test('Badge colour bands stay display-only and do not become heartbeat rules (B3)', () => {
  const page = twinPage();
  page.run("state.powerMode = 'battery'; state.battery = 25; state.rssi = -70; updateDeviceLiveBadges();");

  assert.equal(page.el('badge-battery').textContent, '🔋 25% (Low)');   // yellow band: 10 <= battery < 30
  assert.equal(page.el('badge-wifi').textContent, '📶 Wi-Fi Weak');     // yellow band: -75 <= rssi < -60
  assert.equal(page.run('getHeartbeatAnomaly()'), null);

  page.run('state.battery = 9; updateDeviceLiveBadges();');
  assert.equal(page.el('badge-battery').textContent, '🪫 9% (Critical)'); // red band unchanged: < 10
});

// ---------------------------------------------------------------------------
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
