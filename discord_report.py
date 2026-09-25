"""
Daily/twice-daily automation entry point: runs a full scrape (tier + repeat
pattern + SPL token count + wallet age + X/twitter check), then posts a
formatted report to a Discord webhook:

  1. One summary message.
  2. One message PER "max confidence" (ready to flag) account — its own
     embed (bulleted signals, a ready-to-paste `!flag <wallet>` code block)
     plus a row of real clickable link buttons (View Profile, Solscan).
     Discord attaches buttons to the whole message, not a specific embed, so
     these are sent one-per-message rather than bundled together.
  3. One compact multi-field message for everything else above
     REVIEW_MIN_RISK, no buttons (nothing actionable there yet).

Buttons are LINK-style only — Discord has no "copy to clipboard" button type
for any bot, and a button that actually executes the ban would need a real
Discord Application (bot token + hosted interactions endpoint), not just a
webhook. The code-block already gives one-click copy via Discord's own
hover-to-copy affordance.

Run manually:      python discord_report.py
Run on a schedule:  see raffle-report.service / raffle-report.timer

Requires the DISCORD_WEBHOOK_URL environment variable (loaded from a local
.env if present — see _load_dotenv below). Never hardcode the webhook URL in
this file or commit it — it grants post access to the channel.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles often default to cp1252, which chokes on the emoji in
# embed titles below. Never let printing crash the report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import requests

from scraper import run_scrape


def _load_dotenv(path=".env"):
    """Minimal .env loader so manual runs work the same way the systemd
    EnvironmentFile= deploy does, without adding a python-dotenv dependency
    for one variable. Doesn't override an already-set env var."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Discord hard limits: 25 fields/embed, 5 buttons/action row, 5 rows/message.
MAX_FIELDS_PER_EMBED = 25
POST_DELAY_SECONDS = 0.4  # be polite when sending several messages in a row

COLOR_HEADER = 0x8B5CF6  # purple, matches the dashboard's accent
COLOR_MAX = 0xEF4444     # red
COLOR_REVIEW = 0xEAB308  # yellow
COLOR_CLEAN = 0x22C55E   # green, nothing flagged this run

BUTTON_STYLE_LINK = 5
ACTION_ROW = 1
BUTTON = 2

# One bullet-line builder per signal, in the order they should display.
SIGNAL_BULLETS = {
    "new_wallet": lambda r: (
        f"New wallet — first transaction ~{r['wallet_age_days']}d ago"
        if r.get("wallet_age_days") is not None else "New wallet"
    ),
    "repeat_pattern": lambda r: f"Repeat pattern — {r.get('raffle_count', '?')} T1/T2 raffles in a tight window",
    "no_x_linked": lambda r: "No X account linked",
    "twitter_flag": lambda r: f"X account flagged — {r.get('twitter_flag_reason') or 'renamed/deleted'}",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")


def _signal_bullets(r):
    lines = [SIGNAL_BULLETS[s](r) for s in r.get("signals", []) if s in SIGNAL_BULLETS]
    return "\n".join(f"• {line}" for line in lines) or "• Tier only"


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def build_header(results, max_conf, review):
    return {"embeds": [{
        "title": "Raffle Risk Scan",
        "description": (
            f"**{len(results)}** T1/T2 creators scanned — "
            f"**{len(max_conf)}** ready to flag, **{len(review)}** for review"
        ),
        "color": COLOR_CLEAN if not max_conf and not review else COLOR_HEADER,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "rafffle.famousfoxes.com risk scanner"},
    }]}


def build_max_message(r):
    solscan = f"https://solscan.io/account/{r['wallet']}"
    embed = {
        "title": f"@{r['user_name']} — {r['tier_badge']} — Risk {r['risk']}",
        "description": (
            f"{_signal_bullets(r)}\n\n"
            f"SPL tokens: **{r.get('spl_token_count', '?')}**\n"
            f"Collection: {r.get('collection_name') or '—'}\n\n"
            f"```\n!flag {r['wallet']}\n```"
        ),
        "color": COLOR_MAX,
        "footer": {"text": "Confidence: MAX — ready to flag"},
    }
    components = [{
        "type": ACTION_ROW,
        "components": [
            {"type": BUTTON, "style": BUTTON_STYLE_LINK, "label": "View Profile", "url": r["user_link"]},
            {"type": BUTTON, "style": BUTTON_STYLE_LINK, "label": "Solscan", "url": solscan},
        ],
    }]
    return {"embeds": [embed], "components": components}


def build_review_message(chunk, total_review):
    fields = [{
        "name": f"@{r['user_name']} ({r['tier_badge']})"[:256],
        "value": (f"Risk **{r['risk']}**\n{_signal_bullets(r)}\n[Profile]({r['user_link']})")[:1000],
        "inline": False,
    } for r in chunk]
    return {"embeds": [{
        "title": f"\U0001F7E1 Review ({total_review})",
        "color": COLOR_REVIEW,
        "fields": fields,
    }]}


def post(payload):
    if not DISCORD_WEBHOOK_URL:
        logging.error("DISCORD_WEBHOOK_URL not set — printing message instead of posting.")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    # A classic (non-application-owned) incoming webhook silently drops the
    # "components" field — no error, message still posts fine, buttons just
    # never render — UNLESS this query param is set. Confirmed live
    # 2026-09-25 by checking the actual returned message body (?wait=true),
    # not just the status code: without with_components=true, HTTP 204
    # "succeeds" but the message has no components at all.
    url = DISCORD_WEBHOOK_URL
    if payload.get("components"):
        url += ("&" if "?" in url else "?") + "with_components=true"
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code >= 300:
        logging.error(f"Discord post failed ({resp.status_code}): {resp.text[:300]}")
    time.sleep(POST_DELAY_SECONDS)


def main():
    results = run_scrape()
    max_conf = [r for r in results if r.get("confidence") == "max"]
    review = [r for r in results if r.get("confidence") == "review"]

    post(build_header(results, max_conf, review))

    for r in max_conf:
        post(build_max_message(r))

    for chunk in _chunk(review, MAX_FIELDS_PER_EMBED):
        post(build_review_message(chunk, len(review)))


if __name__ == "__main__":
    sys.exit(main())
