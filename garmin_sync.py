"""
Daily Garmin Connect -> Supabase sync.

Pulls the latest sleep, HRV, VO2 max, body battery, and resting HR from
Garmin Connect and upserts a single row (keyed by date) into Supabase.

Auth: restores the garminconnect DI-token session captured in Colab
(GARMIN_TOKEN_B64 = base64 of Garmin().client.dumps()). garminconnect
auto-refreshes the DI token on login, so this runs unattended with no
interactive login / MFA on every run.

Env vars (GitHub Actions secrets):
    GARMIN_TOKEN_B64      - base64 of the DI-token JSON string
    SUPABASE_URL          - https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  - service_role key (server-side only)
"""

import base64
import os
import tempfile
from datetime import date

import requests
from garminconnect import Garmin

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_TABLE = "garmin_daily"


def get_client() -> Garmin:
    """Restore the Garmin session from the base64 DI-token string."""
    token_json = base64.b64decode(os.environ["GARMIN_TOKEN_B64"]).decode()
    g = Garmin()
    # login() treats a >512-char tokenstore as a token string, else as a path.
    if len(token_json) > 512:
        g.login(tokenstore=token_json)
    else:
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "garmin_tokens.json"), "w") as f:
            f.write(token_json)
        g.login(tokenstore=d)
    return g


def fetch_metrics(g: Garmin, day: str) -> dict:
    data = {"date": day}

    try:
        sleep = g.get_sleep_data(day) or {}
        d = sleep.get("dailySleepDTO", {}) or {}
        data["sleep_seconds"] = d.get("sleepTimeSeconds")
        data["deep_sleep_seconds"] = d.get("deepSleepSeconds")
        data["rem_sleep_seconds"] = d.get("remSleepSeconds")
        data["light_sleep_seconds"] = d.get("lightSleepSeconds")
        scores = d.get("sleepScores") or {}
        overall = scores.get("overall") or {}
        data["sleep_score"] = overall.get("value")
    except Exception as e:
        print("sleep fetch failed:", e)

    try:
        hrv = g.get_hrv_data(day) or {}
        summary = hrv.get("hrvSummary", {}) or {}
        data["hrv_last_night_avg"] = summary.get("lastNightAvg")
        data["hrv_status"] = summary.get("status")
    except Exception as e:
        print("hrv fetch failed:", e)

    try:
        mm = g.get_max_metrics(day) or []
        if mm:
            generic = mm[0].get("generic", {}) or {}
            data["vo2_max"] = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
    except Exception as e:
        print("vo2max fetch failed:", e)

    try:
        bb = g.get_body_battery(day, day) or []
        if bb:
            data["body_battery_charged"] = bb[0].get("charged")
            data["body_battery_drained"] = bb[0].get("drained")
    except Exception as e:
        print("body battery fetch failed:", e)

    try:
        stats = g.get_stats(day) or {}
        data["resting_hr"] = stats.get("restingHeartRate")
    except Exception as e:
        print("stats fetch failed:", e)

    return data


def upsert(row: dict):
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=date"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    resp = requests.post(url, headers=headers, json=[row])
    resp.raise_for_status()
    print("Synced", row["date"], "->", {k: v for k, v in row.items() if k != "date"})


def main():
    g = get_client()
    today = date.today().isoformat()
    row = fetch_metrics(g, today)
    upsert(row)


if __name__ == "__main__":
    main()
