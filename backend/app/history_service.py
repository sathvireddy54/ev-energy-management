"""
Reading history per vehicle (used by the anomaly check).

Each time the owner checks the battery, the full-charge range (range shown / SOC) is saved with the
date and odometer. Later checks compare the battery with its own earlier readings, so the owner does not
need to remember the range when the vehicle was new.

Readings belong to the logged-in user's account (auth_service); the vehicle id sent by the browser is replaced
by the account's own id, so no user can read another user's readings.
Stored in a local SQLite file (data/records.db).
"""
import sqlite3
from datetime import date
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "records.db"


def _vid(vehicle_id):
    """With user accounts, readings always belong to the logged-in user, whatever id the browser sends."""
    from app.auth_service import current_user
    user = current_user.get()
    return user["vehicle_id"] if user else vehicle_id


def reassign(old_vehicle_id, new_vehicle_id):
    """Move readings and trips saved before the user had an account (browser id) to the account."""
    if not old_vehicle_id or old_vehicle_id == new_vehicle_id or old_vehicle_id.startswith("user-"):
        return 0
    with _conn() as c:
        n = c.execute("UPDATE readings SET vehicle_id = ? WHERE vehicle_id = ?", (new_vehicle_id, old_vehicle_id)).rowcount
    with _tconn() as c:
        n += c.execute("UPDATE trips SET vehicle_id = ? WHERE vehicle_id = ?", (new_vehicle_id, old_vehicle_id)).rowcount
    return n


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id TEXT NOT NULL, reading_date TEXT NOT NULL,
        odometer_km REAL NOT NULL, soc_pct REAL NOT NULL, range_km REAL NOT NULL,
        full_charge_km REAL NOT NULL, source TEXT)""")
    return c


def add(vehicle_id, readings, odometer_km, reading_date=None, source="check"):
    """readings: list of (soc_pct, range_km) taken on the same day."""
    vehicle_id = _vid(vehicle_id)
    if not vehicle_id:
        return 0
    d = (reading_date or date.today()).isoformat()
    with _conn() as c:
        added = 0
        for soc, rng in readings:
            dup = c.execute("SELECT 1 FROM readings WHERE vehicle_id = ? AND reading_date = ? AND odometer_km = ? "
                            "AND soc_pct = ? AND range_km = ?", (vehicle_id, d, float(odometer_km), float(soc),
                                                                  float(rng))).fetchone()
            if dup:                      # the same reading checked twice on one day is stored once
                continue
            added += 1
            c.execute("INSERT INTO readings (vehicle_id, reading_date, odometer_km, soc_pct, range_km, full_charge_km, "
                      "source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (vehicle_id, d, float(odometer_km), float(soc), float(rng), float(rng) / (float(soc) / 100), source))
    return added


def get(vehicle_id):
    vehicle_id = _vid(vehicle_id)
    if not vehicle_id:
        return []
    with _conn() as c:
        rows = c.execute("SELECT id, reading_date, odometer_km, soc_pct, range_km, full_charge_km, source FROM readings "
                         "WHERE vehicle_id = ? ORDER BY reading_date, id", (vehicle_id,)).fetchall()
    return [dict(r) for r in rows]


def delete(vehicle_id, reading_id=None):
    vehicle_id = _vid(vehicle_id)
    with _conn() as c:
        if reading_id:
            return c.execute("DELETE FROM readings WHERE vehicle_id = ? AND id = ?", (vehicle_id, reading_id)).rowcount
        return c.execute("DELETE FROM readings WHERE vehicle_id = ?", (vehicle_id,)).rowcount


# ------------------------------------------------------------------ trips (discharge behaviour)
def _tconn():
    c = _conn()
    c.execute("""CREATE TABLE IF NOT EXISTS trips (
        id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id TEXT NOT NULL, trip_date TEXT NOT NULL,
        soc_start REAL, range_start_km REAL, soc_end REAL, distance_km REAL, ride_mode TEXT,
        promised_km REAL, delivered_km REAL, gap_pct REAL)""")
    return c


def add_trip(vehicle_id, t, trip_date=None):
    vehicle_id = _vid(vehicle_id)
    if not vehicle_id:
        return None
    d = (trip_date or date.today()).isoformat()
    with _tconn() as c:
        dup = c.execute("SELECT id FROM trips WHERE vehicle_id = ? AND trip_date = ? AND soc_start = ? AND "
                        "range_start_km = ? AND soc_end = ? AND distance_km = ?",
                        (vehicle_id, d, t["soc_start"], t["range_start_km"], t["soc_end"], t["distance_km"])).fetchone()
        if dup:
            return dup["id"]
        cur = c.execute("INSERT INTO trips (vehicle_id, trip_date, soc_start, range_start_km, soc_end, distance_km, "
                        "ride_mode, promised_km, delivered_km, gap_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (vehicle_id, d, t["soc_start"], t["range_start_km"], t["soc_end"], t["distance_km"],
                         t.get("ride_mode"), t["promised_km"], t["delivered_km"], t["gap_pct"]))
        return cur.lastrowid


def get_trips(vehicle_id):
    vehicle_id = _vid(vehicle_id)
    if not vehicle_id:
        return []
    with _tconn() as c:
        rows = c.execute("SELECT * FROM trips WHERE vehicle_id = ? ORDER BY trip_date, id", (vehicle_id,)).fetchall()
    return [dict(r) for r in rows]


def delete_trips(vehicle_id, trip_id=None):
    vehicle_id = _vid(vehicle_id)
    with _tconn() as c:
        if trip_id:
            return c.execute("DELETE FROM trips WHERE vehicle_id = ? AND id = ?", (vehicle_id, trip_id)).rowcount
        return c.execute("DELETE FROM trips WHERE vehicle_id = ?", (vehicle_id,)).rowcount
