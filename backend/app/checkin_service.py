"""
Driver check-ins - free, crowd-sourced live station status (like PlugShare check-ins).

At a station, a driver reports one of:
    free         - at least one point is free (optionally how many of how many)
    busy         - all points in use, nobody waiting
    queue        - all points in use and N vehicles waiting
    not_working  - the station / points are not working
Reports from the last 45 minutes are treated as live; the most recent report wins, and the number of
agreeing recent reports is shown. Older reports (up to 24 h) are shown for information only.

The live summary uses the same shape as the Google live data, so demand, waiting time and price use it
in the same way.
"""
import sqlite3  # noqa: F401  (local file when DATABASE_URL is not set - see db.py)
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import db

DB = Path(__file__).resolve().parent.parent / "data" / "records.db"
LIVE_MINUTES = 45
INFO_HOURS = 24
STATUSES = {"free": "Points free", "busy": "All points busy", "queue": "Queue", "not_working": "Not working"}


def _conn():
    c = db.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL, station_name TEXT,
        latitude REAL, longitude REAL, created_utc TEXT NOT NULL, status TEXT NOT NULL,
        free_points INTEGER, total_points INTEGER, queue INTEGER)""")
    return c


def _now():
    return datetime.now(timezone.utc)


def add(station_id, status, station_name=None, latitude=None, longitude=None, free_points=None, total_points=None,
        queue=None):
    if status not in STATUSES:
        raise ValueError("Status must be free, busy, queue or not_working.")
    if total_points is not None and free_points is not None and free_points > total_points:
        raise ValueError("Free points cannot be more than the total points.")
    if status == "free" and free_points == 0:
        raise ValueError("'Points free' needs at least 1 free point.")
    with _conn() as c:
        cur = c.execute("INSERT INTO checkins (station_id, station_name, latitude, longitude, created_utc, status, "
                        "free_points, total_points, queue) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(station_id), station_name, latitude, longitude, _now().isoformat(), status, free_points,
                         total_points, queue if status == "queue" else None))
        return cur.lastrowid


def recent(station_ids):
    """station_id -> {'live': summary or None, 'last_report': summary of the latest report within 24 h}."""
    ids = [str(i) for i in station_ids if i is not None]
    if not ids:
        return {}
    since = (_now() - timedelta(hours=INFO_HOURS)).isoformat()
    q = f"SELECT * FROM checkins WHERE station_id IN ({','.join('?' * len(ids))}) AND created_utc >= ? ORDER BY created_utc DESC"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(q, (*ids, since)).fetchall()]
    out = {}
    for sid in ids:
        rs = [r for r in rows if r["station_id"] == sid]
        if not rs:
            continue
        for r in rs:
            r["minutes_ago"] = max(0, round((_now() - datetime.fromisoformat(r["created_utc"])).total_seconds() / 60))
        latest = rs[0]
        live_rs = [r for r in rs if r["minutes_ago"] <= LIVE_MINUTES]
        agree = sum(1 for r in live_rs if r["status"] == latest["status"])
        out[sid] = {"latest": {"status": latest["status"], "label": STATUSES[latest["status"]],
                               "minutes_ago": latest["minutes_ago"], "free_points": latest["free_points"],
                               "total_points": latest["total_points"], "queue": latest["queue"]},
                    "is_live": latest["minutes_ago"] <= LIVE_MINUTES, "reports_live": len(live_rs),
                    "agreeing_reports": agree, "reports_24h": len(rs)}
    return out


def to_live(summary, station_points):
    """Check-in summary -> live availability dict (same shape as live_service.parse output)."""
    l = summary["latest"]
    total = l["total_points"] or station_points or 1
    if l["status"] == "free":
        avail, oos, queue = (l["free_points"] if l["free_points"] is not None else 1), 0, 0
    elif l["status"] == "busy":
        avail, oos, queue = 0, 0, 0
    elif l["status"] == "queue":
        avail, oos, queue = 0, 0, l["queue"] or 1
    else:
        avail, oos, queue = 0, total, 0
    avail = min(avail, total)
    return {"source": "driver_checkins", "connector_count": total, "available": avail, "out_of_service": oos,
            "queue": queue, "has_live_availability": True, "updated_min_ago": l["minutes_ago"],
            "connectors": [], "reports": summary["reports_live"], "agreeing_reports": summary["agreeing_reports"],
            "label": l["label"], "free_points_reported": l["free_points"] is not None}


def ocm_checkins(raw_station, days=90):
    """Open Charge Map user check-ins for one station: recent successes / failures and the latest one."""
    comments = raw_station.get("UserComments") or []
    now = _now()
    items = []
    for cm in comments:
        try:
            dt = datetime.fromisoformat(str(cm.get("DateCreated")).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        st = cm.get("CheckinStatusType") or {}
        title = st.get("Title")
        if not title:
            continue
        pos = st.get("IsPositive")
        if pos is None:
            pos = "success" in title.lower()
        items.append({"date": dt.date().isoformat(), "days_ago": (now - dt).days, "title": title, "positive": bool(pos)})
    items.sort(key=lambda x: x["days_ago"])
    recent_items = [i for i in items if i["days_ago"] <= days]
    verified = raw_station.get("DateLastVerified") or raw_station.get("DateLastStatusUpdate")
    verified_days = None
    if verified:
        try:
            v = datetime.fromisoformat(str(verified).replace("Z", "+00:00"))
            verified_days = (now - (v if v.tzinfo else v.replace(tzinfo=timezone.utc))).days
        except ValueError:
            pass
    if not items and verified_days is None:
        return None
    return {"latest": items[0] if items else None, "successes_90d": sum(i["positive"] for i in recent_items),
            "failures_90d": sum(not i["positive"] for i in recent_items), "total": len(items),
            "status_updated_days_ago": verified_days}
