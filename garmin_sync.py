"""
Daily Garmin Connect -> Supabase sync.

Pulls the latest sleep, HRV, VO2 max, body battery, resting HR, calories and
steps from Garmin Connect and upserts one row per date into Supabase.

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
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from datetime import datetime

import requests
from garminconnect import Garmin

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_TABLE = "garmin_daily"

AUTH_TABLE = "garmin_auth"
AUTH_ID = "default"

# Runners are UTC. At 8pm Denver it is already tomorrow in UTC, so asking for
# "today" would skip the day actually being lived. Anchor to local time.
LOCAL_TZ = ZoneInfo("America/Denver")

# Re-read yesterday as well as today. Garmin keeps revising a day's figures for
# hours after midnight as the watch finishes uploading, and with several runs a
# day the last one of the evening is often the only one that sees the full
# picture. One extra call, and a whole class of off-by-one disappears.
DAYS_BACK = 2


def _sb_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def load_token_b64() -> str:
    """Prefer the self-refreshing copy in Supabase. Fall back to the GitHub secret."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{AUTH_TABLE}",
            params={"select": "token_b64", "id": f"eq.{AUTH_ID}"},
            headers=_sb_headers(),
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if rows and rows[0].get("token_b64"):
            print("[auth] using token from Supabase")
            return rows[0]["token_b64"]
    except Exception as e:
        print(f"[auth] couldn't read token from Supabase ({e}) - falling back to secret")

    print("[auth] using GARMIN_TOKEN_B64 secret (bootstrap)")
    return os.environ["GARMIN_TOKEN_B64"]


def save_token_b64(token_b64: str) -> None:
    """Write the REFRESHED token back. This is the line that fixes the bug."""
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{AUTH_TABLE}",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
            json=[{"id": AUTH_ID, "token_b64": token_b64}],
            timeout=30,
        )
        r.raise_for_status()
        print("[auth] refreshed token saved back to Supabase")
    except Exception as e:
        print(f"[auth] WARNING - couldn't save refreshed token ({e})")


def get_client() -> Garmin:
    """Restore the Garmin session, then persist whatever garth refreshed.

    garth changed what dumps()/loads() speak. Older versions handed back raw
    JSON and expected raw JSON; newer ones hand back base64 and expect base64.
    The script assumed the old contract on both sides, which produced two bugs
    at once after a library update:

      loading  - we base64-decoded the stored token and passed JSON to garth,
                 which base64-decoded it AGAIN:
                 UnicodeDecodeError: 'utf-8' codec can't decode byte 0x91
      saving   - dumps() already returned base64 and we base64-encoded it a
                 second time, storing a double-wrapped token for tomorrow

    Rather than pin to one garth and be broken by the next change, detect which
    dialect is in front of us and speak it.
    """
    stored_b64 = load_token_b64()

    def _decoded(s: str):
        try:
            return base64.b64decode(s).decode()
        except Exception:
            return None

    g = Garmin()
    attempts = []

    # Newer garth: hand it the base64 exactly as stored.
    attempts.append(("base64 as stored", lambda: g.login(tokenstore=stored_b64)))

    # Older garth: hand it the decoded JSON.
    plain = _decoded(stored_b64)
    if plain:
        attempts.append(("decoded JSON", lambda: g.login(tokenstore=plain)))
        # Oldest path: a directory containing the token file.
        def _via_dir():
            d = tempfile.mkdtemp()
            with open(os.path.join(d, "garmin_tokens.json"), "w") as f:
                f.write(plain)
            return g.login(tokenstore=d)
        attempts.append(("token directory", _via_dir))

    last = None
    for label, fn in attempts:
        try:
            fn()
            print(f"[auth] logged in via {label}")
            last = None
            break
        except Exception as e:
            print(f"[auth] {label} did not work ({type(e).__name__}: {e})")
            last = e
    if last is not None:
        raise last

    # garth refreshed the session during login(). Save it, or it dies with this
    # process and tomorrow replays today's stale token.
    try:
        try:
            fresh = g.garth.dumps()
        except AttributeError:
            fresh = g.client.dumps()

        # Store base64, always — but only wrap it if it is not already wrapped.
        # A string starting with { or [ is raw JSON and needs encoding; one that
        # survives a base64 round-trip is already encoded and must be left alone.
        stripped = fresh.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            out = base64.b64encode(fresh.encode()).decode()
            print("[auth] dumps() returned JSON - encoding before save")
        else:
            out = fresh
            print("[auth] dumps() returned base64 already - saving as-is")
        save_token_b64(out)
    except Exception as e:
        print(f"[auth] WARNING - couldn't dump refreshed token ({e})")

    return g


def _int(v):
    """Garmin returns these as ints, floats or strings depending on endpoint."""
    try:
        if v is None:
            return None
        n = round(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


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

    # get_stats() IS Garmin's daily summary. It was already being called for
    # resting HR alone while the whole energy picture came back in the same
    # response and was discarded. Nothing extra is fetched here - the calories
    # and steps below were always arriving, just never read.
    try:
        stats = g.get_stats(day) or {}
        data["resting_hr"] = stats.get("restingHeartRate")

        data["active_calories"] = _int(stats.get("activeKilocalories"))
        data["resting_calories"] = _int(stats.get("bmrKilocalories"))
        data["total_calories"] = _int(stats.get("totalKilocalories"))
        data["steps"] = _int(stats.get("totalSteps"))
        data["floors_climbed"] = _int(stats.get("floorsAscended"))

        # If any of the above land empty, the key name is wrong for this
        # account or endpoint version. Uncomment to see what actually came back:
        # print("[stats] raw keys:", sorted(stats.keys()))
    except Exception as e:
        print("stats fetch failed:", e)

    data["synced_at"] = datetime.now(LOCAL_TZ).isoformat()
    return data


def upsert(row: dict):
    # Strip empties before writing. This matters far more now than it did on a
    # once-a-day schedule: a midday run that can't see last night's sleep would
    # otherwise send sleep_seconds=None and wipe what the morning run stored.
    # Anything Garmin hasn't filled in yet keeps its previous value instead.
    clean = {k: v for k, v in row.items() if v is not None}
    if len(clean) <= 2:  # nothing beyond date + synced_at
        print("Skipped", row["date"], "- nothing came back")
        return

    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=date"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    resp = requests.post(url, headers=headers, json=[clean], timeout=60)
    resp.raise_for_status()
    print("Synced", clean["date"], "->", {k: v for k, v in clean.items() if k != "date"})


def main():
    g = get_client()
    today = datetime.now(LOCAL_TZ).date()

    failures = 0
    for i in range(DAYS_BACK):
        day = (today - timedelta(days=i)).isoformat()
        try:
            upsert(fetch_metrics(g, day))
        except Exception as e:
            # one bad day shouldn't take the whole run down with it
            failures += 1
            print(f"FAILED {day}: {e}")

    if failures == DAYS_BACK:
        raise SystemExit("every day failed - see errors above")


if __name__ == "__main__":
    main()
