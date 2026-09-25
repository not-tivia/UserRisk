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
| `NEW_WALLET_DAYS_THRESHOLD` | 14 | Wallet flagged "new" if its oldest known tx is more recent than this |
| `NEW_WALLET_RISK` | 25 | Added when the wallet is flagged new |
| `TWITTER_FLAG_RISK` | 25 | Added when `twitter_check.py` flags the linked X account (renamed/deleted) |
| `NO_X_LINKED_RISK` | 20 | Added when there's no linked X account at all (display name isn't handle-shaped) — legit creators link one immediately |
| `MAX_CONFIDENCE_MIN_RISK` | 90 | Risk floor for "ready to flag" |
| `MAX_CONFIDENCE_MIN_SIGNALS` | 2 | Independent signals required alongside the risk floor |
| `REVIEW_MIN_RISK` | 40 | Below this, a user doesn't show up in the daily report (still visible in the dashboard) |

### Why "ready to flag" requires 2+ signals

The tier + repeat-raffle pattern alone can hit risk 100 on its own (see
`calc_risk`), but it's just a timing pattern — not proof of a bot. `"max"`
confidence (the only tier the automation calls out as ready to ban) requires
the score to be at the ceiling **and** at least one other independent
signal — a wallet that's only days old, or an X account that just changed
its handle or disappeared. Anything else lands in the "review" bucket
instead of "ready to flag": a heuristic score is not proof of fraud, and a
wrongful ban is a real support/reputation cost, so the automation is
deliberately biased toward under-flagging rather than over-flagging.

## Automated Reports (Discord)

`discord_report.py` runs a full scrape (tier/repeat pattern + SPL token count
+ wallet age + X account check), then posts two sections to a Discord webhook:

- **READY TO FLAG** — `confidence == "max"` entries, each with a ready-to-paste
  `!flag <wallet>` command. This does **not** call Discord's bot or ban
  anything itself — it only prepares the command for you to paste, same as
  the dashboard's "Flag" button.
- **REVIEW** — everything else above `REVIEW_MIN_RISK`, for a human look.

### One-time setup

1. Create a Discord webhook for the channel you want reports in
   (Channel Settings → Integrations → Webhooks → New Webhook), copy its URL.
2. On the machine that will run it, set `DISCORD_WEBHOOK_URL` — don't hardcode
   it or commit it anywhere:
   ```
   echo 'DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...' > .env
   ```
3. Test it manually first: `python discord_report.py` (needs Chrome/chromedriver
   available, same as the scraper — Selenium auto-downloads the driver).
4. If X/twitter checks haven't been set up on this machine yet, run
   `python twitter_check.py --import-cookies` once (headless-safe, no browser
   window needed) so `data/chrome-profile/` has a logged-in session.

### Running every 12 hours on the Ubuntu server

Unit files are in `deploy/`. Install Chrome (`sudo apt install chromium-browser`
or Google Chrome), then:

```
sudo mkdir -p /opt/raffle-dashboard
sudo cp -r . /opt/raffle-dashboard      # or git clone the repo there
cd /opt/raffle-dashboard
python3 -m venv venv && venv/bin/pip install -r requirements.txt
echo 'DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...' | sudo tee .env
python venv/bin/python twitter_check.py --import-cookies   # one-time X login

sudo cp deploy/raffle-report.service deploy/raffle-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now raffle-report.timer
sudo systemctl start raffle-report.service   # run once immediately to verify
journalctl -u raffle-report.service -f       # watch it run
```

The timer fires at 00:00 and 12:00 server time (`deploy/raffle-report.timer`
`OnCalendar`), with a random up-to-5-minute delay to avoid hammering the site
at the exact same second every run.

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
