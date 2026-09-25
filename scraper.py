"""
Raffle scraper — crawls rafffle.famousfoxes.com, scores risk, saves to JSON.
Can be run standalone (python scraper.py) or imported by the dashboard app.
"""

import json
import time
import logging
import os
import re
import copy
import requests
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, WebDriverException,
    NoSuchElementException, ElementNotInteractableException,
)
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions

# ─── Configuration ────────────────────────────────────────────────────────────
BASE_URL = "https://rafffle.famousfoxes.com"

CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", None)

# Where we persist results between runs
DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "raffles.json"
LOG_FILE = DATA_DIR / "scraper.log"

# Risk weights
TIER_RISK = {"T1": 30, "T2": 15}
NEAR_END_TIME_RISK = 35
MULTI_RAFFLE_RISK = 10
SPL_TOKEN_RISK_REDUCTION = 0.65
SPL_TOKEN_THRESHOLD = 20
NEW_WALLET_DAYS_THRESHOLD = 14   # wallet risk-flagged if its oldest known tx is newer than this
NEW_WALLET_RISK = 25
TWITTER_FLAG_RISK = 25           # added when twitter_check.py flags the linked X account

# A user only counts as "max confidence" (ready to auto-flag) when the risk
# score is at the top of the scale AND multiple independent signals agree —
# tier/repeat pattern alone is not enough to auto-flag.
MAX_CONFIDENCE_MIN_RISK = 90
MAX_CONFIDENCE_MIN_SIGNALS = 2
REVIEW_MIN_RISK = 40             # below this, don't bother surfacing in the daily report

# Scraping
MAX_SCROLL_ATTEMPTS = 10
MIN_END_TIME_HOURS = 24
SCROLL_PAUSE = 3

# ─── Updated CSS selectors (2026-03) ─────────────────────────────────────────
CARD_SELECTOR = "div.flex.flex-col.gap-4.transition-all.pt-4.overflow-hidden"
END_TIME_SELECTOR = 'span[role="button"] > span'
USER_LINK_SELECTOR = 'a[href^="/profile/"].text-fffPurple2.font-bold'
TIER_BADGE_SELECTOR = ".tipcontainer .tierBadgeTooltip + div"
TITLE_SELECTOR = "h2.line-clamp-1"
COLLECTION_LINK_SELECTOR = ".flex.items-center a"

# ─── Logging ──────────────────────────────────────────────────────────────────
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
logging.getLogger().addHandler(console)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def convert_to_hours(text: str) -> float:
    matches = re.findall(r"(\d+)\s*h(?:rs?)?|(\d+)\s*m(?:ins?)?|(\d+)\s*s", text)
    total = 0.0
    for h, m, s in matches:
        total += int(h) if h else 0
        total += int(m) / 60 if m else 0
        total += int(s) / 3600 if s else 0
    return total


def end_time_seconds(text: str) -> int:
    matches = re.findall(r"(\d+)\s*h(?:rs?)?|(\d+)\s*m(?:ins?)?|(\d+)\s*s", text)
    total = 0
    for h, m, s in matches:
        total += int(h) * 3600 if h else 0
        total += int(m) * 60 if m else 0
        total += int(s) if s else 0
    return total


def create_driver(headless=True):
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    if CHROMEDRIVER_PATH:
        svc = ChromeService(executable_path=CHROMEDRIVER_PATH)
        return webdriver.Chrome(service=svc, options=opts)
    # Let Selenium auto-download the correct chromedriver
    return webdriver.Chrome(options=opts)


def close_popup(driver):
    try:
        btn = WebDriverWait(driver, 8).until(
            EC.visibility_of_element_located(
                (By.XPATH, '//button[contains(@class,"absolute") and contains(@class,"text-white")]')
            )
        )
        btn.click()
        logging.info("Popup closed.")
    except (NoSuchElementException, TimeoutException):
        pass
    except Exception as e:
        logging.warning(f"Popup close error: {e}")


def scroll_page(driver, max_scrolls, min_hours):
    prev_count = 0
    for i in range(max_scrolls):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        cards = soup.select(CARD_SELECTOR)
        logging.info(f"Scroll {i+1}/{max_scrolls} — {len(cards)} cards loaded")

        # If no new cards loaded, we've hit the bottom
        if len(cards) == prev_count and i > 0:
            logging.info("No new cards loaded, stopping scroll.")
            break
        prev_count = len(cards)

        # Check the LAST card — if it's old enough, we have enough data
        if cards:
            last_span = cards[-1].select_one(END_TIME_SELECTOR)
            if last_span and convert_to_hours(last_span.text.strip()) >= min_hours:
                logging.info(f"Last card ends in {last_span.text.strip()} — enough data loaded.")
                return True

    return len(BeautifulSoup(driver.page_source, "html.parser").select(CARD_SELECTOR)) > 0


# ─── Extraction ───────────────────────────────────────────────────────────────
def extract_card(card):
    try:
        end_span = card.select_one(END_TIME_SELECTOR)
        end_time_text = end_span.text.strip() if end_span else ""

        user_tag = card.select_one(USER_LINK_SELECTOR)
        user_link = user_tag["href"] if user_tag else ""
        user_name = user_tag.text.strip().lstrip("@") if user_tag else ""

        tier_tag = card.select_one(TIER_BADGE_SELECTOR)
        tier = tier_tag.text.strip() if tier_tag else None

        title_tag = card.select_one(TITLE_SELECTOR)
        title = title_tag.text.strip() if title_tag else ""

        col_tag = card.select_one(COLLECTION_LINK_SELECTOR)
        if col_tag:
            href = col_tag.get("href", "")
            if "magiceden.io" in href:
                collection = href.split("/")[-1]
            elif "solscan.io" in href:
                collection = f"{col_tag.text.strip()} {title}"
            else:
                collection = title
        else:
            collection = title

        return {
            "collection_name": collection,
            "tier_badge": tier,
            "end_time_text": end_time_text,
            "user_link": user_link,
            "user_name": user_name,
        }
    except Exception as e:
        logging.exception(f"Card extraction error: {e}")
        return None


# ─── Risk ─────────────────────────────────────────────────────────────────────
def calc_risk(tier, user_name, existing, wallet_age_days=None, time_frame_hrs=3):
    """Returns (risk, signals) — signals is a set of independent risk-factor
    names, used later to gate auto-flagging on more than just raw score."""
    risk = TIER_RISK.get(tier, 0)
    signals = set()
    for r in existing:
        if r.get("user_name") != user_name:
            continue
        hrs = convert_to_hours(r.get("end_time_text", ""))
        if hrs <= 1:
            risk += NEAR_END_TIME_RISK
            signals.add("repeat_pattern")
        if hrs <= time_frame_hrs:
            risk += MULTI_RAFFLE_RISK
            signals.add("repeat_pattern")
    if wallet_age_days is not None and wallet_age_days <= NEW_WALLET_DAYS_THRESHOLD:
        risk += NEW_WALLET_RISK
        signals.add("new_wallet")
    return min(risk, 100), signals


# ─── Token lookup (via Solana RPC — free, no API key) ─────────────────────────
def get_spl_tokens(wallet):
    """
    Count SPL token accounts for a wallet using Solana's public JSON RPC.
    No browser, no API key needed.
    """
    rpc_urls = [
        "https://api.mainnet-beta.solana.com",
        "https://rpc.ankr.com/solana",
    ]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ]
    }

    for rpc_url in rpc_urls:
        try:
            resp = requests.post(rpc_url, json=payload, timeout=10)
            data = resp.json()
            if "result" in data and "value" in data["result"]:
                token_count = len(data["result"]["value"])
                logging.info(f"Wallet {wallet[:8]}... has {token_count} SPL tokens (via RPC)")
                return token_count
        except Exception as e:
            logging.error(f"RPC error ({rpc_url}): {e}")
            continue

    logging.warning(f"All RPC endpoints failed for {wallet[:8]}...")
    return 0


def get_wallet_age_days(wallet):
    """
    Estimate wallet age from its oldest known transaction signature.
    getSignaturesForAddress returns newest-first; a single 1000-signature page
    covers the full history of any genuinely new wallet, so if the page comes
    back full we already know the wallet predates our lookup window and can
    skip further (expensive) pagination — we just report it as not-new.
    Returns None if we can't determine an age (skips the new-wallet signal).
    """
    rpc_urls = [
        "https://api.mainnet-beta.solana.com",
        "https://rpc.ankr.com/solana",
    ]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [wallet, {"limit": 1000}],
    }

    for rpc_url in rpc_urls:
        try:
            resp = requests.post(rpc_url, json=payload, timeout=15)
            data = resp.json()
            sigs = data.get("result")
            if sigs is None:
                continue
            if not sigs:
                logging.info(f"Wallet {wallet[:8]}... has no on-chain history — treating as new.")
                return 0
            if len(sigs) >= 1000:
                logging.info(f"Wallet {wallet[:8]}... has >1000 signatures — not new.")
                return None
            block_time = sigs[-1].get("blockTime")
            if not block_time:
                return None
            age_days = max((datetime.now() - datetime.fromtimestamp(block_time)).days, 0)
            logging.info(f"Wallet {wallet[:8]}... age ~{age_days}d")
            return age_days
        except Exception as e:
            logging.error(f"Signature RPC error ({rpc_url}): {e}")
            continue

    logging.warning(f"All RPC endpoints failed for wallet age of {wallet[:8]}...")
    return None


# ─── Combine duplicates ──────────────────────────────────────────────────────
def combine(data_list):
    combined = {}
    for d in data_list:
        name = d["user_name"].strip()
        if name in combined:
            c = combined[name]
            c["raffle_count"] += 1
            c["risk"] = max(c.get("risk", 0), d.get("risk", 0))
            c["spl_token_count"] = max(c.get("spl_token_count", 0), d.get("spl_token_count", 0))
            c["signals"] = sorted(set(c.get("signals", [])) | set(d.get("signals", [])))
        else:
            entry = copy.deepcopy(d)
            entry["raffle_count"] = 1
            combined[name] = entry
    return list(combined.values())


def classify_confidence(risk, signals):
    if risk >= MAX_CONFIDENCE_MIN_RISK and len(signals) >= MAX_CONFIDENCE_MIN_SIGNALS:
        return "max"
    if risk >= REVIEW_MIN_RISK:
        return "review"
    return "low"


# Real X handles are letters/digits/underscore only, max 15 chars. Confirmed
# live 2026-09-25: the raffle site falls back to showing a truncated wallet
# address (e.g. "6pGW...4pAF") as the display name for users who haven't set
# one — checking that against x.com always "resolves" as a nonexistent
# account, which isn't a real signal, just a false "account gone" flag. Skip
# anything that isn't shaped like a real handle instead of misreporting it.
VALID_X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def apply_twitter_signal(results):
    """
    Run twitter_check.py's check_handles() against every combined user and
    fold a flagged (renamed/dead) X account into risk + signals. Best-effort:
    any failure (not logged into X, network error, etc.) just skips this
    signal for the run rather than failing the whole scrape.
    """
    if not results:
        return results

    checkable = [r["user_name"] for r in results if VALID_X_HANDLE.match(r["user_name"])]
    skipped = len(results) - len(checkable)
    if skipped:
        logging.info(f"Skipping twitter check for {skipped} user(s) with non-handle display names.")
    if not checkable:
        for r in results:
            r["twitter_flag_reason"] = None
        return results

    try:
        from twitter_check import check_handles
        tw_results = check_handles(checkable)
    except Exception as e:
        logging.error(f"Twitter check unavailable this run: {e}")
        for r in results:
            r["twitter_flag_reason"] = None
        return results

    for r in results:
        r["twitter_flag_reason"] = None
        if not VALID_X_HANDLE.match(r["user_name"]):
            continue
        tw = tw_results.get(r["user_name"].lstrip("@"))
        if tw and tw.get("flagged"):
            r["risk"] = min(r.get("risk", 0) + TWITTER_FLAG_RISK, 100)
            signals = set(r.get("signals", []))
            signals.add("twitter_flag")
            r["signals"] = sorted(signals)
            r["twitter_flag_reason"] = tw.get("flag_reason")
    return results


# ─── Main scrape ──────────────────────────────────────────────────────────────
def run_scrape():
    """Run a full scrape. Returns list of raffle dicts and saves to JSON."""
    driver = None
    results = []

    try:
        driver = create_driver(headless=True)
        driver.get(BASE_URL)
        close_popup(driver)
        logging.info("Page loaded.")

        skip = set()
        wallet_info = {}   # wallet -> {"spl": int, "wallet_age_days": int|None}, looked up once per wallet

        if not scroll_page(driver, MAX_SCROLL_ATTEMPTS, MIN_END_TIME_HOURS):
            logging.info("No raffles found after scrolling.")
            return []

        soup = BeautifulSoup(driver.page_source, "html.parser")
        cards = soup.select(CARD_SELECTOR)
        logging.info(f"Found {len(cards)} cards.")

        for card in cards:
            data = extract_card(card)
            if not data:
                logging.debug("Card extraction returned None, skipping.")
                continue

            logging.info(
                f"Card: user={data['user_name']}, tier={data['tier_badge']}, "
                f"end={data['end_time_text']}, collection={data['collection_name']}"
            )

            if data["user_name"] in skip:
                logging.debug(f"  -> Skipping {data['user_name']} (already in skip list)")
                continue

            # If tier badge is missing, skip this card but don't blacklist the user
            # (they might have another card where the badge is detected)
            if data["tier_badge"] is None:
                logging.debug(f"  -> No tier badge detected for {data['user_name']}, skipping card")
                continue

            if data["tier_badge"] not in ("T1", "T2"):
                skip.add(data["user_name"])
                logging.info(f"  -> Skipping {data['user_name']} — tier {data['tier_badge']}")
                continue

            logging.info(f"  -> MATCH: {data['user_name']} is {data['tier_badge']}")

            wallet = data["user_link"].split("/")[-1]
            if wallet:
                if wallet not in wallet_info:
                    wallet_info[wallet] = {
                        "spl": get_spl_tokens(wallet),
                        "wallet_age_days": get_wallet_age_days(wallet),
                    }
                spl = wallet_info[wallet]["spl"]
                wallet_age_days = wallet_info[wallet]["wallet_age_days"]
            else:
                spl = 0
                wallet_age_days = None

            risk, signals = calc_risk(data["tier_badge"], data["user_name"], results, wallet_age_days)
            if spl > SPL_TOKEN_THRESHOLD:
                risk = int(risk * SPL_TOKEN_RISK_REDUCTION)

            results.append({
                "risk": risk,
                "signals": sorted(signals),
                "spl_token_count": spl,
                "wallet_age_days": wallet_age_days,
                "user_name": data["user_name"],
                "tier_badge": data["tier_badge"],
                "end_time": data["end_time_text"],
                "end_time_seconds": end_time_seconds(data["end_time_text"]),
                "collection_name": data["collection_name"],
                "user_link": BASE_URL + data["user_link"],
                "wallet": wallet,
                "raffle_count": 1,
            })

        results = combine(results)
        results = apply_twitter_signal(results)
        for r in results:
            r["confidence"] = classify_confidence(r["risk"], r.get("signals", []))
        results.sort(key=lambda x: (x["risk"], -x["spl_token_count"], x["end_time_seconds"]), reverse=True)

    except (TimeoutException, WebDriverException) as e:
        logging.error(f"WebDriver error: {e}")
    finally:
        if driver:
            driver.quit()

    # Persist
    output = {
        "scraped_at": datetime.now().isoformat(),
        "total": len(results),
        "raffles": results,
    }
    DATA_DIR.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(output, indent=2, default=str))
    logging.info(f"Saved {len(results)} results to {DATA_FILE}")

    return results


if __name__ == "__main__":
    run_scrape()
