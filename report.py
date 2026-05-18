"""
Garmin 90-day health report generator.

Fetches 90 days of data from Garmin Connect and produces a structured
markdown document designed for AI longevity evaluation.

Raw API responses are cached in report_cache/ so re-running regenerates
the document without hitting the API again. Only missing dates are fetched.

Usage:
    python report.py                        # last 90 days ending yesterday
    python report.py --end 2026-05-01       # 90 days ending on a specific date
    python report.py --days 30              # last 30 days instead of 90
    python report.py --output my_report.md  # custom output filename
"""

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("GARMIN_EMAIL")
PASSWORD = os.getenv("GARMIN_PASSWORD")
TOKEN_DIR = Path(".garmin_tokens")
CACHE_DIR = Path("report_cache")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

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
        client.login(tokenstore=str(TOKEN_DIR))
        print(f"Authenticated as {client.display_name}")
    except GarminConnectTooManyRequestsError as exc:
        print(f"Rate limited: {exc}\nWait a few minutes and try again.")
        sys.exit(1)
    except GarminConnectAuthenticationError as exc:
        print(f"Authentication failed: {exc}")
        sys.exit(1)
    return client


# ---------------------------------------------------------------------------
# Cached fetching — one JSON file per (date, endpoint)
# ---------------------------------------------------------------------------

def cache_path(date_str: str, endpoint: str) -> Path:
    return CACHE_DIR / f"{date_str}_{endpoint}.json"


def fetch_cached(client, date_str: str, endpoint: str, fetcher) -> dict | list | None:
    path = cache_path(date_str, endpoint)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        data = fetcher()
        CACHE_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, default=str), encoding="utf-8")
        return data
    except Exception as exc:
        print(f"    WARN {endpoint} on {date_str}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Per-day data extraction
# ---------------------------------------------------------------------------

def extract_day(client, d: date) -> dict:
    date_str = d.isoformat()
    row = {"date": date_str}

    # --- Wellness (covers HR, stress, body battery, SpO2, respiration, steps) ---
    w = fetch_cached(client, date_str, "wellness", lambda: client.get_stats(date_str))
    if w:
        row["resting_hr"]           = w.get("restingHeartRate")
        row["hr_min"]               = w.get("minHeartRate")
        row["hr_max"]               = w.get("maxHeartRate")
        row["steps"]                = w.get("totalSteps")
        row["active_kcal"]          = w.get("activeKilocalories")
        row["total_kcal"]           = w.get("totalKilocalories")
        row["mod_intensity_mins"]   = w.get("moderateIntensityMinutes", 0)
        row["vig_intensity_mins"]   = w.get("vigorousIntensityMinutes", 0)
        row["sedentary_hours"]      = round((w.get("sedentarySeconds") or 0) / 3600, 1)
        row["avg_stress"]           = w.get("averageStressLevel")
        row["max_stress"]           = w.get("maxStressLevel")
        row["stress_qualifier"]     = w.get("stressQualifier")
        row["bb_at_wake"]           = w.get("bodyBatteryAtWakeTime")
        row["bb_highest"]           = w.get("bodyBatteryHighestValue")
        row["bb_lowest"]            = w.get("bodyBatteryLowestValue")
        row["bb_end_of_day"]        = w.get("bodyBatteryMostRecentValue")
        row["bb_charged"]           = w.get("bodyBatteryChargedValue")
        row["bb_drained"]           = w.get("bodyBatteryDrainedValue")
        row["spo2_avg"]             = w.get("averageSpo2")
        row["spo2_low"]             = w.get("lowestSpo2")
        row["resp_avg_waking"]      = w.get("avgWakingRespirationValue")
        row["resp_high"]            = w.get("highestRespirationValue")
        row["resp_low"]             = w.get("lowestRespirationValue")

    # --- Sleep ---
    s = fetch_cached(client, date_str, "sleep", lambda: client.get_sleep_data(date_str))
    if s:
        dto = s.get("dailySleepDTO") or {}
        row["sleep_total_mins"]     = round((dto.get("sleepTimeSeconds") or 0) / 60)
        row["sleep_deep_mins"]      = round((dto.get("deepSleepSeconds") or 0) / 60)
        row["sleep_light_mins"]     = round((dto.get("lightSleepSeconds") or 0) / 60)
        row["sleep_rem_mins"]       = round((dto.get("remSleepSeconds") or 0) / 60)
        row["sleep_awake_mins"]     = round((dto.get("awakeSleepSeconds") or 0) / 60)
        row["sleep_avg_hr"]         = dto.get("avgHeartRate")
        row["sleep_spo2_avg"]       = dto.get("averageSpO2Value")
        row["sleep_spo2_low"]       = dto.get("lowestSpO2Value")
        row["sleep_resp_avg"]       = dto.get("averageRespirationValue")
        row["sleep_stress_avg"]     = dto.get("avgSleepStress")
        row["breathing_disruption"] = dto.get("breathingDisruptionSeverity", "NONE")
        row["restless_moments"]     = s.get("restlessMomentsCount")
        row["sleep_feedback"]       = dto.get("sleepScoreFeedback")

    # --- HRV ---
    h = fetch_cached(client, date_str, "hrv", lambda: client.get_hrv_data(date_str))
    if h:
        summary = h.get("hrvSummary") or {}
        baseline = summary.get("baseline") or {}
        row["hrv_last_night"]       = summary.get("lastNightAvg")
        row["hrv_weekly_avg"]       = summary.get("weeklyAvg")
        row["hrv_5min_high"]        = summary.get("lastNight5MinHigh")
        row["hrv_status"]           = summary.get("status")
        row["hrv_baseline_low"]     = baseline.get("balancedLow")
        row["hrv_baseline_high"]    = baseline.get("balancedUpper")

    return row


# ---------------------------------------------------------------------------
# Activities for the full date range (single API call)
# ---------------------------------------------------------------------------

def fetch_activities(client, start_date: date, end_date: date) -> list:
    path = CACHE_DIR / f"activities_{start_date}_{end_date}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        acts = client.get_activities_by_date(start_date.isoformat(), end_date.isoformat())
        CACHE_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(acts, default=str), encoding="utf-8")
        return acts or []
    except Exception as exc:
        print(f"  WARN activities: {exc}")
        return []


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------

def _val(v, unit="", missing="—"):
    return f"{v}{unit}" if v is not None else missing


def _pct(part, total):
    if part and total and total > 0:
        return round(part / total * 100)
    return None


def avg_of(rows: list, key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def trend(rows: list, key: str) -> str:
    """Return a simple trend description over the period."""
    vals = [(i, r[key]) for i, r in enumerate(rows) if r.get(key) is not None]
    if len(vals) < 7:
        return "insufficient data"
    first_avg = sum(v for _, v in vals[:7]) / 7
    last_avg  = sum(v for _, v in vals[-7:]) / 7
    delta = last_avg - first_avg
    if abs(delta) < 1:
        return "stable"
    direction = "improving" if delta > 0 else "declining"
    return f"{direction} ({first_avg:.0f} → {last_avg:.0f})"


def hrv_trend(rows: list) -> str:
    """Higher HRV = better, so 'improving' means rising."""
    return trend(rows, "hrv_last_night")


def rhr_trend(rows: list) -> str:
    """Lower RHR = better, so invert the label."""
    vals = [(i, r["resting_hr"]) for i, r in enumerate(rows) if r.get("resting_hr") is not None]
    if len(vals) < 7:
        return "insufficient data"
    first_avg = sum(v for _, v in vals[:7]) / 7
    last_avg  = sum(v for _, v in vals[-7:]) / 7
    delta = last_avg - first_avg
    if abs(delta) < 1:
        return "stable"
    direction = "improving" if delta < 0 else "declining"
    return f"{direction} ({first_avg:.0f} → {last_avg:.0f})"


def generate_report(rows: list, activities: list, end_date: date, days: int) -> str:
    start_date = end_date - timedelta(days=days - 1)
    generated  = date.today().isoformat()
    data_rows  = [r for r in rows if any(r.get(k) for k in ("resting_hr", "hrv_last_night", "sleep_total_mins"))]

    lines = []

    # ---- Header ----
    lines += [
        f"# Garmin Health Data — {days}-Day Report",
        f"",
        f"**Period:** {start_date} to {end_date}  ",
        f"**Generated:** {generated}  ",
        f"**Device:** Garmin Fenix 7X  ",
        f"**Data source:** Garmin Connect via garminconnect library  ",
        f"",
        "---",
        "",
    ]

    # ---- Context note for AI ----
    lines += [
        "## Note for AI Evaluator",
        "",
        "This document contains wearable sensor data for longitudinal health and "
        "longevity evaluation. All values are recorded continuously by the device "
        "during normal daily life — not clinic measurements. Interpret accordingly:",
        "",
        "- **HRV (Heart Rate Variability):** Nightly average during sleep window. "
          "Higher = better autonomic function. Track trend and deviation from personal baseline.",
        "- **Resting HR:** Daily resting value. Lower (within reason) = better cardiovascular fitness.",
        "- **Sleep stages:** Deep sleep drives cellular repair; REM drives cognitive health. "
          "Percentages matter as much as absolute minutes.",
        "- **Sleep SpO2 min:** Values consistently below 90% warrant sleep apnea evaluation.",
        "- **Body battery at wake:** Garmin's recovery score (0–100) at the moment of waking. "
          "Reflects overnight recovery quality. Below 50 consistently = under-recovery.",
        "- **Breathing disruption severity:** NONE / MILD / MODERATE / SEVERE. "
          "Anything above NONE warrants attention.",
        "- **Stress qualifier:** Garmin's daily stress classification based on HRV fluctuation patterns.",
        "",
        "---",
        "",
    ]

    # ---- 90-Day Summary ----
    lines += [
        "## Summary Statistics",
        "",
        f"| Metric | 90-Day Average | Trend |",
        f"|--------|---------------|-------|",
        f"| Resting HR | {_val(avg_of(rows, 'resting_hr'), ' bpm')} | {rhr_trend(rows)} |",
        f"| HRV (last night avg) | {_val(avg_of(rows, 'hrv_last_night'), ' ms')} | {hrv_trend(rows)} |",
        f"| Total sleep | {_val(avg_of(rows, 'sleep_total_mins'), ' min')} | {trend(rows, 'sleep_total_mins')} |",
        f"| Deep sleep | {_val(avg_of(rows, 'sleep_deep_mins'), ' min')} | {trend(rows, 'sleep_deep_mins')} |",
        f"| REM sleep | {_val(avg_of(rows, 'sleep_rem_mins'), ' min')} | {trend(rows, 'sleep_rem_mins')} |",
        f"| Sleep SpO2 low | {_val(avg_of(rows, 'sleep_spo2_low'), '%')} | — |",
        f"| Body battery at wake | {_val(avg_of(rows, 'bb_at_wake'), '')} | {trend(rows, 'bb_at_wake')} |",
        f"| Avg daily stress | {_val(avg_of(rows, 'avg_stress'), '')} | {trend(rows, 'avg_stress')} |",
        f"| Daily steps | {_val(avg_of(rows, 'steps'), '')} | {trend(rows, 'steps')} |",
        f"| Waking respiration | {_val(avg_of(rows, 'resp_avg_waking'), ' br/min')} | — |",
        "",
        "---",
        "",
    ]

    # ---- Daily Log Table ----
    lines += [
        "## Daily Log",
        "",
        "Columns: RHR = resting heart rate (bpm) | HRV = last-night average (ms) | "
        "Deep/REM = sleep stage minutes | SpO2L = lowest SpO2 during sleep (%) | "
        "BD = breathing disruption | BBwake = body battery at wake | BBend = body battery end of day | "
        "Stress = avg daily stress | Steps",
        "",
        "| Date | RHR | HRV | HRV Status | Deep | REM | Sleep h | SpO2L | BD | BBwake | BBend | Stress | Steps |",
        "|------|-----|-----|------------|------|-----|---------|-------|----|--------|-------|--------|-------|",
    ]

    for r in rows:
        sleep_h = f"{r['sleep_total_mins']//60}h{r['sleep_total_mins']%60:02d}m" if r.get("sleep_total_mins") else "—"
        bd = (r.get("breathing_disruption") or "—").replace("NONE", "✓").replace("MILD", "MILD").replace("MODERATE", "MOD").replace("SEVERE", "SEV")
        lines.append(
            f"| {r['date']} "
            f"| {_val(r.get('resting_hr'))} "
            f"| {_val(r.get('hrv_last_night'))} "
            f"| {_val(r.get('hrv_status'), missing='—')} "
            f"| {_val(r.get('sleep_deep_mins'))} "
            f"| {_val(r.get('sleep_rem_mins'))} "
            f"| {sleep_h} "
            f"| {_val(r.get('sleep_spo2_low'))} "
            f"| {bd} "
            f"| {_val(r.get('bb_at_wake'))} "
            f"| {_val(r.get('bb_end_of_day'))} "
            f"| {_val(r.get('avg_stress'))} "
            f"| {_val(r.get('steps'))} |"
        )

    lines += ["", "---", ""]

    # ---- Last 7 Days Detail ----
    recent = rows[-7:]
    lines += [
        "## Last 7 Days — Detailed",
        "",
    ]
    for r in recent:
        total_sleep = r.get("sleep_total_mins") or 0
        deep_pct  = _pct(r.get("sleep_deep_mins"),  total_sleep)
        rem_pct   = _pct(r.get("sleep_rem_mins"),   total_sleep)
        light_pct = _pct(r.get("sleep_light_mins"), total_sleep)

        lines += [
            f"### {r['date']}",
            "",
            f"**Cardiovascular:** Resting HR {_val(r.get('resting_hr'), ' bpm')} "
            f"(min {_val(r.get('hr_min'))}, max {_val(r.get('hr_max'))})  ",
            f"**HRV:** {_val(r.get('hrv_last_night'), ' ms')} last night | "
            f"{_val(r.get('hrv_weekly_avg'), ' ms')} weekly avg | "
            f"5-min high {_val(r.get('hrv_5min_high'), ' ms')} | "
            f"baseline {_val(r.get('hrv_baseline_low'))}–{_val(r.get('hrv_baseline_high'))} ms | "
            f"status: **{_val(r.get('hrv_status'))}**  ",
            f"**Sleep:** {_val(r.get('sleep_total_mins'), ' min total')} — "
            f"Deep {_val(r.get('sleep_deep_mins'), ' min')} ({_val(deep_pct, '%')}) | "
            f"REM {_val(r.get('sleep_rem_mins'), ' min')} ({_val(rem_pct, '%')}) | "
            f"Light {_val(r.get('sleep_light_mins'), ' min')} ({_val(light_pct, '%')}) | "
            f"Awake {_val(r.get('sleep_awake_mins'), ' min')} | "
            f"Restless moments: {_val(r.get('restless_moments'))}  ",
            f"**Sleep quality:** SpO2 avg {_val(r.get('sleep_spo2_avg'), '%')} / "
            f"low {_val(r.get('sleep_spo2_low'), '%')} | "
            f"Avg HR {_val(r.get('sleep_avg_hr'), ' bpm')} | "
            f"Avg respiration {_val(r.get('sleep_resp_avg'), ' br/min')} | "
            f"Breathing disruption: {_val(r.get('breathing_disruption'))} | "
            f"Feedback: {_val(r.get('sleep_feedback'))}  ",
            f"**Recovery:** Body battery at wake {_val(r.get('bb_at_wake'))} | "
            f"highest {_val(r.get('bb_highest'))} | "
            f"end of day {_val(r.get('bb_end_of_day'))} | "
            f"charged {_val(r.get('bb_charged'))} / drained {_val(r.get('bb_drained'))}  ",
            f"**Stress:** avg {_val(r.get('avg_stress'))} | "
            f"max {_val(r.get('max_stress'))} | "
            f"qualifier: {_val(r.get('stress_qualifier'))}  ",
            f"**Activity:** {_val(r.get('steps'), ' steps')} | "
            f"active kcal {_val(r.get('active_kcal'))} | "
            f"mod intensity {_val(r.get('mod_intensity_mins'), ' min')} | "
            f"vigorous {_val(r.get('vig_intensity_mins'), ' min')} | "
            f"sedentary {_val(r.get('sedentary_hours'), ' h')}  ",
            f"**Waking respiration:** avg {_val(r.get('resp_avg_waking'), ' br/min')} "
            f"(range {_val(r.get('resp_low'))}–{_val(r.get('resp_high'))})  ",
            "",
        ]

    lines += ["---", ""]

    # ---- Activity Log ----
    lines += [
        "## Activity Log",
        "",
        f"Total recorded activities in period: {len(activities)}",
        "",
        "| Date | Activity | Duration | Avg HR | Max HR | Distance |",
        "|------|----------|----------|--------|--------|----------|",
    ]
    for a in sorted(activities, key=lambda x: x.get("startTimeLocal", ""), reverse=True):
        act_date = (a.get("startTimeLocal") or "")[:10]
        name     = a.get("activityName") or a.get("activityType", {}).get("typeKey", "Unknown")
        dur_secs = a.get("duration") or 0
        dur_str  = f"{int(dur_secs)//3600}h{(int(dur_secs)%3600)//60:02d}m" if dur_secs else "—"
        avg_hr   = a.get("averageHR") or "—"
        max_hr   = a.get("maxHR") or "—"
        dist_m   = a.get("distance")
        dist_str = f"{dist_m/1000:.1f} km" if dist_m else "—"
        lines.append(f"| {act_date} | {name} | {dur_str} | {avg_hr} | {max_hr} | {dist_str} |")

    lines += ["", "---", ""]

    # ---- Notable Flags ----
    flags = []
    for r in rows:
        d = r["date"]
        if r.get("sleep_spo2_low") and r["sleep_spo2_low"] < 90:
            flags.append(f"- **{d}:** Sleep SpO2 dropped to {r['sleep_spo2_low']}% (below 90% threshold)")
        bd = r.get("breathing_disruption", "NONE")
        if bd and bd not in ("NONE", None):
            flags.append(f"- **{d}:** Breathing disruption: {bd}")
        if r.get("hrv_last_night") and r.get("hrv_baseline_low"):
            if r["hrv_last_night"] < r["hrv_baseline_low"]:
                flags.append(f"- **{d}:** HRV {r['hrv_last_night']} ms below personal baseline low ({r['hrv_baseline_low']} ms)")
        if r.get("bb_at_wake") and r["bb_at_wake"] < 40:
            flags.append(f"- **{d}:** Body battery at wake only {r['bb_at_wake']} — poor overnight recovery")

    lines += ["## Notable Flags", ""]
    if flags:
        lines += flags
    else:
        lines.append("No significant flags detected in this period.")
    lines += ["", "---", ""]

    lines += [
        "*Report generated by garmin-health-app report.py*",
        f"*Data cached in report_cache/ — delete cache files to force re-fetch*",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate 90-day Garmin health report")
    parser.add_argument("--days",   type=int, default=90,   help="Number of days to cover (default: 90)")
    parser.add_argument("--end",    type=str, default=None, help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--output", type=str, default=None, help="Output filename (default: health_report_DATE.md)")
    args = parser.parse_args()

    if not EMAIL or not PASSWORD:
        print("Error: set GARMIN_EMAIL and GARMIN_PASSWORD in .env")
        sys.exit(1)

    end_date = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=args.days - 1)
    output_path = args.output or f"health_report_{end_date}.md"

    print(f"Garmin health report — {start_date} to {end_date} ({args.days} days)")
    print(f"Output: {output_path}\n")

    client = build_client()
    print()

    # Fetch per-day data
    rows = []
    dates = [start_date + timedelta(days=i) for i in range(args.days)]
    uncached = [d for d in dates if not all(
        cache_path(d.isoformat(), ep).exists() for ep in ("wellness", "sleep", "hrv")
    )]

    if uncached:
        print(f"Fetching {len(uncached)} days from API ({len(dates) - len(uncached)} already cached)...")
    else:
        print(f"All {len(dates)} days found in cache — no API calls needed.")

    for i, d in enumerate(dates):
        cached = all(cache_path(d.isoformat(), ep).exists() for ep in ("wellness", "sleep", "hrv"))
        marker = "." if cached else "f"
        print(f"  {marker} {d.isoformat()}", end="", flush=True)
        rows.append(extract_day(client, d))
        if not cached:
            time.sleep(0.5)   # be gentle with Garmin's API on live fetches
        if (i + 1) % 10 == 0:
            print()  # newline every 10 days for readability

    print(f"\n\nFetched {len(rows)} days.")

    # Fetch activities (single call for the whole range)
    print("Fetching activity log...", end=" ", flush=True)
    activities = fetch_activities(client, start_date, end_date)
    print(f"{len(activities)} activities found.")

    # Generate report
    print("Generating report...", end=" ", flush=True)
    report = generate_report(rows, activities, end_date, args.days)
    Path(output_path).write_text(report, encoding="utf-8")
    print(f"done.\n")
    print(f"Report written to: {output_path}")
    print(f"({len(report.splitlines())} lines, {len(report):,} characters)")


if __name__ == "__main__":
    main()
