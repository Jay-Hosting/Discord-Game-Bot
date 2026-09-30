# Tidy Up Everything — Game Finder

Finds new/upcoming/on-sale cleaning & tidying games and writes them to
`docs/candidates.json` once a day via GitHub Actions. It does **not** post to
Discord directly — instead, a Claude-hosted checklist page reads this file so
you (and your mods) can approve which games actually get posted. Posting
itself happens later, on a schedule, once something is approved.

## One-time setup

1. **Push these files** to your `Discord-Game-Bot` repo (replacing the old
   `post_releases.py` / `.github/workflows/daily-release-post.yml`, which are
   no longer used — delete them if they're still there).
2. **Set the `RAWG_API_KEY` secret**: repo → Settings → Secrets and variables →
   Actions → New repository secret. (The old `DISCORD_WEBHOOK_URL` secret is
   no longer needed by this script and can be removed.)
3. **Enable GitHub Pages**: repo → Settings → Pages → Build and deployment →
   Source: "Deploy from a branch" → Branch: `main`, folder `/docs` → Save.
   This is what makes `docs/candidates.json` reachable at
   `https://joldes-source.github.io/Discord-Game-Bot/candidates.json`
   (GitHub Pages URLs are case-sensitive, so the casing has to match the
   repo name exactly), which the daily automation reads to refresh the
   checklist page.
4. **Test it**: repo → Actions tab → "Find Daily Candidates" → "Run workflow".
   After it finishes, `docs/candidates.json` should be updated with real data.

## How the whole pipeline fits together

1. **GitHub Actions** (`find-candidates.yml`, runs daily at 6am Pacific) runs
   `find_candidates.py`, which checks RAWG + Steam and writes
   `docs/candidates.json` with three lists: `new_release`, `coming_soon`,
   `on_sale`.
2. **The checklist page** (a Claude artifact — you'll get the link
   separately) shows those candidates and lets you and your mods check the
   ones that should be posted.
3. **A daily scheduled Claude task** picks up anything approved and posts it
   to the right Discord channel, then marks it as posted so it won't post
   twice.

## Files

- `find_candidates.py` — the finder script.
- `config.json` — non-secret settings (date windows, popularity threshold,
  keyword list, the curated `extra_games` list for the on_sale pool, Steam
  country code).
- `docs/candidates.json` — the output GitHub Pages serves. Don't hand-edit
  this; it gets overwritten daily.
- `.github/workflows/find-candidates.yml` — the daily GitHub Actions job.
