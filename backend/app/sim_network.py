"""
Built-in charger network simulation - every station you search reports live, anywhere in India.

No Indian charging network offers a free live feed, so the backend simulates one: whenever stations appear in a
search (Station finder, Plan my charge, Demand, Pricing), each Open Charge Map station without a real feed gets
a simulated charge point "OCM-<id>". It speaks OCPP 1.6-J messages to the same central system
(ocpp_service.handle) that real chargers would use - BootNotification, StatusNotification, Start/StopTransaction,
MeterValues, Heartbeat every 30 s and its tariff (DataTransfer "Tariff") - so availability, demand, tariff,
dynamic price and adaptive learning all run on it exactly as on a real feed.

Behaviour per station: cars arrive at random (Poisson) with the station demand model's arrival rates for the
current hour x SIM_DEMAND and stay a random (exponential) time around the model's session length; points fail
now and then. SIM_SPEED (default 20) makes arrivals and sessions happen faster so changes are visible within
minutes, without changing how busy a station is on average. Stations not searched for SIM_IDLE_H hours stop.

Settings in backend/.env:  SIMULATE_CHARGERS=0 switches it off;  SIM_SPEED, SIM_DEMAND, SIM_FAULT_RATE.
Stations shown with this feed are labelled "charger feed (simulated)". A real charger or the stand-alone
simulator (simulator/ocpp_charger_sim.py) connected for a station takes precedence.
"""
import json
import math
import os
import random
import threading
import time
from datetime import datetime

from app import ocpp_service
from app import station_demand_service as sds

VENDOR = "EVEM-BuiltInSim"
MAX_STATIONS = 600
HEARTBEAT_S = 30
TICK_S = 1.0
_lock = threading.Lock()
_cps = {}                     # station_id -> ChargePoint
_thread = None
_uid = [0]


def enabled():
    return os.getenv("SIMULATE_CHARGERS", "1").strip().lower() not in ("0", "false", "no", "off")


def _speed():
    return float(os.getenv("SIM_SPEED", "20") or 20)


def _demand():
    return float(os.getenv("SIM_DEMAND", "1") or 1)


def _fault_rate():
    return float(os.getenv("SIM_FAULT_RATE", "0.02") or 0.02)


def _idle_s():
    return float(os.getenv("SIM_IDLE_H", "3") or 3) * 3600


def _call(cp_id, action, payload):
    _uid[0] += 1
    reply = ocpp_service.handle(cp_id, json.dumps([2, str(_uid[0]), action, payload]))
    try:
        msg = json.loads(reply) if reply else None
    except ValueError:
        return None
    return msg[2] if msg and msg[0] == 3 else None


def _now_iso():
    return datetime.now().astimezone().isoformat()


class ChargePoint:
    def __init__(self, st):
        self.sid = str(st["id"])
        self.cp_id = f"OCM-{self.sid}"
        self.name = st.get("name") or self.cp_id
        self.n = max(1, min(int(st.get("number_of_points") or 2), 12))
        self.power = st.get("max_power_kw")
        self.tariff = self._pick_tariff(st)
        self.status = {i: "Available" for i in range(1, self.n + 1)}
        self.ends, self.tx, self.meter = {}, {}, {i: 0 for i in range(1, self.n + 1)}
        self.last_used = time.time()
        self.next_hb = time.monotonic() + random.uniform(5, HEARTBEAT_S)
        self._wd = None
        self.booted = False
        self._profile()

    def _profile(self):
        wd = datetime.now().weekday()
        if wd != self._wd:
            p = sds.arrival_profile(self.power, wd)
            self.rates, self.session_h, self._wd = p["arrivals_per_point_per_hour"], p["session_hours"], wd

    @staticmethod
    def _pick_tariff(st):
        if st.get("price_comparable") and st.get("observed_price_inr"):
            return round(float(st["observed_price_inr"]), 2)
        kw = st.get("max_power_kw") or 7
        lo, hi = (18, 24) if kw > 22 else (8, 12) if kw <= 3.5 else (12, 16)
        rnd = random.Random(st["id"])            # same station -> same tariff after a restart
        return round(rnd.uniform(lo, hi) * 2) / 2

    def _set(self, i, status, error="NoError"):
        self.status[i] = status
        _call(self.cp_id, "StatusNotification", {"connectorId": i, "status": status, "errorCode": error,
                                                 "timestamp": _now_iso()})

    def _send_tariff(self):
        _call(self.cp_id, "DataTransfer", {"vendorId": "EVEM", "messageId": "Tariff", "data": json.dumps(
            {"price_per_kwh": self.tariff, "currency": "INR", "includes_tax": True})})

    def boot(self):
        _call(self.cp_id, "BootNotification", {"chargePointVendor": VENDOR, "chargePointModel": f"{self.n}-point"})
        self._send_tariff()
        p_busy = min(0.95, _demand() * self.rates[datetime.now().hour] * self.session_h)
        for i in self.status:
            if random.random() < p_busy:
                self._start(i, time.monotonic())
            else:
                self._set(i, "Available")
        _call(self.cp_id, "Heartbeat", {})
        self.booted = True

    def _start(self, i, now):
        self.ends[i] = now + random.expovariate(1 / self.session_h) * 3600 / _speed()
        self._set(i, "Charging")
        r = _call(self.cp_id, "StartTransaction", {"connectorId": i, "idTag": f"SIM{random.randint(1000, 9999)}",
                                                   "meterStart": self.meter[i], "timestamp": _now_iso()})
        self.tx[i] = (r or {}).get("transactionId")

    def _stop(self, i, reason="EVDisconnected"):
        self.meter[i] += int(random.uniform(5, 30) * 1000)
        _call(self.cp_id, "StopTransaction", {"transactionId": self.tx.pop(i, None) or 0, "meterStop": self.meter[i],
                                              "timestamp": _now_iso(), "reason": reason})
        self.ends.pop(i, None)

    def step(self, now, dt):
        self._profile()
        sim_h = dt * _speed() / 3600
        for i, end in list(self.ends.items()):
            if now >= end:
                if self.status[i] == "Charging":
                    self._stop(i)
                else:
                    self.ends.pop(i, None)
                self._set(i, "Available")
        lam = self.rates[datetime.now().hour] * self.n * _demand() * sim_h
        for _ in range(_poisson(lam)):
            free = [i for i, s in self.status.items() if s == "Available"]
            if free:
                self._start(random.choice(free), now)
        for i, s in list(self.status.items()):
            if s != "Faulted" and random.random() < _fault_rate() * sim_h:
                if s == "Charging":
                    self._stop(i, "Other")
                self.ends[i] = now + (20 / 60) * 3600 / _speed()
                self._set(i, "Faulted", "OtherError")
        if now >= self.next_hb:
            self.next_hb = now + HEARTBEAT_S
            _call(self.cp_id, "Heartbeat", {})
            self._send_tariff()


def _poisson(lam):
    if lam <= 0:
        return 0
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p <= L:
            return k
        k += 1


def _run():
    last = time.monotonic()
    while True:
        time.sleep(TICK_S)
        now = time.monotonic()
        dt, last = now - last, now
        with _lock:
            idle = [k for k, cp in _cps.items() if time.time() - cp.last_used > _idle_s()]
            for k in idle:
                _cps.pop(k, None)
            cps = list(_cps.values())
        for cp in cps:
            if not cp.booted:                   # its first report is still being sent by the search request
                continue
            try:
                cp.step(now, dt)
            except Exception as exc:            # one station's problem must not stop the others
                print(f"Simulated charger {cp.cp_id}: {exc}")


def start():
    global _thread
    if enabled() and _thread is None:
        _thread = threading.Thread(target=_run, name="charger-sim", daemon=True)
        _thread.start()


def ensure(stations):
    """Give every searched Open Charge Map station a simulated charger feed (unless a real / stand-alone
    charger reports for it). New stations report their first status right away, so the same search is live."""
    if not enabled():
        return 0
    start()
    cands = [s for s in stations if s.get("id") is not None and not str(s["id"]).startswith("g:")]
    others = ocpp_service.external_active([s["id"] for s in cands], VENDOR)
    new = []
    with _lock:
        for s in cands:
            sid = str(s["id"])
            if sid in others:
                _cps.pop(sid, None)
                continue
            if sid in _cps:
                _cps[sid].last_used = time.time()
            elif len(_cps) < MAX_STATIONS:
                _cps[sid] = cp = ChargePoint(s)
                new.append(cp)
    # the stations report straight away (shown live in this search); beyond 150 the rest start in the background
    dist = {str(s["id"]): s.get("distance_km") or 0 for s in cands}
    new.sort(key=lambda cp: dist.get(cp.sid, 0))
    now_n = int(os.getenv("SIM_BOOT_NOW", "150") or 150)     # all searched stations live in the same search
    for cp in new[:now_n]:
        cp.boot()
    if new[now_n:]:
        threading.Thread(target=lambda rest=new[now_n:]: [cp.boot() for cp in rest], daemon=True).start()
    return len(new)


def status():
    with _lock:
        n = len(_cps)
    return {"enabled": enabled(), "simulated_stations": n, "speed": _speed(), "demand": _demand(),
            "fault_rate_per_hour": _fault_rate()}
