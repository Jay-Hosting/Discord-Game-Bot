#!/usr/bin/env python3
"""
Cleaning/Tidying Game Candidate Finder
---------------------------------------
Replaces the old auto-posting digest (post_releases.py) with a
review-first pipeline: instead of posting straight to Discord, this
script finds candidate games and writes them to docs/candidates.json,
which GitHub Pages serves as a static file. A published checklist page
(a Claude Artifact) reads that file and lets you and your mods approve
which games actually get posted — the real posting happens later, on
a schedule, once something is approved.

Three categories, each meant for a different Discord channel:
  - new_release : a candidate game whose RAWG release date is TODAY
  - coming_soon : a candidate game releasing in the next N days
  - on_sale     : a candidate game (any release date) currently
                  discounted on Steam

Data sources:
  - RAWG (https://rawg.io/apidocs) for game discovery or release dates
  - Steam's storefront API for live pricing/discount data

Secrets: RAWG_API_KEY environment variable (no Discord webhook needed
here anymore - posting is a separate, later step).

Non-secret settings in config.json:
  - coming_soon_days, popularity_threshold, timezone, tags, keywords,
    keyword_scan_pages: same meaning as before (see the old
    post_releases.py header for details) - still used for the
    new_release / coming_soon categories.
  - extra_games: RAWG slugs for well-known genre titles that RAWG's
    tags don't reliably catch (e.g. "powerwash-simulator",
    "unpacking-2"). These are always included in the on_sale pool
    regardless of tags, since sale-checking depends on already
    knowing which games belong to the genre, not on a date window.
  - manual_steam_appids: Steam app ids (as strings) for games you want
    tracked directly, bypassing RAWG entirely. Use this for a game you
    know about that RAWG hasn't indexed yet (common for small/newer
    indie titles) - add its numeric Steam app id here (found in its
    store URL, e.g. store.steampowered.com/app/<APPID>/...) and it'll
    show up in coming_soon (or new_release, once Steam says it has
    actually released) regardless of what RAWG has. Remove it once
    RAWG catches up and it's showing up on its own, or once you're done
    tracking it.
  - steam_country: 2-letter country code for Steam pricing (default "us").
"""

import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"
OUTPUT_PATH = Path(__file__).parent / "docs" / "candidates.json"
RAWG_BASE = "https://api.rawg.io/api/games"
STEAM_APPDETAILS = "https://store.steampowered.com/api/appdetails"
STEAM_APPID_RE = re.compile(r"/app/(\d+)")

# Steam's storefront API is picky about requests that look like a script
# (the default python-requests User-Agent, no Accept/Language headers) -
# cloud CI runners (like GitHub Actions) get silently blocked/empty-results
# far more often than a normal browser-like request would. A real
# User-Agent fixes this in practice.
STEAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    rawg_api_key = os.environ.get("RAWG_API_KEY")
    if not rawg_api_key:
        sys.exit(
            "Missing secret. Set the RAWG_API_KEY environment variable "
            "(in GitHub: Settings > Secrets and variables > Actions; "
            "locally: export it before running)."
        )
    cfg["rawg_api_key"] = rawg_api_key
    return cfg


def today_in_tz(tz_name):
    return datetime.now(ZoneInfo(tz_name)).date()


def fetch_games(api_key, date_from, date_to, tags=None, page_size=40):
    params = {
        "key": api_key,
        "dates": f"{date_from.isoformat()},{date_to.isoformat()}",
        "ordering": "-added",
        "page_size": page_size,
        "exclude_additions": "true",
    }
    if tags:
        params["tags"] = ",".join(tags)
    resp = requests.get(RAWG_BASE, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("results", [])


def fetch_games_broad(api_key, date_from, date_to, max_pages=10, page_size=40):
    results = []
    params = {
        "key": api_key,
        "dates": f"{date_from.isoformat()},{date_to.isoformat()}",
        "ordering": "-added",
        "page_size": page_size,
        "exclude_additions": "true",
    }
    url = RAWG_BASE
    for _ in range(max_pages):
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        next_url = data.get("next")
        if not next_url:
            break
        url = next_url
        params = None
    return results


def fetch_all_tagged_games(api_key, tags, max_pages=15, page_size=40):
    """Fetch every game matching `tags`, NO date restriction. Used to build
    the pool of "known genre games" that on_sale checks run against."""
    if not tags:
        return []
    results = []
    params = {
        "key": api_key,
        "tags": ",".join(tags),
        "ordering": "-added",
        "page_size": page_size,
        "exclude_additions": "true",
    }
    url = RAWG_BASE
    for _ in range(max_pages):
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        next_url = data.get("next")
        if not next_url:
            break
        url = next_url
        params = None
    return results


def fetch_game_by_slug(api_key, slug):
    """Look up one game by its RAWG slug (used for `extra_games`)."""
    resp = requests.get(f"{RAWG_BASE}/{slug}", params={"key": api_key}, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def keyword_match(game, keywords):
    if not keywords:
        return False
    name = (game.get("name") or "").lower()
    return any(kw.lower() in name for kw in keywords)


def combine_matches(tag_games, broad_games, keywords):
    by_id = {g["id"]: g for g in tag_games if g.get("id") is not None}
    for g in broad_games:
        if g.get("id") is not None and keyword_match(g, keywords):
            by_id.setdefault(g["id"], g)
    return list(by_id.values())


def filter_and_rank(games, threshold, limit):
    filtered = [
        g for g in games
        if not g.get("tba") and (g.get("added") or 0) >= threshold
    ]
    filtered.sort(key=lambda g: g.get("added") or 0, reverse=True)
    return filtered[:limit]


def platform_names(game):
    plats = game.get("platforms") or []
    names = []
    for p in plats:
        name = (p.get("platform") or {}).get("name")
        if name:
            names.append(name)
    return names


def rawg_url(game):
    slug = game.get("slug", "")
    return f"https://rawg.io/games/{slug}" if slug else "https://rawg.io"


def get_steam_appid(api_key, rawg_game_id):
    """Look up a game's Steam store URL via RAWG's stores sub-resource and
    pull out the numeric Steam app id. Returns None if not on Steam."""
    resp = requests.get(
        f"{RAWG_BASE}/{rawg_game_id}/stores", params={"key": api_key}, timeout=15
    )
    if resp.status_code != 200:
        return None
    for entry in resp.json().get("results", []):
        if entry.get("store_id") == 1:  # 1 = Steam in RAWG's store list
            url = entry.get("url") or ""
            m = STEAM_APPID_RE.search(url)
            if m:
                return m.group(1)
    return None


def get_steam_price(appid, country="us"):
    """Returns the price_overview dict from Steam, or None (not found, free,
    or region-restricted). Retries once on a transient/blocked response,
    since cloud CI IPs occasionally get a 429/403 that a plain retry with
    a short pause clears up."""
    for attempt in range(2):
        try:
            resp = requests.get(
                STEAM_APPDETAILS,
                params={"appids": appid, "cc": country, "filters": "price_overview"},
                headers=STEAM_HEADERS,
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"  steam appdetails request error for {appid}: {e}")
            return None
        if resp.status_code == 200:
            data = resp.json().get(str(appid), {})
            if not data.get("success"):
                print(f"  steam appdetails: appid {appid} success=false (not found / delisted / region-locked)")
                return None
            return (data.get("data") or {}).get("price_overview")
        print(f"  steam appdetails: appid {appid} returned HTTP {resp.status_code} (attempt {attempt + 1})")
        if attempt == 0:
            time.sleep(1.5)
    return None


def get_steam_appdetails_full(appid, country="us"):
    """Full appdetails (name, release_date, platforms, price_overview) for
    one Steam app id, with no `filters` param so we get everything in one
    call. Used for the manual watchlist, which bypasses RAWG entirely -
    RAWG sometimes hasn't indexed a small/new indie game yet, so relying
    on RAWG alone can silently miss real upcoming titles. Returns None on
    any failure (same retry-once behavior as get_steam_price)."""
    for attempt in range(2):
        try:
            resp = requests.get(
                STEAM_APPDETAILS,
                params={"appids": appid, "cc": country},
                headers=STEAM_HEADERS,
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"  manual watchlist: request error for steam appid {appid}: {e}")
            return None
        if resp.status_code == 200:
            entry = resp.json().get(str(appid), {})
            if not entry.get("success"):
                print(f"  manual watchlist: steam appid {appid} success=false (delisted / wrong id?)")
                return None
            return entry.get("data")
        print(f"  manual watchlist: steam appid {appid} returned HTTP {resp.status_code} (attempt {attempt + 1})")
        if attempt == 0:
            time.sleep(1.5)
    return None


def build_manual_watchlist_entry(appid, data):
    """Build a candidate entry straight from Steam data for a manually
    curated appid, when RAWG doesn't have the game yet. Category is
    coming_soon while Steam still lists it as coming soon, and flips to
    new_release once Steam says it has actually released (the user can
    remove it from `manual_steam_appids` in config.json once it's been
    out for a while and RAWG has caught up)."""
    release_info = data.get("release_date") or {}
    is_coming_soon = bool(release_info.get("coming_soon"))
    category = "coming_soon" if is_coming_soon else "new_release"
    plat_flags = data.get("platforms") or {}
    platform_labels = {"windows": "PC", "mac": "macOS", "linux": "Linux"}
    platforms = [label for key, label in platform_labels.items() if plat_flags.get(key)]
    return {
        "id": f"{category}:steam-{appid}",
        "rawg_id": None,
        "category": category,
        "name": data.get("name", "Unknown title"),
        "platforms": platforms,
        "released": release_info.get("date"),
        "rawg_url": None,
        "steam_url": f"https://store.steampowered.com/app/{appid}/",
        "added": 9999,  # always show manually curated picks first
    }


def build_entry(game, category, extra=None):
    entry = {
        "id": f"{category}:{game['id']}",
        "rawg_id": game["id"],
        "category": category,
        "name": game.get("name", "Unknown title"),
        "platforms": platform_names(game),
        "released": game.get("released"),
        "rawg_url": rawg_url(game),
        "added": game.get("added") or 0,
    }
    if extra:
        entry.update(extra)
    return entry


def main():
    cfg = load_config()
    tz = cfg.get("timezone", "America/Los_Angeles")
    today = today_in_tz(tz)
    soon_start = today + timedelta(days=1)
    soon_end = today + timedelta(days=cfg.get("coming_soon_days", 7))

    tags = cfg.get("tags") or None
    keywords = cfg.get("keywords") or []
    scan_pages = cfg.get("keyword_scan_pages", 10)
    threshold = cfg.get("popularity_threshold", 5)
    steam_country = cfg.get("steam_country", "us")

    # --- new_release + coming_soon (date-windowed, same approach as before) ---
    today_tag_raw = fetch_games(cfg["rawg_api_key"], today, today, tags=tags)
    soon_tag_raw = fetch_games(cfg["rawg_api_key"], soon_start, soon_end, tags=tags)

    if keywords:
        today_broad_raw = fetch_games_broad(cfg["rawg_api_key"], today, today, max_pages=scan_pages)
        soon_broad_raw = fetch_games_broad(cfg["rawg_api_key"], soon_start, soon_end, max_pages=scan_pages)
    else:
        today_broad_raw, soon_broad_raw = [], []

    today_combined = combine_matches(today_tag_raw, today_broad_raw, keywords)
    soon_combined = combine_matches(soon_tag_raw, soon_broad_raw, keywords)

    new_release_games = filter_and_rank(today_combined, threshold, cfg.get("max_today", 10))
    coming_soon_games = filter_and_rank(soon_combined, threshold, cfg.get("max_coming_soon", 15))

    new_release = [build_entry(g, "new_release") for g in new_release_games]
    coming_soon = [build_entry(g, "coming_soon") for g in coming_soon_games]

    # --- manual watchlist (bypasses RAWG entirely, for games RAWG hasn't
    # indexed yet - straight from Steam by app id) ---
    for appid in cfg.get("manual_steam_appids", []):
        data = get_steam_appdetails_full(str(appid), steam_country)
        time.sleep(0.2)
        if not data:
            continue
        entry = build_manual_watchlist_entry(str(appid), data)
        if entry["category"] == "coming_soon":
            coming_soon.insert(0, entry)
        else:
            new_release.insert(0, entry)

    # --- on_sale (whole known-genre pool, no date window) ---
    pool = fetch_all_tagged_games(cfg["rawg_api_key"], tags)
    pool_by_id = {g["id"]: g for g in pool if g.get("id") is not None}

    for slug in cfg.get("extra_games", []):
        g = fetch_game_by_slug(cfg["rawg_api_key"], slug)
        if g and g.get("id") is not None:
            pool_by_id.setdefault(g["id"], g)

    on_sale = []
    for game in pool_by_id.values():
        if game.get("tba"):
            continue
        appid = get_steam_appid(cfg["rawg_api_key"], game["id"])
        time.sleep(0.2)  # be polite to both APIs
        if not appid:
            print(f"  no Steam listing found via RAWG for {game.get('name')!r} (rawg id {game['id']})")
            continue
        price = get_steam_price(appid, steam_country)
        time.sleep(0.2)
        if not price:
            print(f"  {game.get('name')!r} (steam appid {appid}): no price data returned")
            continue
        if not price.get("discount_percent"):
            print(f"  {game.get('name')!r} (steam appid {appid}): not currently discounted")
            continue
        on_sale.append(build_entry(game, "on_sale", extra={
            "steam_appid": appid,
            "steam_url": f"https://store.steampowered.com/app/{appid}/",
            "discount_percent": price.get("discount_percent"),
            "initial_formatted": price.get("initial_formatted"),
            "final_formatted": price.get("final_formatted"),
        }))
    on_sale.sort(key=lambda e: e.get("discount_percent", 0), reverse=True)

    output = {
        "generated_at": datetime.now(ZoneInfo(tz)).isoformat(),
        "timezone": tz,
        "categories": {
            "new_release": new_release,
            "coming_soon": coming_soon,
            "on_sale": on_sale,
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(
        f"Wrote {len(new_release)} new_release, {len(coming_soon)} coming_soon, "
        f"{len(on_sale)} on_sale candidates to {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
