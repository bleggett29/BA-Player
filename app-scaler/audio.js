// BA Player app scaler - Copyright (C) 2026 Brian Scott
// SPDX-License-Identifier: GPL-3.0-or-later
// ba_probe app scaler - volume/mute for broadcaster apps launched from ba_probe.
// Runs in the page's own JavaScript world so it can reach every sound source:
// <audio>/<video> (also ones never added to the page) and Web Audio (all output
// goes through one gain per AudioContext). Players inside frames get the setting
// by message. Only acts in tabs opened as an app (?wsURL=, as the scaler).
// Keys: Alt+M mute, Alt+Up / Alt+Down volume. Remembered per app site.
// In frames the hooks stay inert (volume 1) unless the app tab sends a setting.
(() => {
  const isTop = window === window.top;
  const MSG = '__baProbeAudio';
  let vol = 1, muted = false, active = false;
  const media = new Set(), gains = new Set();

  function applyTo(m) {
    try {
      if (Math.abs(m.volume - vol) > 0.001) m.volume = vol;
      if (m.muted !== muted) m.muted = muted;
    } catch (e) {}
  }
  function apply() {
    if (!active) return;
    document.querySelectorAll('audio,video').forEach(m => media.add(m));
    for (const m of media) applyTo(m);
    for (const g of gains) { try { g.gain.value = muted ? 0 : vol; } catch (e) {} }
    for (let i = 0; i < window.frames.length; i++) {
      try { window.frames[i].postMessage({ [MSG]: { vol, muted } }, '*'); } catch (e) {}
    }
  }
  // media elements: the play event doesn't bubble, so listen in the capture phase
  document.addEventListener('play', e => { if (active && e.target instanceof HTMLMediaElement) { media.add(e.target); applyTo(e.target); } }, true);
  document.addEventListener('volumechange', e => { if (active && e.target instanceof HTMLMediaElement) applyTo(e.target); }, true);

  // top: only app tabs (?wsURL=) are touched at all
  let on = !isTop;
  if (isTop) {
    try {
      on = /[?&]wsURL=/.test(location.search + location.hash) || sessionStorage.getItem('ba_probe_scale') !== null;
    } catch (e) {
      on = /[?&]wsURL=/.test(location.href);
    }
    if (!on) return;
  }
  // Hooks go in every frame (games such as Gameloop play from a frame through
  // Web Audio). They change nothing until the app tab sends a volume.
  // <audio>/<video>, including new Audio() objects that never join the page
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () { media.add(this); if (active) applyTo(this); return play.apply(this, arguments); };

  // Web Audio: route whatever goes to the speakers through one gain per context
  const masters = new WeakMap();
  // works for nodes from other frames too (instanceof doesn't cross frames)
  const isDest = d => !!d && Object.prototype.toString.call(d) === '[object AudioDestinationNode]';
  const connect = AudioNode.prototype.connect, disconnect = AudioNode.prototype.disconnect;
  function master(dest) {
    let g = masters.get(dest);
    if (!g) {
      g = dest.context.createGain();
      g.gain.value = active ? (muted ? 0 : vol) : 1;
      connect.call(g, dest);
      masters.set(dest, g);
      gains.add(g);
    }
    return g;
  }
  AudioNode.prototype.connect = function (dest, ...rest) {
    if (isDest(dest)) dest = master(dest);
    return connect.call(this, dest, ...rest);
  };
  AudioNode.prototype.disconnect = function (dest, ...rest) {
    if (isDest(dest) && masters.has(dest)) dest = masters.get(dest);
    return disconnect.call(this, dest, ...rest);
  };

  if (!isTop) {
    // inside a frame: wait for the app tab to send the setting
    addEventListener('message', e => {
      const d = e.data && e.data[MSG];
      if (!d) return;
      vol = d.vol; muted = d.muted; active = true;
      apply();
    });
    return;
  }

  if (!on) return;
  active = true;
  const SKEY = 'ba_probe_audio';
  try { const s = JSON.parse(localStorage.getItem(SKEY) || '{}'); if (typeof s.vol === 'number') vol = s.vol; muted = !!s.muted; } catch (e) {}

  // the control
  let box, btn, slider, pct, hideT;
  function save() { try { localStorage.setItem(SKEY, JSON.stringify({ vol, muted })); } catch (e) {} }
  function set(v, m) {
    vol = Math.min(1, Math.max(0, Math.round(v * 100) / 100)); muted = m;
    save(); apply(); draw(); show();
  }
  function draw() {
    if (!box) return;
    btn.textContent = muted || vol === 0 ? '\u{1F507}' : vol < 0.5 ? '\u{1F509}' : '\u{1F50A}';
    slider.value = Math.round(vol * 100);
    pct.textContent = muted ? 'muted' : Math.round(vol * 100) + '%';
  }
  function show() {
    if (!box) return;
    box.style.opacity = '1';
    clearTimeout(hideT);
    hideT = setTimeout(() => { if (!box.matches(':hover')) box.style.opacity = '0'; }, 2500);
  }
  function build() {
    if (box || !document.body) return;
    box = document.createElement('div');
    box.setAttribute('style', 'position:fixed;top:10px;right:10px;z-index:2147483647;display:flex;align-items:center;gap:8px;' +
      'padding:6px 10px;border-radius:20px;background:rgba(0,0,0,.7);color:#fff;font:14px system-ui,sans-serif;' +
      'transition:opacity .4s;opacity:1;user-select:none');
    btn = document.createElement('button');
    btn.setAttribute('style', 'all:unset;cursor:pointer;font-size:18px;line-height:1');
    btn.title = 'Mute (Alt+M)';
    btn.onclick = e => { e.stopPropagation(); set(vol, !muted); };
    slider = document.createElement('input');
    slider.type = 'range'; slider.min = 0; slider.max = 100;
    slider.setAttribute('style', 'width:110px;cursor:pointer');
    slider.title = 'Volume (Alt+Up / Alt+Down)';
    slider.oninput = e => { e.stopPropagation(); set(slider.value / 100, false); };
    slider.onkeydown = e => e.stopPropagation();
    pct = document.createElement('span');
    pct.setAttribute('style', 'min-width:44px;font-variant-numeric:tabular-nums');
    box.append(btn, slider, pct);
    box.addEventListener('mouseenter', show);
    document.body.appendChild(box);
    draw(); show();
  }
  // keep the control readable at any app scale, and on top if the app rebuilds its page
  setInterval(() => {
    if (!box) return build();
    if (!box.isConnected && document.body) document.body.appendChild(box);
    const z = parseFloat(getComputedStyle(document.documentElement).zoom || '1') || 1;
    box.style.zoom = String(1 / z);
    apply();
  }, 1000);
  addEventListener('mousemove', show, true);
  addEventListener('keydown', e => {
    if (!e.altKey) return;
    if (e.key === 'm' || e.key === 'M') set(vol, !muted);
    else if (e.key === 'ArrowUp') set(vol + 0.1, false);
    else if (e.key === 'ArrowDown') set(vol - 0.1, muted);
    else return;
    e.preventDefault(); e.stopPropagation();
  }, true);
  document.addEventListener('DOMContentLoaded', () => { build(); apply(); });
})();
