/**
 * Ring Hardware Digital Twin Engine & Web Audio Chime Synthesizer
 * Manages physical doorbell tactile interaction, synthesized chime tone,
 * and physical hardware fault injection.
 */

let audioCtx = null;

function getAudioContext() {
  if (!audioCtx) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) audioCtx = new AudioContextClass();
  }
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume();
  }
  return audioCtx;
}

function playRingChime() {
  try {
    const ctx = getAudioContext();
    if (!ctx) return;
    const now = ctx.currentTime;

    // Tone 1: High Bell (G#5 ~830.6 Hz)
    const osc1 = ctx.createOscillator();
    const gain1 = ctx.createGain();
    osc1.type = 'sine';
    osc1.frequency.setValueAtTime(830.6, now);
    gain1.gain.setValueAtTime(0.35, now);
    gain1.gain.exponentialRampToValueAtTime(0.0001, now + 0.85);
    osc1.connect(gain1);
    gain1.connect(ctx.destination);
    osc1.start(now);
    osc1.stop(now + 0.85);

    // High metallic harmonic
    const osc1h = ctx.createOscillator();
    const gain1h = ctx.createGain();
    osc1h.type = 'sine';
    osc1h.frequency.setValueAtTime(1661.2, now);
    gain1h.gain.setValueAtTime(0.08, now);
    gain1h.gain.exponentialRampToValueAtTime(0.0001, now + 0.6);
    osc1h.connect(gain1h);
    gain1h.connect(ctx.destination);
    osc1h.start(now);
    osc1h.stop(now + 0.6);

    // Tone 2: Low Bell (E5 ~659.25 Hz after 340ms)
    const t2 = now + 0.34;
    const osc2 = ctx.createOscillator();
    const gain2 = ctx.createGain();
    osc2.type = 'sine';
    osc2.frequency.setValueAtTime(659.25, t2);
    gain2.gain.setValueAtTime(0.38, t2);
    gain2.gain.exponentialRampToValueAtTime(0.0001, t2 + 1.4);
    osc2.connect(gain2);
    gain2.connect(ctx.destination);
    osc2.start(t2);
    osc2.stop(t2 + 1.4);

    // Harmonic bell body
    const osc2h = ctx.createOscillator();
    const gain2h = ctx.createGain();
    osc2h.type = 'sine';
    osc2h.frequency.setValueAtTime(1318.5, t2);
    gain2h.gain.setValueAtTime(0.09, t2);
    gain2h.gain.exponentialRampToValueAtTime(0.0001, t2 + 1.0);
    osc2h.connect(gain2h);
    gain2h.connect(ctx.destination);
    osc2h.start(t2);
    osc2h.stop(t2 + 1.0);
  } catch (e) {
    console.warn('Audio chime error:', e);
  }
}

let ringTimeout = null;
function pressDoorbellButton() {
  playRingChime();

  const btn = document.getElementById('ring-bell-btn');
  if (btn) {
    btn.classList.add('pressed');
    setTimeout(() => btn.classList.remove('pressed'), 250);
  }

  const halo = document.getElementById('ring-led-halo');
  if (halo) {
    halo.className = 'ring-led-halo led-ringing';
    if (ringTimeout) clearTimeout(ringTimeout);
    ringTimeout = setTimeout(() => {
      updateLedHaloState();
    }, 5500);
  }

  const banner = document.getElementById('chime-banner');
  if (banner) {
    banner.style.display = 'flex';
    setTimeout(() => { banner.style.display = 'none'; }, 5500);
  }

  showToast('🔔 DING! Simulated doorbell press (Digital Twin)', 'info');

  // Wake camera if it was on Standby (simulated 7 s doorbell session)
  if (typeof activateCamera === 'function') {
    activateCamera('ding', 7);
  }
}

function changePlateColor(color) {
  const plate = document.getElementById('ring-faceplate');
  if (!plate) return;
  plate.className = 'ring-faceplate plate-' + color;

  document.querySelectorAll('.plate-color-dot').forEach(d => d.classList.remove('active'));
  const dot = document.getElementById('dot-' + color);
  if (dot) dot.classList.add('active');

  const names = {
    nickel: 'Satin Nickel',
    bronze: 'Venetian Bronze',
    black:  'Midnight Black',
    white:  'Pearl White',
  };
  showToast(`🎨 Faceplate finish changed to: ${names[color] || color}`, 'info');
}

// Hardware Physical Fault Injection
function toggleDirtyLens() {
  state.hardwareFaults.dirtyLens = !state.hardwareFaults.dirtyLens;
  const active = state.hardwareFaults.dirtyLens;
  document.getElementById('lens-dirty-layer')?.classList.toggle('active', active);
  document.getElementById('hw-btn-dirty')?.classList.toggle('active-warn', active);
  updateCanvasEffects();
  refreshTelemetry();

  if (active) {
    showToast('💧 Dirty lens: Optical blur introduced on camera', 'warning');
  } else {
    showToast('🧽 Lens clean and clear', 'info');
  }
}

function toggleNightMode() {
  state.hardwareFaults.nightMode = !state.hardwareFaults.nightMode;
  const active = state.hardwareFaults.nightMode;
  ['ir-top', 'ir-right', 'ir-bottom', 'ir-left'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('active', active);
  });
  document.getElementById('hw-btn-night')?.classList.toggle('active', active);
  updateCanvasEffects();

  if (active) {
    showToast('🌙 Infrared LEDs active (B/W Night Mode)', 'info');
  } else {
    showToast('☀️ Infrared LEDs off (Day Mode)', 'info');
  }
}

function togglePinkFilter() {
  state.hardwareFaults.pinkFilter = !state.hardwareFaults.pinkFilter;
  const active = state.hardwareFaults.pinkFilter;
  document.getElementById('lens-pink-layer')?.classList.toggle('active', active);
  document.getElementById('hw-btn-pink')?.classList.toggle('active-warn', active);
  updateCanvasEffects();
  refreshTelemetry();

  if (active) {
    showToast('🌸 IR-cut filter solenoid stuck (Magenta discoloration physical fault)', 'warning');
  } else {
    showToast('✅ IR-cut filter repositioned correctly', 'info');
  }
}

function toggleFlickerLight() {
  state.hardwareFaults.flicker = !state.hardwareFaults.flicker;
  const active = state.hardwareFaults.flicker;
  document.getElementById('hw-btn-flicker')?.classList.toggle('active-warn', active);
  updateCanvasEffects();
  refreshTelemetry();

  if (active) {
    showToast('💡 External lighting strobe flicker (50/60 Hz)', 'warning');
  } else {
    showToast('💡 Normal lighting stabilized', 'info');
  }
}

function togglePowerCut() {
  state.hardwareFaults.powerCut = !state.hardwareFaults.powerCut;
  const active = state.hardwareFaults.powerCut;
  document.getElementById('hw-btn-power')?.classList.toggle('active-warn', active);

  const batSl   = document.getElementById('sl-battery');
  const batItem = document.getElementById('item-battery');
  const batLbl  = document.getElementById('lbl-battery');

  if (active) {
    state.transformer = 0;
    const sl = document.getElementById('sl-transformer');
    const lbl = document.getElementById('lbl-transformer');
    if (sl) sl.value = 0;
    if (lbl) lbl.textContent = '0 V (Cut)';

    // Cutting power in hardwired mode activates backup battery and enables slider
    if (batSl)   batSl.disabled = false;
    if (batItem) batItem.style.opacity = '1';
    if (batLbl)  batLbl.textContent = `${state.battery}%`;

    showToast('⚡ Transformer disconnected: 0V AC voltage (Power fault)', 'warning');
  } else {
    state.transformer = 18.0;
    const sl = document.getElementById('sl-transformer');
    const lbl = document.getElementById('lbl-transformer');
    if (sl) sl.value = 18.0;
    if (lbl) lbl.textContent = '18.0 V';

    // Restoring power returns battery to continuous trickle charge at 100%
    if (state.powerMode === 'wired') {
      state.battery = 100;
      if (batSl)   { batSl.value = 100; batSl.disabled = true; }
      if (batItem) batItem.style.opacity = '0.65';
      if (batLbl)  batLbl.textContent = '100% ⚡ (Continuous power)';
    }

    showToast('⚡ Transformer reconnected: 18.0V AC nominal', 'info');
  }

  updateCanvasEffects();
  updateDeviceLiveBadges();
  refreshTelemetry();
}

function updateLedHaloState() {
  const halo = document.getElementById('ring-led-halo');
  if (!halo) return;
  if (halo.classList.contains('led-ringing')) return;

  if (typeof showsMeasuredDataOnly === 'function' && showsMeasuredDataOnly()) {
    // Outside the Twin only Ring-reported reachability drives the halo; unknown stays neutral.
    const dh = (state.telemetry && state.telemetry.device_health) || {};
    halo.className = dh.online === true ? 'ring-led-halo led-idle'
                   : dh.online === false ? 'ring-led-halo led-offline'
                   : 'ring-led-halo';
    return;
  }

  // Twin halo follows the severity table: any non-GREEN metric shows the warning ring.
  const twin = typeof classifyTwinTelemetry === 'function' ? classifyTwinTelemetry() : null;
  if (state.hardwareFaults.powerCut || state.transformer === 0) {
    halo.className = 'ring-led-halo led-offline';
  } else if (twin && twin.level !== 'GREEN') {
    halo.className = 'ring-led-halo led-warning';
  } else if (state.hardwareFaults.pinkFilter || state.hardwareFaults.dirtyLens) {
    halo.className = 'ring-led-halo led-error';
  } else {
    halo.className = 'ring-led-halo led-idle';
  }
}
