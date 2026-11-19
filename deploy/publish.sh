#!/bin/bash
# Publish the freshly built site. Called by floodcast_master.py after every site build via
# `[publish] post_command = "bash <repo>/deploy/publish.sh"`.
#
#   1. GitHub: the built site + the login Worker go to the PRIVATE repo ue-hydro/bur_pc_floodcast_site
#      (one force-pushed commit, so the repo never grows; nothing is pushed when nothing changed).
#      Cloudflare Workers Builds, connected to that repo, deploys it to floodcast.diogocosta.org.
#   2. Until that connection exists (no run/.cloudflare_builds_connected) we also deploy directly
#      with wrangler, so the online site is always current.
#   3. The chat Worker gets the latest forecast summary (KV).
#
# Safety: nothing is published until the login is confirmed working (run/.access_confirmed).
# The results repo MUST stay private (the code repo is public).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SITE="${FLOODCAST_SITE:-$REPO/run/site}"
SITE_REMOTE="${FLOODCAST_SITE_REMOTE:-https://github.com/ue-hydro/bur_pc_floodcast_site.git}"
SITE_REPO="$REPO/run/site_repo"
WR="npx --yes --prefix $REPO/deploy/chat-worker wrangler"
say() { echo "$(date '+%F %T') publish: $*"; }

if [ ! -f "$REPO/run/.access_confirmed" ]; then
  say "skipped (login not confirmed: touch run/.access_confirmed)"
  exit 0
fi
cd "$REPO"

# ---- 1. GitHub (private results repo) --------------------------------------------------------
CHANGED=1
SLUG="${SITE_REMOTE#https://github.com/}"; SLUG="${SLUG%.git}"
if [ "$(gh repo view "$SLUG" --json visibility -q .visibility 2>/dev/null)" != "PRIVATE" ]; then
  say "NOT pushing to GitHub: $SITE_REMOTE is not private (or unreachable)"
else
  if [ ! -d "$SITE_REPO/.git" ]; then
    git init -q -b main "$SITE_REPO"
    git -C "$SITE_REPO" remote add origin "$SITE_REMOTE"
  fi
  mkdir -p "$SITE_REPO/site" "$SITE_REPO/src"
  rsync -a --delete --exclude .DS_Store "$SITE/" "$SITE_REPO/site/"
  printf ".DS_Store\n" > "$SITE_REPO/.gitignore"; rm -f "$SITE_REPO/.DS_Store"
  rsync -a --delete "$REPO/deploy/site-worker/src/" "$SITE_REPO/src/"
  sed -e 's#^directory = .*#directory = "./site"#' "$REPO/deploy/site-worker/wrangler.toml" > "$SITE_REPO/wrangler.toml"
  cat > "$SITE_REPO/README.md" <<'EOF'
# Floodcast Alcácer do Sal — built site (PRIVATE)

Written automatically by the forecast loop (`deploy/publish.sh` in ue-hydro/bur_pc_floodcast).
Do not edit here: every forecast replaces this repository's single commit.
Cloudflare Workers Builds deploys it (`npx wrangler deploy`) to https://floodcast.diogocosta.org,
behind the email-code login. Keep this repository private until the ANEPC/APA review.
EOF
  cd "$SITE_REPO"
  git add -A
  if git rev-parse -q --verify HEAD >/dev/null && git diff --cached --quiet; then
    say "GitHub: no change"; CHANGED=0
  else
    cyc=$(python3 -c "import json;print(json.load(open('site/api/latest.json')).get('cycle','?'))" 2>/dev/null || echo "?")
    git checkout -q --orphan next
    git -c user.name="floodcast-bot" -c user.email="floodcast@localhost" commit -q -m "site for forecast $cyc ($(date -u +%FT%TZ))"
    git branch -D main -q 2>/dev/null || true
    git branch -m main
    ok=0
    for i in 1 2 3; do
      if err=$(git push -q -f origin main 2>&1); then ok=1; break; fi
      sleep $((10 * i))
    done
    if [ $ok = 1 ]; then say "GitHub: pushed (forecast $cyc)"; else say "GitHub push FAILED: $(echo "$err" | tail -1)"; fi
    git reflog expire --expire=now --all && git gc -q --prune=now
  fi
  cd "$REPO"
fi

# ---- 2. direct deploy until Cloudflare builds from GitHub ---------------------------------------
if [ ! -f "$REPO/run/.cloudflare_builds_connected" ] && [ $CHANGED = 1 ]; then
  if $WR deploy --config deploy/site-worker/wrangler.toml --assets "$SITE" >/dev/null 2>&1; then
    say "Cloudflare: deployed directly"
  else
    say "Cloudflare direct deploy FAILED"
  fi
fi

# ---- 3. chat context ---------------------------------------------------------------------------
if [ -f "$SITE/api/chat_context.json" ] && [ $CHANGED = 1 ]; then
  $WR kv key put --binding=CTX --config deploy/chat-worker/wrangler.toml --remote chat_context \
      --path "$SITE/api/chat_context.json" >/dev/null 2>&1 || say "chat context upload failed"
fi
