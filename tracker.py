"""
Competitor booking tracker (Ile de Re studio)

Pulls the public host-schedule feed (same one their booking widget uses)
and writes:
  - docs/data/latest.json   merged snapshot, used by the frontend
  - docs/data/history.csv   appended each run, builds a time series

Meant to run daily via GitHub Actions (see .github/workflows/daily.yml).

Usage:
    pip install requests pytz
    python tracker.py
"""

import csv
import json
import os
from datetime import datetime, timezone
from typing import List, Dict
import pytz
import requests

PARIS_TZ = pytz.timezone("Europe/Paris")

HOST_ID = 278958
BASE_URL = f"https://readonly-api.momence.com/host-plugins/host/{HOST_ID}/host-schedule/sessions"
PAGE_SIZE = 200
DATA_DIR = "docs/data"
LATEST_JSON = os.path.join(DATA_DIR, "latest.json")
HISTORY_CSV = os.path.join(DATA_DIR, "history.csv")

SESSION_TYPES = [
    "course-class",
    "fitness",
    "retreat",
    "special-event",
    "special-event-new",
]


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.pilates-reformer-ile-de-re.com/",
    "Origin": "https://www.pilates-reformer-ile-de-re.com",
}


def fetch_all_sessions(from_date_iso: str) -> List[Dict]:
    """Paginate through the full schedule feed and return all session records."""
    all_sessions = []
    page = 0

    while True:
        params = [("sessionTypes[]", t) for t in SESSION_TYPES]
        params += [
            ("fromDate", from_date_iso),
            ("pageSize", PAGE_SIZE),
            ("page", page),
            ("timeZone", "Europe/Paris"),
        ]

        resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        payload = data.get("payload", [])
        all_sessions.extend(payload)

        total_count = data.get("pagination", {}).get("totalCount", 0)
        if (page + 1) * PAGE_SIZE >= total_count or not payload:
            break
        page += 1

    return all_sessions


JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def to_record(s: Dict, snapshot_time: str) -> Dict:
    """Turn one raw API session into the flat record used everywhere else."""
    capacity = s.get("capacity") or 0

    # remainingSpots used to carry the live count but the API now returns it
    # as null on every session. ticketsSold is still populated and reliable,
    # so use that directly instead of falling back to "capacity" (which
    # silently produced a booked count of 0 for every session).
    tickets_sold = s.get("ticketsSold")
    if tickets_sold is not None:
        booked = tickets_sold
        remaining = max(capacity - booked, 0)
    else:
        remaining_spots = s.get("remainingSpots") or {}
        remaining = remaining_spots.get("remaining", capacity)
        booked = capacity - remaining

    fill_rate = round(100 * booked / capacity, 1) if capacity else 0

    dt_utc = None
    raw = s.get("startsAt")
    if raw:
        dt_utc = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    dt_paris = dt_utc.astimezone(PARIS_TZ) if dt_utc else None

    return {
        "horodatage": snapshot_time,
        "id_session": s.get("id"),
        "nom_session": s.get("sessionName"),
        "date_session": dt_paris.strftime("%Y-%m-%d %H:%M:%S") if dt_paris else "",
        "jour_semaine": JOURS_FR[dt_paris.weekday()] if dt_paris else "",
        "heure": dt_paris.strftime("%H:%M") if dt_paris else "",
        "coach": s.get("teacher"),
        "lieu": s.get("location"),
        "capacite": capacity,
        "places_restantes": remaining,
        "places_reservees": booked,
        "taux_remplissage_pct": fill_rate,
        "liste_attente_pleine": bool(s.get("waitlistFull")),
    }


def load_existing_sessions() -> Dict[str, Dict]:
    """Load the previous latest.json (if any) as a dict keyed by id_session (as string)."""
    if not os.path.isfile(LATEST_JSON):
        return {}
    try:
        with open(LATEST_JSON, "r", encoding="utf-8") as f:
            old = json.load(f)
        return {str(r["id_session"]): r for r in old.get("sessions", [])}
    except (json.JSONDecodeError, KeyError):
        return {}


def write_latest_json(new_records: List[Dict], snapshot_time: str) -> None:
    """
    Merge this run's records into the existing snapshot instead of overwriting it.

    The feed only ever returns current/future sessions, so once a
    session's date passes it silently drops out of the API response. Without
    merging, that session would vanish from the dashboard the next day. Here
    we keep every session ever seen, refreshing the ones the API still
    returns and leaving past ones exactly as they were last recorded.
    """
    existing = load_existing_sessions()

    for r in new_records:
        existing[str(r["id_session"])] = r

    merged = list(existing.values())
    merged.sort(key=lambda r: r.get("date_session", ""))

    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {"snapshot_time": snapshot_time, "sessions": merged}
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def append_history_csv(records: List[Dict], snapshot_time: str) -> None:
    """Append one row per session to the long-running history log."""
    os.makedirs(DATA_DIR, exist_ok=True)
    file_exists = os.path.isfile(HISTORY_CSV)

    fieldnames = [
        "horodatage",
        "id_session",
        "nom_session",
        "date_session",
        "jour_semaine",
        "heure",
        "coach",
        "lieu",
        "capacite",
        "places_restantes",
        "places_reservees",
        "taux_remplissage_pct",
        "liste_attente_pleine",
    ]

    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in records:
            writer.writerow(r)


def main():
    now = datetime.now(timezone.utc)
    snapshot_time = now.strftime("%Y-%m-%d %H:%M:%S")
    from_date_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    raw_sessions = fetch_all_sessions(from_date_iso)
    records = [to_record(s, snapshot_time) for s in raw_sessions]

    write_latest_json(records, snapshot_time)
    append_history_csv(records, snapshot_time)

    print(f"Snapshot {snapshot_time}: {len(records)} sessions written to {LATEST_JSON} and {HISTORY_CSV}")


if __name__ == "__main__":
    main()
