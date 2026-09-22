#!/usr/bin/env python3
import json
import os
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TOKEN_URL = "https://auth.polar.com/oauth/token"
API_BASE = "https://www.polaraccesslink.com/v4/data"
TZ = ZoneInfo("Europe/Berlin")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def refresh_access_token() -> str:
    client_id = required_env("POLAR_CLIENT_ID")
    client_secret = required_env("POLAR_CLIENT_SECRET")
    refresh_token = required_env("POLAR_REFRESH_TOKEN")

    response = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Polar token response contained no access_token")
    return token


def api_get(token: str, path: str, params):
    response = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=60,
    )
    if response.status_code == 204:
        return None
    response.raise_for_status()
    return response.json()


def day_range(target: date):
    start = target.isoformat()
    end = (target + timedelta(days=1)).isoformat()
    return start, end


def repeated_params(start: str, end: str, features):
    params = [("from", start), ("to", end)]
    params.extend(("features", feature) for feature in features)
    return params


def fetch_day(token: str, target: date):
    start, end = day_range(target)
    return {
        "date": start,
        "fetched_at": datetime.now(TZ).isoformat(),
        "activity": api_get(
            token,
            "/activity/list",
            repeated_params(start, end, ["samples", "activity-target", "physical-information"]),
        ),
        "continuous_hr": api_get(
            token,
            "/continuous-samples",
            repeated_params(start, end, ["heart-rate-samples"]),
        ),
        "training": api_get(
            token,
            "/training-sessions/list",
            repeated_params(
                start,
                end,
                [
                    "samples",
                    "training-load-report",
                    "zones",
                    "pause-times",
                    "strength-training-results",
                    "physical-info",
                ],
            ),
        ),
        "sleep": api_get(
            token,
            "/sleep-wake-vectors",
            repeated_params(
                start,
                end,
                ["sleep-result", "sleep-evaluation", "sleep-score"],
            ),
        ),
        "nightly_recharge": api_get(
            token,
            "/nightly-recharge-results",
            [("from", start), ("to", end)],
        ),
    }


def extract_steps(activity):
    try:
        days = activity["activities"]["activityDays"]
    except (TypeError, KeyError):
        return None
    total = 0
    found = False
    for day in days:
        for dev in day.get("activitiesPerDevice", []):
            for sample in dev.get("activitySamples", []):
                ss = sample.get("stepSamples") or {}
                steps = ss.get("steps") or []
                if steps:
                    total += sum(x for x in steps if isinstance(x, (int, float)))
                    found = True
    return int(total) if found else None


def extract_hr(continuous_hr):
    try:
        days = continuous_hr["continuousSamples"]["heartRateSamplesPerDay"]
    except (TypeError, KeyError):
        return {}
    values = []
    for day in days:
        for sample in day.get("samples", []):
            hr = sample.get("heartRate")
            if isinstance(hr, (int, float)):
                values.append(hr)
    if not values:
        return {}
    return {
        "samples": len(values),
        "min_bpm": min(values),
        "avg_bpm": round(statistics.fmean(values), 1),
        "max_bpm": max(values),
    }


def extract_training(training):
    sessions = []
    try:
        source = training.get("trainingSessions", [])
    except AttributeError:
        source = []
    for s in source:
        sessions.append(
            {
                "id": (s.get("identifier") or {}).get("id"),
                "name": s.get("name"),
                "start_time": s.get("startTime"),
                "stop_time": s.get("stopTime"),
                "duration_min": round((s.get("durationMillis") or 0) / 60000, 1),
                "calories_kcal": s.get("calories"),
                "hr_avg_bpm": s.get("hrAvg"),
                "hr_max_bpm": s.get("hrMax"),
                "training_load": s.get("trainingLoad"),
                "training_benefit": s.get("trainingBenefit"),
                "feeling": s.get("feeling"),
                "product": (s.get("product") or {}).get("modelName"),
                "strength_training_results": [
                    e.get("strengthTrainingResults")
                    for e in (s.get("exercises") or [])
                    if e.get("strengthTrainingResults")
                ],
            }
        )
    return sessions


def extract_sleep(sleep):
    if not isinstance(sleep, dict):
        return None
    # Keep a compact copy of the most useful high-level fields while raw.json
    # retains the complete response. API shapes can evolve, so this is defensive.
    vectors = sleep.get("sleepWakeVectors") or sleep.get("sleepResults") or sleep
    return vectors


def extract_nightly(nightly):
    if not isinstance(nightly, dict):
        return None
    results = nightly.get("nightlyRechargeResults")
    if not results:
        return nightly
    compact = []
    for r in results:
        compact.append(
            {
                "sleep_result_date": r.get("sleepResultDate"),
                "ans_status": r.get("ansStatus"),
                "recovery_indicator": r.get("recoveryIndicator"),
                "recovery_indicator_sublevel": r.get("recoveryIndicatorSubLevel"),
                "ans_rate": r.get("ansRate"),
                "mean_nightly_rri": r.get("meanNightlyRecoveryRri"),
                "mean_nightly_rmssd": r.get("meanNightlyRecoveryRmssd"),
                "exercise_tip": r.get("exerciseTip"),
                "sleep_tip": r.get("sleepTip"),
                "vitality_tip": r.get("vitalityTip"),
            }
        )
    return compact


def build_summary(raw):
    trainings = extract_training(raw.get("training"))
    training_calories = [
        t["calories_kcal"] for t in trainings if isinstance(t.get("calories_kcal"), (int, float))
    ]
    return {
        "date": raw["date"],
        "fetched_at": raw["fetched_at"],
        "steps": extract_steps(raw.get("activity")),
        "continuous_hr": extract_hr(raw.get("continuous_hr")),
        "training_sessions": trainings,
        "training_calories_total_kcal": sum(training_calories) if training_calories else 0,
        "sleep": extract_sleep(raw.get("sleep")),
        "nightly_recharge": extract_nightly(raw.get("nightly_recharge")),
        "daily_total_calories_kcal": None,
        "daily_total_calories_note": (
            "Nicht berechnet: AccessLink v4 weist im verwendeten Daily-Activity-Endpunkt "
            "keinen belastbaren einzelnen Gesamt-kcal-Wert aus."
        ),
    }


def target_date() -> date:
    requested = os.getenv("POLAR_DATE", "").strip()
    if requested:
        return date.fromisoformat(requested)
    return datetime.now(TZ).date() - timedelta(days=1)


def main():
    target = target_date()
    token = refresh_access_token()
    raw = fetch_day(token, target)
    summary = build_summary(raw)

    out = Path("data") / target.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
