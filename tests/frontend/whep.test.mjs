// Ring live view (WHEP) in the browser: run with `node --test tests/frontend`.
// Loads web/js/whep.js into a VM context with a fake DOM, fake timers, a stubbed RTCPeerConnection and a
// mocked fetch. No browser, no network, no Ring.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const SOURCE = readFileSync(new URL('../../web/js/whep.js', import.meta.url), 'utf8');
const ANSWER = 'v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=sendonly\r\n';
const SESSION = { session_id: 'a'.repeat(32), sdp_answer: ANSWER, max_seconds_hint: 30 };

const flush = () => new Promise(resolve => setImmediate(resolve));

class FakeElement {
  constructor(id) {
    this.id = id;
    this.textContent = '';
    this.disabled = false;
    this.style = {};
    this.srcObject = null;
  }
}

/** Page with whep.js; `post` is the reply to POST /api/ring/whep/session ({status, body}). */
function loadLiveView({ post = { status: 200, body: SESSION }, ice = 'event', stats = [] } = {}) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, new FakeElement(id));
    return elements.get(id);
  };
  el('partner-snapshot').style.display = '';
  el('ring-live-video').style.display = 'none';
  el('monitor-source-label').textContent = 'Ring snapshot (latest recording) · 2026-10-23 10:00:00 UTC · recording';

  const calls = [];
  const requests = [];
  const pcs = [];
  const tracks = [];
  const listeners = {};
  let now = 0;
  const timers = [];
  let nextTimer = 1;
  const schedule = (fn, ms, repeat) => {
    const id = nextTimer++;
    timers.push({ id, at: now + ms, ms, fn, repeat });
    return id;
  };
  const cancel = id => {
    const i = timers.findIndex(t => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };
  const advance = async ms => {
    const end = now + ms;
    for (;;) {
      timers.sort((a, b) => a.at - b.at);
      const next = timers[0];
      if (!next || next.at > end) break;
      now = next.at;
      if (next.repeat) next.at += next.ms; else timers.shift();
      next.fn();
      await flush();
    }
    now = end;
    await flush();
  };

  class FakePeerConnection {
    constructor(config) {
      this.config = config;
      this.iceGatheringState = 'new';
      this.connectionState = 'new';
      this.closed = false;
      this.transceivers = [];
      this.localDescription = null;
      this.remoteDescription = null;
      this.statsQueue = [...stats];
      pcs.push(this);
      calls.push('new RTCPeerConnection');
    }
    addTransceiver(kind, init) {
      calls.push(`addTransceiver ${kind} ${init.direction}`);
      this.transceivers.push({ kind, direction: init.direction });
    }
    async createOffer() {
      calls.push(`createOffer handlers=${[this.ontrack, this.onicecandidate, this.onconnectionstatechange]
        .map(h => typeof h).join(',')}`);
      return { type: 'offer', sdp: 'v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\n' };
    }
    async setLocalDescription(description) {
      calls.push('setLocalDescription');
      this.localDescription = { ...description };
      this.iceGatheringState = 'gathering';
      if (ice === 'event') {
        setImmediate(() => {
          this.localDescription.sdp += 'a=candidate:1 1 udp 1 192.0.2.10 5000 typ host\r\n';
          this.iceGatheringState = 'complete';
          if (this.onicecandidate) this.onicecandidate({ candidate: null });
        });
      }
    }
    async setRemoteDescription(description) {
      calls.push(`setRemoteDescription ${description.type}`);
      this.remoteDescription = description;
    }
    getReceivers() {
      return this.receivers || [];
    }
    async getStats() {
      const next = this.statsQueue.length > 1 ? this.statsQueue.shift() : this.statsQueue[0];
      return new Map((next || []).map((s, i) => [`s${i}`, s]));
    }
    close() {
      this.closed = true;
      calls.push('close');
    }
    // Test helpers
    deliverTrack() {
      const track = { kind: 'video', stopped: false, stop() { this.stopped = true; } };
      tracks.push(track);
      this.receivers = [{ track }];
      this.ontrack({ track, streams: [{ id: 'ring-stream', tracks: [track] }] });
    }
    setConnectionState(state) {
      this.connectionState = state;
      this.onconnectionstatechange();
    }
  }

  const fetch = async (url, options = {}) => {
    requests.push({ url, method: options.method || 'GET', keepalive: options.keepalive,
      headers: options.headers, body: options.body ? JSON.parse(options.body) : null });
    const route = url === '/api/ring/whep/session' ? (typeof post === 'function' ? await post() : post)
      : { status: 200, body: { closed: true } };
    return { ok: route.status >= 200 && route.status < 300, status: route.status, json: async () => route.body };
  };

  const hints = [];
  const context = vm.createContext({
    document: { getElementById: el },
    window: { addEventListener: (name, fn) => { listeners[name] = fn; } },
    fetch,
    RTCPeerConnection: FakePeerConnection,
    MediaStream: class { constructor(t) { this.tracks = t; } },
    renderRingErrorHint: reason => hints.push(reason),
    setTimeout: (fn, ms) => schedule(fn, ms, false),
    clearTimeout: cancel,
    setInterval: (fn, ms) => schedule(fn, ms, true),
    clearInterval: cancel,
    console,
  });
  vm.runInContext(SOURCE, context);
  const run = code => vm.runInContext(code, context);
  return { run, el, calls, requests, pcs, tracks, listeners, advance, hints, timers };
}

async function startAndConnect(page) {
  const started = page.run('startRingLiveView()');
  await flush(); await flush(); await flush();
  await started;
  page.pcs[0].deliverTrack();
  return page.pcs[0];
}

const deletes = page => page.requests.filter(r => r.method === 'DELETE');

test('handlers are attached before the offer, video is recvonly and audio is off', async () => {
  const page = loadLiveView();
  const pc = await startAndConnect(page);

  assert.deepEqual(JSON.parse(JSON.stringify(pc.config)), { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  assert.deepEqual(page.calls.slice(0, 5), [
    'new RTCPeerConnection',
    'addTransceiver video recvonly',
    'createOffer handlers=function,function,function',
    'setLocalDescription',
    'setRemoteDescription answer',
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(pc.transceivers)), [{ kind: 'video', direction: 'recvonly' }]);
});

test('the offer is posted to our server only after ICE gathering completes', async () => {
  const page = loadLiveView();
  const pc = await startAndConnect(page);

  const [post] = page.requests;
  assert.equal(post.url, '/api/ring/whep/session');
  assert.equal(post.method, 'POST');
  assert.equal(post.headers['Content-Type'], 'application/json');
  assert.deepEqual(Object.keys(post.body), ['sdp']);
  assert.match(post.body.sdp, /a=candidate:1 1 udp/);   // gathered candidates are in the offer
  assert.deepEqual({ ...pc.remoteDescription }, { type: 'answer', sdp: ANSWER });
});

test('without ICE completion the offer is sent after the 4 s cap', async () => {
  const page = loadLiveView({ ice: 'never' });
  const started = page.run('startRingLiveView()');
  await flush();

  await page.advance(3999);
  assert.equal(page.requests.length, 0);
  await page.advance(1);
  await started;
  assert.equal(page.requests.length, 1);
  assert.doesNotMatch(page.requests[0].body.sdp, /a=candidate/);
});

test('the video replaces the snapshot and the label shows the start time', async () => {
  const page = loadLiveView();
  await startAndConnect(page);

  assert.equal(page.el('ring-live-video').style.display, '');
  assert.equal(page.el('ring-live-video').srcObject.id, 'ring-stream');
  assert.equal(page.el('partner-snapshot').style.display, 'none');
  assert.match(page.el('monitor-source-label').textContent, /^Ring live view · started \d{2}:\d{2}:\d{2}$/);
  assert.match(page.el('ring-live-view-status').textContent, /ends automatically within 30–60 s/);
  assert.equal(page.el('btn-ring-live-stop').style.display, '');
});

for (const [name, end] of [
  ['Stop', page => page.run("stopRingLiveView('stopped')")],
  ['connection failed', page => page.pcs[0].setConnectionState('failed')],
  ['connection disconnected', page => page.pcs[0].setConnectionState('disconnected')],
  ['page unload', page => page.listeners.beforeunload()],
  ['end of the session', page => page.advance(31000)],
]) {
  test(`${name}: full teardown and exactly one DELETE`, async () => {
    const page = loadLiveView();
    const pc = await startAndConnect(page);

    await end(page);
    page.run("stopRingLiveView('stopped')");   // a second exit path does nothing
    if (pc.onconnectionstatechange) pc.setConnectionState('closed');
    await flush();

    assert.equal(pc.closed, true);
    assert.equal(page.tracks.every(t => t.stopped), true);
    const del = deletes(page);
    assert.equal(del.length, 1);
    assert.equal(del[0].url, `/api/ring/whep/session/${SESSION.session_id}`);
    assert.equal(del[0].keepalive, name === 'page unload');
    assert.equal(page.el('ring-live-video').style.display, 'none');
    assert.equal(page.el('ring-live-video').srcObject, null);
    assert.equal(page.el('partner-snapshot').style.display, '');
    assert.match(page.el('monitor-source-label').textContent, /^Ring snapshot \(latest recording\)/);
    assert.equal(page.el('btn-ring-live').disabled, false);
    assert.equal(page.el('btn-ring-live-stop').style.display, 'none');
    assert.equal(page.timers.length, 0);   // no countdown or stats timer left
  });
}

test('a server error shows the reason, restores the monitor and needs no DELETE', async () => {
  const page = loadLiveView({ post: { status: 503, body: { detail: {
    reason: 'device_offline_503', message: 'Ring says the device is offline or busy (HTTP 503).' } } } });
  const started = page.run('startRingLiveView()');
  await flush(); await flush();
  await started;

  assert.equal(page.el('ring-live-view-status').textContent,
    'Ring live view is not available: Ring says the device is offline or busy (HTTP 503). (device_offline_503)');
  assert.deepEqual(page.hints, ['device_offline_503']);
  assert.equal(page.pcs[0].closed, true);
  assert.equal(deletes(page).length, 0);
  assert.equal(page.el('partner-snapshot').style.display, '');
  assert.equal(page.el('btn-ring-live').disabled, false);
});

test('one attempt per click: no automatic reconnection after a failure', async () => {
  const page = loadLiveView();
  await startAndConnect(page);
  page.pcs[0].setConnectionState('failed');
  await page.advance(120000);

  assert.equal(page.pcs.length, 1);
  assert.equal(page.requests.filter(r => r.method === 'POST').length, 1);
});

test('Stop while Ring is answering closes the session the server opened', async () => {
  let release;
  const page = loadLiveView({ post: () => new Promise(resolve => { release = resolve; }) });
  const started = page.run('startRingLiveView()');
  await flush(); await flush();
  page.run("stopRingLiveView('stopped')");
  release({ status: 200, body: SESSION });
  await started;
  await flush();

  assert.equal(deletes(page).length, 1);
  assert.equal(page.pcs[0].remoteDescription, null);
});

test('a second click while running does not open another session', async () => {
  const page = loadLiveView();
  await startAndConnect(page);
  await page.run('startRingLiveView()');

  assert.equal(page.pcs.length, 1);
});

test('the browser never calls Ring directly', async () => {
  const page = loadLiveView();
  await startAndConnect(page);
  page.run("stopRingLiveView('stopped')");

  assert.ok(page.requests.length >= 2);
  for (const r of page.requests) {
    assert.ok(r.url.startsWith('/api/ring/whep/session'), r.url);
    assert.doesNotMatch(r.url, /amazonvision|ring\.com/);
  }
  assert.doesNotMatch(SOURCE, /amazonvision\.com/);
});
