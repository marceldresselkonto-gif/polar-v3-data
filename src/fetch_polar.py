#!/usr/bin/env python3
import json
import os
import statistics
import hashlib
import base64
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

TOKEN_URL = "https://auth.polar.com/oauth/token"
API_BASE = "https://www.polaraccesslink.com/v4/data"
LEGACY_API_BASE = "https://www.polaraccesslink.com/v3/users"
TZ = ZoneInfo("Europe/Berlin")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _fernet(client_secret: str) -> Fernet:
    key = hashlib.sha256(client_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load_refresh_token(client_secret: str) -> str:
    state_file = Path("state") / "polar_refresh_token.enc"
    if state_file.exists():
        encrypted = state_file.read_bytes()
        return _fernet(client_secret).decrypt(encrypted).decode("utf-8")
    return required_env("POLAR_REFRESH_TOKEN")


def save_refresh_token(client_secret: str, refresh_token: str) -> None:
    state_dir = Path("state")
    state_dir.mkdir(parents=True, exist_ok=True)
    encrypted = _fernet(client_secret).encrypt(refresh_token.encode("utf-8"))
    (state_dir / "polar_refresh_token.enc").write_bytes(encrypted)


def refresh_access_token() -> str:
    client_id = required_env("POLAR_CLIENT_ID")
    client_secret = required_env("POLAR_CLIENT_SECRET")
    refresh_token = load_refresh_token(client_secret)

    response = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Polar token refresh failed with HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Polar token response contained no access_token")

    # Polar may rotate refresh tokens. Persist the newest token encrypted.
    next_refresh = payload.get("refresh_token") or refresh_token
    save_refresh_token(client_secret, next_refresh)
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
    if not response.ok:
        body = response.text[:2000]
        raise RuntimeError(
            f"Polar API {response.status_code} for {response.url}\nResponse: {body}"
        )
    return response.json()


def api_get_v3_optional(token: str, path: str, params=None):
    """Fetch a legacy/non-transactional v3 resource without breaking the daily export."""
    response = requests.get(
        f"{LEGACY_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params or {},
        timeout=60,
    )
    if response.status_code in (204, 403, 404):
        return {
            "_available": False,
            "_status": response.status_code,
            "_message": response.text[:500] if response.text else None,
        }
    if not response.ok:
        return {
            "_available": False,
            "_status": response.status_code,
            "_message": response.text[:500] if response.text else None,
        }
    payload = response.json()
    if isinstance(payload, dict):
        payload["_available"] = True
    return payload


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
        # Start with the base training-session payload. It already contains
        # duration, calories, HR averages/maxima and training load. Optional
        # feature expansion can be added after the core pipeline is verified.
        "training": api_get(
            token,
            "/training-sessions/list",
            [
                ("from", f"{start}T00:00:00"),
                ("to", f"{end}T00:00:00"),
            ],
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
        # v3 non-transactional daily summaries expose Polar's exact daily totals
        # (calories, active calories, daily activity %, durations, distance).
        "daily_activity_summary_v3": api_get_v3_optional(
            token,
            f"/activities/{start}",
            {
                "steps": "true",
                "activity_zones": "true",
                "inactivity_stamps": "true",
            },
        ),
        "sleep_summary_v3": api_get_v3_optional(token, f"/sleep/{start}"),
        "nightly_recharge_v3": api_get_v3_optional(
            token, f"/nightly-recharge/{start}"
        ),
    }


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def extract_step_details(activity, target_date=None):
    """Return step totals per device using the documented v4 activity schema."""
    result = {"by_device": {}, "raw_sum": None}
    try:
        days = activity["activityDays"]
    except (TypeError, KeyError):
        return result

    raw_sum = 0
    found = False
    for day in days:
        if target_date and day.get("date") != target_date:
            continue
        for dev in day.get("activitiesPerDevice", []):
            ref = dev.get("deviceReference") or {}
            device_id = ref.get("deviceId") or ref.get("uuid") or "unknown"
            device_total = 0
            device_found = False
            for sample in dev.get("activitySamples", []):
                step_samples = sample.get("stepSamples") or {}
                steps = step_samples.get("steps") or []
                vals = [x for x in steps if isinstance(x, (int, float))]
                if vals:
                    device_total += sum(vals)
                    raw_sum += sum(vals)
                    device_found = True
                    found = True
            if device_found:
                result["by_device"][device_id] = int(device_total)
    result["raw_sum"] = int(raw_sum) if found else None
    return result


def extract_steps(activity, target_date=None):
    details = extract_step_details(activity, target_date)
    values = list(details["by_device"].values())
    if not values:
        return None
    # Do not add devices together: multiple Polar devices can contain
    # overlapping activity for the same day. Use the largest single-device
    # total until cross-device reconciliation is explicitly implemented.
    return max(values)


def extract_hr(continuous_hr):
    """Collect all continuous HR samples defensively across API shape variants."""
    values = []
    for node in _walk(continuous_hr):
        hr = node.get("heartRate")
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


def extract_sleep_v3(sleep):
    if not isinstance(sleep, dict) or not sleep.get("_available"):
        return None
    stage_seconds = [
        sleep.get("light_sleep"),
        sleep.get("deep_sleep"),
        sleep.get("rem_sleep"),
        sleep.get("unrecognized_sleep_stage"),
    ]
    stage_seconds = [x for x in stage_seconds if isinstance(x, (int, float))]
    total_sleep_seconds = sum(stage_seconds) if stage_seconds else None
    return {
        "sleep_start_time": sleep.get("sleep_start_time"),
        "sleep_end_time": sleep.get("sleep_end_time"),
        "sleep_score": sleep.get("sleep_score"),
        "sleep_charge": sleep.get("sleep_charge"),
        "sleep_rating": sleep.get("sleep_rating"),
        "continuity": sleep.get("continuity"),
        "continuity_class": sleep.get("continuity_class"),
        "total_sleep_min": round(total_sleep_seconds / 60, 1) if total_sleep_seconds is not None else None,
        "light_sleep_min": round((sleep.get("light_sleep") or 0) / 60, 1) if sleep.get("light_sleep") is not None else None,
        "deep_sleep_min": round((sleep.get("deep_sleep") or 0) / 60, 1) if sleep.get("deep_sleep") is not None else None,
        "rem_sleep_min": round((sleep.get("rem_sleep") or 0) / 60, 1) if sleep.get("rem_sleep") is not None else None,
        "interruptions_min": round((sleep.get("total_interruption_duration") or 0) / 60, 1) if sleep.get("total_interruption_duration") is not None else None,
        "sleep_cycles": sleep.get("sleep_cycles"),
        "sleep_goal_min": round((sleep.get("sleep_goal") or 0) / 60, 1) if sleep.get("sleep_goal") is not None else None,
        "duration_score": sleep.get("group_duration_score"),
        "solidity_score": sleep.get("group_solidity_score"),
        "regeneration_score": sleep.get("group_regeneration_score"),
    }


def extract_physical_info(activity, target_date):
    if not isinstance(activity, dict):
        return None
    for day in activity.get("activityDays", []):
        if day.get("date") == target_date:
            p = day.get("physicalInformation") or {}
            return {
                "weight_kg": p.get("weight"),
                "height_cm": p.get("height"),
                "max_hr_bpm": p.get("maximumHeartRate"),
                "resting_hr_bpm": p.get("restingHeartRate"),
                "vo2max": p.get("vo2Max"),
                "training_background": p.get("trainingBackground"),
            }
    return None


def extract_activity_summary_v3(activity):
    if not isinstance(activity, dict) or not activity.get("_available"):
        return None
    distance_m = activity.get("distance_from_steps")
    return {
        "total_calories_kcal": activity.get("calories"),
        "active_calories_kcal": activity.get("active_calories"),
        "steps": activity.get("steps"),
        "activity_goal_pct": activity.get("daily_activity"),
        "active_duration": activity.get("active_duration"),
        "inactive_duration": activity.get("inactive_duration"),
        "distance_km": round(distance_m / 1000, 3) if isinstance(distance_m, (int, float)) else None,
        "inactivity_alert_count": activity.get("inactivity_alert_count"),
    }


def extract_nightly_v3(nightly):
    if not isinstance(nightly, dict) or not nightly.get("_available"):
        return None
    return {
        "nightly_recharge_status": nightly.get("nightly_recharge_status"),
        "ans_charge": nightly.get("ans_charge"),
        "ans_charge_status": nightly.get("ans_charge_status"),
        "night_hr_avg_bpm": nightly.get("heart_rate_avg"),
        "night_hrv_rmssd_ms": nightly.get("heart_rate_variability_avg"),
        "breathing_rate_avg": nightly.get("breathing_rate_avg"),
        "beat_to_beat_avg_ms": nightly.get("beat_to_beat_avg"),
    }


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
        t["calories_kcal"]
        for t in trainings
        if isinstance(t.get("calories_kcal"), (int, float))
    ]

    activity_v3 = extract_activity_summary_v3(raw.get("daily_activity_summary_v3"))
    sleep_v3 = extract_sleep_v3(raw.get("sleep_summary_v3"))
    nightly_v3 = extract_nightly_v3(raw.get("nightly_recharge_v3"))

    # Prefer Polar's exact v3 daily summary when available; otherwise fall back
    # to independently extracted v4 step samples.
    steps = (
        activity_v3.get("steps")
        if activity_v3 and activity_v3.get("steps") is not None
        else extract_steps(raw.get("activity"), raw["date"])
    )

    required_daily = {
        "total_calories_kcal": activity_v3.get("total_calories_kcal") if activity_v3 else None,
        "steps": steps,
        "activity_goal_pct": activity_v3.get("activity_goal_pct") if activity_v3 else None,
    }
    missing_daily = [k for k, v in required_daily.items() if v is None]
    data_status = {
        "daily_energy_complete": len(missing_daily) == 0,
        "status": "complete" if not missing_daily else "provisional",
        "missing_required_fields": missing_daily,
        "note": (
            "Vortag für Kalorien-/Aktivitätsauswertung vollständig."
            if not missing_daily
            else "Polar-Cloud-Daten noch unvollständig; späterer Lauf aktualisiert denselben Tag automatisch."
        ),
    }

    return {
        "date": raw["date"],
        "fetched_at": raw["fetched_at"],
        "data_status": data_status,
        "daily_activity": {
            **(activity_v3 or {}),
            "steps": steps,
            "source": "Polar v3 daily activity summary"
            if activity_v3
            else "Polar v4 activity samples",
        },
        "continuous_hr": extract_hr(raw.get("continuous_hr")),
        "training_sessions": trainings,
        "training_calories_total_kcal": sum(training_calories)
        if training_calories
        else 0,
        "sleep": sleep_v3,
        "nightly_recharge": nightly_v3
        or extract_nightly(raw.get("nightly_recharge")),
        "physical_info": extract_physical_info(raw.get("activity"), raw["date"]),
        "source_status": {
            "daily_activity_v3": (raw.get("daily_activity_summary_v3") or {}).get("_status")
            if not activity_v3
            else 200,
            "sleep_v3": (raw.get("sleep_summary_v3") or {}).get("_status")
            if not sleep_v3
            else 200,
            "nightly_recharge_v3": (raw.get("nightly_recharge_v3") or {}).get("_status")
            if not nightly_v3
            else 200,
        },
    }

def target_dates():
    requested = os.getenv("POLAR_DATE", "").strip()
    if requested:
        return [date.fromisoformat(requested)]

    # Refresh the last three completed calendar days on every automatic run.
    # This deliberately backfills days that were incomplete in Polar cloud
    # because the watch did not sync with the phone until later.
    today = datetime.now(TZ).date()
    return [today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1)]


def write_day(target: date, raw, summary, update_latest: bool):
    out = Path("data") / target.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2)
    (out / "summary.json").write_text(summary_text, encoding="utf-8")
    if update_latest:
        Path("polar-v3-latest.json").write_text(summary_text, encoding="utf-8")


def main():
    targets = target_dates()
    token = refresh_access_token()

    for target in targets:
        raw = fetch_day(token, target)
        summary = build_summary(raw)
        write_day(target, raw, summary, update_latest=(target == max(targets)))
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
