"""
Twitter/X username-change checker.

Visits https://x.com/<handle>/about ("About this account") and extracts
when the account last changed its username. Accounts that changed name
recently (or no longer exist) get flagged.

X blocks logged-out visitors, so this uses a persistent Chrome profile:

    python twitter_check.py --login          one-time: opens a visible browser,
                                             log into X manually, then close it
    python twitter_check.py --test <handle>  check one handle, print everything
                                             (page text dump on parse failure)

Can also be imported: check_handles([...]) -> {handle: result_dict}
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions

# ─── Configuration ────────────────────────────────────────────────────────────
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", None)

DATA_DIR = Path(__file__).parent / "data"
PROFILE_DIR = DATA_DIR / "chrome-profile"   # persistent X login lives here
CACHE_FILE = DATA_DIR / "twitter_checks.json"

RECENT_CHANGE_DAYS = 30     # flag if username changed within this many days
CHECK_COOLDOWN_DAYS = 3     # don't re-check a handle more often than this
DELAY_BETWEEN_CHECKS = 7    # seconds between page loads, be polite
PAGE_LOAD_WAIT = 8          # seconds to let the about page render

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)


class NotLoggedIn(Exception):
    """X showed a login wall — run `python twitter_check.py --login` first."""


# ─── Browser ──────────────────────────────────────────────────────────────────
def create_driver(headless=True):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--user-data-dir={PROFILE_DIR.resolve()}")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1280,1000")
    if CHROMEDRIVER_PATH:
        svc = ChromeService(executable_path=CHROMEDRIVER_PATH)
        return webdriver.Chrome(service=svc, options=opts)
    return webdriver.Chrome(options=opts)


# ─── Date parsing ─────────────────────────────────────────────────────────────
def days_since_change(text, today=None):
    """
    Turn X's change-date text into 'days ago'. Handles both relative
    ("3 days ago", "2 weeks ago") and absolute ("Jun 2026", "Jun 15, 2026")
    forms. Month-year-only dates use the END of that month so a borderline
    account is flagged rather than missed. Returns None if unparseable.
    """
    today = today or date.today()
    t = text.strip().lower()

    m = re.search(r"(\d+)\s*(second|minute|hour)s?\s*ago", t)
    if m:
        return 0
    m = re.search(r"(\d+)\s*days?\s*ago", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*weeks?\s*ago", t)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*months?\s*ago", t)
    if m:
        return int(m.group(1)) * 30
    m = re.search(r"(\d+)\s*years?\s*ago", t)
    if m:
        return int(m.group(1)) * 365
    if "yesterday" in t:
        return 1
    if "today" in t:
        return 0

    # "Jun 15, 2026" — full date
    m = re.search(r"([a-z]{3,9})\s+(\d{1,2}),\s*(\d{4})", t)
    if m and m.group(1)[:3] in MONTHS:
        d = date(int(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2)))
        return max(0, (today - d).days)

    # "Jun 2026" — month granularity; assume end of month (favor flagging)
    m = re.search(r"([a-z]{3,9})\s+(\d{4})", t)
    if m and m.group(1)[:3] in MONTHS:
        year, month = int(m.group(2)), MONTHS[m.group(1)[:3]]
        if month == 12:
            eom = date(year, 12, 31)
        else:
            eom = date(year, month + 1, 1)
        return max(0, (today - eom).days)

    return None


# ─── Page parsing ─────────────────────────────────────────────────────────────
def parse_about_page(page_text, today=None):
    """
    Extract username-change info from the visible text of x.com/<handle>/about.
    Works off text patterns, not CSS selectors — X's class names are obfuscated
    and churn constantly, but the wording is stable.
    """
    text = re.sub(r"\s+", " ", page_text)
    low = text.lower()

    result = {
        "exists": True,
        "joined": None,
        "change_count": None,
        "last_change_text": None,
        "days_since_change": None,
        "parse_ok": False,
    }

    if "this account doesn" in low and "exist" in low:
        result["exists"] = False
        result["parse_ok"] = True
        return result
    if "account suspended" in low:
        result["exists"] = False
        result["parse_ok"] = True
        return result

    m = re.search(r"joined\s+([a-z]{3,9}\s+\d{4})", low)
    if m:
        result["joined"] = m.group(1).title()

    # "Username changes: 3" / "3 username changes" / "changed their username 3 times"
    m = (
        re.search(r"username changes?\s*:?\s*(\d+)", low)
        or re.search(r"(\d+)\s+username changes?", low)
        or re.search(r"changed (?:their|its) username\s+(\d+)", low)
    )
    if m:
        result["change_count"] = int(m.group(1))
        result["parse_ok"] = True

    # "No username changes" / "hasn't changed their username"
    if re.search(r"no username changes|not changed (?:their|its) username|never changed", low):
        result["change_count"] = 0
        result["parse_ok"] = True

    # "Last changed Jun 2026" / "most recently Jun 2026" / "... 3 days ago"
    m = re.search(
        r"(?:last changed|most recently(?: changed)?(?: on)?)\s+"
        r"((?:[a-z]{3,9}\s+\d{1,2},?\s*\d{4})|(?:[a-z]{3,9}\s+\d{4})|(?:[^.]{1,30}?ago))",
        low,
    )
    if m:
        result["last_change_text"] = m.group(1).strip().title()
        result["days_since_change"] = days_since_change(m.group(1), today)
        result["parse_ok"] = True

    return result


def flag_result(result):
    """Decide whether a parsed result should alert. Returns (flagged, reason)."""
    if not result.get("exists", True):
        return True, "account gone (renamed away, deleted, or suspended)"
    days = result.get("days_since_change")
    if days is not None and days <= RECENT_CHANGE_DAYS:
        return True, f"username changed ~{days}d ago"
    if not result.get("parse_ok"):
        return False, "could not parse about page"
    return False, ""


# ─── Checking ─────────────────────────────────────────────────────────────────
def _detect_login_wall(page_text):
    low = re.sub(r"\s+", " ", page_text).lower()
    return bool(
        re.search(r"sign in to x|log in to x|don.t miss what.s happening", low)
        and "joined" not in low
    )


def check_handle(driver, handle, dump_on_fail=False):
    """Load x.com/<handle>/about and return a parsed result dict."""
    url = f"https://x.com/{handle}/about"
    logging.info(f"Checking @{handle} -> {url}")
    driver.get(url)
    time.sleep(PAGE_LOAD_WAIT)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    if _detect_login_wall(page_text):
        raise NotLoggedIn(
            "X is showing a login wall. Run:  python twitter_check.py --login"
        )

    result = parse_about_page(page_text)
    result["handle"] = handle
    result["checked_at"] = datetime.now().isoformat()
    result["flagged"], result["flag_reason"] = flag_result(result)

    if not result["parse_ok"] and dump_on_fail:
        print("\n──── raw page text (parse failed — send this back) " + "─" * 20)
        print(page_text[:3000])
        print("─" * 70)

    return result


# ─── Cache ────────────────────────────────────────────────────────────────────
def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logging.warning("Cache file unreadable, starting fresh.")
    return {}


def save_cache(cache):
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, default=str))


def _cache_fresh(entry):
    try:
        checked = datetime.fromisoformat(entry["checked_at"])
    except (KeyError, ValueError):
        return False
    # Failed parses get retried next run instead of sitting in cache for days
    if not entry.get("parse_ok") and entry.get("exists", True):
        return False
    return (datetime.now() - checked).days < CHECK_COOLDOWN_DAYS


def check_handles(handles, headless=True):
    """
    Check a list of handles, using the cache to skip recently-checked ones.
    Returns {handle: result_dict}. This is the entry point for scraper.py.
    """
    cache = load_cache()
    results = {}
    to_check = []

    for h in handles:
        h = h.strip().lstrip("@")
        if not h:
            continue
        if h in cache and _cache_fresh(cache[h]):
            logging.info(f"@{h}: cached ({cache[h].get('flag_reason') or 'ok'})")
            results[h] = cache[h]
        else:
            to_check.append(h)

    if not to_check:
        return results

    driver = None
    try:
        driver = create_driver(headless=headless)
        for i, h in enumerate(to_check):
            if i > 0:
                time.sleep(DELAY_BETWEEN_CHECKS)
            try:
                result = check_handle(driver, h)
            except NotLoggedIn:
                logging.error("Not logged into X — skipping remaining checks. "
                              "Run: python twitter_check.py --login")
                break
            except Exception as e:
                logging.error(f"@{h}: check failed: {e}")
                continue
            results[h] = result
            cache[h] = result
            if result["flagged"]:
                logging.warning(f"@{h}: FLAGGED — {result['flag_reason']}")
    finally:
        if driver:
            driver.quit()
        save_cache(cache)

    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────
def do_login():
    print("Opening a visible browser. Log into X, then close the window.")
    print("(Your session is saved to data/chrome-profile and reused from then on.)")
    driver = create_driver(headless=False)
    driver.get("https://x.com/login")
    try:
        # Wait until the user closes the window
        while True:
            time.sleep(2)
            _ = driver.window_handles
    except Exception:
        pass
    print("Browser closed. Login saved (if you completed it).")


def do_test(handle):
    driver = create_driver(headless=True)
    try:
        result = check_handle(driver, handle.lstrip("@"), dump_on_fail=True)
    except NotLoggedIn as e:
        print(f"\n{e}")
        sys.exit(1)
    finally:
        driver.quit()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="X username-change checker")
    parser.add_argument("--login", action="store_true",
                        help="open a visible browser to log into X (one-time)")
    parser.add_argument("--test", metavar="HANDLE",
                        help="check a single handle and print the full result")
    args = parser.parse_args()

    if args.login:
        do_login()
    elif args.test:
        do_test(args.test)
    else:
        parser.print_help()
