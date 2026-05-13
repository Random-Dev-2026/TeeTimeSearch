#!/usr/bin/env python3
"""
ForeUp Tee Time Monitor  —  GitHub Actions edition
====================================================
Designed to be triggered by GitHub Actions on a schedule (every 5 minutes).
This version runs ONCE, checks all target dates, sends a notification if a
matching slot is found, then exits.  GitHub Actions handles the scheduling.

No Tor in this version — GitHub Actions runs from Microsoft Azure IPs which
are residential/commercial and not flagged by ForeUp. A different IP is used
on every run automatically.

Secrets are stored in GitHub Actions Secrets (never in code or the repo).
See README_ACTIONS.md for full setup instructions.
"""

import os
import sys
import smtplib
import logging
import requests
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── CONFIGURATION  (all values come from GitHub Actions Secrets / env vars) ──

FACILITY_ID = "19765"
SCHEDULE_ID = os.environ["SCHEDULE_ID"]          # e.g. "2432"

FOREUP_USERNAME = os.environ["FOREUP_USERNAME"]
FOREUP_PASSWORD = os.environ["FOREUP_PASSWORD"]
LOGIN_TYPE      = os.environ.get("LOGIN_TYPE", "Resident")

WINDOW_START = os.environ.get("WINDOW_START", "07:00")
WINDOW_END   = os.environ.get("WINDOW_END",   "11:30")
MIN_PLAYERS  = int(os.environ.get("MIN_PLAYERS", "1"))

# Comma-separated YYYY-MM-DD dates, or blank for next 7 days
WATCH_DATES_RAW = os.environ.get("WATCH_DATES", "")

# Notification
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = "rezshark.bookings@gmail.com"
SMTP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]   # stored as GitHub Secret
NOTIFY_EMAIL  = os.environ["NOTIFY_EMAIL"]
NOTIFY_SMS    = os.environ.get("NOTIFY_SMS", "")

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

BASE_URL  = "https://foreupsoftware.com"
LOGIN_URL = f"{BASE_URL}/index.php/api/login"
TIMES_URL = f"{BASE_URL}/index.php/api/booking/times"

# ─── SESSION ──────────────────────────────────────────────────────────────────

session = requests.Session()
session.headers.update({
    # Realistic Chrome UA — GitHub Actions runners use Azure, not a datacenter
    # flagged for scraping, so this looks like a regular browser visit.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer":          f"{BASE_URL}/index.php/booking/{FACILITY_ID}/2431",
    "X-Requested-With": "XMLHttpRequest",
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":  "en-US,en;q=0.9",
})

# ─── AUTH ─────────────────────────────────────────────────────────────────────

def login() -> bool:
    try:
        resp = session.post(
            LOGIN_URL,
            data={
                "username":    FOREUP_USERNAME,
                "password":    FOREUP_PASSWORD,
                "login_type":  LOGIN_TYPE,
                "facility_id": FACILITY_ID,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success" or "token" in data or "user_id" in data:
            log.info("✅ Logged in.")
            return True
        if resp.cookies or session.cookies:
            log.info("✅ Logged in (cookie).")
            return True
        log.error("Login rejected: %s", data)
        return False
    except Exception as e:
        log.error("Login error: %s", e)
        return False

# ─── TEE TIME FETCHING ────────────────────────────────────────────────────────

def get_tee_times(for_date: str) -> list:
    api_date = datetime.strptime(for_date, "%Y-%m-%d").strftime("%m-%d-%Y")
    params = {
        "time":          "all",
        "date":          api_date,
        "holes":         "all",
        "players":       "0",
        "booking_class": "",
        "schedule_id":   SCHEDULE_ID,
        "facility_id":   FACILITY_ID,
        "specials_only": "0",
        "api_key":       "no_limits",
    }
    try:
        resp = session.get(TIMES_URL, params=params, timeout=20)
        if resp.status_code == 401:
            log.warning("Session expired mid-run, re-logging in…")
            login()
            resp = session.get(TIMES_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.error("Error fetching times for %s: %s", for_date, e)
        return []

# ─── FILTERING ────────────────────────────────────────────────────────────────

def time_in_window(time_str: str) -> bool:
    t = time_str.strip().lower()
    try:
        dt = (datetime.strptime(t, "%I:%M%p")
              if ("am" in t or "pm" in t)
              else datetime.strptime(t, "%H:%M"))
    except ValueError:
        try:
            dt = datetime.strptime(t, "%I%p")
        except ValueError:
            return False
    return WINDOW_START <= dt.strftime("%H:%M") <= WINDOW_END

def filter_times(times: list) -> list:
    return [
        t for t in times
        if time_in_window(t.get("time", ""))
        and int(t.get("available_spots", 0)) >= MIN_PLAYERS
    ]

# ─── DATES ────────────────────────────────────────────────────────────────────

def dates_to_check() -> list:
    if WATCH_DATES_RAW.strip():
        return [d.strip() for d in WATCH_DATES_RAW.split(",") if d.strip()]
    today = date.today()
    return [(today + timedelta(days=i)).isoformat() for i in range(8)]

# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────

def send_notification(subject: str, body: str) -> None:
    recipients = [r for r in [NOTIFY_EMAIL, NOTIFY_SMS] if r]
    if not recipients:
        log.warning("No notification recipients configured.")
        return
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        log.info("📬 Notification sent to: %s", recipients)
    except Exception as e:
        log.error("Failed to send notification: %s", e)

def format_alert(for_date: str, slots: list) -> tuple:
    subject = f"⛳ Tee Time Available — {for_date} ({len(slots)} slot(s))"
    lines = [
        f"Tee times are available on {for_date}!\n",
        f"Book now: {BASE_URL}/index.php/booking/{FACILITY_ID}/{SCHEDULE_ID}#/teetimes\n",
        "Matching slots:",
    ]
    for s in slots:
        lines.append(
            f"  • {s.get('time','?')}  |  {s.get('holes','?')} holes"
            f"  |  {s.get('available_spots','?')} spots  |  ${s.get('green_fee','?')}"
        )
    return subject, "\n".join(lines)

# ─── MAIN (run once) ──────────────────────────────────────────────────────────

def main():
    log.info("=== ForeUp Monitor (GitHub Actions — single run) ===")
    log.info("Schedule ID: %s | Window: %s–%s | Min players: %s",
             SCHEDULE_ID, WINDOW_START, WINDOW_END, MIN_PLAYERS)

    if not login():
        log.error("Login failed. Check FOREUP_USERNAME / FOREUP_PASSWORD secrets.")
        sys.exit(1)

    for watch_date in dates_to_check():
        log.info("Checking %s…", watch_date)
        times   = get_tee_times(watch_date)
        matches = filter_times(times)

        if matches:
            log.info("🎉 Found %d matching slot(s) on %s!", len(matches), watch_date)
            subject, body = format_alert(watch_date, matches)
            print("\n" + body + "\n")
            send_notification(subject, body)
            log.info("✅ Notification sent. Exiting.")
            sys.exit(0)   # success — GitHub Actions marks run as passed
        else:
            log.info("   No matching slots on %s.", watch_date)

    log.info("No matching slots found this run. GitHub Actions will try again in 5 minutes.")
    sys.exit(0)  # normal exit — not a failure

if __name__ == "__main__":
    main()
