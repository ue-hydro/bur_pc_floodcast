/* Floodcast public site: language toggle, staleness check, optional map. No cookies, no trackers. */
(function () {
  "use strict";
  var root = document.documentElement;
  var LOCALE = { pt: "pt-PT", en: "en-GB", es: "es-ES" };
  var FC = { subs: [], t0: null, t1: null, epoch: null, last: 0, LOOP_MS: 30000, speed: 1, started: false };  // 72 h in 30 s at 1x
  var SCN = { cur: "central", subs: [] };

  var langs = (root.getAttribute("data-langs") || "pt").split(",");
  var def = root.getAttribute("data-default-lang") || langs[0];
  var stored = null;
  try { stored = window.localStorage.getItem("floodcast-lang"); } catch (e) {}
  function setLang(l) {
    if (langs.indexOf(l) < 0) l = def;
    root.setAttribute("data-lang", l);
    root.setAttribute("lang", l);
    var b = document.querySelectorAll(".langtoggle button");
    for (var i = 0; i < b.length; i++) b[i].setAttribute("aria-pressed", b[i].getAttribute("data-set") === l ? "true" : "false");
    try { window.localStorage.setItem("floodcast-lang", l); } catch (e) {}
    localTimes(l);
    var fr = document.querySelector("#anim-card iframe");
    if (fr) setFrameLang(fr, l);
  }
  // Pass the language to the FLUXOS viewer through the iframe URL hash. Before the (lazy) iframe
  // has loaded, only rewrite its src attribute: touching contentWindow would navigate about:blank.
  function setFrameLang(fr, l) {
    if (fr.dataset.loaded === "1") { try { fr.contentWindow.location.hash = "lang=" + l; } catch (e) {} return; }
    var src = fr.getAttribute("src") || "";
    if (src) fr.setAttribute("src", src.split("#")[0] + "#lang=" + l);
  }
  function fmt(iso, l) {
    try {
      return new Date(iso).toLocaleString(LOCALE[l] || "en-GB",
        { timeZone: "Europe/Lisbon", day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch (e) { return ""; }
  }
  function localTimes(l) {
    var sh = document.querySelectorAll("time.short[datetime]");
    for (var q = 0; q < sh.length; q++) {
      try {
        sh[q].textContent = new Date(sh[q].getAttribute("datetime")).toLocaleString(LOCALE[l] || "pt-PT",
          { timeZone: "Europe/Lisbon", weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
      } catch (e) {}
    }
    var t = document.querySelectorAll("time[datetime]:not(.short)");
    for (var i = 0; i < t.length; i++) {
      var s = t[i].querySelector(".local");
      if (!s) { s = document.createElement("span"); s.className = "local"; t[i].appendChild(s); }
      var iso = t[i].getAttribute("datetime");
      var f = fmt(iso, l);
      var same = false;
      try {
        var o = { hour: "2-digit", minute: "2-digit", hour12: false };
        same = new Date(iso).toLocaleTimeString("en-GB", Object.assign({ timeZone: "Europe/Lisbon" }, o)) ===
               new Date(iso).toLocaleTimeString("en-GB", Object.assign({ timeZone: "UTC" }, o));
      } catch (e) {}
      s.textContent = f && !same ? " (" + f + (l === "en" ? ", Lisbon time" : ", hora de Lisboa") + ")" : "";
    }
  }
  var initial = stored && langs.indexOf(stored) >= 0 ? stored : (langs.indexOf("en") >= 0 && !stored && /^en/i.test(navigator.language || "") ? "en" : def);
  setLang(initial);
  var btns = document.querySelectorAll(".langtoggle button");
  for (var i = 0; i < btns.length; i++) btns[i].addEventListener("click", function () { setLang(this.getAttribute("data-set")); });

  // animation speed for every chart, map and the 3-D view (one shared clock); remembered per visitor
  (function () {
    var btns = document.querySelectorAll(".fc-speed button[data-speed]");
    if (!btns.length) return;
    function pick(v) {
      FC.speed = parseFloat(v) || 0;
      Array.prototype.forEach.call(btns, function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-speed") === v ? "true" : "false"); });
    }
    var v0 = "1";
    try { var st = localStorage.getItem("fc-speed"); if (st !== null && document.querySelector('.fc-speed button[data-speed="' + st + '"]')) v0 = st; } catch (e) {}
    pick(v0);
    Array.prototype.forEach.call(btns, function (b) {
      b.addEventListener("click", function () {
        var v = b.getAttribute("data-speed"); pick(v);
        try { localStorage.setItem("fc-speed", v); } catch (e) {}
      });
    });
  })();

  // light/dark theme: light by default, the visitor's choice is remembered
  var tbtn = document.querySelector(".themetoggle");
  if (tbtn) tbtn.addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") !== "dark";
    if (dark) root.setAttribute("data-theme", "dark"); else root.removeAttribute("data-theme");
    try { localStorage.setItem("floodcast-theme", dark ? "dark" : "light"); } catch (e) {}
  });

  // ---- live progress of the cycle being computed (polls api/progress.json) ----------------
  var progEl = document.getElementById("progress");
  if (progEl) {
    var PT = {
      pt: { h: "Cálculo em curso", precip: "Avaliação da precipitação", hydrology: "Modelo hidrológico (SUMMA–mizuRoute)",
            hydraulics: "Modelo hidrodinâmico (FLUXOS)", started: "início", eta: "previsto", done: "concluído",
            skipped: "não necessário", failed: "falhou", pending: "a aguardar", results: "Resultados atualizados previstos às",
            cycle: "ciclo", next: "Próxima atualização prevista", last: "Último cálculo concluído" },
      en: { h: "Computation in progress", precip: "Precipitation evaluation", hydrology: "Hydrological model (SUMMA–mizuRoute)",
            hydraulics: "Hydrodynamic model (FLUXOS)", started: "started", eta: "expected", done: "done",
            skipped: "not needed", failed: "failed", pending: "waiting", results: "Updated results expected at",
            cycle: "cycle", next: "Next update expected", last: "Last computation finished" },
      es: { h: "Cálculo en curso", precip: "Evaluación de la precipitación", hydrology: "Modelo hidrológico (SUMMA–mizuRoute)",
            hydraulics: "Modelo hidrodinámico (FLUXOS)", started: "inicio", eta: "previsto", done: "completado",
            skipped: "no necesario", failed: "falló", pending: "en espera", results: "Resultados actualizados previstos a las",
            cycle: "ciclo", next: "Próxima actualización prevista", last: "Último cálculo terminado" }
    };
    function hm(iso) {
      if (!iso) return "–";
      try { return new Date(iso).toLocaleTimeString(LOCALE[root.getAttribute("data-lang")] || "pt-PT", { timeZone: "Europe/Lisbon", hour: "2-digit", minute: "2-digit" }); }
      catch (e) { return iso.substr(11, 5); }
    }
    var lastDoc = null;
    function renderProgress(d) {
      lastDoc = d;
      var t = PT[root.getAttribute("data-lang")] || PT.pt;
      if (!d || !d.stages) { progEl.hidden = true; return; }
      if (d.state !== "running") {
        progEl.hidden = !d.finished_utc;
        progEl.className = "progress idle";
        progEl.innerHTML = '<span>' + t.last + ': <b>' + hm(d.finished_utc) + '</b></span>' +
          (d.next_update_expected_utc ? '<span>' + t.next + ': <b>' + hm(d.next_update_expected_utc) + '</b></span>' : "");
        return;
      }
      progEl.hidden = false; progEl.className = "progress running";
      var rows = ["precip", "hydrology", "hydraulics"].map(function (k) {
        var st = d.stages[k] || { state: "pending", pct: 0 };
        var pct = st.state === "done" ? 100 : (st.pct || 0);
        var meta = st.state === "running"
          ? t.started + " " + hm(st.started_utc) + " · " + t.eta + " " + hm(st.eta_utc) + (st.detail ? " · " + st.detail : "")
          : (t[st.state] || st.state) + (st.finished_utc && st.state === "done" ? " " + hm(st.finished_utc) : "");
        return '<div class="pg-row pg-' + st.state + '"><span class="pg-name">' + t[k] + '</span>' +
          '<span class="pg-bar"><i style="width:' + pct + '%"></i></span><span class="pg-pct">' + pct + '%</span>' +
          '<span class="pg-meta">' + meta + '</span></div>';
      }).join("");
      progEl.innerHTML = '<div class="pg-head"><strong>' + t.h + '</strong> <span class="muted">' + t.cycle + " " +
        (d.cycle || "").substr(0, 13).replace("T", " ") + ' UTC</span>' +
        (d.results_expected_utc ? '<span class="pg-res">' + t.results + ' <b>' + hm(d.results_expected_utc) + '</b></span>' : "") +
        '</div>' + rows;
    }
    function pollProgress() {
      fetch(progEl.getAttribute("data-src") + "?t=" + Date.now(), { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; }).then(renderProgress).catch(function () {});
    }
    pollProgress(); setInterval(pollProgress, 8000);
    new MutationObserver(function () { renderProgress(lastDoc); }).observe(root, { attributes: true, attributeFilter: ["data-lang"] });
  }

  // ---- "now" reference line on every time chart, moving with the clock ------------------
  function nowLines() {
    var svgs = document.querySelectorAll("svg.chart[data-cursor]"), s = Date.now() / 1000;
    var word = { pt: "agora", en: "now", es: "ahora" }[root.getAttribute("data-lang")] || "agora";
    for (var i = 0; i < svgs.length; i++) {
      var svg = svgs[i], c; try { c = JSON.parse(svg.getAttribute("data-cursor")); } catch (e) { continue; }
      var n = c.t.length, x = null;
      if (s >= c.t[0] && s <= c.t[n - 1]) {
        for (var j = 1; j < n; j++) if (s <= c.t[j]) { var f = (s - c.t[j - 1]) / ((c.t[j] - c.t[j - 1]) || 1); x = c.x[j - 1] + f * (c.x[j] - c.x[j - 1]); break; }
      }
      var g = svg.querySelector("g.now");
      if (!g) {
        g = document.createElementNS("http://www.w3.org/2000/svg", "g"); g.setAttribute("class", "now");
        var ln = document.createElementNS("http://www.w3.org/2000/svg", "line");
        ln.setAttribute("y1", c.y0); ln.setAttribute("y2", c.y1);
        var tx = document.createElementNS("http://www.w3.org/2000/svg", "text");
        tx.setAttribute("y", Math.max(9, c.y0 - 3)); tx.setAttribute("text-anchor", "middle");  // above the plot area
        g.appendChild(ln); g.appendChild(tx); svg.appendChild(g);
      }
      g.style.display = x == null ? "none" : "";
      if (x != null) {
        g.firstChild.setAttribute("x1", x); g.firstChild.setAttribute("x2", x);
        g.lastChild.setAttribute("x", x); g.lastChild.textContent = word;
      }
    }
  }
  nowLines(); setInterval(nowLines, 30000);
  new MutationObserver(nowLines).observe(root, { attributes: true, attributeFilter: ["data-lang"] });

  var upd = document.body.getAttribute("data-updated");
  var stale = document.getElementById("stale");
  if (upd && stale) {
    var age = (Date.now() - Date.parse(upd)) / 36e5;
    if (age > 18) stale.classList.add("on");
  }

  // Highlight the jump-bar link of the section in view.
  var jl = document.querySelectorAll(".jumpbar a[data-target]");
  if (jl.length && window.IntersectionObserver) {
    var byId = {};
    for (var j = 0; j < jl.length; j++) byId[jl[j].getAttribute("data-target")] = jl[j];
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          for (var k in byId) byId[k].classList.remove("active");
          var a = byId[e.target.id] || byId[e.target.getAttribute("data-jump")]; if (a) { a.classList.add("active"); if (a.scrollIntoView && a.parentNode.scrollWidth > a.parentNode.clientWidth) a.parentNode.scrollLeft = a.offsetLeft - 40; }
        }
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    for (var id in byId) { var sec = document.getElementById(id); if (sec) io.observe(sec); }
    if (byId["flood-group"]) ["map-card", "anim-card"].forEach(function (sid) {   // 3.1 / 3.2 belong to section 3
      var el = document.getElementById(sid); if (el) { el.setAttribute("data-jump", "flood-group"); io.observe(el); }
    });
  }


  // ---- Q&A chat (only when the site was built with publish.chat_endpoint) -------------
  var chatUrl = document.body.getAttribute("data-chat");
  if (chatUrl) initChat(chatUrl);
  function initChat(endpoint) {
    var T = {
      pt: { teaser: "Tem dúvidas sobre a previsão? Pergunte aqui ao assistente.", sub: "Assistente IA · responde com base nesta previsão",
            open: "Pergunte o que quiser sobre esta previsão", openSub: "Assistente IA · aberto a todos · em PT, ES ou EN", title: "Pergunte sobre esta previsão", ph: "Escreva a sua pergunta…", send: "Enviar",
            note: "Respostas geradas por IA (Claude) a partir desta previsão experimental; podem conter erros. Informação indicativa, não substitui as comunicações oficiais. Em emergência ligue 112.",
            hello: "Olá! Posso explicar a previsão atual para o rio Sado em Alcácer do Sal. O que quer saber?",
            wait: "A pensar…", fail: "Sem resposta de momento. Em emergência ligue 112.",
            sugg: ["Há risco de cheia nos próximos dias?", "Quando é o pico do caudal?", "Quanto vai chover?", "Como funciona este sistema?"] },
      en: { teaser: "Questions about the forecast? Ask the assistant here.", sub: "AI assistant · answers from this forecast",
            open: "Ask anything about this forecast", openSub: "AI assistant · open to everyone · in PT, ES or EN", title: "Ask about this forecast", ph: "Type your question…", send: "Send",
            note: "Answers are AI-generated (Claude) from this experimental forecast and may be wrong. Guidance only; it does not replace official communications. In an emergency call 112.",
            hello: "Hi! I can explain the current forecast for the Sado river at Alcácer do Sal. What would you like to know?",
            wait: "Thinking…", fail: "No answer right now. In an emergency call 112.",
            sugg: ["Is there a flood risk in the coming days?", "When will the flow peak?", "How much rain is expected?", "How does this system work?"] },
      es: { teaser: "¿Dudas sobre la previsión? Pregunte aquí al asistente.", sub: "Asistente IA · responde a partir de esta previsión",
            open: "Pregunte lo que quiera sobre esta previsión", openSub: "Asistente IA · abierto a todos · en PT, ES o EN", title: "Pregunte sobre esta previsión", ph: "Escriba su pregunta…", send: "Enviar",
            note: "Respuestas generadas por IA (Claude) a partir de esta previsión experimental; pueden contener errores. Información orientativa, no sustituye a las comunicaciones oficiales. En emergencias llame al 112.",
            hello: "¡Hola! Puedo explicar la previsión actual del río Sado en Alcácer do Sal. ¿Qué quiere saber?",
            wait: "Pensando…", fail: "Sin respuesta en este momento. En emergencias llame al 112.",
            sugg: ["¿Hay riesgo de inundación en los próximos días?", "¿Cuándo será la punta de caudal?", "¿Cuánta lluvia se espera?", "¿Cómo funciona este sistema?"] }
    };
    function L() { var l = root.getAttribute("data-lang"); return T[l] ? l : "pt"; }
    var history = [];
    var btn = document.createElement("button");
    btn.type = "button"; btn.className = "chat-fab"; btn.setAttribute("aria-expanded", "false"); btn.setAttribute("aria-controls", "chat-panel");
    var panel = document.createElement("section");
    panel.id = "chat-panel"; panel.className = "chat-panel"; panel.hidden = true; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", "Chat");
    panel.innerHTML = '<header><span class="c-head"><strong class="c-title"></strong><span class="c-sub"></span></span>' +
      '<button type="button" class="c-close" aria-label="Fechar / Close">✕</button></header>' +
      '<div class="c-log" aria-live="polite"></div><div class="c-sugg"></div>' +
      '<form class="c-form"><textarea rows="2" maxlength="600" required></textarea><button type="submit" class="c-send"></button></form>' +
      '<p class="c-note"></p>';
    document.body.appendChild(btn); document.body.appendChild(panel);
    var teaser = document.createElement("div");
    teaser.className = "chat-teaser"; teaser.hidden = true;
    teaser.innerHTML = '<button type="button" class="t-txt"></button><button type="button" class="t-x" aria-label="✕">✕</button>';
    document.body.appendChild(teaser);
    teaser.querySelector(".t-txt").addEventListener("click", function () { open(true); });
    teaser.querySelector(".t-x").addEventListener("click", function () {
      hideTeaser(); try { sessionStorage.setItem("fc-teaser", "1"); } catch (e) {}
    });
    function hideTeaser() { teaser.hidden = true; btn.classList.remove("pulse"); }
    var log = panel.querySelector(".c-log"), form = panel.querySelector(".c-form"), ta = form.querySelector("textarea"), sugg = panel.querySelector(".c-sugg");
    function texts() {
      var t = T[L()];
      btn.innerHTML = '<svg class="c-ico-svg" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4 4h16a2 2 0 012 2v9a2 2 0 01-2 2H9l-5 4v-4H4a2 2 0 01-2-2V6a2 2 0 012-2z"/></svg><span class="c-fab-t"><b>' + t.open + '</b><small>' + t.openSub + "</small></span>" +
        '<span class="c-fab-up" aria-hidden="true">▲</span>';
      panel.querySelector(".c-title").textContent = t.title; ta.placeholder = t.ph;
      form.querySelector(".c-send").textContent = t.send; panel.querySelector(".c-note").textContent = t.note;
      if (greetEl && !history.length) greetEl.textContent = t.hello;
      if (teaser) teaser.querySelector(".t-txt").textContent = t.teaser;
      var sub = panel.querySelector(".c-sub"); if (sub) sub.textContent = t.sub;
      sugg.innerHTML = "";
      if (!history.length) t.sugg.forEach(function (q) {
        var b = document.createElement("button"); b.type = "button"; b.textContent = q;
        b.addEventListener("click", function () { ask(q); }); sugg.appendChild(b);
      });
    }
    function bubble(role, text) {
      var d = document.createElement("div"); d.className = "c-msg c-" + role; d.textContent = text;
      log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
    }
    var greetEl = null;
    function open(o) {
      panel.hidden = !o; btn.setAttribute("aria-expanded", o ? "true" : "false"); btn.classList.toggle("on", o);
      if (o && !greetEl) greetEl = bubble("assistant", T[L()].hello);
      if (o) hideTeaser();
      if (o && !panel.classList.contains("docked")) ta.focus();
    }
    btn.addEventListener("click", function () { open(panel.hidden); });
    panel.querySelector(".c-close").addEventListener("click", function () { open(false); btn.focus(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) open(false); });
    var busy = false;
    function ask(q) {
      q = (q || "").trim(); if (!q || busy) return;
      busy = true; sugg.innerHTML = ""; bubble("user", q); ta.value = "";
      var pending = bubble("assistant", T[L()].wait); pending.classList.add("c-wait");
      fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, history: history.slice(-6), lang: L() }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var a = (d && d.answer) || T[L()].fail;
          pending.textContent = a; pending.classList.remove("c-wait");
          history.push({ role: "user", content: q }, { role: "assistant", content: a });
        })
        .catch(function () { pending.textContent = T[L()].fail; pending.classList.remove("c-wait"); })
        .then(function () { busy = false; log.scrollTop = log.scrollHeight; });
    }
    form.addEventListener("submit", function (e) { e.preventDefault(); ask(ta.value); });
    ta.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(ta.value); } });
    new MutationObserver(texts).observe(root, { attributes: true, attributeFilter: ["data-lang"] });
    texts();
    // Minimised bar at the bottom on every screen; it opens into a panel above it. A one-time
    // hint appears after a few seconds the first time a visitor comes (dismissible).
    open(false);
    var dismissed = false; try { dismissed = sessionStorage.getItem("fc-teaser") === "1"; } catch (e) {}
    if (!dismissed) { btn.classList.add("pulse"); setTimeout(function () { btn.classList.remove("pulse"); }, 12000); }
  }

  // ---- 3-D view lock: locked by default so scrolling the page never zooms the model
  Array.prototype.forEach.call(document.querySelectorAll(".webglwrap"), function (wrap) {
    var b = wrap.querySelector(".viewlock");  // inside .viewctl, still within the wrapper
    if (!b) return;
    b.addEventListener("click", function () {
      var locked = !wrap.classList.contains("locked");
      wrap.classList.toggle("locked", locked);
      b.setAttribute("aria-pressed", locked ? "true" : "false");
      sendLock(wrap);
    });
    var fr = wrap.querySelector("iframe");   // the viewer blocks only camera input while locked; its own controls stay usable
    if (fr) fr.addEventListener("load", function () { setTimeout(function () { sendLock(wrap); }, 300); });
  });
  function sendLock(wrap) {
    var fr = wrap.querySelector("iframe");
    try { if (fr && fr.contentWindow) fr.contentWindow.postMessage({ type: "fluxos-lock", locked: wrap.classList.contains("locked") }, "*"); } catch (e) {}
  }

  // ---- sun button on the 3-D viewer: on = sun follows the simulation time, off = full daylight
  var SUN = false;   // default: full daylight; on = real local light at the forecast time
  function sendSun(w) { try { w.postMessage({ type: "fluxos-sun", follow: SUN }, "*"); } catch (e) {} }
  Array.prototype.forEach.call(document.querySelectorAll(".suntoggle"), function (b) {
    b.addEventListener("click", function () {
      SUN = !SUN;
      b.setAttribute("aria-pressed", SUN ? "true" : "false");
      var fr = document.querySelector("#anim-card iframe");
      if (fr && fr.contentWindow) sendSun(fr.contentWindow);
      if (popWin && !popWin.closed) sendSun(popWin);
    });
  });

  // ---- 3-D viewer in its own pop-up window, driven by this page's clock
  var popWin = null;
  Array.prototype.forEach.call(document.querySelectorAll("button.popout"), function (b) {
    b.addEventListener("click", function () {
      var w = Math.min(1600, screen.availWidth - 40), h = Math.min(1000, screen.availHeight - 60);
      var url = b.getAttribute("data-src").split("#")[0] + "#lang=" + root.getAttribute("data-lang");
      popWin = window.open(url, "floodcast-fluxos-3d", "popup=yes,width=" + w + ",height=" + h);
      if (popWin) { popWin.focus(); setTimeout(function () { sendSun(popWin); }, 6000); }
    });
  });

  // ---- FLUXOS animation <-> hydrograph sync (the viewer posts its simulation time) ------
  var animCard = document.getElementById("anim-card");
  if (animCard) {
    var fEl = animCard.querySelector(".anim-forcing"), forc = null, views = null;
    try { var vEl = animCard.querySelector(".anim-views"); views = vEl ? JSON.parse(vEl.textContent) : null; } catch (e) { views = null; }
    try { forc = fEl ? JSON.parse(fEl.textContent) : null; } catch (e) { forc = null; }
    var frame = animCard.querySelector("iframe"), nowEl = document.querySelector(".an-val");
    if (views && frame) scnSwitch(animCard, function (k) {
      var v = views[k]; if (!v) return;
      frame.dataset.loaded = "";
      frame.setAttribute("src", v.src + "#lang=" + root.getAttribute("data-lang"));
      var pb = animCard.querySelector("button.popout"); if (pb) pb.setAttribute("data-src", v.src);
      if (v.forcing) forc = { start: v.forcing.sim_start_utc, inflow: v.forcing.inflow, rain: v.forcing.rain };
      if (popWin && !popWin.closed) popWin.location.href = v.src + "#lang=" + root.getAttribute("data-lang");
    });
    if (frame) frame.addEventListener("load", function () {
      try { if (frame.contentWindow.location.href.indexOf("about:") === 0) return; } catch (e) {}
      frame.dataset.loaded = "1";
      setTimeout(function () { sendSun(frame.contentWindow); }, 1500);
    });
    function valAt(arr, key, t, step) {
      if (!arr || !arr.length) return null;
      if (!step) {  // linear interpolation (hydrograph)
        if (t <= arr[0].t_s) return arr[0][key];
        for (var i = 1; i < arr.length; i++) if (t <= arr[i].t_s) {
          var f = (t - arr[i - 1].t_s) / ((arr[i].t_s - arr[i - 1].t_s) || 1);
          return arr[i - 1][key] + f * (arr[i][key] - arr[i - 1][key]);
        }
        return arr[arr.length - 1][key];
      }
      var v = 0; for (var j = 0; j < arr.length; j++) if (arr[j].t_s <= t) v = arr[j][key]; return v;  // step (rain)
    }
    if (forc) {
      var f0 = Date.parse(forc.start), lastPost = 0;
      var fEnd = f0;
      (forc.inflow || []).concat(forc.rain || []).forEach(function (r) { fEnd = Math.max(fEnd, f0 + r.t_s * 1000); });
      var outEl = animCard.querySelector(".outsim");
      fcRegister(f0, fEnd, function (epoch) {
        var t = Math.max(0, (epoch - Date.parse(forc.start)) / 1000), now = Date.now();
        var last = (forc.inflow && forc.inflow.length) ? forc.inflow[forc.inflow.length - 1].t_s : 0;
        var rel = (epoch - Date.parse(forc.start)) / 1000;
        if (outEl) outEl.hidden = !(rel < -900 || rel > last + 900);
        if (now - lastPost > 60) {
          lastPost = now;
          if (frame && frame.dataset.loaded) { try { frame.contentWindow.postMessage({ type: "fluxos-set-time", t: t }, "*"); } catch (e) {} }
          if (popWin && !popWin.closed) { try { popWin.postMessage({ type: "fluxos-set-time", t: t }, "*"); } catch (e) {} }
        }
        if (nowEl) {
          var q = valAt(forc.inflow, "q_m3s", t), r = valAt(forc.rain, "mm_h", t, true);
          nowEl.textContent = stamp(forc.start, t / 3600) + " · " + (q == null ? "–" : Math.round(q)) + " m³/s" +
            (r ? " · " + r.toFixed(1) + " mm/h" : "");
        }
      });
    }
  }

  var mapEl = document.getElementById("map");
  var dataEl = document.getElementById("map-data");
  if (mapEl && dataEl) {
    var cfg; try { cfg = JSON.parse(dataEl.textContent); } catch (e) { cfg = null; }
    if (cfg && cfg.scenarios && cfg.scenarios.length) loadLeaflet(function () { initMap(cfg); });
  }
  var amaps = document.querySelectorAll(".amap");
  if (amaps.length) loadLeaflet(function () { for (var k = 0; k < amaps.length; k++) initAnimMap(amaps[k]); });

  var leafletQueue;  // (hoisted; must not be re-initialised after the first loadLeaflet call)
  function loadLeaflet(cb) {
    if (window.L) { cb(); return; }
    if (leafletQueue) { leafletQueue.push(cb); return; }
    leafletQueue = [cb];
    loadLeafletOnce(function () { var q = leafletQueue; for (var i = 0; i < q.length; i++) q[i](); });
  }
  function loadLeafletOnce(cb) {
    var css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css";
    css.integrity = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="; css.crossOrigin = "";
    document.head.appendChild(css);
    var s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js";
    s.integrity = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="; s.crossOrigin = "";
    s.onload = cb;
    document.head.appendChild(s);
  }
  // ---- animated maps: rain per sub-basin, flow per river reach ------------------------
  var RAMP = ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"];
  var FLOWRAMP = ["#ffffcc", "#a1dab4", "#41b6c4", "#2c7fb8", "#253494"];
  function hex(c) { return [parseInt(c.substr(1, 2), 16), parseInt(c.substr(3, 2), 16), parseInt(c.substr(5, 2), 16)]; }
  function ramp(stops, t) {
    t = Math.max(0, Math.min(1, t)); var x = t * (stops.length - 1), i = Math.min(Math.floor(x), stops.length - 2), f = x - i;
    var a = hex(stops[i]), b = hex(stops[i + 1]);
    return "rgb(" + [0, 1, 2].map(function (k) { return Math.round(a[k] + (b[k] - a[k]) * f); }).join(",") + ")";
  }
  function propId(p) { return p.hruId != null ? p.hruId : (p.segId != null ? p.segId : p.id); }
  function stamp(cycleIso, h) {
    var d = new Date(Date.parse(cycleIso) + h * 36e5), l = root.getAttribute("data-lang");
    try {
      return d.toLocaleString(LOCALE[l] || "en-GB", { timeZone: "Europe/Lisbon", weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    } catch (e) { return d.toISOString().substr(0, 16).replace("T", " ") + " UTC"; }
  }

  // ---- One shared, always-running clock ---------------------------------------------------
  // Every animated element (rain map, reach-flow map, chart cursors, the 3-D flood viewer)
  // registers here and is driven by the same looping forecast time. No play buttons: it runs
  // all the time; dragging any slider just moves the shared clock, which then carries on.
  // (FC is defined further down with the shared-clock helpers; it is declared here because
  //  sections above register with it while the script is still running top to bottom)
  function fcRegister(t0, t1, set) {
    FC.subs.push({ t0: t0, t1: t1, set: set });
    var win = document.getElementById("clockwin");
    if (win && !FC.win) FC.win = [Date.parse(win.getAttribute("data-start")), Date.parse(win.getAttribute("data-end"))];
    if (FC.win) { FC.t0 = FC.win[0]; FC.t1 = FC.win[1]; }   // every component plays the same 72 h
    else {
      FC.t0 = FC.t0 == null ? t0 : Math.min(FC.t0, t0);
      FC.t1 = FC.t1 == null ? t1 : Math.max(FC.t1, t1);
    }
    if (FC.epoch == null) FC.epoch = FC.t0;
    if (!FC.started) { FC.started = true; requestAnimationFrame(fcTick); }
  }
  function fcSeek(epoch) { FC.epoch = epoch; fcApply(); }
  function fcApply() {
    for (var i = 0; i < FC.subs.length; i++) { try { FC.subs[i].set(FC.epoch); } catch (e) {} }
    cursorAll(FC.epoch);
  }
  function fcTick(now) {
    var dt = FC.last ? Math.min(250, now - FC.last) : 0; FC.last = now;
    var span = FC.t1 - FC.t0;
    if (span > 0 && !document.hidden) {
      FC.epoch += dt * FC.speed / FC.LOOP_MS * span;
      if (FC.epoch > FC.t1) FC.epoch = FC.t0;
      fcApply();
    }
    requestAnimationFrame(fcTick);
  }
  function nearest(epochs, e) {
    var b = 0; for (var i = 0; i < epochs.length; i++) if (epochs[i] <= e) b = i; return b;
  }
  function cursorAll(epochMs) {
    var svgs = document.querySelectorAll("svg.chart[data-cursor]");
    for (var i = 0; i < svgs.length; i++) cursorOn(svgs[i], epochMs);
  }
  // Vertical time cursor on a chart.
  function cursorOn(svg, epochMs) {
    var c; try { c = JSON.parse(svg.getAttribute("data-cursor")); } catch (e) { return; }
    var s = epochMs / 1000, n = c.t.length, line = svg.querySelector("line.cursor"), x = null;
    if (s >= c.t[0] && s <= c.t[n - 1]) {
      for (var i = 1; i < n; i++) if (s <= c.t[i]) { var f = (s - c.t[i - 1]) / ((c.t[i] - c.t[i - 1]) || 1); x = c.x[i - 1] + f * (c.x[i] - c.x[i - 1]); break; }
    }
    if (!line) {
      line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("class", "cursor"); line.setAttribute("y1", c.y0); line.setAttribute("y2", c.y1);
      svg.appendChild(line);
    }
    line.style.display = x == null ? "none" : "";
    if (x != null) { line.setAttribute("x1", x); line.setAttribute("x2", x); }
  }
  // ---- basemaps: same choice on every map, remembered for the visit -------------------------
  var BASEMAPS = {
    "OpenStreetMap": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" }],
    "Satélite / Satellite": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 19, attribution: "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, GIS User Community" }],
    "Claro / Light": ["https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      { maxZoom: 19, subdomains: "abcd", attribution: "&copy; OpenStreetMap contributors &copy; CARTO" }],
    "Topográfico / Topographic": ["https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
      { maxZoom: 17, attribution: "&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap (CC-BY-SA)" }]
  };
  var BASE_MAPS = [], BASE_SYNC = { busy: false };
  function addBasemaps(map) {
    var L = window.L, layers = {}, chosen = "Satélite / Satellite";   // satellite by default
    try { chosen = sessionStorage.getItem("fc-basemap") || chosen; } catch (e) {}
    Object.keys(BASEMAPS).forEach(function (k) { layers[k] = L.tileLayer(BASEMAPS[k][0], BASEMAPS[k][1]); });
    (layers[chosen] || layers["Satélite / Satellite"]).addTo(map);
    var ctl = L.control.layers(layers, null, { position: "topright", collapsed: true }).addTo(map);
    map.on("baselayerchange", function (ev) {   // the same basemap on every map of the page
      if (BASE_SYNC.busy) return;   // our own changes below fire this event again on the other maps
      BASE_SYNC.busy = true;
      try {
        try { sessionStorage.setItem("fc-basemap", ev.name); } catch (e) {}
        BASE_MAPS.forEach(function (o) {
          if (o.map === map || o.map.hasLayer(o.layers[ev.name])) return;
          Object.keys(o.layers).forEach(function (k) { if (o.map.hasLayer(o.layers[k])) o.map.removeLayer(o.layers[k]); });
          o.layers[ev.name].addTo(o.map);
        });
      } finally { BASE_SYNC.busy = false; }
    });
    BASE_MAPS.push({ map: map, layers: layers });
    return ctl;
  }
  // overlay label in the three site languages (the inactive ones are hidden by the page CSS)
  function tri(pt, es, en) {
    return '<span lang="pt">' + pt + '</span><span lang="es">' + es + '</span><span lang="en">' + en + "</span>";
  }
  // Reference layers shared by the rain and flow maps: sub-basin limits and the model river network
  // (dashed), switched with tick boxes that are always visible on the map (bottom left).
  var REF_STYLE = {   // readable on the satellite default and on the street basemaps
    basin: { color: "#ffd54f", weight: 2.4, opacity: 0.95, fill: false },
    basins: { color: "#ffffff", weight: 0.9, opacity: 0.75, fill: false },
    rivers: { color: "#4fc3f7", weight: 1.6, opacity: 0.95, dashArray: "5 4", lineCap: "butt" }
  };
  var REF_FILE = { basin: "basin.geojson", basins: "hrus.geojson", rivers: "rivers.geojson" };
  var REF_LABEL = {
    basin: ["Bacia hidrográfica", "Cuenca hidrográfica", "River basin"],
    basins: ["Sub-bacias", "Subcuencas", "Sub-basins"],
    rivers: ["Rede hidrográfica", "Red hidrográfica", "River network"]
  };
  // ⌖ button under the zoom buttons: back to the area of interest
  function addRecenter(map, home) {
    var L = window.L, c = L.control({ position: "topleft" });
    c.onAdd = function () {
      var b = L.DomUtil.create("a", "leaflet-bar recenter");
      b.href = "#"; b.setAttribute("role", "button"); b.innerHTML = "⌖";
      b.title = { pt: "Recentrar na área", en: "Recentre on the area", es: "Recentrar en el área" }[root.getAttribute("data-lang")] || "Recentrar";
      L.DomEvent.on(b, "click", function (e) { L.DomEvent.stop(e); home(); });
      return b;
    };
    c.addTo(map);
  }
  function refLayers(map, geoSrc, show) {
    var L = window.L, base = geoSrc.replace(/[^\/]*$/, ""), keys = ["basin", "basins", "rivers"];
    var box = L.control({ position: "bottomleft" });
    box.onAdd = function () {
      var div = L.DomUtil.create("div", "reftoggles");
      div.innerHTML = keys.map(function (k) {
        return '<label><input type="checkbox" data-ref="' + k + '"' + (show[k] ? " checked" : "") + '><i class="rt-' + k + '"></i>' +
          tri(REF_LABEL[k][0], REF_LABEL[k][1], REF_LABEL[k][2]) + "</label>";
      }).join("");
      L.DomEvent.disableClickPropagation(div); L.DomEvent.disableScrollPropagation(div);
      return div;
    };
    box.addTo(map);
    keys.forEach(function (k) {
      fetch(base + REF_FILE[k]).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (g) {
          var cb = box.getContainer().querySelector('input[data-ref="' + k + '"]');
          if (!g) { cb.parentNode.style.display = "none"; return; }
          var lyr = L.geoJSON(g, { style: REF_STYLE[k], interactive: false });
          if (cb.checked) lyr.addTo(map);
          cb.addEventListener("change", function () { if (cb.checked) lyr.addTo(map); else map.removeLayer(lyr); });
        }).catch(function () {});
    });
  }


  // ONE scenario for the whole page: every Lower / Median / Upper switch is linked, and every map,
  // chart and the 3-D viewer follows the chosen scenario.
  function scnSwitch(box, onPick) { SCN.subs.push(onPick); if (SCN.cur !== "central") onPick(SCN.cur); }
  function setScenario(k) {
    SCN.cur = k;
    Array.prototype.forEach.call(document.querySelectorAll(".scnsw .seg"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-scn") === k ? "true" : "false");
    });
    for (var i = 0; i < SCN.subs.length; i++) { try { SCN.subs[i](k); } catch (e) {} }
    document.documentElement.setAttribute("data-scn", k);
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest ? ev.target.closest(".scnsw .seg") : null;
    if (b && !b.disabled) setScenario(b.getAttribute("data-scn"));
  });
  function initAnimMap(el) {
    var kind = el.getAttribute("data-kind"), box = el.parentNode;
    var slider = box.querySelector(".slider"), label = box.querySelector(".tlabel"), rampEl = box.querySelector(".ramp");
    Promise.all([fetch(el.getAttribute("data-src")).then(function (r) { return r.json(); }),
                 fetch(el.getAttribute("data-geo")).then(function (r) { return r.json(); })]).then(function (res) {
      var d = res[0], geo = res[1], L = window.L;
      if (kind === "rain" && d.lats) { gridRain(el, box, d, geo, slider, label, rampEl); return; }
      var ids = kind === "rain" ? d.hru_ids : d.seg_ids, vals = kind === "rain" ? d.mm : d.q_median;
      if (kind === "flow" && d.scenarios && d.scenarios.central) vals = d.scenarios.central.q_median;
      var hours = kind === "rain" ? d.end_hours : d.hours, col = {};
      for (var i = 0; i < ids.length; i++) col[ids[i]] = i;
      var vmax = 0, allVals = [vals];
      if (kind === "flow" && d.scenarios) allVals = Object.keys(d.scenarios).map(function (k) { return d.scenarios[k].q_median; });
      allVals.forEach(function (vv) { vv.forEach(function (row) { row.forEach(function (v) { if (v > vmax) vmax = v; }); }); });
      vmax = kind === "rain" ? Math.max(vmax, 2) : Math.max(vmax, 1);
      var map = L.map(el, { scrollWheelZoom: false, zoomSnap: 0.25 });
      addBasemaps(map);
      var t = 0;
      function colourOf(v) { return kind === "rain" ? ramp(RAMP, v / vmax) : ramp(FLOWRAMP, Math.log1p(v) / Math.log1p(vmax)); }
      function styleOf(f) {
        var j = col[propId(f.properties)], v = j == null ? 0 : vals[t][j];
        if (kind === "rain") return { fillColor: colourOf(v), fillOpacity: v > 0.05 ? 0.75 : 0.15, color: "#334", weight: 0.6 };
        return { color: colourOf(v), weight: 1.5 + 6 * Math.log1p(v) / Math.log1p(vmax), opacity: 0.95, lineCap: "round" };
      }
      var layer = L.geoJSON(geo, {
        style: styleOf,
        onEachFeature: function (f, lyr) {
          lyr.bindTooltip(function () {
            var j = col[propId(f.properties)], v = j == null ? 0 : vals[t][j];
            var lg = root.getAttribute("data-lang");
            var word = kind === "rain" ? { pt: "Sub-bacia ", en: "Sub-basin ", es: "Subcuenca " } : { pt: "Troço ", en: "Reach ", es: "Tramo " };
            return (word[lg] || word.pt) + propId(f.properties) + ": " + v.toFixed(kind === "rain" ? 1 : 0) + (kind === "rain" ? " mm" : " m³/s");
          }, { sticky: true });
        }
      }).addTo(map);
      map.fitBounds(layer.getBounds(), { padding: [6, 6] });
      refLayers(map, el.getAttribute("data-geo"), { basin: true, basins: false, rivers: false });
      addRecenter(map, function () { map.fitBounds(layer.getBounds(), { padding: [6, 6] }); });
      if (window.ResizeObserver) new ResizeObserver(function () { map.invalidateSize(); }).observe(el);
      slider.max = String(hours.length - 1);
      function draw() {
        layer.setStyle(styleOf);
        var h = hours[t], h0 = kind === "rain" ? (t > 0 ? hours[t - 1] : 0) : h;
        label.textContent = kind === "rain" ? stamp(d.cycle, h0) + " → " + stamp(d.cycle, h).split(" ").pop() + "  (+" + h + " h)" : stamp(d.cycle, h) + "  (+" + h + " h)";
        slider.value = String(t);
      }
      rampEl.innerHTML = '<span>0</span><i style="background:linear-gradient(90deg,' + (kind === "rain" ? RAMP : FLOWRAMP).join(",") + ')"></i><span>' +
        (kind === "rain" ? vmax.toFixed(1) : Math.round(vmax)) + " " + rampEl.getAttribute("data-unit") + (kind === "rain" ? "" : " (log)") + "</span>";
      var ep = hours.map(function (h) { return Date.parse(d.cycle) + h * 36e5; });
      slider.addEventListener("input", function () { fcSeek(ep[+slider.value]); });
      fcRegister(ep[0], ep[ep.length - 1], function (e) { var n = nearest(ep, e); if (n !== t || !box._drawn) { t = n; box._drawn = 1; draw(); } });
      if (kind === "flow" && d.scenarios) scnSwitch(box, function (k) { if (d.scenarios[k]) { vals = d.scenarios[k].q_median; draw(); } });
    }).catch(function (e) { console.error("map failed:", e); box.style.display = "none"; });
  }

  // Rain on the native forecast grid; sub-basin outlines drawn on top for orientation.
  function gridRain(el, box, d, geo, slider, label, rampEl) {
    var MM = d.scenarios ? (d.scenarios.central || d.scenarios[Object.keys(d.scenarios)[0]]).mm : d.mm;
    var L = window.L, T = MM.length, vmax = 0;
    (d.scenarios ? Object.keys(d.scenarios).map(function (k) { return d.scenarios[k].mm; }) : [MM]).forEach(function (mm) {
      mm.forEach(function (g) { g.forEach(function (r) { r.forEach(function (v) { if (v != null && v > vmax) vmax = v; }); }); });
    });
    vmax = Math.max(vmax, 2);
    var map = L.map(el, { scrollWheelZoom: false, zoomSnap: 0.25 });
    addBasemaps(map);
    var cells = [];
    for (var y = 0; y < d.lats.length; y++) for (var x = 0; x < d.lons.length; x++) {
      var la = d.lats[y], lo = d.lons[x];
      var r = L.rectangle([[la - d.dlat / 2, lo - d.dlon / 2], [la + d.dlat / 2, lo + d.dlon / 2]],
        { stroke: true, color: "rgba(255,255,255,0.25)", weight: 0.4, fillOpacity: 0 }).addTo(map);
      (function (yy, xx, rr) { rr.bindTooltip(function () { var v = MM[t][yy][xx]; return v == null ? "–" : v.toFixed(1) + " mm"; }, { sticky: true }); })(y, x, r);
      cells.push([y, x, r]);
    }
    map.fitBounds(L.geoJSON(geo).getBounds(), { padding: [6, 6] });
    refLayers(map, el.getAttribute("data-geo"), { basin: true, basins: false, rivers: false });
    var home0 = L.geoJSON(geo).getBounds();
    addRecenter(map, function () { map.fitBounds(home0, { padding: [6, 6] }); });
    if (window.ResizeObserver) new ResizeObserver(function () { map.invalidateSize(); }).observe(el);
    var t = 0;
    slider.max = String(T - 1);
    function draw() {
      for (var i = 0; i < cells.length; i++) {
        var v = MM[t][cells[i][0]][cells[i][1]];
        cells[i][2].setStyle(v == null ? { fillOpacity: 0, opacity: 0 } :
          { fillColor: ramp(RAMP, 0.15 + 0.85 * v / vmax), fillOpacity: v > 0.05 ? 0.62 : 0, opacity: 1 });
      }
      var h = d.end_hours[t], h0 = t > 0 ? d.end_hours[t - 1] : 0;
      label.textContent = stamp(d.cycle, h0) + " → " + stamp(d.cycle, h).split(" ").pop() + "  (+" + h + " h)";
      slider.value = String(t);
    }
    rampEl.innerHTML = '<span>0</span><i style="background:linear-gradient(90deg,' + RAMP.join(",") + ')"></i><span>' +
      vmax.toFixed(1) + " " + rampEl.getAttribute("data-unit") + "</span>";
    var ep = d.end_hours.map(function (h) { return Date.parse(d.cycle) + h * 36e5; });
    slider.addEventListener("input", function () { fcSeek(ep[+slider.value]); });
    fcRegister(ep[0], ep[ep.length - 1], function (e) { var n = nearest(ep, e); if (n !== t || !box._drawn) { t = n; box._drawn = 1; draw(); } });
    if (d.scenarios) scnSwitch(box, function (k) { if (d.scenarios[k]) { MM = d.scenarios[k].mm; draw(); } });
  }

  function initMap(cfg) {
    var fb = document.getElementById("map-fallback"); if (fb) fb.style.display = "none";
    var L = window.L; if (!L) return;
    var card = document.getElementById("map-card");
    var map = L.map("map", { scrollWheelZoom: false });
    addBasemaps(map);
    var overlay = null, current = null, scn = null, frame = -1, mode = "time", shownSrc = null;
    var slider = card.querySelector(".mapanim .slider"), label = card.querySelector(".mapanim .tlabel"),
        maxBtn = card.querySelector(".mapanim .maxbtn"), anim = card.querySelector(".mapanim"),
        lgDepth = card.querySelector(".depthlegend.dp"), lgLk = card.querySelector(".depthlegend.lk");
    var maxWord = { pt: "Profundidade máxima", en: "Maximum depth", es: "Profundidad máxima" };
    function setImage(src, b) {
      if (src === shownSrc) return;
      var bb = [[b.south, b.west], [b.north, b.east]];
      var next = L.imageOverlay(src, bb, { opacity: 0.85 }).addTo(map);
      if (overlay) map.removeLayer(overlay);
      overlay = next; shownSrc = src;
      if (!current) map.fitBounds(bb);
      current = bb;
    }
    var layer = "depth", lgLy = card.querySelectorAll(".depthlegend.ly");
    function showLegend() {
      if (lgDepth) lgDepth.hidden = layer !== "depth" || mode === "lk";
      Array.prototype.forEach.call(lgLy, function (el) { el.hidden = !el.classList.contains("ly-" + layer) || mode === "lk"; });
    }
    function show() {
      if (!scn) return;
      if (layer !== "depth" && scn.layers && scn.layers[layer]) {   // static decision layer
        setImage(scn.layers[layer], scn); anim.hidden = true; showLegend();
        if (overlay) overlay.setOpacity(0.85);
        return;
      }
      anim.hidden = !scn.frames.length; showLegend();
      if (mode === "max" || !scn.frames.length) {
        setImage(scn.image, scn);
        label.textContent = (maxWord[root.getAttribute("data-lang")] || maxWord.pt) + (scn.area != null ? " · " + scn.area + " km²" : "");
        return;
      }
      var f = scn.frames[Math.max(0, frame)];
      setImage(f.image, scn);
      label.textContent = stamp(f.t, 0) + (f.km2 != null ? " · " + f.km2 + " km²" : "");
      slider.value = String(Math.max(0, frame));
    }
    addRecenter(map, function () { if (current) map.fitBounds(current); });
    var lkBtn = card.querySelector(".lkbtn");
    function select(id) {
      if (lkBtn) lkBtn.setAttribute("aria-pressed", id === "likelihood" ? "true" : "false");
      if (id === "likelihood" && cfg.likelihood) {
        mode = "lk"; shownSrc = null; setImage(cfg.likelihood.image, cfg.likelihood);
        anim.hidden = true; if (lgLk) lgLk.hidden = false; if (lgDepth) lgDepth.hidden = true;
        return;
      }
      for (var i = 0; i < cfg.scenarios.length; i++) if (cfg.scenarios[i].id === id) scn = cfg.scenarios[i];
      if (!scn) scn = cfg.scenarios[0];
      if (lgLk) lgLk.hidden = true; if (lgDepth) lgDepth.hidden = false;
      anim.hidden = false;
      slider.max = String(Math.max(0, scn.frames.length - 1));
      if (mode === "lk") mode = "time";
      show();
    }
    // Frames follow the page's shared clock (same time as the rain/flow maps and the 3-D viewer).
    var all = [];
    cfg.scenarios.forEach(function (s) { s.frames.forEach(function (f) { all.push(Date.parse(f.t)); }); });
    if (all.length) {
      all.sort(function (x, y) { return x - y; });
      var outWord = { pt: "fora do período simulado pelo FLUXOS", en: "outside the period simulated by FLUXOS", es: "fuera del periodo simulado por FLUXOS" };
      fcRegister(all[0], all[all.length - 1], function (e) {
        if (mode !== "time" || layer !== "depth" || !scn || !scn.frames.length) return;
        var ep = scn.frames.map(function (f) { return Date.parse(f.t); });
        var outside = e < ep[0] - 900e3 || e > ep[ep.length - 1] + 900e3;
        if (overlay) overlay.setOpacity(outside ? 0.15 : 0.85);
        var n = nearest(ep, e);
        if (n !== frame || outside !== card._outside) { frame = n; card._outside = outside; show(); }
        if (outside) label.textContent = stamp(new Date(e).toISOString(), 0) + " · " + (outWord[root.getAttribute("data-lang")] || outWord.pt);
      });
    }
    slider.addEventListener("input", function () {
      if (!scn || !scn.frames.length) return;
      mode = "time"; maxBtn.setAttribute("aria-pressed", "false");
      fcSeek(Date.parse(scn.frames[+slider.value].t));
    });
    maxBtn.addEventListener("click", function () {
      mode = mode === "max" ? "time" : "max";
      maxBtn.setAttribute("aria-pressed", mode === "max" ? "true" : "false");
      shownSrc = null; show();
    });
    Array.prototype.forEach.call(card.querySelectorAll(".lyr .seg"), function (b) {
      b.addEventListener("click", function () {
        layer = b.getAttribute("data-layer"); shownSrc = null;
        Array.prototype.forEach.call(card.querySelectorAll(".lyr .seg"), function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
        if (mode === "lk") mode = "time";
        if (lkBtn) lkBtn.setAttribute("aria-pressed", "false");
        show();
      });
    });
    var P = { lower: "p10", central: "p50", upper: "p90" };
    scnSwitch(card, function (k) { shownSrc = null; select(P[k] || "p50"); });
    if (lkBtn) lkBtn.addEventListener("click", function () {
      shownSrc = null;
      if (lkBtn.getAttribute("aria-pressed") === "true") select(P[SCN.cur] || "p50"); else select("likelihood");
    });
    function refit() { map.invalidateSize(); if (current) map.fitBounds(current); }
    setTimeout(refit, 100); setTimeout(refit, 800);
    if (window.ResizeObserver) new ResizeObserver(refit).observe(document.getElementById("map"));
    select((cfg.scenarios.filter(function (x) { return x.id === "p50"; })[0] || cfg.scenarios[0]).id);  // default: median
  }

  // ---- forecast-updates time bar: run used -> results -> NOW -> next run -> new results ----------
  (function () {
    var box = document.querySelector(".fctiming");
    if (!box) return;
    var items = [].slice.call(box.querySelectorAll(".tl-legend li[data-t]"));
    var bar = box.querySelector(".tl-bar"), past = box.querySelector(".tl-past"), nowEl = box.querySelector(".tl-now");
    var W = {
      pt: { ago: "há ", in: "daqui a ", now: "agora", late: "atrasada: esperada há ", h: " h", m: " min" },
      en: { ago: "", agoS: " ago", in: "in ", now: "now", late: "late: expected ", lateS: " ago", h: " h", m: " min" },
      es: { ago: "hace ", in: "dentro de ", now: "ahora", late: "con retraso: prevista hace ", h: " h", m: " min" }
    };
    var LOC = { pt: "pt-PT", en: "en-GB", es: "es-ES" };
    items.forEach(function (li) {   // markers on the bar
      var d = document.createElement("span"); d.className = "tl-mark " + li.className; d.textContent = li.getAttribute("data-n");
      li._mark = d; bar.appendChild(d);
    });
    function dur(ms, w) {
      var min = Math.round(Math.abs(ms) / 60000), h = Math.floor(min / 60), m = min % 60;
      return h ? h + w.h + (m && h < 10 ? " " + m + w.m : "") : m + w.m;
    }
    function draw() {
      var l = root.getAttribute("data-lang"); var w = W[l] || W.pt;
      var now = Date.now(), ts = items.map(function (li) { return Date.parse(li.getAttribute("data-t")); });
      var t0 = Math.min.apply(null, ts.concat([now])), t1 = Math.max.apply(null, ts.concat([now + 30 * 60000]));
      var pos = function (t) { return (100 * (t - t0) / (t1 - t0)).toFixed(2) + "%"; };
      items.forEach(function (li, i) {
        var t = ts[i], diff = t - now, future = diff > 0, expected = li.className.indexOf("next") >= 0;
        li._mark.style.left = pos(t);
        li.classList.toggle("is-future", future); li._mark.classList.toggle("is-future", future);
        var clock = "";
        try { clock = new Date(t).toLocaleString(LOC[l] || "pt-PT", { timeZone: "Europe/Lisbon", weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }); } catch (e) {}
        var rel = future ? w.in + dur(diff, w)
                : (expected ? w.late + dur(diff, w) + (w.lateS || "") : w.ago + dur(diff, w) + (w.agoS || ""));
        li.classList.toggle("is-late", !future && expected);
        li.querySelector(".tl-when").textContent = (expected ? "≈ " : "") + clock + " · " + rel;
      });
      past.style.width = pos(now);
      nowEl.style.left = pos(now);
      var nowClock = "";
      try { nowClock = new Date(now).toLocaleTimeString(LOC[l] || "pt-PT", { timeZone: "Europe/Lisbon", hour: "2-digit", minute: "2-digit" }); } catch (e) {}
      nowEl.querySelector("span").textContent = w.now + " " + nowClock;
    }
    draw();
    setInterval(draw, 60000);
    new MutationObserver(draw).observe(root, { attributes: true, attributeFilter: ["data-lang"] });
  })();

  // ---- hover read-out on every time-series chart: vertical line + values of each series ------
  (function () {
    var tip = document.createElement("div");
    tip.className = "charttip"; tip.hidden = true; document.body.appendChild(tip);
    var LOC = { pt: "pt-PT", en: "en-GB", es: "es-ES" };
    function fmtNum(v, u) {
      if (v === null || v === undefined) return "–";
      var l = root.getAttribute("data-lang");
      try { return v.toLocaleString(LOC[l] || "pt-PT", { maximumFractionDigits: 2 }) + (u ? " " + u : ""); } catch (e) { return v + " " + u; }
    }
    function attach(svg) {
      var d; try { d = JSON.parse(svg.getAttribute("data-cursor")); } catch (e) { return; }
      if (!d || !d.s || !d.x || d.x.length < 2) return;
      var NS = "http://www.w3.org/2000/svg";
      var ln = document.createElementNS(NS, "line");
      ln.setAttribute("class", "hoverl"); ln.setAttribute("y1", d.y0); ln.setAttribute("y2", d.y1); ln.style.display = "none";
      svg.appendChild(ln);
      function move(ev) {
        var m = svg.getScreenCTM(); if (!m) return;
        var x = (ev.clientX - m.e) / m.a;
        if (x < d.x[0] - 20 || x > d.x[d.x.length - 1] + 20) { hide(); return; }
        var i = 0, best = Infinity;
        for (var k = 0; k < d.x.length; k++) { var dd = Math.abs(d.x[k] - x); if (dd < best) { best = dd; i = k; } }
        ln.setAttribute("x1", d.x[i]); ln.setAttribute("x2", d.x[i]); ln.style.display = "";
        var l = root.getAttribute("data-lang"), when = "";
        try { when = new Date(d.t[i] * 1000).toLocaleString(LOC[l] || "pt-PT", { timeZone: "Europe/Lisbon", weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }); } catch (e) {}
        var rows = d.s.map(function (sr) {
          var lab = sr.l[l] || sr.l.pt || sr.l.en || "";
          return '<div class="ct-row"><i class="ct-k ' + sr.c + '"></i><span>' + lab + '</span><b>' + fmtNum(sr.v[i], sr.u) + "</b></div>";
        }).join("");
        tip.innerHTML = '<div class="ct-t">' + when + "</div>" + rows;
        tip.hidden = false;
        var w = tip.offsetWidth, h = tip.offsetHeight, px = ev.clientX + 14, py = ev.clientY - h - 12;
        if (px + w > window.innerWidth - 8) px = ev.clientX - w - 14;
        if (py < 8) py = ev.clientY + 16;
        tip.style.left = px + "px"; tip.style.top = py + "px";
      }
      function hide() { if (!drag) { ln.style.display = "none"; tip.hidden = true; } }
      // press and drag on a chart = move the shared clock (every chart, map and the 3-D view follow)
      var drag = false;
      function seekAt(ev) {
        var m = svg.getScreenCTM(); if (!m) return;
        var x = (ev.clientX - m.e) / m.a, i = 0, best = Infinity;
        for (var k = 0; k < d.x.length; k++) { var dd = Math.abs(d.x[k] - x); if (dd < best) { best = dd; i = k; } }
        // interpolate between neighbouring samples so dragging is smooth
        var j = x > d.x[i] ? Math.min(i + 1, d.x.length - 1) : Math.max(i - 1, 0), t = d.t[i];
        if (j !== i && d.x[j] !== d.x[i]) t = d.t[i] + (d.t[j] - d.t[i]) * Math.max(0, Math.min(1, (x - d.x[i]) / (d.x[j] - d.x[i])));
        fcSeek(t * 1000);
      }
      svg.addEventListener("pointermove", function (ev) { move(ev); if (drag) seekAt(ev); });
      svg.addEventListener("pointerdown", function (ev) {
        drag = true; try { svg.setPointerCapture(ev.pointerId); } catch (e) {}
        move(ev); seekAt(ev); ev.preventDefault();
      });
      function endDrag(ev) { if (!drag) return; drag = false; try { svg.releasePointerCapture(ev.pointerId); } catch (e) {} }
      svg.addEventListener("pointerup", endDrag);
      svg.addEventListener("pointercancel", endDrag);
      svg.addEventListener("pointerleave", hide);
      svg.style.cursor = "ew-resize";
      svg.style.touchAction = "pan-y";
    }
    Array.prototype.forEach.call(document.querySelectorAll("svg.chart[data-cursor]"), attach);
    window.addEventListener("scroll", function () { tip.hidden = true; }, { passive: true });
  })();

  // ---- section 4: "full details" links pick the scenario; 📷 opens a mini-map of the place ------
  Array.prototype.forEach.call(document.querySelectorAll(".detbtn[data-scn]"), function (a) {
    a.addEventListener("click", function () { setScenario(a.getAttribute("data-scn")); });
  });
  Array.prototype.forEach.call(document.querySelectorAll(".snapbtn"), function (b) {
    b.addEventListener("click", function () {
      var tr = b.closest("tr"), nxt = tr.nextElementSibling;
      if (nxt && nxt.classList.contains("snaprow")) { nxt.remove(); b.setAttribute("aria-pressed", "false"); return; }
      var L = window.L; if (!L) return;
      var row = document.createElement("tr"); row.className = "snaprow";
      var td = document.createElement("td"); td.colSpan = tr.children.length;
      var el = document.createElement("div"); el.className = "snapmap";
      td.appendChild(el); row.appendChild(td); tr.parentNode.insertBefore(row, tr.nextSibling);
      b.setAttribute("aria-pressed", "true");
      var lat = +b.getAttribute("data-lat"), lon = +b.getAttribute("data-lon");
      var map = L.map(el, { scrollWheelZoom: false, zoomControl: true, attributionControl: true }).setView([lat, lon], 17);
      L.tileLayer(BASEMAPS["Satélite / Satellite"] ? BASEMAPS["Satélite / Satellite"][0] : "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                  BASEMAPS["Satélite / Satellite"] ? BASEMAPS["Satélite / Satellite"][1] : { maxZoom: 19, attribution: "Tiles © Esri" }).addTo(map);
      try { L.imageOverlay(b.getAttribute("data-img"), JSON.parse(b.getAttribute("data-bounds")), { opacity: 0.75 }).addTo(map); } catch (e) {}
      addRecenter(map, function () { map.setView([lat, lon], 17); });
      L.circleMarker([lat, lon], { radius: 7, color: "#fff", weight: 2, fillColor: "#c62828", fillOpacity: 1 }).addTo(map)
        .bindTooltip(b.getAttribute("data-name"), { permanent: true, direction: "top", offset: [0, -8] });
      setTimeout(function () { map.invalidateSize(); }, 50);
    });
  });
})();
