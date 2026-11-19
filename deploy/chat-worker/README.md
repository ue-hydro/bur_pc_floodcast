# Floodcast Q&A chat (Cloudflare Worker + Claude)

A small "Ask about the forecast" chat box on the public site. Visitors' questions go to this Worker,
which answers with Claude **using only the latest forecast** (`<site>/api/chat_context.json`, written by
every site build). The Anthropic API key lives in the Worker as a secret and never reaches the browser.

```
browser chat box ──POST /chat──► Worker (this folder) ──► Claude API (key = Worker secret)
                                     └── reads <SITE_URL>/api/chat_context.json (cached 5 min)
```

## Costs and limits
- Model: `CHAT_MODEL` in `wrangler.toml`, default **Claude Haiku 5.5** (~€0.001 per question; a few euros in a
  busy month). Switch to `claude-sonnet-5-5` / `claude-opus-5-5` for richer answers at 20–40× the cost.
- **Set a monthly spend limit in the Anthropic console** - that is the hard cost cap.
- The Worker also limits each visitor (`MAX_PER_IP_PER_HOUR`) and the daily total (`MAX_PER_DAY`), answers in
  ≤ ~120 words, and refuses off-topic questions. Cloudflare's free Workers plan covers the traffic.

## 1. Get an API key
Create an account at platform.claude.com (separate from a claude.ai subscription), add billing,
set the spend limit, create an API key.

## 2. Test on the laptop
```bash
cd deploy/chat-worker
npm install
cp .dev.vars.example .dev.vars      # then put your key in .dev.vars (git-ignored)
```
`run/start.sh` starts the Worker on http://localhost:8787 automatically: with a key in `.dev.vars` it calls
Claude, without one it runs in **mock mode** (echoes the forecast numbers) so the plumbing can be tested.
The laptop config `run/laptop.toml` has `chat_endpoint = "http://localhost:8787/chat"`.

## 3. Deploy
```bash
npx wrangler login
npx wrangler secret put ANTHROPIC_API_KEY
# edit wrangler.toml: SITE_URL = public site URL, ALLOWED_ORIGINS = that origin
npx wrangler deploy                 # prints https://floodcast-chat.<account>.workers.dev
```
Then set `[publish] chat_endpoint = "https://floodcast-chat.<account>.workers.dev/chat"` in the operational
TOML; the next site build shows the chat box. Leave `chat_endpoint` empty to remove it.

## Privacy
Questions are sent to Anthropic to generate the answer; the Worker stores nothing (no logs of questions or
IPs beyond in-memory rate-limit counters). Say so in the site's "Method and limitations" page before going public.
