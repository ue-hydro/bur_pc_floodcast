/* Floodcast site Worker: static files (the built site) behind the SAME email-code login as the lectures
   and exams sites (src/login.js is a verbatim copy of LECTURE_MATERIAL/worker/login.js — keep them equal).
   Only the emails in ALLOWED_EMAILS can request a code and see the site. Codes and sessions live in the
   shared D1 database «exames» (site = "floodcast"). */
import { makeLogin } from "./login.js";

const allowed = env => String(env.ALLOWED_EMAILS || "").split(",").map(s => s.trim().toLowerCase()).filter(Boolean);

const LOGIN = makeLogin({
  site: "floodcast",
  title: { pt: "Floodcast Alcácer do Sal (teste)", en: "Floodcast Alcácer do Sal (test)" },
  logo: "/assets/logos/uevora.png",
  hours: 24 * 30,
  db: env => env.LOGIN_DB,
  mayRequest: async (email, env) => allowed(env).includes(email),
});

const PUBLIC = /^\/(assets\/logos\/[\w.-]+|favicon\.ico)$/;   // the login page shows the logo

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/robots.txt") return new Response("User-agent: *\nDisallow: /\n", { headers: { "content-type": "text/plain" } });
    const r = await LOGIN.handle(req, env, url);
    if (r) return r;
    if (!PUBLIC.test(url.pathname)) {
      const who = await LOGIN.identify(req, env);
      if (!who || !allowed(env).includes(who.email)) return LOGIN.toLogin(req, url);
    }
    const res = await env.ASSETS.fetch(req);
    const out = new Response(res.body, res);
    out.headers.set("x-robots-tag", "noindex, nofollow");
    out.headers.set("cache-control", "private, no-store");
    return out;
  },
};
