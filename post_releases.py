#!/usr/bin/env python3
"""
Discord Game Release Bot
-------------------------
Posts a daily digest to a Discord channel (via webhook) with:
  1. Games releasing TODAY
  2. Games releasing in the next N days ("Coming Soon")

Data source: RAWG Video Games Database API (https://rawg.io/apidocs)
Delivery: Discord webhook (no persistent bot process needed)

Secrets come from environment variables (set these as GitHub Actions
repository secrets, or export them in your shell for local testing):
  - RAWG_API_KEY: your free RAWG API key
  - DISCORD_WEBHOOK_URL: the webhook URL for the target channel

Non-secret settings live in config.json next to this script:
  - coming_soon_days: how many days ahead to look (default 7)
  - popularity_threshold: minimum RAWG "added" count to filter out obscure/noise titles
  - max_today / max_coming_soon: cap on how many games to list per section
  - timezone: which timezone "today" is calculated in
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"
RAWG_BASE = "https://api.rawg.io/api/games"


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    rawg_api_key = os.environ.get("RAWG_API_KEY")
    discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not rawg_api_key or not discord_webhook_url:
        sys.exit(
            "Missing secrets. Set the RAWG_API_KEY and DISCORD_WEBHOOK_URL "
            "environment variables (in GitHub: Settings > Secrets and variables "
            "> Actions; locally: export them before running)."
        )

    cfg["rawg_api_key"] = rawg_api_key
    cfg["discord_webhook_url"] = discord_webhook_url
    return cfg


def today_in_tz(tz_name):
    return datetime.now(ZoneInfo(tz_name)).date()


def fetch_games(api_key, date_from, date_to, page_size=40):
    """Fetch games releasing in [date_from, date_to] (inclusive), ordered by popularity."""
    params = {
        "key": api_key,
        "dates": f"{date_from.isoformat()},{date_to.isoformat()}",
        "ordering": "-added",
        "page_size": page_size,
        "exclude_additions": "true",  # skip DLC/editions where supported
    }
    resp = requests.get(RAWG_BASE, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("results", [])


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
    return ", ".join(names) if names else "Unknown platform"


def format_game_line(game, show_date=False):
    name = game.get("name", "Unknown title")
    slug = game.get("slug", "")
    url = f"https://rawg.io/games/{slug}" if slug else "https://rawg.io"
    plats = platform_names(game)
    line = f"**[{name}]({url})**\n{plats}"
    if show_date and game.get("released"):
        line += f" • releases {game['released']}"
    return line


def build_section(games, show_date=False):
    if not games:
        return None
    return "\n\n".join(format_game_line(g, show_date=show_date) for g in games)


def post_to_discord(webhook_url, today_games, soon_games, today_str, coming_soon_days):
    embeds = []

    today_desc = build_section(today_games)
    embeds.append({
        "title": f"🎮 Releasing Today — {today_str}",
        "description": today_desc or "No notable releases today.",
        "color": 0x5865F2,
    })

    soon_desc = build_section(soon_games, show_date=True)
    embeds.append({
        "title": f"📅 Coming Soon (next {coming_soon_days} days)",
        "description": soon_desc or "Nothing notable on the horizon this week.",
        "color": 0x57F287,
        "footer": {"text": "Data from RAWG.io • filtered to notable titles"},
    })

    payload = {"embeds": embeds}
    resp = requests.post(webhook_url, json=payload, timeout=15)
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Discord webhook failed: {resp.status_code} {resp.text}")


def main():
    cfg = load_config()
    tz = cfg.get("timezone", "America/Los_Angeles")
    today = today_in_tz(tz)
    soon_end = today + timedelta(days=cfg.get("coming_soon_days", 7))
    soon_start = today + timedelta(days=1)

    today_raw = fetch_games(cfg["rawg_api_key"], today, today)
    soon_raw = fetch_games(cfg["rawg_api_key"], soon_start, soon_end)

    today_games = filter_and_rank(today_raw, cfg.get("popularity_threshold", 50), cfg.get("max_today", 10))
    soon_games = filter_and_rank(soon_raw, cfg.get("popularity_threshold", 50), cfg.get("max_coming_soon", 15))

    post_to_discord(
        cfg["discord_webhook_url"],
        today_games,
        soon_games,
        today.strftime("%B %d, %Y"),
        cfg.get("coming_soon_days", 7),
    )
    print(f"Posted {len(today_games)} today-releases and {len(soon_games)} coming-soon games.")


if __name__ == "__main__":
    main()
