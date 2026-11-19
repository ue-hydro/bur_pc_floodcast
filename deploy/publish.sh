#!/bin/bash
# Publish the freshly built site to Cloudflare (Worker with static assets) and hand the latest
# forecast summary to the Q&A chat Worker (KV). Called by floodcast_master.py after every site
# build via `[publish] post_command = "bash <repo>/deploy/publish.sh"`.
#
# Safety: nothing is published until the site is protected with Cloudflare Access — create
# `run/.access_confirmed` once Access is on (see deploy/CLOUDFLARE.md).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SITE="${FLOODCAST_SITE:-$REPO/run/site}"
WR="npx --yes --prefix $REPO/deploy/chat-worker wrangler"
if [ ! -f "$REPO/run/.access_confirmed" ]; then
  echo "publish skipped: Cloudflare Access not confirmed (touch run/.access_confirmed)" >&2
  exit 0
fi
cd "$REPO"
$WR deploy --config deploy/site-worker/wrangler.toml --assets "$SITE" >/dev/null
if [ -f "$SITE/api/chat_context.json" ]; then
  $WR kv key put --binding=CTX --config deploy/chat-worker/wrangler.toml --remote chat_context \
      --path "$SITE/api/chat_context.json" >/dev/null 2>&1 || echo "chat context upload failed" >&2
fi
echo "published $(date -u +%FT%TZ)"
