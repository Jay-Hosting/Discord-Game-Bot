#!/usr/bin/env python3
"""
Cleaning/Tidying Game Candidate Finder (Steam-based)
----------------------------------------------------
Finds candidate games straight from Steam's store search and writes them to
docs/candidates.json, which GitHub Pages serves. A Claude checklist page
shows them so you and your mods can approve which ones get posted; a daily
scheduled task posts the approved ones to Discord.

A game is a candidate if EITHER:
  - it carries one of the Steam tags in `steam_tag_ids` (default: "Cleaning"), or
  - its title contains one of the words in `name_keywords` (default: "tidy").

Three categories, each meant for a different Discord channel:
  - new_release : Steam release date is TODAY
  - coming_soon : Steam release date is an exact day within the next
                  `coming_soon_days` days (vague dates like "Q4 2026" or
                  "October 2026" are skipped until the developer sets a day)
  - on_sale     : currently discounted on Steam (any release date)

Only full games are included (Steam's "Games" category) - no demos, DLC,
or soundtracks.

Settings (config.json):
  - timezone, coming_soon_days, steam_country
  - steam_tag_ids: {"Tag name": numeric Steam tag id}
  - name_keywords: words that count if they appear in a game's title
  - max_new_release, max_coming_soon, max_on_sale: caps per category
  - max_pages_per_search: 50 results per page; safety limit
  - manual_steam_appids: Steam app ids to always include even if they don't
    match the tags/keywords (e.g. a genre game the developer tagged oddly)

No API keys needed - Steam's store search is public.
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    # The workflow installs this, but install it here too so an older
    # workflow file (that only installs `requests`) still works.
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "beautifulsoup4"])
    from bs4 import BeautifulSoup

CONFIG_PATH = Path(__file__).parent / "config.json"
OUTPUT_PATH = Path(__file__).parent / "docs" / "candidates.json"
STEAM_SEARCH = "https://store.steampowered.com/search/results/"
STEAM_APPDETAILS = "https://store.steampowered.com/api/appdetails"
GAMES_ONLY = "998"  # Steam search category for full games (excludes demos/DLC/soundtracks)
PAGE_SIZE = 50

# Steam is picky about requests that look like a script; cloud CI runners
# (like GitHub Actions) get blocked or empty results far more often without
# a normal browser-like User-Agent.
STEAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DATE_FORMATS = ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B, %Y")
PLATFORM_LABELS = {"win": "PC", "mac": "macOS", "linux": "Linux"}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def parse_steam_date(text):
    """Exact-day Steam dates ("Oct 2, 2026") -> date. Vague ones
    ("Q4 2026", "October 2026", "Coming soon") -> None."""
    text = (text or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def steam_search(params, country, max_pages):
    """Run one Steam store search and yield parsed result rows, page by page.
    Each row: appid, name, released_text, released (date or None),
    platforms, discount_percent, initial_formatted, final_formatted."""
    base = {"cc": country, "l": "english", "infinite": "1",
            "category1": GAMES_ONLY, "count": str(PAGE_SIZE)}
    base.update(params)
    offset = 0
    for page in range(max_pages):
        query = dict(base, start=str(offset))
        data = None
        for attempt in range(2):
            try:
                resp = requests.get(STEAM_SEARCH, params=query, headers=STEAM_HEADERS, timeout=20)
            except requests.RequestException as e:
                print(f"  steam search error ({params}): {e}")
                return
            if resp.status_code == 200:
                data = resp.json()
                break
            print(f"  steam search HTTP {resp.status_code} ({params}), attempt {attempt + 1}")
            time.sleep(3)
        if not data:
            return

        soup = BeautifulSoup(data.get("results_html", ""), "html.parser")
        rows = soup.select("a.search_result_row")
        for a in rows:
            itemkey = a.get("data-ds-itemkey", "")
            if not itemkey.startswith("App_"):
                continue  # skip bundles/packages
            appid = itemkey[len("App_"):]
            title = a.select_one(".title")
            released_el = a.select_one(".search_released")
            released_text = released_el.get_text(strip=True) if released_el else ""
            platforms = [
                label for cls, label in PLATFORM_LABELS.items()
                if a.select_one(f".platform_img.{cls}")
            ]
            discount = a.select_one(".discount_block")
            pct = int(discount.get("data-discount", "0") or 0) if discount else 0
            orig = a.select_one(".discount_original_price")
            final = a.select_one(".discount_final_price")
            yield {
                "appid": appid,
                "name": title.get_text(strip=True) if title else "Unknown title",
                "released_text": released_text,
                "released": parse_steam_date(released_text),
                "platforms": platforms,
                "discount_percent": pct,
                "initial_formatted": orig.get_text(strip=True) if orig else None,
                "final_formatted": final.get_text(strip=True) if final else None,
            }

        # Steam may return fewer rows per page than asked for, so advance by
        # what actually came back and stop only when nothing is left.
        offset += len(rows)
        total = int(data.get("total_count") or 0)
        if not rows or offset >= total:
            return
        time.sleep(0.75)  # be polite


def search_sources(cfg):
    """The (label, params) pairs every category search runs over: one per
    Steam tag, one per title keyword."""
    sources = []
    for tag_name, tag_id in (cfg.get("steam_tag_ids") or {}).items():
        sources.append((f"Steam tag: {tag_name}", {"tags": str(tag_id)}, None))
    for kw in cfg.get("name_keywords") or []:
        # Steam's term search also matches descriptions/tags, so the keyword
        # is re-checked against the title itself (the `required` part).
        sources.append((f"Title contains '{kw}'", {"term": kw}, kw.lower()))
    return sources


def collect(cfg, extra_params, keep, stop_after=None):
    """Run every source with extra_params, keep rows where keep(row) is true,
    dedupe by appid (first match wins, sources are noted)."""
    found = {}
    for label, params, required_word in search_sources(cfg):
        for row in steam_search({**params, **extra_params}, cfg.get("steam_country", "us"),
                                cfg.get("max_pages_per_search", 6)):
            if stop_after and stop_after(row):
                break
            if required_word and required_word not in row["name"].lower():
                continue
            if not keep(row):
                continue
            if row["appid"] in found:
                if label not in found[row["appid"]]["sources"]:
                    found[row["appid"]]["sources"].append(label)
            else:
                found[row["appid"]] = {**row, "sources": [label]}
        time.sleep(0.75)
    return list(found.values())


def build_entry(row, category, sort_key):
    entry = {
        "id": f"{category}:steam-{row['appid']}",
        "category": category,
        "name": row["name"],
        "platforms": row["platforms"],
        "released": row["released_text"],
        "rawg_url": None,
        "steam_url": f"https://store.steampowered.com/app/{row['appid']}/",
        "steam_appid": row["appid"],
        "matched_by": row.get("sources", []),
        "added": sort_key,  # the checklist page sorts each section by this, highest first
    }
    if category == "on_sale":
        entry.update({
            "discount_percent": row["discount_percent"],
            "initial_formatted": row["initial_formatted"],
            "final_formatted": row["final_formatted"],
        })
    return entry


def fetch_manual_app(appid, country):
    """Full appdetails for one manually listed app id -> a search-row-shaped dict."""
    try:
        resp = requests.get(STEAM_APPDETAILS, params={"appids": appid, "cc": country},
                            headers=STEAM_HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"  manual app {appid}: request error {e}")
        return None
    if resp.status_code != 200:
        print(f"  manual app {appid}: HTTP {resp.status_code}")
        return None
    entry = resp.json().get(str(appid), {})
    if not entry.get("success"):
        print(f"  manual app {appid}: not found on Steam")
        return None
    d = entry.get("data") or {}
    rel = d.get("release_date") or {}
    price = d.get("price_overview") or {}
    plats = d.get("platforms") or {}
    return {
        "appid": str(appid),
        "name": d.get("name", "Unknown title"),
        "released_text": rel.get("date", ""),
        "released": parse_steam_date(rel.get("date")),
        "coming_soon": bool(rel.get("coming_soon")),
        "platforms": [PLATFORM_LABELS[k] for k in ("win", "mac", "linux")
                      if plats.get({"win": "windows"}.get(k, k))],
        "discount_percent": price.get("discount_percent") or 0,
        "initial_formatted": price.get("initial_formatted"),
        "final_formatted": price.get("final_formatted"),
        "sources": ["Manual watchlist"],
    }


def main():
    cfg = load_config()
    tz = cfg.get("timezone", "America/Los_Angeles")
    today = datetime.now(ZoneInfo(tz)).date()
    window_end = today + timedelta(days=cfg.get("coming_soon_days", 30))

    # --- coming soon: exact release day within the window ---
    print(f"Coming soon: {today + timedelta(days=1)} .. {window_end}")
    soon_rows = collect(
        cfg, {"filter": "comingsoon"},
        keep=lambda r: r["released"] is not None and today < r["released"] <= window_end,
        # results come back soonest-first, so stop paging once past the window
        stop_after=lambda r: r["released"] is not None and r["released"] > window_end,
    )
    soon_rows.sort(key=lambda r: r["released"])
    coming_soon = [build_entry(r, "coming_soon", 1000 - (r["released"] - today).days)
                   for r in soon_rows[: cfg.get("max_coming_soon", 60)]]

    # --- new releases: released today (newest-first search, stop once past today) ---
    print(f"New releases: {today}")
    new_rows = collect(
        cfg, {"sort_by": "Released_DESC"},
        keep=lambda r: r["released"] == today,
        stop_after=lambda r: r["released"] is not None and r["released"] < today,
    )
    new_release = [build_entry(r, "new_release", 1000)
                   for r in new_rows[: cfg.get("max_new_release", 25)]]

    # --- on sale: currently discounted ---
    print("On sale")
    sale_rows = collect(cfg, {"specials": "1"}, keep=lambda r: r["discount_percent"] > 0)
    sale_rows.sort(key=lambda r: r["discount_percent"], reverse=True)
    on_sale = [build_entry(r, "on_sale", r["discount_percent"])
               for r in sale_rows[: cfg.get("max_on_sale", 40)]]

    # --- manual watchlist: always included, if not already found above ---
    seen = {e["steam_appid"] for e in coming_soon + new_release + on_sale}
    for appid in cfg.get("manual_steam_appids", []):
        appid = str(appid)
        if appid in seen:
            continue
        row = fetch_manual_app(appid, cfg.get("steam_country", "us"))
        time.sleep(0.5)
        if not row:
            continue
        if row["coming_soon"]:
            coming_soon.insert(0, build_entry(row, "coming_soon", 9999))
        elif row["released"] == today:
            new_release.insert(0, build_entry(row, "new_release", 9999))
        if row["discount_percent"]:
            on_sale.insert(0, build_entry(row, "on_sale", 9999))

    output = {
        "generated_at": datetime.now(ZoneInfo(tz)).isoformat(),
        "timezone": tz,
        "source": "steam",
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
