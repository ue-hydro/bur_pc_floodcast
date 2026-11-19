/* Entrada por código enviado por email — o MESMO ficheiro nos dois Workers:
     FREQ_E_EXAMES/worker/login.js  (exames.diogocosta.org)   e   LECTURE_MATERIAL/worker/login.js  (lectures.diogocosta.org)
   Alterar um → copiar para o outro (os testes de cada projeto verificam que são iguais, se o outro estiver ao lado).

   Porquê: o Cloudflare Access conta um «lugar» por cada pessoa que entra e o plano gratuito só tem 50. Os ALUNOS passam a entrar
   por aqui (não ocupam lugares); o Access continua a proteger as páginas do professor e a sua identidade continua a ser aceite
   (cabeçalho Cf-Access-Jwt-Assertion ou cookie CF_Authorization).

   • GET  /entrar             página: email → código de 6 algarismos por email → sessão (cookie HttpOnly só deste site)
   • POST /entrar/codigo      {email, volta} envia o código SÓ a quem tem acesso à página pedida (senão: «este email não tem acesso») (válido 10 min; no máx. 1 por minuto e 5 por hora por email, 300 por hora no total)
   • POST /entrar/verificar   {email, code}  confere o código (5 tentativas) e abre a sessão
   • POST /entrar/passe       {code[, email, volta]}  código DADO PELO PROFESSOR (8 caracteres, p. ex. K7PM-4XQR) — nas aulas basta o código
                                             (um só uso, por pessoa); nos exames (passEmail + checkPass) o aluno escreve o email e o professor
                                             escreve o código da prova. 20 tentativas erradas por hora por endereço IP.
   • GET  /sair               termina a sessão
   Códigos e sessões ficam na base D1 (tabelas login_codes e login_sessions, criadas aqui na primeira utilização — sem migração manual).
   Se o email falhar, o professor gera códigos no painel dos exames ou em /prof das aulas (issueCodes) e dá-os aos alunos. */

const CODE_MIN = 10, MAX_TRIES = 5, RESEND_S = 60, SENDS_PER_HOUR = 5, GLOBAL_PER_HOUR = 300, ADMIN_CODE_MIN = 180;
const COOKIE = "__Host-ue_sessao";
const EMAIL_RE = /^[^@\s"<>,;]+@[^@\s"<>,;]+\.[a-z]{2,}$/;
const TABLES = [
  "CREATE TABLE IF NOT EXISTS login_codes (email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at INTEGER NOT NULL, tries INTEGER NOT NULL DEFAULT 0, sent_at INTEGER, sends INTEGER NOT NULL DEFAULT 0, window_at INTEGER)",
  "CREATE TABLE IF NOT EXISTS login_sessions (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, site TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)",
  "CREATE TABLE IF NOT EXISTS login_passes (code_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at INTEGER NOT NULL)",
  "CREATE TABLE IF NOT EXISTS login_fails (ip TEXT PRIMARY KEY, window_at INTEGER NOT NULL, n INTEGER NOT NULL)",
];
const ready = new WeakSet();
async function tables(DB) { if (ready.has(DB)) return; for (const s of TABLES) await DB.prepare(s).run(); ready.add(DB); }

const enc = new TextEncoder();
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
const sha = async s => hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
const b64u = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=")), c => c.charCodeAt(0));
const randomToken = () => btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const randomCode = () => String(crypto.getRandomValues(new Uint32Array(1))[0] % 1000000).padStart(6, "0");
const codeHash = (email, code) => sha("ue-login-v1|" + email + "|" + code);
/* códigos do professor: 8 caracteres sem os que se confundem (0/O, 1/I) — 32^8 ≈ 10^12 combinações, por isso dispensam o email */
const PASS_ABC = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ", PASS_FAILS = 20;
const randomPass = () => { const s = [...crypto.getRandomValues(new Uint8Array(8))].map(x => PASS_ABC[x & 31]).join(""); return s.slice(0, 4) + "-" + s.slice(4); };
const normPass = c => String(c || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
const passHash = code => sha("ue-pass-v1|" + normPass(code));
const normEmail = e => String(e || "").trim().toLowerCase();
function cookies(req) {
  const out = {}; String(req.headers.get("cookie") || "").split(/;\s*/).forEach(p => { const i = p.indexOf("="); if (i > 0) out[p.slice(0, i)] = p.slice(i + 1); });
  return out;
}
/* «volta»: só caminhos deste site (nada de //outro.site nem de voltar à própria página de entrada) */
function safeBack(v) {
  const s = String(v || "");
  return /^\/(?![\/\\])[^\s\\]*$/.test(s) && !/^\/(entrar|sair)\b/.test(s) ? s : "/";
}
const json = (obj, status = 200, extra) => new Response(JSON.stringify(obj), { status, headers: Object.assign({ "content-type": "application/json; charset=utf-8", "cache-control": "no-store" }, extra || {}) });
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- identidade do Cloudflare Access (professor) ---------- */
let jwksCache = { at: 0, keys: null };
async function verifyAccessJwt(token, env) {
  const [h, p, s] = token.split("."); if (!s) throw new Error("jwt");
  const header = JSON.parse(new TextDecoder().decode(b64u(h))), payload = JSON.parse(new TextDecoder().decode(b64u(p)));
  if (Date.now() - jwksCache.at > 3600e3 || !jwksCache.keys) { const r = await fetch(`https://${env.ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`); jwksCache = { at: Date.now(), keys: (await r.json()).keys }; }
  const jwk = jwksCache.keys.find(k => k.kid === header.kid); if (!jwk) throw new Error("kid");
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64u(s), enc.encode(h + "." + p)); if (!ok) throw new Error("sig");
  const auds = String(env.ACCESS_AUD || "").split(",").map(x => x.trim()).filter(Boolean), aud = [].concat(payload.aud || []);
  if (!auds.length || !aud.some(a => auds.includes(a))) throw new Error("aud");
  if (payload.iss !== "https://" + env.ACCESS_TEAM_DOMAIN) throw new Error("iss");
  if (payload.exp * 1000 < Date.now()) throw new Error("exp");
  return normEmail(payload.email);
}
async function accessEmail(req, env) {
  const token = req.headers.get("Cf-Access-Jwt-Assertion") || cookies(req).CF_Authorization; if (!token || !env.ACCESS_TEAM_DOMAIN) return null;
  try { return (await verifyAccessJwt(token, env)) || null; } catch (e) { return null; }
}

/* ---------- páginas ---------- */
const TXT = {
  pt: { title: "Entrar", lead: "Escreva o seu <b>email institucional</b>. Vamos enviar-lhe um código de 6 algarismos.", email: "Email", send: "Enviar código", have: "Já tenho um código (dado pelo professor)",
    sent: "Enviámos um código para <b>{e}</b>. Pode demorar um minuto — veja também a pasta de <i>lixo</i> / <i>spam</i>.", typed: "Escreva o código de 6 algarismos para <b>{e}</b>.", code: "Código", enter: "Entrar",
    resend: "Reenviar código", resendIn: "Reenviar código ({s} s)", other: "Usar outro email", wait: "A enviar…", checking: "A verificar…", lang: "English",
    err: { invalid_email: "Escreva um email válido.", not_allowed: "Este email não tem acesso. Se é aluno, use o email institucional (o mesmo do Moodle); se já o usou, peça ao professor para o acrescentar.", use_admin: "Professor: entre pela página do professor (/admin).",
      wait: "Acabou de pedir um código. Espere {s} s antes de pedir outro.", too_many: "Pediu demasiados códigos. Tente de novo dentro de {m} min, ou peça um código ao professor.",
      busy: "O serviço está muito ocupado. Tente dentro de alguns minutos, ou peça um código ao professor.", email_failed: "Não foi possível enviar o email. Peça um código ao professor.",
      not_configured: "A entrada por código ainda não está ativa neste site. Fale com o professor.", wrong: "Código errado. Restam {n} tentativa(s).", expired: "O código expirou ou já foi usado. Peça um novo.",
      locked: "Demasiadas tentativas erradas. Peça um novo código.", bad_request: "Pedido inválido. Recarregue a página.", network: "Sem ligação ao servidor. Tente de novo.",
      wrong_pass: "Código inválido ou expirado. Confirme-o com o professor.", pass_format: "O código do professor tem 8 caracteres (p. ex. K7PM-4XQR).",
      pass_format_exam: "O código de entrada tem 8 caracteres (p. ex. K7PM-4XQR). O de 6 algarismos é o código de desbloqueio.", too_many_tries: "Demasiadas tentativas erradas. Espere uma hora ou peça ajuda ao professor." },
    passLead: "Escreva o código que o professor lhe deu (8 caracteres, p. ex. K7PM-4XQR). Não precisa do email.", back: "← Voltar",
    passLeadEmail: "Escreva o seu email institucional. O professor escreve o código de entrada.",
    ended: "O seu acesso temporário terminou. Para continuar, peça um novo código ao professor." },
  en: { title: "Sign in", lead: "Enter your <b>institutional email</b>. We will send you a 6-digit code.", email: "Email", send: "Send code", have: "I already have a code (given by the teacher)",
    sent: "We sent a code to <b>{e}</b>. It may take a minute — check your <i>junk</i> / <i>spam</i> folder too.", typed: "Enter the 6-digit code for <b>{e}</b>.", code: "Code", enter: "Sign in",
    resend: "Resend code", resendIn: "Resend code ({s} s)", other: "Use another email", wait: "Sending…", checking: "Checking…", lang: "Português",
    err: { invalid_email: "Enter a valid email.", not_allowed: "This email has no access. If you are a student, use your institutional email (the one in Moodle); if you already did, ask the teacher to add you.", use_admin: "Teacher: sign in through the teacher page (/admin).",
      wait: "You just asked for a code. Wait {s} s before asking for another.", too_many: "Too many codes requested. Try again in {m} min, or ask the teacher for a code.",
      busy: "The service is very busy. Try again in a few minutes, or ask the teacher for a code.", email_failed: "The email could not be sent. Ask the teacher for a code.",
      not_configured: "Sign-in by code is not active on this site yet. Talk to the teacher.", wrong: "Wrong code. {n} attempt(s) left.", expired: "The code has expired or was already used. Ask for a new one.",
      locked: "Too many wrong attempts. Ask for a new code.", bad_request: "Invalid request. Reload the page.", network: "No connection to the server. Try again.",
      wrong_pass: "Invalid or expired code. Check it with the teacher.", pass_format: "The teacher's code has 8 characters (e.g. K7PM-4XQR).",
      pass_format_exam: "The entry code has 8 characters (e.g. K7PM-4XQR). The 6-digit one is the unlock code.", too_many_tries: "Too many wrong attempts. Wait an hour or ask the teacher for help." },
    passLead: "Enter the code the teacher gave you (8 characters, e.g. K7PM-4XQR). No email needed.", back: "← Back",
    passLeadEmail: "Enter your institutional email. The teacher types the entry code.",
    ended: "Your temporary access has ended. To continue, ask the teacher for a new code." },
};
function loginPage(o, back, logoUrl) {
  return new Response(`<!DOCTYPE html><html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Entrar · ${esc(o.title.pt)}</title><meta name="robots" content="noindex">
<style>body{margin:0;background:#F3F7FA;color:#0A2239;font:15px/1.5 Inter,"Segoe UI",system-ui,sans-serif}.card{max-width:440px;margin:56px auto;background:#fff;border:1px solid #D7E2EA;border-left:5px solid #155A75;border-radius:12px;padding:22px 26px}
@media(max-width:480px){.card{margin:24px 16px}}img{height:42px}h1{margin:10px 0 2px;font:700 22px Georgia,serif}.site{color:#5B7183;font-size:14px;margin:0 0 14px}label{display:block;font-weight:600;font-size:14px;margin:12px 0 4px}
input{width:100%;box-sizing:border-box;font:inherit;padding:9px 11px;border:1px solid #B9CAD6;border-radius:8px}input#code{font:600 22px/1.2 ui-monospace,Menlo,monospace;letter-spacing:6px}
button{margin-top:14px;width:100%;background:#155A75;color:#fff;border:0;border-radius:8px;padding:10px;font:600 15px inherit;cursor:pointer}button:disabled{opacity:.55;cursor:default}
.links{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:12px;font-size:13.5px}a{color:#155A75}.err{background:#FBEAEC;border:1px solid #E3B5BC;color:#7A1626;border-radius:8px;padding:8px 11px;margin-top:12px;font-size:14px}
.muted{color:#5B7183;font-size:13px}.top{display:flex;justify-content:space-between;align-items:center}.info{background:#FFF6E0;border:1px solid #E8D29A;border-radius:8px;padding:8px 11px;margin:0 0 6px;font-size:14px}</style></head><body>
<div class="card"><div class="top"><img src="${esc(logoUrl)}" alt="Universidade de Évora"><a href="#" id="lg" class="muted"></a></div><h1 id="t"></h1><p class="site" id="site"></p><p class="info" id="ended" hidden></p>
<form id="f1"><p id="lead"></p><label for="email" id="le"></label><input id="email" type="email" autocomplete="email" inputmode="email" required placeholder="l12345@alunos.uevora.pt"><button id="b1"></button>
<div class="links"><a href="#" id="have"></a></div></form>
<form id="f2" hidden><p id="msg2"></p><label for="code" id="lc"></label><input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required><button id="b2"></button>
<div class="links"><a href="#" id="rs"></a><a href="#" id="ot"></a></div></form>
<form id="f3" hidden><p id="msg3"></p><div id="pe" hidden><label for="pemail" id="lpe"></label><input id="pemail" type="email" autocomplete="email" inputmode="email" placeholder="l12345@alunos.uevora.pt"></div><label for="pass" id="lp"></label><input id="pass" autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="12" required style="text-transform:uppercase;font:600 22px/1.2 ui-monospace,Menlo,monospace;letter-spacing:3px"><button id="b3"></button>
<div class="links"><a href="#" id="bk"></a></div></form><div class="err" id="err" hidden></div></div>
<script>
const TXT=${JSON.stringify(TXT)}, SITE=${JSON.stringify(o.title)}, BACK=${JSON.stringify(back)}, PASS_EMAIL=${o.passEmail ? "true" : "false"};
const $=s=>document.getElementById(s);
let L="pt"; try{ const q=new URLSearchParams(location.search).get("lang"), s=localStorage.getItem("hydro_lang"); L=q==="en"||q==="pt"?q:s==="en"||s==="pt"?s:(navigator.language||"pt").toLowerCase().startsWith("pt")?"pt":"en"; }catch(e){}
let email="", timer=null, left=0, sentByEmail=false; const ENDED=new URLSearchParams(location.search).has("fim");   // o acesso temporário acabou
const fill=(s,v)=>s.replace(/\\{(\\w)\\}/g,(_,k)=>v&&v[k]!==undefined?v[k]:"");
const escH=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function paint(){ const T=TXT[L]; document.documentElement.lang=L; $("t").textContent=T.title; $("site").textContent=SITE[L]; $("lead").innerHTML=T.lead; $("le").textContent=T.email; $("b1").textContent=T.send; $("have").textContent=T.have;
  $("lc").textContent=T.code; $("b2").textContent=T.enter; $("ot").textContent=T.other; $("lg").textContent=T.lang; $("msg2").innerHTML=fill(sentByEmail?T.sent:T.typed,{e:escH(email)});
  $("ended").textContent=T.ended; $("ended").hidden=!ENDED; $("msg3").textContent=PASS_EMAIL?T.passLeadEmail:T.passLead; $("lpe").textContent=T.email; $("pe").hidden=!PASS_EMAIL; $("lp").textContent=T.code; $("b3").textContent=T.enter; $("bk").textContent=T.back; tick(); }
function tick(){ const T=TXT[L]; $("rs").textContent=left>0?fill(T.resendIn,{s:left}):T.resend; $("rs").style.pointerEvents=left>0?"none":""; $("rs").style.opacity=left>0?.5:1; }
function showErr(code,data){ const T=TXT[L], m=T.err[code]||T.err.bad_request; $("err").textContent=fill(m,data||{}); $("err").hidden=false; }
const hideErr=()=>{ $("err").hidden=true; };
async function post(path,body){ try{ const r=await fetch(path,{method:"POST",headers:{"content-type":"application/json"},credentials:"same-origin",body:JSON.stringify(body)}); return await r.json(); }catch(e){ return {error:"network"}; } }
function step2(byEmail){ sentByEmail=byEmail; $("f1").hidden=true; $("f2").hidden=false; $("code").value=""; paint(); $("code").focus(); }
function countdown(s){ clearInterval(timer); left=s; tick(); timer=setInterval(()=>{ left--; tick(); if(left<=0) clearInterval(timer); },1000); }
async function send(){ hideErr(); const v=$("email").value.trim().toLowerCase(); if(!/^[^@\\s]+@[^@\\s]+\\.[a-z]{2,}$/.test(v)) return showErr("invalid_email"); email=v;
  $("b1").disabled=true; $("b1").textContent=TXT[L].wait; const r=await post("/entrar/codigo",{email,volta:BACK}); $("b1").disabled=false; $("b1").textContent=TXT[L].send;
  if(r.ok){ step2(true); countdown(60); return; } if(r.error==="wait"){ step2(true); countdown(r.seconds||60); } showErr(r.error,{s:r.seconds,m:r.minutes}); }
$("f1").onsubmit=e=>{ e.preventDefault(); send(); };
$("have").onclick=e=>{ e.preventDefault(); hideErr(); $("f1").hidden=true; $("f3").hidden=false; $("pass").value=""; if(PASS_EMAIL){ $("pemail").value=$("email").value; $("pemail").required=true; $("pemail").focus(); } else $("pass").focus(); };   // código do professor (nos exames: com o email do aluno)
$("bk").onclick=e=>{ e.preventDefault(); hideErr(); $("f3").hidden=true; $("f1").hidden=false; $("email").focus(); };
$("f3").onsubmit=async e=>{ e.preventDefault(); hideErr(); const code=$("pass").value.toUpperCase().replace(/[^A-Z0-9]/g,""), em=$("pemail").value.trim().toLowerCase();
  if(PASS_EMAIL && !/^[^@\\s]+@[^@\\s]+\\.[a-z]{2,}$/.test(em)){ $("pemail").focus(); return showErr("invalid_email"); } if(!PASS_EMAIL && code.length!==8) return showErr("pass_format");
  $("b3").disabled=true; $("b3").textContent=TXT[L].checking; const r=await post("/entrar/passe",PASS_EMAIL?{code,email:em,volta:BACK}:{code}); $("b3").disabled=false; $("b3").textContent=TXT[L].enter;
  if(r.ok){ location.replace(BACK); return; } showErr(r.error); };
$("f2").onsubmit=async e=>{ e.preventDefault(); hideErr(); const code=$("code").value.replace(/\\D/g,""); if(code.length!==6) return showErr("wrong",{n:"…"});
  $("b2").disabled=true; $("b2").textContent=TXT[L].checking; const r=await post("/entrar/verificar",{email,code}); $("b2").disabled=false; $("b2").textContent=TXT[L].enter;
  if(r.ok){ location.replace(BACK); return; } showErr(r.error,{n:r.left}); };
$("rs").onclick=e=>{ e.preventDefault(); if(left>0) return; $("f1").hidden=false; $("f2").hidden=true; $("email").value=email; send(); };
$("ot").onclick=e=>{ e.preventDefault(); hideErr(); clearInterval(timer); left=0; $("f2").hidden=true; $("f1").hidden=false; $("email").focus(); };
$("lg").onclick=e=>{ e.preventDefault(); L=L==="pt"?"en":"pt"; try{ localStorage.setItem("hydro_lang",L); }catch(e){} paint(); };
paint(); $("email").focus();
</script></body></html>`, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-frame-options": "DENY", "referrer-policy": "no-referrer" } });
}

/* o = { site: "aulas" | "exames", title: {pt, en}, logo: "/caminho/logo.png", hours: duração da sessão,
         passEmail + checkPass (opcional): async (env, email, code, volta) => true | false | "not_allowed" (email fora da lista) | "admin" — códigos do professor com o email do aluno,
         db: env => base D1, sessionUntil (opcional): async (email, env) => fim máximo da sessão (ms) ou null, mayRequest: async (email, env, volta) => true | false | "admin" (quem pode pedir um código:
         verificado ANTES de enviar o email; volta = página que a pessoa quer abrir, p. ex. /Hidrogeografia/ ou /p/123456) } */
export function makeLogin(o) {
  const cache = new Map();   // token_hash → {email, exp, at}: evita ler a base em todos os pedidos (2 min)
  const sessionMs = (o.hours || 12) * 3600e3;

  async function sessionEmail(req, env) {
    const tok = cookies(req)[COOKIE]; if (!tok || !/^[A-Za-z0-9_-]{30,100}$/.test(tok)) return null;
    const DB = o.db(env); if (!DB) return null;
    const th = await sha(tok), now = Date.now(), c = cache.get(th);
    if (c && now - c.at < 120e3) return c.exp > now ? c.email : null;
    try {
      await tables(DB);
      const r = await DB.prepare("SELECT email, expires_at FROM login_sessions WHERE token_hash=?").bind(th).first();
      if (cache.size > 5000) cache.clear();
      cache.set(th, { email: r ? r.email : null, exp: r ? r.expires_at : 0, at: now });
      return r && r.expires_at > now ? r.email : null;
    } catch (e) { console.error("login session:", e && e.message || e); return null; }
  }

  /* quem é? → {email, via: "dev" | "access" | "login"} ou null */
  async function identify(req, env) {
    if (env.DEV_MODE === "1") { const d = normEmail(req.headers.get("x-dev-email") || env.DEV_EMAIL); if (d) return { email: d, via: "dev" }; }   // só em desenvolvimento local
    const a = await accessEmail(req, env); if (a) return { email: a, via: "access" };
    const s = await sessionEmail(req, env); if (s) return { email: s, via: "login" };
    return null;
  }

  async function newSession(env, email) {
    const DB = o.db(env), tok = randomToken(), now = Date.now();
    let exp = now + sessionMs;
    if (o.sessionUntil) { const u = await o.sessionUntil(email, env); if (u) exp = Math.min(exp, u); }   // acesso temporário: a sessão acaba com ele
    await tables(DB);
    await DB.prepare("INSERT INTO login_sessions (token_hash,email,site,created_at,expires_at) VALUES (?,?,?,?,?)").bind(await sha(tok), email, o.site, now, exp).run();
    try { await DB.prepare("DELETE FROM login_sessions WHERE expires_at < ?").bind(now).run(); } catch (e) {}   // limpeza das sessões antigas
    return `${COOKIE}=${tok}; Path=/; Max-Age=${Math.max(1, Math.floor((exp - now) / 1000))}; HttpOnly; Secure; SameSite=Lax`;
  }
  /* professor que entrou pelo Access numa página do professor: abre também uma sessão deste site, para as páginas de aluno
     (p. ex. «Testar como aluno») o reconhecerem mesmo sem o cookie do Access. Devolve o Set-Cookie ou null. */
  async function bridge(req, env, email) {
    if (!o.db(env) || await sessionEmail(req, env) === email) return null;
    try { return await newSession(env, email); } catch (e) { console.error("login bridge:", e && e.message || e); return null; }
  }

  async function requestCode(env, email, back) {
    if (!EMAIL_RE.test(email)) return { error: "invalid_email" };
    const may = await o.mayRequest(email, env, back); if (may === "admin") return { error: "use_admin" }; if (may !== true) return { error: "not_allowed" };
    const DB = o.db(env); if (!DB) return { error: "not_configured" };
    await tables(DB);
    const now = Date.now(), row = await DB.prepare("SELECT sent_at, sends, window_at FROM login_codes WHERE email=?").bind(email).first();
    if (row && row.sent_at && now - row.sent_at < RESEND_S * 1000) return { error: "wait", seconds: Math.ceil((row.sent_at + RESEND_S * 1000 - now) / 1000) };
    const inWin = row && row.window_at && now - row.window_at < 3600e3, sends = inWin ? row.sends : 0, windowAt = inWin ? row.window_at : now;
    if (sends >= SENDS_PER_HOUR) return { error: "too_many", minutes: Math.ceil((windowAt + 3600e3 - now) / 60000) };
    const g = await DB.prepare("SELECT COUNT(*) AS n FROM login_codes WHERE sent_at > ?").bind(now - 3600e3).first();
    if (g && g.n >= GLOBAL_PER_HOUR) return { error: "busy" };
    if (!env.EMAIL) return { error: "email_failed" };
    const code = randomCode();
    await DB.prepare(`INSERT INTO login_codes (email,code_hash,expires_at,tries,sent_at,sends,window_at) VALUES (?,?,?,0,?,?,?)
      ON CONFLICT(email) DO UPDATE SET code_hash=excluded.code_hash,expires_at=excluded.expires_at,tries=0,sent_at=excluded.sent_at,sends=excluded.sends,window_at=excluded.window_at`)
      .bind(email, await codeHash(email, code), now + CODE_MIN * 60e3, now, sends + 1, windowAt).run();
    try {
      const T = o.title;
      await env.EMAIL.send({
        to: email, from: env.LOGIN_FROM || "codigo@acesso.diogocosta.org", subject: `${code} — código de acesso / access code`,
        text: `O seu código de acesso é ${code} (válido ${CODE_MIN} minutos).\nSe não pediu este código, ignore este email.\n\nYour access code is ${code} (valid for ${CODE_MIN} minutes).\nIf you did not ask for it, ignore this email.\n\n— ${T.pt} · Universidade de Évora`,
        html: `<div style="font:15px/1.5 Arial,sans-serif;color:#0A2239"><p>O seu código de acesso é<br><b style="font:700 28px/1.4 monospace;letter-spacing:4px">${code}</b><br>(válido ${CODE_MIN} minutos). Se não pediu este código, ignore este email.</p>
<p style="color:#5B7183">Your access code is <b>${code}</b> (valid for ${CODE_MIN} minutes). If you did not ask for it, ignore this email.</p><p style="color:#5B7183;font-size:13px">— ${esc(T.pt)} · Universidade de Évora</p></div>`,
      });
    } catch (e) {
      console.error("login email:", e && (e.code || e.message) || e);
      try { await DB.prepare("UPDATE login_codes SET sent_at=NULL, sends=? WHERE email=?").bind(sends, email).run(); } catch (e2) {}   // pode tentar de novo já
      return { error: "email_failed" };
    }
    return { ok: true };
  }

  async function verify(env, email, code) {
    if (!EMAIL_RE.test(email) || !/^\d{6}$/.test(code)) return { error: "wrong", left: 0 };
    const DB = o.db(env); if (!DB) return { error: "not_configured" };
    await tables(DB);
    const now = Date.now();
    /* cada tentativa conta ANTES de comparar (atómico: pedidos em paralelo não passam do limite) */
    const r = await DB.prepare("UPDATE login_codes SET tries=tries+1 WHERE email=? AND tries<? AND expires_at>? AND code_hash<>'' RETURNING code_hash, tries").bind(email, MAX_TRIES, now).first();
    if (!r) {
      const row = await DB.prepare("SELECT tries, expires_at, code_hash FROM login_codes WHERE email=?").bind(email).first();
      return row && row.code_hash && row.expires_at > now ? { error: "locked" } : { error: "expired" };
    }
    if (r.code_hash !== await codeHash(email, code)) return r.tries >= MAX_TRIES ? { error: "locked" } : { error: "wrong", left: MAX_TRIES - r.tries };
    await DB.prepare("UPDATE login_codes SET code_hash='', expires_at=0, tries=0 WHERE email=?").bind(email).run();   // cada código só serve uma vez
    return { ok: true, cookie: await newSession(env, email) };
  }

  /* códigos gerados pelo professor (se o email falhar): válidos 3 h, substituem o código pendente. Nunca para emails de professor. */
  async function issueCodes(env, emails, minutes) {
    const DB = o.db(env); if (!DB) throw new Error("login_db_missing");
    await tables(DB);
    const now = Date.now(), exp = now + Math.max(5, Math.min(24 * 60, +minutes || ADMIN_CODE_MIN)) * 60e3, out = [], seen = new Set();
    for (const raw of emails || []) {
      const email = normEmail(raw); if (!EMAIL_RE.test(email) || seen.has(email)) continue; seen.add(email);
      if (await o.mayRequest(email, env) === "admin") { out.push({ email, error: "admin" }); continue; }
      const code = randomPass();
      await DB.prepare("DELETE FROM login_passes WHERE email=? OR expires_at<?").bind(email, now).run();   // o código anterior desta pessoa deixa de valer
      await DB.prepare("INSERT INTO login_passes (code_hash,email,expires_at) VALUES (?,?,?)").bind(await passHash(code), email, exp).run();
      out.push({ email, code, expiresAt: exp });
    }
    return out;
  }
  /* entrar só com o código do professor (sem email): um só uso; limite de tentativas erradas por endereço IP */
  async function usePass(env, raw, ip, email, back) {
    const DB = o.db(env); if (!DB) return { error: "not_configured" };
    await tables(DB);
    const now = Date.now(), code = normPass(raw);
    const f = ip ? await DB.prepare("SELECT window_at, n FROM login_fails WHERE ip=?").bind(ip).first() : null;
    if (f && now - f.window_at < 3600e3 && f.n >= PASS_FAILS) return { error: "too_many_tries" };
    let row = null;
    if (o.checkPass) {   // exames: email do aluno + código de entrada da prova (escrito pelo professor); primeiro o email, depois o código
      if (!EMAIL_RE.test(email)) return { error: "invalid_email" };
      const ok = await o.checkPass(env, email, code, back);
      if (ok === "admin") return { error: "use_admin" };
      if (ok === "not_allowed") return { error: "not_allowed" };
      if (code.length !== 8) return { error: "pass_format_exam" };
      if (ok === true) row = { email };
    } else if (code.length === 8) row = await DB.prepare("DELETE FROM login_passes WHERE code_hash=? AND expires_at>? RETURNING email").bind(await passHash(code), now).first();
    if (!row) {
      if (ip) await DB.prepare(`INSERT INTO login_fails (ip,window_at,n) VALUES (?,?,1) ON CONFLICT(ip) DO UPDATE SET
        n=CASE WHEN ?-login_fails.window_at<3600000 THEN login_fails.n+1 ELSE 1 END, window_at=CASE WHEN ?-login_fails.window_at<3600000 THEN login_fails.window_at ELSE ? END`).bind(ip, now, now, now, now).run();
      return { error: "wrong_pass" };
    }
    return { ok: true, cookie: await newSession(env, row.email) };
  }
  /* sessões ativas deste site para uma lista de emails → {email: fim}; e terminá-las (o aluno volta a ter de entrar) */
  async function activeSessions(env, emails) {
    const DB = o.db(env), out = {}; if (!DB || !emails.length) return out; await tables(DB);
    for (let i = 0; i < emails.length; i += 80) {
      const part = emails.slice(i, i + 80), r = await DB.prepare(`SELECT email, MAX(expires_at) AS until FROM login_sessions WHERE site=? AND expires_at>? AND email IN (${part.map(() => "?").join(",")}) GROUP BY email`).bind(o.site, Date.now(), ...part).all();
      (r.results || []).forEach(x => { out[x.email] = x.until; });
    }
    return out;
  }
  async function endSessions(env, emails) {
    const DB = o.db(env); if (!DB || !emails.length) return; await tables(DB);
    for (let i = 0; i < emails.length; i += 80) { const part = emails.slice(i, i + 80); await DB.prepare(`DELETE FROM login_sessions WHERE site=? AND email IN (${part.map(() => "?").join(",")})`).bind(o.site, ...part).run(); }
    cache.clear();   // nesta instância já; nas outras, em ≤ 2 min
  }
  /* terminar já o acesso de alguém: códigos pendentes (do email e do professor) e, se sessions, as sessões deste site */
  async function revoke(env, email, sessions) {
    const DB = o.db(env); if (!DB) return; await tables(DB);
    await DB.prepare("DELETE FROM login_passes WHERE email=?").bind(email).run();
    await DB.prepare("UPDATE login_codes SET code_hash='', expires_at=0 WHERE email=?").bind(email).run();
    if (sessions) { await DB.prepare("DELETE FROM login_sessions WHERE email=? AND site=?").bind(email, o.site).run(); cache.clear(); }
  }

  /* página de entrada para quem não tem sessão (GET) — os outros pedidos recebem 401 */
  function toLogin(req, url) {
    if (req.method !== "GET" && req.method !== "HEAD") return json({ error: "unauthenticated" }, 401);
    return new Response(null, { status: 302, headers: { location: "/entrar?volta=" + encodeURIComponent(url.pathname + url.search), "cache-control": "no-store" } });
  }

  /* rotas /entrar… e /sair; devolve null para todos os outros pedidos */
  async function handle(req, env, url) {
    const p = url.pathname.replace(/\/+$/, "") || "/";
    if (p === "/entrar" && (req.method === "GET" || req.method === "HEAD")) return loginPage(o, safeBack(url.searchParams.get("volta")), o.logo);
    if (p === "/entrar/codigo" || p === "/entrar/verificar" || p === "/entrar/passe") {
      if (req.method !== "POST") return json({ error: "bad_request" }, 405);
      const origin = req.headers.get("origin");
      if ((origin && origin !== url.origin) || !String(req.headers.get("content-type") || "").includes("application/json")) return json({ error: "bad_request" }, 403);   // só a partir da própria página
      let body = {}; try { body = await req.json(); } catch (e) {}
      const email = normEmail(body.email);
      try {
        if (p === "/entrar/codigo") { const r = await requestCode(env, email, safeBack(body.volta)); return json(r, r.ok ? 200 : 400); }
        if (p === "/entrar/passe") { const r = await usePass(env, body.code, req.headers.get("cf-connecting-ip"), email, safeBack(body.volta)); return r.ok ? json({ ok: true }, 200, { "set-cookie": r.cookie }) : json(r, 400); }
        const r = await verify(env, email, String(body.code || "").replace(/\D/g, ""));
        return r.ok ? json({ ok: true }, 200, { "set-cookie": r.cookie }) : json(r, 400);
      } catch (e) { console.error("login:", e && e.stack || e); return json({ error: "not_configured" }, 500); }
    }
    if (p === "/sair") {
      const tok = cookies(req)[COOKIE], DB = o.db(env);
      if (tok && DB) { try { const th = await sha(tok); cache.delete(th); await tables(DB); await DB.prepare("DELETE FROM login_sessions WHERE token_hash=?").bind(th).run(); } catch (e) {} }
      const back = safeBack(url.searchParams.get("volta"));
      const to = cookies(req).CF_Authorization ? "/cdn-cgi/access/logout" : back !== "/" ? "/entrar?volta=" + encodeURIComponent(back) : "/";
      return new Response(null, { status: 302, headers: { location: to, "cache-control": "no-store", "set-cookie": `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax` } });
    }
    return null;
  }

  return { identify, handle, toLogin, bridge, issueCodes, revoke, activeSessions, endSessions, _test: { requestCode, verify, safeBack, COOKIE } };
}
export { randomPass, normPass };
export const LOGIN_DOMAINS = env => String(env.LOGIN_DOMAINS || "alunos.uevora.pt,uevora.pt").split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
export const inDomains = (email, env) => LOGIN_DOMAINS(env).includes(String(email).split("@")[1] || "");
export const _test = { safeBack, COOKIE };
