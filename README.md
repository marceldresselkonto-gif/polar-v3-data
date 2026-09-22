# polar-v3-data

Automatischer Export von Polar-Vantage-V3-Daten über die offizielle Polar AccessLink Dynamic API v4.

## Ziel

Ein täglicher, maschinenlesbarer Datensatz für die spätere Auswertung in ChatGPT:

- Schritte / Aktivitäts-Samples
- 24/7-Herzfrequenz
- Trainingseinheiten inkl. Dauer, Polar-kcal, Ø-/Max-HF, Training Load
- Schlaf
- Nightly Recharge / HRV
- körperliche Polar-Parameter, soweit die API sie im Datensatz liefert

**Wichtig:** Die API liefert Trainings-kcal direkt. Ein vollständiger täglicher Gesamtenergieverbrauch ist in der aktuell dokumentierten v4-Daily-Activity-Antwort nicht als verlässliches Einzel-Feld ausgewiesen. Dieses Repository erfindet deshalb keinen Tages-kcal-Wert.

## Dateien

```
src/fetch_polar.py
data/YYYY-MM-DD/raw.json
data/YYYY-MM-DD/summary.json
.github/workflows/polar-daily.yml
```

## Benötigte GitHub Actions Secrets

Unter **Repository → Settings → Secrets and variables → Actions** anlegen:

- `POLAR_CLIENT_ID`
- `POLAR_CLIENT_SECRET`
- `POLAR_REFRESH_TOKEN`

Die Zugangsdaten niemals als normale Datei committen.

## Automatik

Der Workflow läuft täglich und holt standardmäßig den **vollständig abgeschlossenen Vortag in Europe/Berlin**. Er kann zusätzlich manuell über **Actions → Polar daily export → Run workflow** gestartet werden.

## Polar API

Basis: `https://www.polaraccesslink.com/v4/data`

Verwendete Endpunkte:

- `/activity/list`
- `/continuous-samples`
- `/training-sessions/list`
- `/sleep-wake-vectors`
- `/nightly-recharge-results`

Der Access Token wird bei jedem Lauf mit dem Refresh Token neu erzeugt.
