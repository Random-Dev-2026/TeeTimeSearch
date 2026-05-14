#!/usr/bin/env python3
"""
ForeUp Tee Time Monitor — GitHub Actions edition
=================================================
Triggered via workflow_dispatch from the GUI.
- Polls ForeUp every 30 seconds for up to 2 hours
- Stops immediately when a matching slot is found
- Always sends a notification at the end (found OR not found)
- All config passed as workflow inputs → environment variables
"""

import os, sys, time, smtplib, logging, requests
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config from workflow inputs ────────────────────────────────────────────────
FACILITY_ID     = "19765"
SCHEDULE_ID     = os.environ.get("SCHEDULE_ID",     "2432")
FOREUP_USERNAME = os.environ.get("FOREUP_USERNAME", "")
FOREUP_PASSWORD = os.environ.get("FOREUP_PASSWORD", "")
LOGIN_TYPE      = os.environ.get("LOGIN_TYPE",      "Resident")
WINDOW_START    = os.environ.get("WINDOW_START",    "07:00")
WINDOW_END      = os.environ.get("WINDOW_END",      "11:30")
MIN_PLAYERS     = int(os.environ.get("MIN_PLAYERS", "1"))
WATCH_DATES_RAW = os.environ.get("WATCH_DATES",     "")
NOTIFY_EMAIL    = os.environ.get("NOTIFY_EMAIL",    "")
NOTIFY_SMS      = os.environ.get("NOTIFY_SMS",      "")
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL", "30"))  # seconds between checks
MAX_RUNTIME     = 2 * 60 * 60  # 2 hours in seconds

# ── Gmail (stored as GitHub repo secret — never in workflow inputs) ────────────
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = "rezshark.bookings@gmail.com"
SMTP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

BASE_URL  = "https://foreupsoftware.com"
LOGIN_URL = f"{BASE_URL}/index.php/api/login"
TIMES_URL = f"{BASE_URL}/index.php/api/booking/times"

# ── Session ───────────────────────────────────────────────────────────────────
session = requests.Session()
session.headers.update({
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

# ── Auth ──────────────────────────────────────────────────────────────────────
def login() -> bool:
    """Try multiple login endpoints used by ForeUp consumer booking pages."""
    urls_to_try = [
        f"{BASE_URL}/index.php/api/login",
        f"{BASE_URL}/index.php/api/customers/login",
        f"{BASE_URL}/index.php/booking/ajaxreq",
    ]
    payloads = [
        {"username": FOREUP_USERNAME, "password": FOREUP_PASSWORD,
         "login_type": LOGIN_TYPE, "facility_id": FACILITY_ID},
        {"email": FOREUP_USERNAME, "password": FOREUP_PASSWORD,
         "login_type": LOGIN_TYPE, "facility_id": FACILITY_ID},
    ]
    for url in urls_to_try:
        for payload in payloads:
            try:
                log.info("Trying login: %s", url)
                resp = session.post(url, data=payload, timeout=20)
                log.info("  HTTP %d", resp.status_code)
                if resp.status_code == 404:
                    break  # this URL doesn't exist, try next
                if resp.status_code in (200, 201):
                    try:
                        data = resp.json()
                        log.info("  Response: %s", str(data)[:200])
                        if (data.get("status") == "success"
                                or "token" in data or "user_id" in data
                                or "customer" in data):
                            log.info("✅ Logged in via %s", url)
                            return True
                    except Exception:
                        pass
                    if session.cookies:
                        log.info("✅ Logged in (cookie) via %s", url)
                        return True
            except Exception as e:
                log.warning("  Error: %s", e)
    log.error("All login attempts failed. user=%s type=%s facility=%s",
              FOREUP_USERNAME, LOGIN_TYPE, FACILITY_ID)
    return False

# ── Fetch ─────────────────────────────────────────────────────────────────────
def get_tee_times(for_date: str) -> list:
    api_date = datetime.strptime(for_date, "%Y-%m-%d").strftime("%m-%d-%Y")
    params = {
        "time": "all", "date": api_date, "holes": "all", "players": "0",
        "booking_class": "", "schedule_id": SCHEDULE_ID,
        "facility_id": FACILITY_ID, "specials_only": "0", "api_key": "no_limits",
    }
    try:
        resp = session.get(TIMES_URL, params=params, timeout=20)
        if resp.status_code == 401:
            log.warning("Session expired, re-logging in…"); login()
            resp = session.get(TIMES_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.error("Error fetching %s: %s", for_date, e); return []

# ── Filter ────────────────────────────────────────────────────────────────────
def time_in_window(time_str: str) -> bool:
    t = time_str.strip().lower()
    try:
        dt = (datetime.strptime(t, "%I:%M%p") if ("am" in t or "pm" in t)
              else datetime.strptime(t, "%H:%M"))
    except ValueError:
        try: dt = datetime.strptime(t, "%I%p")
        except: return False
    return WINDOW_START <= dt.strftime("%H:%M") <= WINDOW_END

def filter_times(times: list) -> list:
    return [t for t in times
            if time_in_window(t.get("time", ""))
            and int(t.get("available_spots", 0)) >= MIN_PLAYERS]

# ── Dates ─────────────────────────────────────────────────────────────────────
def dates_to_check() -> list:
    if WATCH_DATES_RAW.strip():
        return [d.strip() for d in WATCH_DATES_RAW.split(",") if d.strip()]
    today = date.today()
    return [(today + timedelta(days=i)).isoformat() for i in range(8)]

# ── Notify ────────────────────────────────────────────────────────────────────
def send_notification(subject: str, body: str) -> None:
    recipients = [r for r in [NOTIFY_EMAIL, NOTIFY_SMS] if r.strip()]
    if not recipients:
        log.warning("No recipients configured."); return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo(); server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        log.info("📬 Notification sent to: %s", recipients)
    except Exception as e:
        log.error("Failed to send notification: %s", e)

def notify_found(for_date: str, slots: list) -> None:
    subject = f"⛳ Tee Time Available — {for_date} ({len(slots)} slot(s))"
    lines = [
        f"Good news! Tee times are available on {for_date}.\n",
        f"Book now: {BASE_URL}/index.php/booking/{FACILITY_ID}/{SCHEDULE_ID}#/teetimes\n",
        "Matching slots:",
    ]
    for s in slots:
        lines.append(
            f"  • {s.get('time','?')}  |  {s.get('holes','?')} holes"
            f"  |  {s.get('available_spots','?')} spots  |  ${s.get('green_fee','?')}"
        )
    send_notification(subject, "\n".join(lines))

def notify_not_found(duration_mins: int) -> None:
    dates = dates_to_check()
    date_range = f"{dates[0]} to {dates[-1]}" if len(dates) > 1 else dates[0]
    subject = "⛳ No Tee Times Found"
    body = (
        f"The ForeUp monitor ran for {duration_mins} minutes and found no available tee times.\n\n"
        f"Dates checked:   {date_range}\n"
        f"Time window:     {WINDOW_START}–{WINDOW_END} EST\n"
        f"Min players:     {MIN_PLAYERS}\n"
        f"Course:          Schedule {SCHEDULE_ID}\n\n"
        "You can run the monitor again from the web app when you're ready to try again."
    )
    send_notification(subject, body)

# ── Main loop (runs for up to 2 hours) ────────────────────────────────────────
def main():
    log.info("=== ForeUp Monitor (GitHub Actions) ===")
    log.info("Window: %s–%s | Min players: %s | Max runtime: 2 hours", 
             WINDOW_START, WINDOW_END, MIN_PLAYERS)

    if not FOREUP_USERNAME or not FOREUP_PASSWORD:
        log.error("Missing ForeUp credentials."); sys.exit(1)
    if not SMTP_PASSWORD:
        log.error("Missing GMAIL_APP_PASSWORD secret."); sys.exit(1)

    if not login():
        log.error("Login failed."); sys.exit(1)

    start_time  = time.time()
    check_count = 0

    while True:
        elapsed = time.time() - start_time

        # ── 2-hour hard stop ──────────────────────────────────────────────────
        if elapsed >= MAX_RUNTIME:
            duration_mins = int(elapsed / 60)
            log.info("⏱ 2-hour limit reached after %d checks. Sending not-found notice.", check_count)
            notify_not_found(duration_mins)
            sys.exit(0)

        check_count += 1
        log.info("── Check #%d (%.0f min elapsed) ──", check_count, elapsed / 60)

        for watch_date in dates_to_check():
            log.info("  Checking %s…", watch_date)
            times   = get_tee_times(watch_date)
            matches = filter_times(times)

            if matches:
                log.info("🎉 Found %d slot(s) on %s!", len(matches), watch_date)
                notify_found(watch_date, matches)
                log.info("✅ Done — tee time found and notification sent.")
                sys.exit(0)

        remaining_mins = int((MAX_RUNTIME - elapsed) / 60)
        log.info("  No matches yet. %d min remaining. Sleeping %ds…",
                 remaining_mins, POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
