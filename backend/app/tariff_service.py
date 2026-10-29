"""
Station tariffs - the price a station actually charges, from the best source available.

    1. Charger feed (OCPP)   a real charger announces its tariff (DataTransfer "Tariff"); live while it is connected
                             or seen in the last 5 minutes. A simulated charger's tariff is used only when no driver
                             has reported a price.
    2. Driver reports        "I paid Rs x per kWh" sent with a status report; used for 30 days (tariffs change
                             rarely); the median of the last 30 days' reports, so one wrong entry has little effect.
    3. Open Charge Map       the usage cost listed for the station (volunteer data, may be outdated). A listed price
                             is preferred to a simulated charger feed's tariff, which is not the station's real price.
    4. none                  the tariff is not known - the dynamic price then starts from the user's base price.

All tariffs are treated as the final price per kWh including GST.

This is separate from the dynamic price (pricing_service / plan_service._price): the dynamic price is this
system's recommendation (tariff x time-of-day factor x demand factor); the tariff is what the station charges.
No Indian charging network publishes its prices through a public API, so tariffs are only as complete as
these sources.
"""
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "records.db"
REPORT_DAYS = 30
LIVE_S = 300
MIN_PRICE, MAX_PRICE = 1.0, 100.0
LABELS = {"ocpp": "Live tariff – charger feed", "driver_report": "Reported by drivers",
          "open_charge_map": "Listed on Open Charge Map"}
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS tariffs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL, source TEXT NOT NULL,
        price_per_kwh REAL NOT NULL, created_utc TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS tariffs_station ON tariffs (station_id, created_utc)")
    return c


def add(station_id, price_per_kwh, source="driver_report"):
    price = float(price_per_kwh)
    if not MIN_PRICE <= price <= MAX_PRICE:
        raise ValueError(f"Price must be between Rs {MIN_PRICE:g} and Rs {MAX_PRICE:g} per kWh.")
    if source not in ("ocpp", "driver_report"):
        raise ValueError("Unknown tariff source.")
    with _lock, _conn() as c:
        if source == "ocpp":             # keep only the latest announcement per charger feed
            c.execute("DELETE FROM tariffs WHERE station_id = ? AND source = 'ocpp'", (str(station_id),))
        c.execute("INSERT INTO tariffs (station_id, source, price_per_kwh, created_utc) VALUES (?, ?, ?, ?)",
                  (str(station_id), source, round(price, 2), _now().isoformat()))


def known(station_ids, feed_alive=None, simulated=()):
    """station_id -> best tariff from the charger feed or driver reports (Open Charge Map is added in resolve).
    A real charger's own tariff comes first; drivers' reports win over a simulated charger feed."""
    ids = [str(i) for i in station_ids if i is not None]
    if not ids:
        return {}
    since = (_now() - timedelta(days=REPORT_DAYS)).isoformat()
    q = ",".join("?" * len(ids))
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM tariffs WHERE station_id IN ({q}) AND (source = 'ocpp' OR created_utc >= ?) "
            f"ORDER BY created_utc DESC", (*ids, since)).fetchall()]
    now, out = _now(), {}
    for sid in ids:
        rs = [r for r in rows if r["station_id"] == sid]
        feed = next((r for r in rs if r["source"] == "ocpp"), None)
        age = lambda r: (now - datetime.fromisoformat(r["created_utc"])).total_seconds()
        reps = [r for r in rs if r["source"] == "driver_report"]
        if feed and ((feed_alive or {}).get(sid) or age(feed) <= LIVE_S) and not (sid in simulated and reps):
            out[sid] = {"price_per_kwh": feed["price_per_kwh"], "source": "ocpp", "label": LABELS["ocpp"],
                        "seconds_ago": round(age(feed)), "reports": 1, "live": True,
                        "simulated": sid in simulated}
            continue
        if reps:
            out[sid] = {"price_per_kwh": round(statistics.median(r["price_per_kwh"] for r in reps), 2),
                        "source": "driver_report", "label": LABELS["driver_report"],
                        "seconds_ago": round(age(reps[0])), "reports": len(reps), "live": False,
                        "latest_report": reps[0]["price_per_kwh"]}
    return out


def resolve(station, known_tariff=None):
    """Best tariff for one station feature (feed / driver reports / Open Charge Map) or None.
    Order: real charger feed, driver reports, Open Charge Map listing, simulated charger feed."""
    listed = bool(station.get("price_comparable") and station.get("observed_price_inr"))
    if known_tariff and not (known_tariff.get("simulated") and listed):
        return known_tariff
    if listed:
        return {"price_per_kwh": round(float(station["observed_price_inr"]), 2), "source": "open_charge_map",
                "label": LABELS["open_charge_map"], "seconds_ago": None, "reports": None, "live": False,
                "raw": station.get("usage_cost_raw")}
    return None
