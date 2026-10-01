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
    }


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def extract_steps(activity):
    """Sum all Polar step sample buckets defensively across API shape variants."""
    values = []
    for node in _walk(activity):
        step_samples = node.get("stepSamples")
        if isinstance(step_samples, dict):
            steps = step_samples.get("steps")
            if isinstance(steps, list):
                values.extend(x for x in steps if isinstance(x, (int, float)))
    return int(sum(values)) if values else None


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
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2)
    (out / "summary.json").write_text(summary_text, encoding="utf-8")
    Path("polar-v3-latest.json").write_text(summary_text, encoding="utf-8")

    print(summary_text)


if __name__ == "__main__":
    main()
