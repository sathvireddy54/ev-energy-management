"""
Real-time charger feed over OCPP 1.6-J (Open Charge Point Protocol, JSON over WebSocket).

OCPP is the open standard real chargers use to report to their operator's back office. This module is a
minimal "central system": a charge point connects by WebSocket to /ocpp/<charge point id>, sends a
BootNotification, then StatusNotification whenever one of its points changes (Available, Charging, Faulted ...),
plus Heartbeat, StartTransaction / StopTransaction and MeterValues.

Messages (OCPP-J):  call [2, id, action, payload]  ->  result [3, id, payload]  or  error [4, id, code, text, {}]

Charge point ids "OCM-<id>" are linked to Open Charge Map station <id>. Each station's latest point statuses
become its live availability (source "ocpp"), used the same way as Google Maps data or driver reports.
In this project the chargers are simulated (simulator/ocpp_charger_sim.py); a real operator's chargers or
back office would connect the same way.
"""
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "records.db"
STALE_S = 300                    # a disconnected charger's last status is still used for 5 minutes
HEARTBEAT_S = 30                 # interval given to chargers in the BootNotification reply
IN_USE = {"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing", "Reserved"}
OUT_OF_SERVICE = {"Faulted", "Unavailable"}
STATUSES = IN_USE | OUT_OF_SERVICE | {"Available"}
CP_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_lock = threading.Lock()
_listeners = []                  # callbacks(station_id, snapshot) - used by adaptive learning


def _now():
    return datetime.now(timezone.utc)


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS ocpp_chargers (
        cp_id TEXT PRIMARY KEY, station_id TEXT, vendor TEXT, model TEXT, booted_utc TEXT,
        last_seen_utc TEXT, connected INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ocpp_connectors (
        cp_id TEXT NOT NULL, connector_id INTEGER NOT NULL, station_id TEXT, status TEXT, error_code TEXT,
        updated_utc TEXT, PRIMARY KEY (cp_id, connector_id))""")
    return c


def station_of(cp_id):
    return cp_id[4:] if cp_id.upper().startswith("OCM-") else cp_id


def valid_id(cp_id):
    return bool(CP_ID.match(cp_id or ""))


def on_snapshot(fn):
    _listeners.append(fn)


def _touch(c, cp_id):
    c.execute("INSERT INTO ocpp_chargers (cp_id, station_id, last_seen_utc, connected) VALUES (?, ?, ?, 1) "
              "ON CONFLICT(cp_id) DO UPDATE SET last_seen_utc = excluded.last_seen_utc, connected = 1",
              (cp_id, station_of(cp_id), _now().isoformat()))


def set_connected(cp_id, on):
    with _lock, _conn() as c:
        if on:
            _touch(c, cp_id)
        else:
            c.execute("UPDATE ocpp_chargers SET connected = 0, last_seen_utc = ? WHERE cp_id = ?",
                      (_now().isoformat(), cp_id))


def _result(uid, payload):
    return json.dumps([3, uid, payload])


def _error(uid, code, text):
    return json.dumps([4, uid, code, text, {}])


def handle(cp_id, raw):
    """One OCPP-J message from a charge point -> reply text (or None for results/errors sent to us)."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(msg, list) or len(msg) < 3 or msg[0] != 2:
        return None                       # results/errors to our own calls - we send none
    if len(msg) < 4 or not isinstance(msg[3], dict):
        return _error(msg[1], "FormationViolation", "Expected [2, id, action, payload]")
    _, uid, action, p = msg[:4]
    now = _now().isoformat()
    snapshot, tariff_data = False, None
    with _lock, _conn() as c:
        _touch(c, cp_id)
        if action == "BootNotification":
            c.execute("UPDATE ocpp_chargers SET vendor = ?, model = ?, booted_utc = ? WHERE cp_id = ?",
                      (p.get("chargePointVendor"), p.get("chargePointModel"), now, cp_id))
            reply = {"status": "Accepted", "currentTime": now, "interval": HEARTBEAT_S}
        elif action == "Heartbeat":
            reply, snapshot = {"currentTime": now}, True
        elif action == "StatusNotification":
            cid, status = p.get("connectorId"), p.get("status")
            if not isinstance(cid, int) or status not in STATUSES:
                return _error(uid, "PropertyConstraintViolation", "connectorId (int) and a valid status are required")
            if cid > 0:                    # connector 0 = the charge point as a whole
                c.execute("INSERT INTO ocpp_connectors (cp_id, connector_id, station_id, status, error_code, updated_utc) "
                          "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(cp_id, connector_id) DO UPDATE SET status = excluded.status, "
                          "error_code = excluded.error_code, updated_utc = excluded.updated_utc",
                          (cp_id, cid, station_of(cp_id), status, p.get("errorCode"), now))
            reply = {}
        elif action == "StartTransaction":
            reply = {"transactionId": int(_now().timestamp() * 1000) % 2_000_000_000, "idTagInfo": {"status": "Accepted"}}
        elif action in ("StopTransaction", "Authorize"):
            reply = {"idTagInfo": {"status": "Accepted"}}
        elif action == "MeterValues":
            reply = {}
        elif action == "DataTransfer":
            reply = {"status": "UnknownMessageId"}
            if p.get("messageId") == "Tariff":           # the charger announces its price per kWh (incl. GST)
                tariff_data = p.get("data")
        else:
            return _error(uid, "NotImplemented", f"{action} is not supported by this central system")
    if tariff_data is not None:
        try:
            data = json.loads(tariff_data) if isinstance(tariff_data, str) else tariff_data
            from app import tariff_service
            tariff_service.add(station_of(cp_id), data["price_per_kwh"], "ocpp")
            reply = {"status": "Accepted"}
        except (ValueError, KeyError, TypeError):
            reply = {"status": "Rejected"}
    if snapshot:
        live = live_for([station_of(cp_id)]).get(station_of(cp_id))
        for fn in _listeners:
            try:
                fn(station_of(cp_id), live)
            except Exception as exc:          # learning must never break the charger connection
                print(f"OCPP listener failed: {exc}")
    return _result(uid, reply)


def live_for(station_ids):
    """station_id -> live availability (same shape as Google / driver-report live data)."""
    ids = [str(i) for i in station_ids if i is not None]
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT k.*, ch.connected, ch.last_seen_utc, ch.vendor FROM ocpp_connectors k JOIN ocpp_chargers ch ON ch.cp_id = k.cp_id "
            f"WHERE k.station_id IN ({q}) ORDER BY k.connector_id", ids).fetchall()]
    now = _now()
    out = {}
    for sid in ids:
        rs = [r for r in rows if r["station_id"] == sid]
        # live only if the charger was heard from recently (heartbeat every 30 s) - a connection flag alone
        # could be left over if the server stopped while the charger was connected
        fresh = [r for r in rs if (now - datetime.fromisoformat(r["last_seen_utc"])).total_seconds() <= STALE_S]
        if not fresh:
            continue
        count = len(fresh)
        avail = sum(r["status"] == "Available" for r in fresh)
        oos = sum(r["status"] in OUT_OF_SERVICE for r in fresh)
        ages = [(now - datetime.fromisoformat(r["updated_utc"])).total_seconds() for r in fresh]
        seen = min((now - datetime.fromisoformat(r["last_seen_utc"])).total_seconds() for r in fresh)
        out[sid] = {"source": "ocpp", "connector_count": count, "available": avail, "out_of_service": oos,
                    "in_use": count - avail - oos, "queue": 0, "has_live_availability": True,
                    "updated_min_ago": round(seen / 60), "updated_s_ago": round(seen),
                    "last_change_s_ago": round(min(ages)),
                    "connected": any(r["connected"] for r in fresh) and seen <= 3 * HEARTBEAT_S,
                    "charge_points": sorted({r["cp_id"] for r in fresh}),
                    "simulated": all((r.get("vendor") or "").startswith("EVEM") for r in fresh),
                    "connectors": [{"point": r["connector_id"], "status": r["status"],
                                    "error_code": r["error_code"]} for r in fresh]}
    return out


def external_active(station_ids, own_vendor, within_s=120):
    """Stations with a recently seen charger that is not the built-in simulation (a real charger or the
    stand-alone simulator) - the built-in simulation leaves those alone."""
    ids = [str(i) for i in station_ids]
    if not ids:
        return set()
    with _lock, _conn() as c:
        rows = c.execute(f"SELECT station_id, vendor, last_seen_utc FROM ocpp_chargers WHERE station_id IN "
                         f"({','.join('?' * len(ids))})", ids).fetchall()
    now = _now()
    return {r["station_id"] for r in rows if (r["vendor"] or "") != own_vendor and r["last_seen_utc"] and
            (now - datetime.fromisoformat(r["last_seen_utc"])).total_seconds() <= within_s}


def reset_connections():
    """At server start no charger is connected yet (flags may be left over from a previous run)."""
    with _lock, _conn() as c:
        c.execute("UPDATE ocpp_chargers SET connected = 0")


def status():
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ocpp_chargers ORDER BY cp_id").fetchall()]
    now = _now()
    for r in rows:
        r["seconds_since_seen"] = round((now - datetime.fromisoformat(r["last_seen_utc"])).total_seconds()) \
            if r.get("last_seen_utc") else None
    return {"connected": sum(1 for r in rows if r["connected"]), "known": len(rows), "chargers": rows,
            "endpoint": "ws://<server>/ocpp/OCM-<Open Charge Map station id>  (subprotocol ocpp1.6)"}
