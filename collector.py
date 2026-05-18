"""
Stage 0: Garmin Connect data collector — proof-of-concept.

Authenticates with Garmin Connect via the garminconnect library and fetches
data for every endpoint the health app will use. Saves raw JSON responses to
collector_output/ for inspection. Proves the API works before building the app.

Usage:
    pip install garminconnect python-dotenv
    cp .env.example .env          # fill in GARMIN_EMAIL and GARMIN_PASSWORD
    python collector.py           # fetches yesterday
    python collector.py 2026-05-10  # fetches a specific date

On first run you may be prompted for a Garmin MFA code. After that, saved
tokens in .garmin_tokens/ are reused automatically.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("GARMIN_EMAIL")
PASSWORD = os.getenv("GARMIN_PASSWORD")
TOKEN_DIR = Path(".garmin_tokens")
OUTPUT_DIR = Path("collector_output")


def get_mfa():
    return input("  Garmin MFA code: ").strip()


def build_client():
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectTooManyRequestsError,
    )

    client = Garmin(email=EMAIL, password=PASSWORD, prompt_mfa=get_mfa)

    TOKEN_DIR.mkdir(exist_ok=True)
    try:
        # login() loads saved tokens from TOKEN_DIR if valid;
        # otherwise authenticates with credentials and saves tokens automatically.
        client.login(tokenstore=str(TOKEN_DIR))
        print(f"Authenticated as {client.display_name} (tokens: {TOKEN_DIR})")
    except GarminConnectTooManyRequestsError as exc:
        print(f"Rate limited by Garmin: {exc}")
        print("Wait a few minutes and try again.")
        sys.exit(1)
    except GarminConnectAuthenticationError as exc:
        print(f"Authentication failed: {exc}")
        sys.exit(1)

    return client


def save(name: str, date_str: str, data) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{date_str}_{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path


def summarise(data) -> str:
    if data is None:
        return "None"
    if isinstance(data, list):
        if not data:
            return "[] (empty)"
        sample_keys = list(data[0].keys()) if isinstance(data[0], dict) else []
        return f"{len(data)} records | keys: {sample_keys[:6]}"
    if isinstance(data, dict):
        return f"1 record | keys: {list(data.keys())[:8]}"
    return str(data)[:120]


def run_endpoint(label: str, fn, date_str: str) -> bool:
    try:
        data = fn()
        path = save(label, date_str, data)
        print(f"  OK  {label}")
        print(f"      {summarise(data)}")
        print(f"      saved -> {path.name}")
        return True
    except Exception as exc:
        print(f"  ERR {label}: {exc}")
        return False


def main():
    if not EMAIL or not PASSWORD:
        print("Error: set GARMIN_EMAIL and GARMIN_PASSWORD in .env")
        sys.exit(1)

    target = date.today() - timedelta(days=1)
    if len(sys.argv) > 1:
        try:
            target = date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"Invalid date '{sys.argv[1]}' — use YYYY-MM-DD")
            sys.exit(1)

    date_str = target.isoformat()
    print(f"Garmin Connect collector — fetching data for {date_str}\n")

    client = build_client()
    print()

    act_start = (target - timedelta(days=1)).isoformat()
    act_end = date_str

    endpoints = [
        ("daily_wellness",      lambda: client.get_stats(date_str)),
        ("intraday_hr",         lambda: client.get_heart_rates(date_str)),
        ("intraday_stress",     lambda: client.get_stress_data(date_str)),
        ("body_battery",        lambda: client.get_body_battery(date_str, date_str)),
        ("sleep",               lambda: client.get_sleep_data(date_str)),
        ("spo2",                lambda: client.get_spo2_data(date_str)),
        ("respiration",         lambda: client.get_respiration_data(date_str)),
        ("hrv",                 lambda: client.get_hrv_data(date_str)),
        ("activities",          lambda: client.get_activities_by_date(act_start, act_end)),
    ]

    print("Fetching wellness and health endpoints:")
    results = {}
    for label, fn in endpoints:
        results[label] = run_endpoint(label, fn, date_str)
        print()

    print("Fetching activity FIT file (most recent activity):")
    try:
        recent = client.get_activities(0, 1)
        if recent:
            activity_id = recent[0]["activityId"]
            activity_name = recent[0].get("activityName", "unknown")
            activity_date = recent[0].get("startTimeLocal", "")[:10]
            print(f"  Most recent: '{activity_name}' on {activity_date} (id={activity_id})")

            fit_data = client.download_activity(
                activity_id,
                dl_fmt=client.ActivityDownloadFormat.ORIGINAL,
            )
            fit_path = OUTPUT_DIR / f"{activity_date}_activity_{activity_id}.fit"
            with open(fit_path, "wb") as f:
                f.write(fit_data)
            size_kb = round(len(fit_data) / 1024, 1)
            print(f"  OK  activity_fit: {size_kb} KB -> {fit_path.name}")
            results["activity_fit"] = True
        else:
            print("  SKIP activity_fit: no activities found")
            results["activity_fit"] = None
    except Exception as exc:
        print(f"  ERR activity_fit: {exc}")
        results["activity_fit"] = False

    ok = sum(1 for v in results.values() if v is True)
    skipped = sum(1 for v in results.values() if v is None)
    failed = sum(1 for v in results.values() if v is False)

    print(f"\n{'='*50}")
    print(f"Results for {date_str}:")
    print(f"  {ok} succeeded | {failed} failed | {skipped} skipped")
    print(f"  Raw JSON saved to {OUTPUT_DIR}/")

    if failed:
        print("\nFailed endpoints:")
        for label, result in results.items():
            if result is False:
                print(f"  - {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
