# Raffle Risk Scanner — Local Dashboard

A local web dashboard that scrapes [rafffle.famousfoxes.com](https://rafffle.famousfoxes.com), flags T1/T2 tier raffles, scores their risk, and displays everything in a sortable table.

## Quick Start

### 1. Install dependencies

```
cd raffle-dashboard
pip install -r requirements.txt
```

You also need **Firefox** installed and **geckodriver** on your system.  
Update the geckodriver path in `scraper.py` if it's not at the default location, or set it as an environment variable:

```
set GECKODRIVER_PATH=C:\path\to\geckodriver.exe
```

### 2. Run the dashboard

```
python app.py
```

Open **http://localhost:5000** in your browser.  
Click **Run Scrape** to kick off the first scan. Results are saved to `data/raffles.json` and persist between restarts.

---

## Auto-Scraping with Windows Task Scheduler

To run the scraper automatically once a day:

1. Open **Task Scheduler** (search for it in the Start menu)
2. Click **Create Basic Task**
3. Give it a name like `Raffle Daily Scrape`
4. Set the trigger to **Daily** at whatever time you want (e.g., 8:00 AM)
5. For the action, choose **Start a Program**
6. Browse to `daily_scrape.bat` inside this folder
7. Set **Start in** to the full path of this folder (e.g., `C:\Users\Deez\raffle-dashboard`)
8. Finish the wizard

The scraper will run in the background, save results to `data/raffles.json`, and next time you open the dashboard it'll show the latest data.

---

## Project Structure

```
raffle-dashboard/
├── app.py              ← Flask dashboard server
├── scraper.py          ← Scraping + risk scoring logic
├── daily_scrape.bat    ← Windows batch file for Task Scheduler
├── requirements.txt
├── data/
│   ├── raffles.json    ← Persisted scan results (auto-created)
│   └── scraper.log     ← Log file
└── templates/
    └── dashboard.html  ← Dashboard UI
```

## X Username-Change Checker

`twitter_check.py` visits `x.com/<handle>/about` for flagged users and reads
when the account last changed its username. A change within the last 30 days
(or an account that no longer exists) gets flagged.

One-time setup — X requires a logged-in session. Two ways:

**A. Import an existing session (recommended — avoids X login rate limits):**

```
python twitter_check.py --import-cookies
```

In a browser already logged into X: F12 -> Application/Storage -> Cookies ->
`https://x.com` -> copy the `auth_token` Value, paste it when prompted.

**B. Log in fresh:**

```
python twitter_check.py --login
```

Log into X in the browser that opens, then close it.

Either way the session is saved to `data/chrome-profile/` and reused for all
future (headless) checks.

Try it on a single handle:

```
python twitter_check.py --test some_handle
```

Results are cached in `data/twitter_checks.json`; a handle is only re-checked
every `CHECK_COOLDOWN_DAYS` (3) days. Tune `RECENT_CHANGE_DAYS`,
`CHECK_COOLDOWN_DAYS`, and `DELAY_BETWEEN_CHECKS` at the top of the file.

## Adjusting Risk Scoring

All risk weights are constants at the top of `scraper.py`:

| Constant | Default | Meaning |
|---|---|---|
| `TIER_RISK["T1"]` | 30 | Base risk for Tier 1 creators |
| `TIER_RISK["T2"]` | 15 | Base risk for Tier 2 creators |
| `NEAR_END_TIME_RISK` | 35 | Added when raffle ends within 1 hour |
| `MULTI_RAFFLE_RISK` | 10 | Added per raffle within the time window |
| `SPL_TOKEN_THRESHOLD` | 20 | Token count above which risk is reduced |
| `SPL_TOKEN_RISK_REDUCTION` | 0.65 | Multiplier applied when tokens > threshold |

## CSS Selectors

When the site changes its layout again, update the selector constants near the top of `scraper.py`:

```python
CARD_SELECTOR = "div.flex.flex-col.gap-4.transition-all.pt-4.overflow-hidden"
END_TIME_SELECTOR = 'span[role="button"] > span'
USER_LINK_SELECTOR = 'a[href^="/profile/"].text-fffPurple2.font-bold'
TIER_BADGE_SELECTOR = ".tipcontainer .tierBadgeTooltip + div"
TITLE_SELECTOR = "h2.line-clamp-1"
COLLECTION_LINK_SELECTOR = ".flex.items-center a"
```
