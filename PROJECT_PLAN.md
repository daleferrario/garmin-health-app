# Project Plan: Garmin-Powered Health Application

This project plan organizes the development of the health application into three stages: a standalone proof-of-concept data collector, followed by a single-user Phase 1 app, followed by a multi-user Phase 2 analytics app.

## Deployment Scope

**Initial launch:** Single-user application — one Garmin account, one web login.

**Future:** Multi-user capable. Every design decision accounts for this:
- All database tables include a `user_id` foreign key from day one
- A `users` table exists from the start (initially one row)
- Authentication middleware is abstracted so adding registration and role-based access requires no schema migrations
- The single-user constraint lives only at the application layer (a config flag), not in the data model

---

## Data Source: garminconnect Library

Garmin's official Health API is restricted to large commercial partners. The Garmin Connect personal data export contains only daily summaries — no intraday resolution. The watch filesystem clears its monitoring data after each phone sync, making it useless for any user who syncs regularly.

The practical solution is the **`garminconnect` Python library** (github.com/cyberjunky/python-garminconnect), which speaks the same undocumented REST API used by the Garmin Connect website and mobile app. This gives access to:

| Data Type | Resolution | Available via garminconnect |
|---|---|---|
| Heart rate (all-day) | 2-minute intervals (720/day) | ✓ `get_heart_rates()` |
| Stress index (0–100) | 3-minute intervals (480/day) | ✓ `get_stress_data()` |
| Body battery (full daily curve) | 3-minute intervals (480/day) | ✓ `get_stress_data()` — the full body battery timeline is embedded in the stress response (`bodyBatteryValuesArray`); the dedicated `get_body_battery()` endpoint returns only sparse event data (~6 records) |
| SpO2 | Per reading throughout day; also embedded in sleep response | ✓ `get_spo2_data()` + `get_sleep_data()` |
| Respiration rate | Per reading throughout day | ✓ `get_respiration_data()` |
| Sleep (stages + detail) | Per session | ✓ `get_sleep_data()` — includes stage durations, SpO2 during sleep, restless moments, movement data |
| HRV summary | One record per night | ✓ `get_hrv_data()` — weekly avg, last-night avg, 5-min high, personal baseline range (low/balanced/upper), status |
| HRV readings | ~85 individual readings/night (~5-min intervals during sleep) | ✓ `get_hrv_data()` — enough data to chart a full nightly HRV curve, not just a single value |
| Daily wellness summary | One row per day | ✓ `get_stats()` |
| Activity list | Per activity | ✓ `get_activities_by_date()` |
| Activity FIT file | Per-second during activity | ✓ `download_activity()` |

**Authentication:** Garmin username and password stored in `.env`. The library authenticates via Garmin's OAuth flow and saves session tokens to disk — subsequent runs reuse the saved tokens without re-entering credentials or MFA.

**Risk:** This API is unofficial and undocumented. Garmin can change it without notice, which may temporarily break syncing. The `garminconnect` library has an active maintainer and typically recovers quickly. For a personal single-user app this is an acceptable tradeoff.

**Sync model:** The app runs a nightly scheduled pull that fetches the previous day's data for all metrics. The user can also trigger a manual sync from the dashboard. There are no webhooks, no file uploads, and no watch connection required.

---

## Stage 0: Proof-of-Concept Collector (Before Building the App)

**Objective:** Validate that the `garminconnect` library works for this Garmin account and returns the expected data for all required metrics before writing a line of app code.

A standalone script (`collector.py`) authenticates with Garmin Connect, fetches data for a recent date across every endpoint the app will use, and saves the raw JSON responses to disk for inspection.

**Success criteria:**
- All endpoints return data (no auth failures, no empty responses)
- Intraday HR, stress, and body battery contain the expected resolution (~1440, ~480, ~480 records respectively for a full day)
- HRV data is present for recent dates
- Sleep data includes stage breakdown
- Activity FIT file download works

**Only after Stage 0 succeeds does Phase 1 development begin.**

---

## Phase 1: Single-User App — Ingestion, Storage, and Visualization

**Objective:** Build a working single-user web application that syncs Garmin data nightly, stores it in SQLite, and renders it in a browser dashboard.

### Task 1.1: Environment Setup & Authentication

* **Action:** Set up the Flask project structure, configure credentials, and establish authenticated access to Garmin Connect.
* **Implementation:**
  * `garminconnect` library configured with Garmin username/password from `.env`
  * Token file stored on server so nightly sync never prompts for MFA
  * Google OAuth for web app login (same pattern as GolfAnalytics)
  * Single-user guard in config: registration disabled, one allowed Google account
* **Deliverable:** A confirmed authenticated connection to Garmin Connect that can be exercised from the server without manual intervention.

### Task 1.2: Data Sync Pipeline & Schema

* **Action:** Build a sync service that pulls all Garmin data for a given date and writes it to SQLite.
* **Implementation:**
  * `sync_service.py` fetches each data type for a target date via `garminconnect` and upserts rows into the corresponding table
  * Nightly scheduled job (APScheduler) pulls yesterday's data automatically
  * Manual "Sync Now" button in the dashboard triggers the same path on demand
  * Sync history table records each run (date range, rows written per table, status, errors)
  * Upsert logic keyed on (user_id, timestamp/date) prevents duplicates on re-sync
  * Backfill: the app can be instructed to sync a historical date range; rate-limit awareness to avoid triggering Garmin's throttling
* **Deliverable:** A running nightly sync that populates all database tables from Garmin Connect data.

### Task 1.3: Raw Data Visualization (The UI)

* **Action:** Build a clean dashboard that renders what the data actually contains, with no algorithmic interpretation.
* **Implementation:**
  * **Heart Rate:** Full 24-hour line chart at 2-minute resolution (720 points) — the actual continuous BPM curve for the day including sleep
  * **Stress:** Full 24-hour area chart at 3-minute resolution (480 points) — the actual stress index curve
  * **Body Battery:** Full daily drain/recovery curve at 3-minute resolution (480 points) — sourced from the stress endpoint response alongside stress data
  * **Sleep Stages:** Stacked horizontal bar per night — Deep, Light, REM, Awake durations, with restless moments and SpO2 summary from the same response
  * **SpO2 & Respiration:** Point plots for the day showing actual readings
  * **HRV Nightly Curve:** ~85-point line chart of HRV readings during the sleep window (~5-minute intervals) — a full shape of the night, not just a single number
  * **HRV Trend:** Daily summary line over weeks/months — last-night average, weekly average, and personal baseline band for context
  * **Activity HR:** Per-second HR curve for a selected recorded activity (from FIT file)
  * **Steps & Calories:** Daily bar charts
  * All charts use raw data directly from SQLite with no statistical modification
* **Deliverable:** A working dashboard where every chart matches the values shown in the Garmin Connect app for the same date.

### Phase 1 Milestone

> **Success Criteria:** Nightly sync runs without intervention and populates all tables. Every chart value matches Garmin Connect exactly. The 24-hour HR, stress, and body battery curves show full intraday detail.

---

## Phase 2: Systemic Analysis & Predictive Coaching

**Objective:** Layer custom algorithmic models over the Phase 1 database to interpret trends, monitor homeostatic deviations, and deliver actionable physiological recovery insights.

### Task 2.1: Autonomic Nervous System (ANS) Analytics

* **Action:** Transition from raw daily HRV values to active ANS trajectory charting.
* **Implementation:** Process rolling 7-day and 3-week baselines of the nightly HRV metric. Build a correlation engine that contrasts day-over-day HRV changes against sleep architecture quality and daytime stress curves to identify whether the nervous system is safely adapting or signaling chronic fatigue.
* **Deliverable:** An ANS balance index mapping systemic strain against restorative trends.

### Task 2.2: Metabolic Recovery & Deviation Forecasting

* **Action:** Build an anomaly-detection engine targeting multi-metric deviations.
* **Implementation:** Synthesize sleep stage architecture, HRV trends, and daytime stress curves. Flag concurrent deviations — such as a 15% drop in HRV below personal baseline alongside elevated overnight stress — to predict immune or overtraining stress before physical symptoms manifest.
  * Note: skin temperature is not available on the user's current device (Fenix 7X, non-Pro).
* **Deliverable:** A proactive "Systemic Alert" mechanism highlighting homeostatic imbalances.

### Task 2.3: Dynamic Operational Budgeting (Advanced Energy Reserves)

* **Action:** Evolve body battery and activity load data into a forward-looking performance budget.
* **Implementation:** Evaluate morning body battery recovery against recent training load from activity FIT data. Calculate cumulative training stress to provide a threshold of physiological strain that can be safely absorbed before incurring a recovery deficit.
* **Deliverable:** A daily personalized energy-budget forecaster.

### Phase 2 Milestone

> **Success Criteria:** The app accurately identifies deviations from the user's historical baselines and delivers data-justified insights detailing current systemic strain and recovery readiness.

---

## Execution Timeline

```
+----------------------------------------------------------+
| STAGE 0: COLLECTOR (Days 1 - 2)                          |
| [collector.py] --> validate all endpoints work           |
+----------------------------------------------------------+
                        |
                        v (Gate: all endpoints confirmed)
+----------------------------------------------------------+
| PHASE 1: SINGLE-USER APP (Weeks 1 - 6)                   |
| [Auth + Sync] --> [DB Schema] --> [UI Dashboard]         |
+----------------------------------------------------------+
                        |
                        v (Milestone: Verified Data Delivery)
+----------------------------------------------------------+
| PHASE 2: ANALYTICS (Weeks 7 - 12)                        |
| [ANS Engine] --> [Deviation Triggers] --> [Energy Budget]|
+----------------------------------------------------------+
```
