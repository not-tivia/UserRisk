"""
Daily/twice-daily automation entry point: runs a full scrape (tier + repeat
pattern + SPL token count + wallet age + X/twitter check), then posts a
formatted embed report to a Discord webhook with two sections:

  READY TO FLAG — "max confidence" entries (top risk score AND 2+ independent
                  signals), red sidebar, each with a ready-to-paste
                  `!flag <wallet>` code block — still requires you to paste
                  it, nothing bans automatically.
  REVIEW        — everything else above REVIEW_MIN_RISK, yellow sidebar.

Run manually:      python discord_report.py
Run on a schedule:  see raffle-report.service / raffle-report.timer

Requires the DISCORD_WEBHOOK_URL environment variable. Never hardcode the
webhook URL in this file or commit it — it grants post access to the channel.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

# Windows consoles often default to cp1252, which chokes on the emoji in
# embed titles below. Never let printing crash the report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import requests

from scraper import run_scrape

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Discord hard limits: 25 fields/embed, 10 embeds/message, 1024 chars/field value.
MAX_FIELDS_PER_EMBED = 25
MAX_EMBEDS_PER_MESSAGE = 10
MAX_FIELD_VALUE = 1000

COLOR_HEADER = 0x8B5CF6  # purple, matches the dashboard's accent
COLOR_MAX = 0xEF4444     # red
COLOR_REVIEW = 0xEAB308  # yellow
COLOR_CLEAN = 0x22C55E   # green, nothing flagged this run

SIGNAL_LABELS = {
    "repeat_pattern": "repeat pattern",
    "new_wallet": "new wallet",
    "twitter_flag": "X flag",
    "no_x_linked": "no X linked",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")


def _reasons(r):
    labels = [SIGNAL_LABELS.get(s, s) for s in r.get("signals", [])]
    return ", ".join(labels) or "tier"


def _field_for(r, ready_to_paste):
    value = f"Risk **{r['risk']}** — {_reasons(r)}\n[Profile]({r['user_link']})"
    if ready_to_paste:
        value += f"\n```\n!flag {r['wallet']}\n```"
    return {
        "name": f"@{r['user_name']} ({r['tier_badge']})"[:256],
        "value": value[:MAX_FIELD_VALUE],
        "inline": False,
    }


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def build_embeds(results):
    max_conf = [r for r in results if r.get("confidence") == "max"]
    review = [r for r in results if r.get("confidence") == "review"]

    embeds = [{
        "title": "Raffle Risk Scan",
        "description": (
            f"**{len(results)}** T1/T2 creators scanned — "
            f"**{len(max_conf)}** ready to flag, **{len(review)}** for review"
        ),
        "color": COLOR_CLEAN if not max_conf and not review else COLOR_HEADER,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "rafffle.famousfoxes.com risk scanner"},
    }]

    for chunk in _chunk(max_conf, MAX_FIELDS_PER_EMBED):
        embeds.append({
            "title": f"\U0001F534 Ready To Flag ({len(max_conf)})",
            "color": COLOR_MAX,
            "fields": [_field_for(r, ready_to_paste=True) for r in chunk],
        })

    for chunk in _chunk(review, MAX_FIELDS_PER_EMBED):
        embeds.append({
            "title": f"\U0001F7E1 Review ({len(review)})",
            "color": COLOR_REVIEW,
            "fields": [_field_for(r, ready_to_paste=False) for r in chunk],
        })

    return embeds


def post_embeds(embeds):
    if not DISCORD_WEBHOOK_URL:
        logging.error("DISCORD_WEBHOOK_URL not set — printing report instead of posting.")
        print(json.dumps(embeds, indent=2))
        return
    for chunk in _chunk(embeds, MAX_EMBEDS_PER_MESSAGE):
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": chunk}, timeout=15)
        if resp.status_code >= 300:
            logging.error(f"Discord post failed ({resp.status_code}): {resp.text[:300]}")


def main():
    results = run_scrape()
    post_embeds(build_embeds(results))


if __name__ == "__main__":
    sys.exit(main())
