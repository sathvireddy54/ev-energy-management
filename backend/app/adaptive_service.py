"""
Adaptive learning - live observations recalibrate each station's demand estimate.

Every real live reading of a station is stored as an observation: the share of its points in use at that local
hour (a real charger's feed over OCPP every heartbeat, a driver report, or Google Maps availability when used).
Simulated charger feeds are NOT used: they are generated from the demand model itself, so learning from them
would be circular. The method is validated separately with a known answer (training/validate_adaptive.py).

The station demand model (learned on the Palo Alto ChargePoint sessions) predicts that share from the
station's points and power. For each station the ratio between observed and predicted use is learned,
first for the station as a whole and then per hour of the day, with shrinkage so that a few readings move
the estimate only a little:

    station factor   R_s  = (sum observed + K x mean predicted) / (sum predicted + K x mean predicted)
    hour factor      R_sh = (sum observed_h + K x mean predicted_h x R_s) / (sum predicted_h + K x mean predicted_h)

K = 6 readings (prior weight towards "the model is right"); factors are kept within x0.25-x4.
The estimate for that station and hour then uses arrivals x R_sh, so it moves towards what is really
observed there - the estimates keep improving as live data comes in, also for hours with no live reading now.

Evaluation: observations are split by time (first 80 % learn, last 20 % test) and the error of the plain
model is compared with the calibrated one.
"""
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app import station_demand_service as sds

DB = Path(__file__).resolve().parent.parent / "data" / "records.db"
K = 6.0
R_MIN, R_MAX = 0.25, 4.0
THROTTLE_S = 25          # at most one observation per station and source every 25 s
REFRESH_S = 20           # recompute the calibration at most this often
_lock = threading.Lock()
_cache = {"at": 0.0, "dirty": True, "cal": {}}


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT NOT NULL, ts_utc TEXT NOT NULL, local_date TEXT,
        weekday INTEGER, hour INTEGER, source TEXT, points INTEGER, busy REAL, out_of_service INTEGER,
        utilisation REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS obs_station ON observations (station_id, ts_utc)")
    c.execute("""CREATE TABLE IF NOT EXISTS station_meta (
        station_id TEXT PRIMARY KEY, name TEXT, points INTEGER, power_kw REAL, updated_utc TEXT)""")
    return c


def remember_stations(feats):
    """Keep each station's points and power so its observations can be compared with the model."""
    rows = [(str(s["id"]), s.get("name"), s.get("number_of_points"), s.get("max_power_kw"))
            for s in feats if s.get("id") is not None and not str(s["id"]).startswith("g:")]
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as c:
        c.executemany("INSERT INTO station_meta (station_id, name, points, power_kw, updated_utc) VALUES (?, ?, ?, ?, ?) "
                      "ON CONFLICT(station_id) DO UPDATE SET name = excluded.name, "
                      "points = COALESCE(excluded.points, station_meta.points), "
                      "power_kw = COALESCE(excluded.power_kw, station_meta.power_kw), updated_utc = excluded.updated_utc",
                      [(*r, now) for r in rows])


def summary_meta(station_ids):
    ids = [str(i) for i in station_ids]
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM station_meta WHERE station_id IN ({','.join('?' * len(ids))})", ids).fetchall()]


def record(station_id, source, live, when=None):
    """live: live availability dict (connector_count, available, out_of_service, queue)."""
    if not live or station_id is None or str(station_id).startswith("g:"):
        return False
    usable = live["connector_count"] - (live.get("out_of_service") or 0)
    if usable <= 0:
        return False                         # station not working - says nothing about demand
    busy = usable - (live.get("available") or 0)
    util = 1.0 if live.get("queue") else max(0.0, min(1.0, busy / usable))
    t = when or datetime.now()
    utc = t.astimezone(timezone.utc) if when else datetime.now(timezone.utc)
    with _lock, _conn() as c:
        last = c.execute("SELECT ts_utc FROM observations WHERE station_id = ? AND source = ? ORDER BY id DESC LIMIT 1",
                         (str(station_id), source)).fetchone()
        if last and when is None and 0 <= (utc - datetime.fromisoformat(last["ts_utc"])).total_seconds() < THROTTLE_S:
            return False
        c.execute("INSERT INTO observations (station_id, ts_utc, local_date, weekday, hour, source, points, busy, "
                  "out_of_service, utilisation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (str(station_id), utc.isoformat(), t.date().isoformat(), t.weekday(), t.hour, source,
                   live["connector_count"], busy, live.get("out_of_service") or 0, util))
        _cache["dirty"] = True
    return True


def _rows():
    with _lock, _conn() as c:
        obs = [dict(r) for r in c.execute("SELECT * FROM observations ORDER BY ts_utc").fetchall()]
        meta = {r["station_id"]: dict(r) for r in c.execute("SELECT * FROM station_meta").fetchall()}
    return obs, meta


def _predicted(meta, points, weekday, hour, factor=1.0):
    """Model share of points in use (average day of that weekday, scale 1) x learned factor."""
    p = (meta or {}).get("points") or points
    kw = (meta or {}).get("power_kw")
    return _util(p, kw, weekday, hour, round(factor, 3))


@lru_cache(maxsize=20000)
def _util(points, power_kw, weekday, hour, factor):
    return sds.station_demand(points, power_kw, weekday, hour, factor)["utilisation_pct"] / 100


def _fit(obs, meta):
    """station_id -> {'R': station factor, 'n': readings, 'hours': {hour: (factor, n)}}"""
    by = {}
    for o in obs:
        m = meta.get(o["station_id"])
        pred = _predicted(m, o["points"], o["weekday"], o["hour"])
        by.setdefault(o["station_id"], []).append((o["hour"], o["utilisation"], pred))
    cal = {}
    for sid, xs in by.items():
        n = len(xs)
        O, M = sum(x[1] for x in xs), sum(x[2] for x in xs)
        mbar = M / n
        if mbar < 0.005:                       # model expects ~no use at these hours - compare on a floor
            mbar = 0.005
            M = max(M, mbar * n)
        R = min(max((O + K * mbar) / (M + K * mbar), R_MIN), R_MAX)
        hours = {}
        for h in sorted({x[0] for x in xs}):
            hx = [x for x in xs if x[0] == h]
            oh, mh = sum(x[1] for x in hx), sum(x[2] for x in hx)
            if mh / len(hx) < 0.01:            # model expects ~no use at this hour: a ratio says little,
                hours[h] = (round(R, 3), len(hx))   # so use the station's overall factor
                continue
            mbh = mh / len(hx)
            hours[h] = (round(min(max((oh + K * mbh * R) / (mh + K * mbh), R_MIN), R_MAX), 3), len(hx))
        cal[sid] = {"R": round(R, 3), "n": n, "hours": hours, "observed_mean_pct": round(O / n * 100, 1),
                    "model_mean_pct": round(sum(x[2] for x in xs) / n * 100, 1)}
    return cal


def calibration():
    if _cache["dirty"] or time.time() - _cache["at"] > REFRESH_S:
        obs, meta = _rows()
        _cache.update(cal=_fit(obs, meta), at=time.time(), dirty=False)
    return _cache["cal"]


def factor(station_id, hour):
    """(arrival factor for this station and hour, readings behind it); (1.0, 0) when nothing is learned."""
    c = calibration().get(str(station_id))
    if not c:
        return 1.0, 0
    if hour in c["hours"]:
        f, n = c["hours"][hour]
        return f, n
    return c["R"], 0


def learned(station_id):
    return str(station_id) in calibration()


def evaluate(obs=None, meta=None):
    """Learn from the first 80 % of readings (by time), test on the last 20 %.
    Scored on the average share of points in use per station, day and hour - what the estimate predicts
    (one reading of a 2-point station is only ever 0, 50 or 100 %)."""
    obs, meta = (obs, meta) if obs is not None else _rows()
    if len(obs) < 40:
        return None
    cut = int(len(obs) * 0.8)
    train, test = obs[:cut], obs[cut:]
    cal = _fit(train, meta)
    groups = {}
    for o in test:
        groups.setdefault((o["station_id"], o["local_date"], o["hour"]), []).append(o)
    e_model, e_cal, n_read = [], [], 0
    for (sid, _, hour), os_ in groups.items():
        o = os_[0]
        m = meta.get(sid)
        observed = sum(x["utilisation"] for x in os_) / len(os_)
        base = _predicted(m, o["points"], o["weekday"], hour)
        c = cal.get(sid)
        f = (c["hours"][hour][0] if hour in c["hours"] else c["R"]) if c else 1.0
        adj = _predicted(m, o["points"], o["weekday"], hour, f)
        e_model.append(abs(observed - base))
        e_cal.append(abs(observed - adj))
        n_read += len(os_)
    mm, mc = sum(e_model) / len(e_model), sum(e_cal) / len(e_cal)
    return {"train_readings": len(train), "test_readings": n_read, "test_station_hours": len(groups),
            "mae_model_pct": round(mm * 100, 2), "mae_calibrated_pct": round(mc * 100, 2),
            "improvement_pct": round((1 - mc / mm) * 100, 1) if mm > 0 else None,
            "note": "Average error in the share of a station's points in use per station-hour, on the latest "
                    "20 % of readings - learned from the earlier 80 % only."}


def summary():
    obs, meta = _rows()
    cal = calibration()
    by_source = {}
    for o in obs:
        by_source[o["source"]] = by_source.get(o["source"], 0) + 1
    last = {}
    for o in obs:
        last[o["station_id"]] = o["ts_utc"]
    now = datetime.now(timezone.utc)
    stations = []
    for sid, c in sorted(cal.items(), key=lambda kv: -kv[1]["n"]):
        m = meta.get(sid) or {}
        stations.append({"station_id": sid, "name": m.get("name") or f"Station {sid}", "readings": c["n"],
                         "factor": c["R"], "observed_mean_pct": c["observed_mean_pct"],
                         "model_mean_pct": c["model_mean_pct"],
                         "hours_learned": len(c["hours"]),
                         "last_reading_s_ago": max(0, round((now - datetime.fromisoformat(last[sid])).total_seconds()))})
    return {"observations": len(obs), "by_source": by_source, "stations_calibrated": len(cal),
            "stations": stations, "evaluation": evaluate(obs, meta),
            "method": __doc__.strip().split("\n\n")[1].replace("\n", " ") if __doc__ else ""}


def purge_simulated():
    """Once: remove charger-feed readings stored before simulated feeds were excluded from learning (every charger
    feed so far was simulated). Driver reports and Google readings are kept."""
    with _lock, _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        if c.execute("SELECT 1 FROM meta WHERE key = 'simulated_purged'").fetchone():
            return 0
        n = c.execute("DELETE FROM observations WHERE source = 'ocpp'").rowcount
        c.execute("INSERT INTO meta (key, value) VALUES ('simulated_purged', ?)", (datetime.now(timezone.utc).isoformat(),))
        _cache["dirty"] = True
    return n


def reset():
    with _lock, _conn() as c:
        n = c.execute("DELETE FROM observations").rowcount
        _cache["dirty"] = True
    return n
