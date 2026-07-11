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
def calc_risk(tier, user_name, existing, time_frame_hrs=3):
    risk = TIER_RISK.get(tier, 0)
    for r in existing:
        if r.get("user_name") != user_name:
            continue
        hrs = convert_to_hours(r.get("end_time_text", ""))
        if hrs <= 1:
            risk += NEAR_END_TIME_RISK
        if hrs <= time_frame_hrs:
            risk += MULTI_RAFFLE_RISK
    return min(risk, 100)


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
        else:
            entry = copy.deepcopy(d)
            entry["raffle_count"] = 1
            combined[name] = entry
    return list(combined.values())


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
        seen_wallets = set()

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
            if wallet and wallet not in seen_wallets:
                spl = get_spl_tokens(wallet)
                seen_wallets.add(wallet)
            else:
                spl = 0

            risk = calc_risk(data["tier_badge"], data["user_name"], results)
            if spl > SPL_TOKEN_THRESHOLD:
                risk = int(risk * SPL_TOKEN_RISK_REDUCTION)

            results.append({
                "risk": risk,
                "spl_token_count": spl,
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
