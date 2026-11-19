# Online test deployment (Cloudflare) — private, auto-updating

```
GitHub branch ──(push)──► forecast machine (floodcast_master.py --auto-update)
                              │ pulls new commits every ~10 min, restarts on them
                              │ runs the forecast chain every 6 h
                              └─ deploy/publish.sh ──► Cloudflare Worker "floodcast-alcacer" (static site)
                                                  └──► Workers KV  → Worker "floodcast-chat" (Q&A)
```

* Site: Worker with static assets, `deploy/site-worker/wrangler.toml` (free plan: static requests are
  unlimited; ~25 MB / ~800 files). URL: `https://floodcast-alcacer.<subdomain>.workers.dev` (or a custom
  domain such as `floodcast.diogocosta.org`, like the lectures site).
* Chat: `deploy/chat-worker` (Worker + KV). It reads the forecast summary from KV, so it works while the
  site is behind Access. Mock answers until the `ANTHROPIC_API_KEY` secret is set:
  `npx wrangler secret put ANTHROPIC_API_KEY` in `deploy/chat-worker`, then `CHAT_MOCK = "0"` and deploy.

## Private access (only you)
Cloudflare dashboard → Workers & Pages → **floodcast-alcacer** → Settings → Domains & Routes →
`workers.dev` → **Enable Cloudflare Access**. Then Zero Trust → Access → Applications → the new
application → Policies → edit: *Include → Emails →* your university address. (Preview URLs are disabled
in `wrangler.toml`, so there is no unprotected URL.) When done: `touch run/.access_confirmed` — only then
does `deploy/publish.sh` publish the real site.

## Making changes
Commit and push to the tracked GitHub branch. The forecast machine pulls the change within ~10 min,
restarts, rebuilds the site with the latest forecast and publishes it. New forecasts are published
automatically after every cycle.

## Moving to the university server later
Clone the repo on a Linux server with Docker, copy `run/laptop.toml` to a server config (real thresholds),
`npx wrangler login` once (or a Cloudflare API token in `CLOUDFLARE_API_TOKEN`), and run
`run/start.sh` there (or the systemd unit in `deploy/`). The site can stay on Cloudflare (resilient under
load during a flood) or be served by the university with `deploy/Caddyfile`.

## Current pipeline (since 2026-11-19)

laptop loop → `deploy/publish.sh` → force-push (single commit) to the PRIVATE repo
`ue-hydro/bur_pc_floodcast_site` → Cloudflare Workers Builds (Worker `floodcast-alcacer`, branch `main`,
deploy command `npx wrangler deploy`) → https://floodcast.diogocosta.org (email-code login, only
`diogo.costa@uevora.pt`).

Laptop markers in `run/` (git-ignored):
- `.access_confirmed` — publishing allowed at all.
- `.cloudflare_builds_connected` — Cloudflare builds from GitHub, so no direct `wrangler deploy` from the laptop.
  Delete it to fall back to direct uploads.
The code repo `ue-hydro/bur_pc_floodcast` is PUBLIC: results never go there. `publish.sh` refuses to push
if the site repo is not private.
