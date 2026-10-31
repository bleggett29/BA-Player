// BA Player app scaler - Copyright (C) 2026 Brian Scott
// SPDX-License-Identifier: GPL-3.0-or-later
// ba_probe app scaler: broadcaster apps are TV pages built for a 1920x1080
// screen. When one is opened from ba_probe (its address carries ?wsURL=), zoom
// the page so the whole 1920x1080 fits the window, centered, and follow resizes.
// Alt+Z turns it off/on for the tab. Reapplied whenever the app resets the page. Pages without wsURL are never touched.
(() => {
  const KEY = 'ba_probe_scale';
  let on;
  try {
    if (/[?&]wsURL=/.test(location.search + location.hash)) sessionStorage.setItem(KEY, '1');
    on = sessionStorage.getItem(KEY) === '1';      // stays on if the app changes its own address
  } catch (e) {
    on = /[?&]wsURL=/.test(location.href);
  }
  if (!on) return;
  const W = 1920, H = 1080;
  const root = document.documentElement;
  const style = document.createElement('style');
  style.textContent = 'html{background:#000!important;overflow:hidden!important}' +
                      'body{overflow:hidden!important;margin:0}';
  function fit() {
    if (!on) {
      root.style.removeProperty('zoom'); root.style.removeProperty('margin');
      root.style.removeProperty('width'); root.style.removeProperty('height');
      style.remove();
      return;
    }
    if (!style.isConnected) (document.head || root).appendChild(style);
    const k = Math.min(innerWidth / W, innerHeight / H);
    root.style.setProperty('width', W + 'px', 'important');
    root.style.setProperty('height', H + 'px', 'important');
    root.style.setProperty('zoom', String(k), 'important');
    // margins are in the zoomed element's own pixels
    const mx = Math.max(0, (innerWidth - W * k) / 2 / k), my = Math.max(0, (innerHeight - H * k) / 2 / k);
    root.style.setProperty('margin', my + 'px ' + mx + 'px', 'important');
  }
  addEventListener('resize', fit);
  document.addEventListener('fullscreenchange', fit);
  document.addEventListener('DOMContentLoaded', fit);
  addEventListener('load', fit);
  // Apps rebuild their page or rewrite <html>/<body> styles after they start,
  // which undid the scaling until a reload. Watch for that and put it back.
  let pending = false;
  const again = () => { if (on && !pending) { pending = true; requestAnimationFrame(() => { pending = false; fit(); }); } };
  const mo = new MutationObserver(again);
  mo.observe(root, { attributes: true, attributeFilter: ['style', 'class'], childList: true });
  const watchBody = () => { if (document.body) mo.observe(document.body, { attributes: true, attributeFilter: ['style', 'class'] }); };
  document.addEventListener('DOMContentLoaded', watchBody);
  watchBody();
  // ...and check now and then anyway (cheap): often for the first 20 s, then every 2 s
  const t0 = Date.now();
  (function tick() {
    if (on) {
      const k = Math.min(innerWidth / W, innerHeight / H);
      if (Math.abs(parseFloat(getComputedStyle(root).zoom || '1') - k) > 0.001 || !style.isConnected) fit();
    }
    setTimeout(tick, Date.now() - t0 < 20000 ? 250 : 2000);
  })();
  addEventListener('keydown', e => {
    if (e.altKey && (e.key === 'z' || e.key === 'Z')) {
      on = !on;
      try { sessionStorage.setItem(KEY, on ? '1' : '0'); } catch (x) {}
      fit();
      e.preventDefault();
    }
  }, true);
  fit();
})();
