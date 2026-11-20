/* Floodcast operational adapter for the FLUXOS WebGL viewer (floodcast-owned; the vendored
 * index.html template is left untouched). Injected into every exported viewer by hydraulics.py.
 *
 *  - controls start hidden; only basic controls remain (time, play, fullscreen, speed,
 *    water/particles/satellite-hillshade toggles, sun clock, forecast rain)
 *  - camera views: all except "Cinematic" and "Fly Path"
 *  - sun clock linked to the real (Lisbon) time of day of the simulation; untick = full daylight
 *  - rain overlay driven by the forecast rain at Alcácer do Sal (forcing.json)
 *  - simulation time posted to the parent page so its hydrograph cursor follows the animation
 */
(function () {
  "use strict";
  var forcing = null, rainOn = true, lastPost = 0, pageT = null;   // pageT: forecast second set by the page clock
  // Who owns the clock: the page embedding us (iframe) or the page that opened us (pop-up window).
  var CTRL = window.parent !== window ? window.parent : (window.opener || null);
  var lang = (location.hash.match(/lang=(pt|en|es)/) || [0, "pt"])[1];  // the site passes #lang=pt|en|es
  // View lock (page button "Vista bloqueada"): while locked, mouse/touch/keys on the 3-D canvas do not
  // move the camera (the page scrolls instead), but the viewer's own controls panel keeps working.
  var LOCKED = false;   // unlocked by default; the page's lock button can lock the camera
  ["mousedown", "wheel", "touchstart", "touchmove", "dblclick", "contextmenu"].forEach(function (ev) {
    window.addEventListener(ev, function (e) {
      if (LOCKED && e.target && e.target.tagName === "CANVAS") e.stopImmediatePropagation();
    }, { capture: true, passive: true });
  });
  ["keydown", "keyup"].forEach(function (ev) {
    window.addEventListener(ev, function (e) {
      var tag = (e.target && e.target.tagName) || "";
      if (LOCKED && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") e.stopImmediatePropagation();
    }, { capture: true });
  });
  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (d && d.type === "fluxos-lock" && (!CTRL || ev.source === CTRL)) LOCKED = !!d.locked;
  });

  function txt(ptS, enS, esS) { return lang === "en" ? enS : (lang === "es" ? (esS || enS) : ptS); }
  function hide(el) { if (el) el.style.display = "none"; }
  function rowOf(id) { var e = document.getElementById(id); return e ? e.closest(".ctrl-row") : null; }

  function lisbonDayFraction(iso) {
    try {
      var p = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Lisbon", hour: "2-digit", minute: "2-digit", hour12: false })
        .formatToParts(new Date(iso));
      var h = 0, m = 0;
      p.forEach(function (x) { if (x.type === "hour") h = +x.value % 24; if (x.type === "minute") m = +x.value; });
      return (h + m / 60) / 24;
    } catch (e) { return 0.5; }
  }

  function rainAt(t) {  // forecast rain intensity (mm/h) at simulation second t
    if (!forcing || !forcing.rain || !forcing.rain.length) return 0;
    var r = forcing.rain, i = 0;
    while (i < r.length - 1 && r[i + 1].t_s <= t) i++;
    return t >= r[0].t_s ? r[i].mm_h : 0;
  }

  // template overlays not used in the forecast view: FPS / key-help box and the flow-speed colour bars
  (function () {
    var st = document.createElement("style");
    st.textContent = "#info,#colorbar-wrap,#conc-colorbar-wrap{display:none!important}";
    (document.head || document.documentElement).appendChild(st);
  })();

  function trimControls() {
    var panel = document.getElementById("controls"), tab = document.getElementById("controls-tab");
    if (panel && tab) { panel.dataset.collapsed = "true"; panel.style.transform = ""; tab.hidden = false; }
    // "sl-speed": playback speed is set once for the whole page (charts, maps and this view)
    ["sl-trail", "sl-exag", "sl-foam", "sl-turbidity", "sl-suntime", "sl-rain", "sl-speed"].forEach(function (id) { hide(rowOf(id)); });
    ["btn-reset", "btn-rec", "btn-particles"].forEach(function (id) { hide(document.getElementById(id)); });
    hide(rowOf("sl-particles"));               // no particles in the operational view
    viewer.showParticles = false;
    var ip = document.getElementById("i-particles"); if (ip && ip.parentElement) hide(ip.parentElement);
    // views: drop "Cinematic" and "Fly Path" (both buttons share id="cam-fly" in the template)
    Array.prototype.forEach.call(document.querySelectorAll("#controls button"), function (b) {
      var t = (b.textContent || "").toLowerCase();
      if (t.indexOf("cinematic") >= 0 || t.indexOf("fly path") >= 0) b.remove();
    });
    var flyPanel = document.getElementById("fly-list");
    if (flyPanel) hide(flyPanel.closest("div[id], section, aside") || flyPanel.parentElement);
    var v3d = document.getElementById("cam-3d");
    if (v3d) v3d.click();
    // default view = the "3D" preset. The template sets its own cinematic camera once the data has
    // loaded, so click the preset again after that (twice, to be safe on slow machines).
    function view3d() {   // set the "3D" preset camera directly (no animation) and mark its button
      try {
        var c = viewer.camera;
        c._anim = null; c.theta = -0.5; c.phi = 0.65; c.distance = 1.5;
        if (c.target && c.target.length === 3) { c.target[0] = 0; c.target[1] = 0; c.target[2] = 0; }
      } catch (e) {}
      Array.prototype.forEach.call(document.querySelectorAll('[id^="cam-"]'), function (b) { b.classList.toggle("active", b.id === "cam-3d"); });
    }
    setTimeout(view3d, 1200); setTimeout(view3d, 3000);
    var title = document.getElementById("controls-title");
    if (title) title.textContent = txt("Controlos", "Controls", "Controles");
    var tabLabel = tab && tab.querySelector("span");
    if (tabLabel) tabLabel.textContent = "☰ " + txt("Controlos", "Controls", "Controles");
  }

  // the page's sun button: {type:"fluxos-sun", follow:true|false}
  function listenSun(link) {
    window.addEventListener("message", function (ev) {
      var d = ev.data;
      if (!d || d.type !== "fluxos-sun" || (CTRL && ev.source !== CTRL)) return;
      var ck = document.getElementById("ck-sun-follow"), lbl = document.getElementById("v-sun-follow");
      if (d.follow) { if (ck) ck.checked = true; link(); }
      else {
        if (ck) ck.checked = false;
        viewer.sunFollowSim = false; viewer.sunTime = 0.5; viewer._sunUiLast = undefined;
        if (lbl) lbl.textContent = txt("dia claro", "full daylight", "pleno día");
      }
    });
  }

  function sunClock() {
    var ck = document.getElementById("ck-sun-follow"), lbl = document.getElementById("v-sun-follow");
    var row = ck && ck.closest(".ctrl-row");
    if (row) { var l = row.querySelector("label"); if (l) l.textContent = txt("Hora da previsão", "Forecast time", "Hora de la previsión"); }
    var base = forcing && forcing.sim_start_utc ? lisbonDayFraction(forcing.sim_start_utc) : null;
    function link() {
      if (base == null) return;
      viewer.sunTimeBase = base; viewer.sunFollowSim = true; viewer._sunUiLast = undefined;
      if (lbl) lbl.textContent = txt("sol segue a hora da previsão", "sun follows the forecast time", "el sol sigue la hora de la previsión");
    }
    if (ck) {
      ck.checked = base != null;
      ck.addEventListener("change", function () {  // runs after the template's own handler
        if (ck.checked) link();
        else { viewer.sunFollowSim = false; viewer.sunTime = 0.5; if (lbl) lbl.textContent = txt("dia claro", "full daylight", "pleno día"); }
      });
    }
    // default: full daylight; the page's "local light conditions" switch turns the real sun on
    if (ck) ck.checked = false;
    viewer.sunFollowSim = false; viewer.sunTime = 0.5;
    if (lbl) lbl.textContent = txt("dia claro", "full daylight", "pleno día");
    listenSun(link);
    hide(row);   // the page's "local light conditions" button controls this; no row in the panel
  }

  // rain shown only as forecast (forcing.json); the page's "Forecast rain" button switches it on/off
  function rainToggle() {
    if (!forcing || !forcing.rain || !forcing.rain.length) rainOn = false;
    window.addEventListener("message", function (ev) {
      var d = ev.data;
      if (!d || d.type !== "fluxos-rain" || (CTRL && ev.source !== CTRL)) return;
      rainOn = !!d.on && !!(forcing && forcing.rain && forcing.rain.length);
      if (!rainOn) viewer.rainAmount = 0;
    });
  }

  function tick(now) {
    var t = viewer._simSeconds ? viewer._simSeconds() : 0;
    if (rainOn) {
      var mm = rainAt(pageT != null ? pageT : t);   // the page's time, so rain shows even after the flood run ends
      // any measurable rain is visible; 8 mm/h ~ heavy downpour
      viewer.rainAmount = mm >= 0.1 ? Math.min(1, Math.max(0.25, Math.sqrt(mm / 8))) : 0;
    }
    if (CTRL && now - lastPost > 100) {
      lastPost = now;
      try { CTRL.postMessage({ type: "fluxos-time", t: t, playing: !!viewer.playing }, "*"); } catch (e) {}
    }
    requestAnimationFrame(tick);
  }

  // The page owns the clock: it posts the simulation second, the viewer shows that moment.
  function followPageClock() {
    var m = viewer.meta, n = m.n_timesteps;
    viewer.playing = false;
    hide(document.getElementById("btn-play"));
    // Keep up to ~160 frames on the GPU and fill the cache gradually (3 at a time): decoding every
    // frame at once (289-480 for 72-120 h runs) exhausted GPU memory and crashed the tab.
    var CAP = Math.min(n + 2, 160);
    viewer.frameCacheMax = Math.max(viewer.frameCacheMax || 30, CAP);
    var next = 0, busy = 0;
    function pump() {
      while (busy < 3 && next < Math.min(n, CAP)) {
        var k = next++; busy++;
        Promise.resolve(viewer.loadVelFrame(k)).catch(function () {}).then(function () { busy--; setTimeout(pump, 30); });
      }
    }
    pump();
    window.addEventListener("message", function (ev) {
      var d = ev.data;
      if (!d || d.type !== "fluxos-set-time" || ev.source !== CTRL) return;
      pageT = d.t;
      var ts = m.times, t = d.t, i = 0;
      while (i < n - 2 && ts[i + 1] <= t) i++;
      var span = (ts[i + 1] - ts[i]) || 1, frac = Math.max(0, Math.min(1, (t - ts[i]) / span));
      viewer.playing = false;
      if (viewer.frameCache.has(i)) { viewer.currentFrame = i; viewer.timeFrac = frac; }
      else { viewer.loadVelFrame(i); if (i + 1 < n) viewer.loadVelFrame(i + 1); }
    });
  }

  // Time labels show the real (Lisbon) day and hour of the simulated moment, not elapsed hours.
  function realClock() {
    if (!forcing || !forcing.sim_start_utc) return;
    var t0 = Date.parse(forcing.sim_start_utc);
    var loc = { pt: "pt-PT", en: "en-GB", es: "es-ES" }[lang] || "pt-PT";
    var fmt;
    try {
      fmt = new Intl.DateTimeFormat(loc, { timeZone: "Europe/Lisbon", weekday: "short", day: "2-digit",
                                            month: "short", hour: "2-digit", minute: "2-digit" });
    } catch (e) { fmt = null; }
    window.fmtTime = function (tSec) {
      var d = new Date(t0 + (tSec || 0) * 1000);
      return fmt ? fmt.format(d) : d.toISOString().substr(0, 16).replace("T", " ") + " UTC";
    };
    var vt = document.getElementById("v-time"); if (vt) vt.style.minWidth = "11em";
  }

  // slow automatic rotation around the area; any click / touch / wheel on the 3-D view stops it
  function autoRotate() {
    var on = true, last = 0;
    function stop(e) { if (e.target && e.target.tagName === "CANVAS") on = false; }
    ["mousedown", "touchstart", "wheel"].forEach(function (ev) { window.addEventListener(ev, stop, { capture: true, passive: true }); });
    window.addEventListener("message", function (ev) {
      var d = ev.data; if (d && d.type === "fluxos-rotate") on = !!d.on;
    });
    function step(now) {
      var dt = last ? Math.min(100, now - last) : 0; last = now;
      if (on && !LOCKED) { try { viewer.camera._anim = null; viewer.camera.theta += dt * 0.00006; } catch (e) {} }   // ~1 turn / 105 s
      requestAnimationFrame(step);
    }
    setTimeout(function () { requestAnimationFrame(step); }, 3500);   // after the default 3D view is set
  }

  // Layer from the page's shared bar. "depth" = the live water (template default). Arrival, hazard,
  // duration and probability are static maps: drape them over the satellite ground texture and hide
  // the water so the colours read clearly.
  var LAYER_IMG = { arrival: "../arrival.png", hazard: "../hazard.png", duration: "../duration.png",
                    likelihood: "../../likelihood.png" };
  var baseSat = null, baseUse = null, draped = false, layerTex = {};
  function img(src) {
    return new Promise(function (res, rej) { var i = new Image(); i.onload = function () { res(i); }; i.onerror = rej; i.src = src; });
  }
  function setLayer(k) {
    var gl = viewer.gl;
    if (!gl) return;
    if (!LAYER_IMG[k]) {   // depth: back to the normal view - only if we draped something before
      if (draped) { viewer.satelliteTex = baseSat; viewer.useSatellite = baseUse; draped = false; }
      viewer.showWater = true;
      return;
    }
    function apply(tex) {
      if (!draped) { baseSat = viewer.satelliteTex; baseUse = viewer.useSatellite; draped = true; }   // remember the real ground
      viewer.satelliteTex = tex; viewer.useSatellite = true; viewer.showWater = false;
    }
    if (layerTex[k]) { apply(layerTex[k]); return; }
    Promise.all([img("data/satellite.jpg" + (viewer._v || "")).catch(function () { return img("data/hillshade.png"); }),
                 img(LAYER_IMG[k])]).then(function (r) {
      var c = document.createElement("canvas"); c.width = r[0].naturalWidth; c.height = r[0].naturalHeight;
      var x = c.getContext("2d");
      x.drawImage(r[0], 0, 0, c.width, c.height);
      x.globalAlpha = 0.85; x.imageSmoothingEnabled = false;
      x.drawImage(r[1], 0, 0, c.width, c.height);
      layerTex[k] = createTex(gl, gl.LINEAR, x.getImageData(0, 0, c.width, c.height), 0, 0);
      apply(layerTex[k]);
    }).catch(function () {});
  }
  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (d && d.type === "fluxos-layer" && (!CTRL || ev.source === CTRL)) setLayer(d.layer);
  });

  // the page's vertical-exaggeration slider: {type:"fluxos-exag", factor} x the template's default
  var EXAG0 = null, exagTimer = null;
  function exagControl() {
    EXAG0 = viewer.exaggeration || 0.1;
    window.addEventListener("message", function (ev) {
      var d = ev.data;
      if (!d || d.type !== "fluxos-exag" || (CTRL && ev.source !== CTRL)) return;
      var f = Math.max(0.25, Math.min(10, +d.factor || 1));
      viewer.exaggeration = EXAG0 * f;
      clearTimeout(exagTimer);   // the mesh rebuild is heavy: once the slider settles
      exagTimer = setTimeout(function () { try { viewer.rebuildTerrainMesh(); } catch (e) {} }, 60);
    });
  }

  function start() {
    // bluer water: mostly the clear palette with a little river sediment (template default 0.55)
    viewer.turbidity = 0.2;
    var st = document.getElementById("sl-turbidity"); if (st) st.value = "20";
    trimControls();
    realClock();
    if (CTRL) followPageClock();
    sunClock();
    rainToggle();
    exagControl();
    autoRotate();
    requestAnimationFrame(tick);
  }

  var tries = 0;
  var wait = setInterval(function () {
    tries++;
    if (typeof viewer !== "undefined" && viewer && viewer.meta) {
      clearInterval(wait);
      fetch("forcing.json").then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; })
        .then(function (f) { forcing = f; start(); });
    } else if (tries > 600) clearInterval(wait);
  }, 100);
})();
