import io
import json
import os
import threading
from contextlib import redirect_stdout
from datetime import datetime

import ammo_alert
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

SCRAPERS = [
    ("Ammoseek",       ammo_alert.scrape_ammoseek),
    ("r/gundeals",     ammo_alert.scrape_reddit_gundeals),
    ("BulkAmmo",       ammo_alert.scrape_bulkammo),
    ("TargetSportsUSA",ammo_alert.scrape_targetsports),
    ("MidwayUSA",      ammo_alert.scrape_midwayusa),
    ("SGAmmo",         ammo_alert.scrape_sgammo),
]

_state = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "deals": [],
    "sources": {},
    "email_sent": False,
    "email_error": None,
}
_lock = threading.Lock()


def _load_config():
    """Env vars take precedence (cloud); fall back to config.json (local)."""
    if os.environ.get("EMAIL_APP_PASSWORD"):
        return {
            "email": {
                "sender":       os.environ.get("EMAIL_SENDER", ""),
                "recipient":    os.environ.get("EMAIL_RECIPIENT", os.environ.get("EMAIL_SENDER", "")),
                "app_password": os.environ["EMAIL_APP_PASSWORD"],
            },
            "threshold": float(os.environ.get("THRESHOLD", "0.25")),
            "auto_email": os.environ.get("AUTO_EMAIL", "false").lower() == "true",
        }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def _run_scrapers():
    config = _load_config()

    threshold  = float(config.get("threshold", 0.25))
    auto_email = bool(config.get("auto_email", False))
    ammo_alert.MAX_CPR = threshold

    with _lock:
        _state.update({
            "status":      "running",
            "started_at":  datetime.now().isoformat(),
            "finished_at": None,
            "deals":       [],
            "sources":     {n: {"status": "pending", "count": 0, "error": None} for n, _ in SCRAPERS},
            "email_sent":  False,
            "email_error": None,
        })

    all_deals = []
    for name, fn in SCRAPERS:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                deals = fn()
        except Exception:
            deals = []

        output    = buf.getvalue()
        has_error = "Error:" in output
        error_msg = None
        if has_error:
            try:
                error_msg = output.split("Error:", 1)[1].strip().splitlines()[0][:120]
            except Exception:
                error_msg = "Unknown error"

        with _lock:
            _state["sources"][name] = {
                "status": "error" if has_error else "ok",
                "count":  len(deals),
                "error":  error_msg,
            }
        all_deals.extend(deals)

    all_deals.sort(key=lambda d: d["cpr"])

    email_sent  = False
    email_error = None
    if all_deals and auto_email and config.get("email", {}).get("app_password"):
        try:
            ammo_alert.send_email(all_deals, config)
            email_sent = True
        except Exception as e:
            email_error = str(e)[:200]

    with _lock:
        _state.update({
            "status":      "done",
            "finished_at": datetime.now().isoformat(),
            "deals":       all_deals,
            "email_sent":  email_sent,
            "email_error": email_error,
        })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/run", methods=["POST"])
def api_run():
    with _lock:
        if _state["status"] == "running":
            return jsonify({"error": "Scan already in progress"}), 409
    threading.Thread(target=_run_scrapers, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(dict(_state))


@app.route("/api/settings", methods=["GET"])
def get_settings():
    cfg = _load_config()
    # Mask password in response
    pw = cfg.get("email", {}).get("app_password", "")
    return jsonify({
        "email": {
            "sender":       cfg.get("email", {}).get("sender", ""),
            "recipient":    cfg.get("email", {}).get("recipient", ""),
            "app_password": "••••••••" if pw else "",
        },
        "threshold":  cfg.get("threshold", 0.25),
        "auto_email": cfg.get("auto_email", False),
        "env_mode":   bool(os.environ.get("EMAIL_APP_PASSWORD")),
    })


@app.route("/api/settings", methods=["POST"])
def save_settings():
    if os.environ.get("EMAIL_APP_PASSWORD"):
        return jsonify({"error": "Settings are controlled by environment variables on this deployment"}), 400
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return jsonify({"ok": True})


@app.route("/api/send-email", methods=["POST"])
def send_email_now():
    with _lock:
        deals = list(_state["deals"])
    if not deals:
        return jsonify({"error": "No deals — run a scan first"}), 400
    config = _load_config()
    if not config.get("email", {}).get("app_password"):
        return jsonify({"error": "Add email credentials in Settings"}), 400
    try:
        ammo_alert.send_email(deals, config)
        with _lock:
            _state["email_sent"] = True
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    local = not os.environ.get("PORT")
    if local:
        import webbrowser
        print(f"\n  Ammo Alert  →  http://localhost:{port}\n")
        webbrowser.open(f"http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, use_reloader=False)
