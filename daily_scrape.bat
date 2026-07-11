@echo off
REM ──────────────────────────────────────────────
REM  Daily raffle scrape — run via Task Scheduler
REM  This just runs the scraper, no dashboard needed
REM ──────────────────────────────────────────────
cd /d "%~dp0"
python scraper.py
echo Scrape complete at %date% %time%
