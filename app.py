"""
Raffle Dashboard — local Flask app.
Run with: python app.py
Then open http://localhost:5000 in your browser.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify

from scraper import run_scrape, DATA_FILE

app = Flask(__name__)

# Track scrape status so the UI can show progress
scrape_status = {"running": False, "last_run": None, "error": None}


def _do_scrape():
    """Background scrape task."""
    global scrape_status
    scrape_status["running"] = True
    scrape_status["error"] = None
    try:
        run_scrape()
        scrape_status["last_run"] = datetime.now().isoformat()
    except Exception as e:
        scrape_status["error"] = str(e)
    finally:
        scrape_status["running"] = False


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    """Return the most recent scrape data as JSON."""
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text())
        data["status"] = scrape_status
        return jsonify(data)
    return jsonify({"raffles": [], "total": 0, "scraped_at": None, "status": scrape_status})


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """Kick off a new scrape in a background thread."""
    if scrape_status["running"]:
        return jsonify({"ok": False, "msg": "Scrape already in progress"}), 409
    t = threading.Thread(target=_do_scrape, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Scrape started"})


@app.route("/api/status")
def api_status():
    return jsonify(scrape_status)


if __name__ == "__main__":
    print("\n  Raffle Dashboard → http://localhost:5000\n")
    app.run(debug=False, port=5000)
