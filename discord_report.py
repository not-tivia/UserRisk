"""
Daily/twice-daily automation entry point: runs a full scrape (tier + repeat
pattern + SPL token count + wallet age + X/twitter check), then posts a
report to a Discord webhook split into two sections:

  READY TO FLAG — "max confidence" entries (top risk score AND 2+ independent
                  signals). Each line includes a ready-to-paste `!flag <wallet>`
                  command — still requires you to paste it, nothing bans
                  automatically.
  REVIEW        — everything else above REVIEW_MIN_RISK, for a human look.

Run manually:      python discord_report.py
Run on a schedule:  see raffle-report.service / raffle-report.timer

Requires the DISCORD_WEBHOOK_URL environment variable. Never hardcode the
webhook URL in this file or commit it — it grants post access to the channel.
"""

import logging
import os
import sys

import requests

from scraper import run_scrape

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Discord hard limits we chunk around
MAX_CONTENT_CHARS = 1900
MAX_ENTRIES_PER_MESSAGE = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")


def format_max_entry(r):
    reasons = ", ".join(r.get("signals", [])) or "tier"
    return (
        f"**@{r['user_name']}** ({r['tier_badge']}) — risk {r['risk']} — {reasons}\n"
        f"`!flag {r['wallet']}`"
    )


def format_review_entry(r):
    reasons = ", ".join(r.get("signals", [])) or "tier"
    return f"@{r['user_name']} ({r['tier_badge']}) — risk {r['risk']} — {reasons} — {r['user_link']}"


def chunk_lines(lines, max_chars=MAX_CONTENT_CHARS, max_entries=MAX_ENTRIES_PER_MESSAGE):
    chunk, size = [], 0
    for line in lines:
        if chunk and (size + len(line) + 1 > max_chars or len(chunk) >= max_entries):
            yield chunk
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        yield chunk


def post(content):
    if not DISCORD_WEBHOOK_URL:
        logging.error("DISCORD_WEBHOOK_URL not set — printing report instead of posting.")
        print(content)
        return
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
    if resp.status_code >= 300:
        logging.error(f"Discord post failed ({resp.status_code}): {resp.text[:300]}")


def main():
    results = run_scrape()

    max_conf = [r for r in results if r.get("confidence") == "max"]
    review = [r for r in results if r.get("confidence") == "review"]

    header = (
        f"**Raffle Risk Scan** — {len(results)} T1/T2 creators scanned, "
        f"{len(max_conf)} ready to flag, {len(review)} for review"
    )
    post(header)

    if max_conf:
        lines = [format_max_entry(r) for r in max_conf]
        for chunk in chunk_lines(lines):
            post("🔴 **READY TO FLAG**\n\n" + "\n\n".join(chunk))
    else:
        post("🔴 **READY TO FLAG** — none this run")

    if review:
        lines = [format_review_entry(r) for r in review]
        for chunk in chunk_lines(lines):
            post("🟡 **REVIEW**\n" + "\n".join(chunk))


if __name__ == "__main__":
    sys.exit(main())
