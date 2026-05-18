# Architecture: Garmin Health App

## Overview

A web application that pulls health telemetry from Garmin Connect via the `garminconnect` Python library, stores normalized time-series data in SQLite, and renders it in a browser-based dashboard. It launches as a single-user app and is built multi-user-ready from day one. The stack deliberately mirrors GolfAnalytics so both apps run on the same server with minimal operational overhead.

**Data ingestion model:** A nightly scheduled job (and on-demand sync button) authenticates with Garmin Connect using saved OAuth tokens and pulls all health metrics for the previous day. No file uploads, no webhooks, no watch connection required.

---

## Multi-User Readiness Strategy

The app starts with one user but is never designed around that constraint at the data layer:

- All user-specific tables carry a `user_id` foreign key from day one
- A `users` table exists from the start (initially one row)
- Authentication is abstracted behind a decorator (same `@profile_required` pattern as GolfAnalytics)
- To go multi-user: enable registration, remove the single-user guard in config, add per-user Garmin credential storage — no schema migrations required

---

## Technology Stack

Aligned with GolfAnalytics wherever possible to minimize operational divergence.

### Backend
| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Same as GolfAnalytics |
| Framework | Flask 3.x | Same as GolfAnalytics |
| WSGI Server | Waitress | Same as GolfAnalytics; no gunicorn/uvicorn complexity |
| Garmin data | garminconnect | Unofficial but well-maintained Garmin Connect REST client |
| FIT Parsing | garmin-fit-sdk | Garmin's official Python SDK for decoding activity FIT files |
| OAuth Client | Authlib | Same as GolfAnalytics; handles Google OAuth flow |
| Task scheduler | APScheduler | Nightly sync job; runs in-process, no separate worker needed |

### Database
| Concern | Choice | Reason |
|---|---|---|
| Engine | SQLite | Same as GolfAnalytics; sufficient for single-user |
| ORM | Flask-SQLAlchemy | Same as GolfAnalytics |
| Migrations | Flask-Migrate (Alembic) | Same as GolfAnalytics |

### Frontend
| Concern | Choice | Reason |
|---|---|---|
| Templates | Jinja2 (server-rendered) | Same as GolfAnalytics; no separate build step |
| Charts | Plotly | Same as GolfAnalytics; interactive, well-supported |
| Styling | Same CSS approach as GolfAnalytics | Consistency |

### Authentication
| Concern | Choice |
|---|---|
| Web app login | Google OAuth via Authlib — identical flow to GolfAnalytics |
| Session management | Flask server-side session (HTTP-only cookie) |
| Garmin Connect | Username + password in `.env`; OAuth tokens saved to disk; no MFA after initial setup |
| Multi-user path | Per-user encrypted Garmin credentials + registration allowlist |

---

## garminconnect Library — Risk Assessment

The library uses Garmin's undocumented internal REST API (the same one the Garmin Connect website and mobile app use). Risks and mitigations:

| Risk | Likelihood | Mitigation |
|---|---|---|
| Garmin changes API endpoints | Low–medium | Library maintainer typically patches within days; monitor GitHub issues |
| Garmin adds MFA challenge | Has happened | Token persistence avoids re-auth; initial setup requires one interactive login |
| Garmin rate-limits heavy backfill | Medium | Backfill runs with a configurable delay between date requests |
| Garmin blocks the library entirely | Very low | Would require finding an alternative (no good alternatives exist) |

For a personal single-user app this risk profile is acceptable.

---

## Deployment & Co-Hosting

Runs on the same AWS Lightsail Ubuntu instance as GolfAnalytics. Each app binds to a different internal port; nginx routes public traffic by subdomain. SSL via Cloudflare origin certificate (same as GolfAnalytics).

```
Internet (443 HTTPS)
       │
  Cloudflare
       │
     nginx
    ┌──┴──────────────┐
    │                 │
golf-insights.com   health.yourdomain.com
    │                 │
 :8000              :8001
    │                 │
GolfAnalytics      Garmin Health App
(Waitress)         (Waitress + APScheduler)
    │                 │
golf_analytics.db  health_app.db
(SQLite)           (SQLite)
```

**systemd service** (`/etc/systemd/system/healthapp.service`):
```ini
[Unit]
Description=Waitress instance to serve Garmin Health App
After=network.target

[Service]
User=appuser
Group=www-data
WorkingDirectory=/var/www/garmin-health-app
Environment="PATH=/var/www/garmin-health-app/env/bin"
EnvironmentFile=/var/www/garmin-health-app/.env
ExecStart=/var/www/garmin-health-app/env/bin/waitress-serve --host 127.0.0.1 --port 8001 app:app

[Install]
WantedBy=multi-user.target
```

**nginx config** (`/etc/nginx/sites-available/healthapp`):
```nginx
server {
    listen 80;
    server_name health.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name health.yourdomain.com;

    ssl_certificate /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

---

## Directory Structure

```
garmin-health-app/
├── app.py                    # Flask app entry point + APScheduler setup
├── auth_routes.py            # Google OAuth (adapted from GolfAnalytics)
├── decorators.py             # @profile_required (same pattern as GolfAnalytics)
├── models.py                 # SQLAlchemy models
├── globals.py                # App-wide configuration constants
├── logging_utils.py          # Logging setup (same as GolfAnalytics)
├── routes/
│   ├── main_routes.py        # Dashboard
│   ├── sync_routes.py        # Manual sync trigger endpoint
│   └── metrics_routes.py     # Data query endpoints for charts
├── services/
│   ├── garmin_client.py      # garminconnect wrapper: auth, token refresh, one method per endpoint
│   ├── sync_service.py       # Orchestrates a full sync for a date range; called by scheduler + manual trigger
│   └── analytics/            # Phase 2: rolling baselines, anomaly detection
├── charts/                   # Plotly chart generators (mirrors GolfAnalytics charts/)
├── templates/                # Jinja2 templates
├── static/                   # CSS, JS
├── instance/                 # health_app.db (SQLite, gitignored)
├── .garmin_tokens/           # Saved OAuth token files (gitignored)
├── collector.py              # Stage 0: standalone proof-of-concept (not part of the app)
├── requirements.txt
├── .env.example
├── setup-server.sh           # Adapted from GolfAnalytics
├── update-and-restart.sh     # Adapted from GolfAnalytics
└── backup_health_app.sh      # Google Drive backup (same rclone pattern)
```

---

## Data Flow

### Nightly Sync (Automated)

APScheduler fires at 3 AM daily. The sync window is yesterday (all data for that calendar day is complete and stable by then).

```
APScheduler fires at 3 AM
  → sync_service.sync_date(yesterday)
    → garmin_client.get_heart_rates(date)      → intraday_hr (720 rows, 2-min intervals)
    → garmin_client.get_stress_data(date)      → intraday_stress (480 rows, 3-min intervals)
                                               → intraday_body_battery (480 rows, same response)
    → garmin_client.get_spo2_data(date)        → intraday_spo2 table
    → garmin_client.get_respiration_data(date) → intraday_respiration table
    → garmin_client.get_sleep_data(date)       → sleep_sessions (stages + SpO2 + movement)
    → garmin_client.get_hrv_data(date)         → hrv_summary + hrv_readings (~85 rows/night)
    → garmin_client.get_stats(date)            → daily_wellness table
    → garmin_client.get_activities_by_date()   → activity_sessions table
      → for each new activity:
          garmin_client.download_activity(id)  → decode with garmin-fit-sdk
                                               → fit_records table
    → sync_history row written (date, row counts, status)

SQLite
  └─► Dashboard queries via Flask routes
          └─► Plotly renders charts in Jinja2 templates
```

### Manual Sync

User clicks "Sync Now" on dashboard → POST `/sync/trigger` → same `sync_service.sync_date()` path.

### Historical Backfill

On first setup, or to fill gaps: user specifies a date range in the dashboard → `sync_service.sync_range(start, end)` iterates day by day with a short sleep between requests to avoid rate-limiting.

---

## Data Availability from garminconnect

All data types below are accessible from the Garmin Connect API via the garminconnect library. Resolution confirmed for Fenix 7X.

| Data Type | Method | Resolution | Notes |
|---|---|---|---|
| All-day heart rate | `get_heart_rates()` | 2-minute intervals (720/day) | Full 24-hour curve including sleep |
| Stress index | `get_stress_data()` | 3-minute intervals (480/day) | Full 24-hour curve |
| Body battery | `get_stress_data()` | 3-minute intervals (480/day) | **Comes from the stress endpoint**, not `get_body_battery()`. The dedicated body battery endpoint returns only ~6 sparse event records; the full timeline is in `stressValuesArray`'s companion `bodyBatteryValuesArray`. One API call yields both. |
| SpO2 | `get_spo2_data()` | Per reading | Continuous overnight + daytime; also summarised inside `get_sleep_data()` |
| Respiration rate | `get_respiration_data()` | Per reading | Continuous throughout day |
| Sleep stages + detail | `get_sleep_data()` | Per session | Stage durations (Deep/Light/REM/Awake) plus SpO2 during sleep, movement data, restless moments — richer than stage durations alone |
| HRV summary | `get_hrv_data()` | One record per night | weekly avg, last-night avg, 5-min high, personal baseline range (low/balanced/upper), status (BALANCED / UNBALANCED / etc.), feedback phrase |
| HRV readings | `get_hrv_data()` | ~85 readings per night (~5-min intervals during sleep) | Individual HRV values throughout the sleep window — enough to chart a nightly HRV curve, not just a single number |
| Daily wellness | `get_stats()` | One row per day | Steps, calories, resting HR, etc. |
| Activity list | `get_activities_by_date()` | Per activity | Type, duration, distance, HR |
| Activity FIT file | `download_activity()` | Per-second | HR, GPS, power, cadence |

**Not available:** Skin temperature — the user's Fenix 7X (non-Pro) does not have a skin temperature sensor.

---

## Database Schema (Core Tables)

All tables include `user_id` (FK → `users.id`) from day one.

```sql
users               — id, google_id, email, name, profile_complete, created_at

sync_history        — user_id, sync_date, triggered_at, completed_at,
                      hr_rows, stress_rows, body_battery_rows, sleep_rows,
                      hrv_rows, activity_rows, fit_rows,
                      status (running|complete|failed), error_message

daily_wellness      — user_id, calendar_date,
                      resting_hr, hr_min, hr_max,
                      steps, distance_meters, calories,
                      stress_avg, body_battery_start, body_battery_end,
                      intensity_mins_moderate, intensity_mins_vigorous
                      UNIQUE(user_id, calendar_date)

intraday_hr         — user_id, timestamp, bpm
                      (2-minute intervals; 720 rows per full day)
                      UNIQUE(user_id, timestamp)

intraday_stress     — user_id, timestamp, stress_level
                      (3-minute intervals; 480 rows per full day)
                      UNIQUE(user_id, timestamp)

intraday_body_battery — user_id, timestamp, battery_level
                      (3-minute intervals; 480 rows per full day)
                      (populated from get_stress_data() response, not get_body_battery())
                      UNIQUE(user_id, timestamp)

intraday_spo2       — user_id, timestamp, spo2_pct
                      UNIQUE(user_id, timestamp)

intraday_respiration — user_id, timestamp, breaths_per_min
                      UNIQUE(user_id, timestamp)

sleep_sessions      — user_id, session_date, start_time, end_time,
                      deep_mins, light_mins, rem_mins, awake_mins,
                      sleep_score, restless_moments_count,
                      spo2_avg, spo2_min
                      UNIQUE(user_id, session_date)

hrv_summary         — user_id, date,
                      weekly_avg, last_night_avg, last_night_5min_high,
                      baseline_low_upper, balanced_low, balanced_upper,
                      status, feedback_phrase
                      UNIQUE(user_id, date)

hrv_readings        — user_id, date, reading_time, hrv_value
                      (~85 rows per night; individual readings during sleep window)
                      UNIQUE(user_id, reading_time)

activity_sessions   — user_id, activity_id, start_time,
                      activity_type, duration_secs, distance_meters,
                      calories, avg_hr, max_hr
                      UNIQUE(user_id, activity_id)

fit_records         — user_id, activity_id (FK → activity_sessions),
                      timestamp, hr_bpm, speed_ms, power_watts,
                      cadence, lat, lon, altitude_m
```

---

## Phase 2 Additions (Analytics Layer)

Phase 2 analytical work slots into `services/analytics/`:

| Capability | Library |
|---|---|
| Rolling baselines (HRV, 7-day/3-week) | pandas + numpy |
| ANS balance index | scipy |
| Anomaly / deviation detection | statsmodels |
| Nightly recalculation after sync | Runs as post-sync step in APScheduler job |

---

## Code Reuse from GolfAnalytics

| GolfAnalytics file | Health app adaptation |
|---|---|
| `auth_routes.py` | Copy verbatim; Garmin OAuth not needed |
| `decorators.py` | Copy verbatim |
| `logging_utils.py` | Copy verbatim |
| `globals.py` | Copy structure; populate with health app constants |
| `setup-server.sh` | Adapt service name and port |
| `update-and-restart.sh` | Adapt service name |
| `backup_golf_insights.sh` | Adapt paths and folder name |
| systemd service file | Port 8001, add `EnvironmentFile` for `.env` |
| nginx config | Add new server block; cert paths identical |
