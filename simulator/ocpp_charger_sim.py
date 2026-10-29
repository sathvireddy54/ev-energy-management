"""
Charger simulator - nearby charging stations reporting live over OCPP 1.6-J.

Each nearby Open Charge Map station becomes a simulated charge point "OCM-<station id>" that connects to the
backend's central system (ws://<server>/ocpp/OCM-<id>) exactly as a real charger connects to its operator:
BootNotification, StatusNotification for every point (Available / Charging / Faulted), StartTransaction /
StopTransaction, MeterValues and a Heartbeat every 30 s. Each charger also announces its tariff (price per kWh
incl. GST) with a DataTransfer message (messageId "Tariff") at start-up and with every heartbeat.

Cars arrive at random (Poisson) with the arrival rates of the station demand model for the current hour,
times --demand; each stays plugged in for a random (exponential) time around the model's session length.
--speed makes everything happen faster (60 = one simulated hour per real minute) without changing how busy
a station is on average, so changes can be seen within minutes. A car that finds every point busy leaves.

Run (with the backend running, from the project folder):
    python simulator/ocpp_charger_sim.py --location "Banjara Hills, Hyderabad"
Options:
    --stations 5        how many of the nearest stations to simulate
    --demand 1.0        1 = as the model expects; 2 = twice as many cars (try it to watch adaptive learning)
    --speed 60          time acceleration of arrivals and sessions
    --fault-rate 0.02   chance per point per simulated hour of a fault (lasts ~20 simulated minutes)
    --server http://127.0.0.1:8000
    --tariff 18         price per kWh for all stations (default: listed price, or typical for the station type)
    --tariff-change 0.1 chance every 30 s that an operator changes its tariff (to watch live tariffs update)
Stop with Ctrl+C (the chargers disconnect; their last status is kept for 5 minutes).

This is a simulation for demonstrating the real-time pipeline; real chargers or an operator's back office
would connect to the same endpoint with the same messages.
"""
import argparse
import asyncio
import json
import math
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    sys.exit("The 'websockets' package is missing. Install it with:  pip install websockets")

HEARTBEAT_S = 30


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


class ChargePoint:
    def __init__(self, st, args):
        self.st, self.args = st, args
        self.cp_id = f"OCM-{st['id']}"
        self.name = st["name"]
        self.n = max(1, min(int(st["points"] or 2), 12))
        self.status = {i: "Available" for i in range(1, self.n + 1)}
        self.ends = {}                       # point -> real time when the session / fault ends
        self.tx = {}
        self.ws, self.uid, self.lock = None, 0, asyncio.Lock()
        self.meter = {i: 0 for i in range(1, self.n + 1)}
        self.tariff = self.pick_tariff()

    def pick_tariff(self):
        """The station's price per kWh incl. GST: its Open Charge Map price if listed, otherwise a typical Indian
        tariff for its type (sockets for 2W/3W ~Rs 8-12, AC ~Rs 12-16, DC fast ~Rs 18-24), rounded to 50 paise."""
        if self.args.tariff:
            return self.args.tariff
        if self.st.get("listed_tariff"):
            return round(float(self.st["listed_tariff"]), 2)
        kw = self.st.get("power_kw") or 7
        lo, hi = (18, 24) if kw > 22 else (8, 12) if kw <= 3.5 else (12, 16)
        return round(random.uniform(lo, hi) * 2) / 2

    async def send_tariff(self):
        r = await self.call("DataTransfer", {"vendorId": "EVEM", "messageId": "Tariff", "data": json.dumps(
            {"price_per_kwh": self.tariff, "currency": "INR", "includes_tax": True})})
        if (r or {}).get("status") != "Accepted":
            log(f"{self.name}: tariff not accepted ({r})")

    async def call(self, action, payload):
        async with self.lock:
            self.uid += 1
            uid = str(self.uid)
            await self.ws.send(json.dumps([2, uid, action, payload]))
            while True:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
                if msg[0] in (3, 4) and msg[1] == uid:
                    if msg[0] == 4:
                        log(f"{self.name}: server error for {action}: {msg[2]} {msg[3]}")
                    return msg[2] if msg[0] == 3 else None

    async def set_status(self, point, status, error="NoError"):
        self.status[point] = status
        await self.call("StatusNotification", {"connectorId": point, "status": status, "errorCode": error,
                                               "timestamp": now_iso()})

    def busy(self):
        return sum(s == "Charging" for s in self.status.values())

    def summary(self):
        f = sum(s == "Faulted" for s in self.status.values())
        return f"{self.busy()}/{self.n} charging" + (f", {f} faulted" if f else "")

    async def run(self):
        url = self.args.ws + "/ocpp/" + self.cp_id
        backoff = 2
        while True:
            try:
                async with websockets.connect(url, subprotocols=["ocpp1.6"], open_timeout=15) as ws:
                    self.ws, backoff = ws, 2
                    await self.call("BootNotification", {"chargePointVendor": "EVEM-Sim",
                                                         "chargePointModel": f"{self.n}-point simulator"})
                    await self.send_tariff()
                    # start from the model's expected occupancy for this hour
                    hour = datetime.now().hour
                    p_busy = min(0.95, self.args.demand * self.rate(hour) * self.st["session_hours"])
                    for i in self.status:
                        if random.random() < p_busy:
                            await self.start(i, quiet=True)
                        else:
                            await self.set_status(i, "Available")
                    log(f"{self.name} connected as {self.cp_id} · {self.n} point(s) · tariff Rs {self.tariff:g}/kWh · "
                        f"{self.summary()}")
                    await self.loop()
            except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
                log(f"{self.name}: connection problem ({exc.__class__.__name__}) - retrying in {backoff} s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def rate(self, hour):
        """Arrivals per point per hour for the whole station at this hour (model x --demand)."""
        return self.st["arrivals_per_point_per_hour"][hour]

    async def start(self, i, quiet=False):
        mean_h = self.st["session_hours"]
        dur_h = random.expovariate(1 / mean_h)
        self.ends[i] = asyncio.get_running_loop().time() + dur_h * 3600 / self.args.speed
        await self.set_status(i, "Charging")
        r = await self.call("StartTransaction", {"connectorId": i, "idTag": f"SIM{random.randint(1000, 9999)}",
                                                 "meterStart": self.meter[i], "timestamp": now_iso()})
        self.tx[i] = (r or {}).get("transactionId")
        if not quiet:
            log(f"{self.name} · point {i} → Charging ({self.summary()})")

    async def stop(self, i):
        kwh = random.uniform(5, 30)
        self.meter[i] += int(kwh * 1000)
        await self.call("StopTransaction", {"transactionId": self.tx.pop(i, None) or 0, "meterStop": self.meter[i],
                                            "timestamp": now_iso(), "reason": "EVDisconnected"})
        self.ends.pop(i, None)
        await self.set_status(i, "Available")
        log(f"{self.name} · point {i} → Available ({self.summary()})")

    async def loop(self):
        loop = asyncio.get_running_loop()
        next_hb = loop.time() + HEARTBEAT_S
        tick = 1.0
        while True:
            await asyncio.sleep(tick)
            t = loop.time()
            sim_h = tick * self.args.speed / 3600            # simulated hours in this tick
            # sessions / faults that ended
            for i, end in list(self.ends.items()):
                if t >= end:
                    if self.status[i] == "Charging":
                        await self.stop(i)
                    elif self.status[i] == "Faulted":
                        self.ends.pop(i, None)
                        await self.set_status(i, "Available")
                        log(f"{self.name} · point {i} repaired → Available ({self.summary()})")
            # arrivals for the whole station (Poisson)
            lam = self.rate(datetime.now().hour) * self.n * self.args.demand * sim_h
            for _ in range(self._poisson(lam)):
                free = [i for i, s in self.status.items() if s == "Available"]
                if free:
                    await self.start(random.choice(free))
                else:
                    log(f"{self.name}: a car arrived but all points are busy - it leaves")
            # faults
            for i, s in list(self.status.items()):
                if s != "Faulted" and random.random() < self.args.fault_rate * sim_h:
                    if s == "Charging":
                        await self.call("StopTransaction", {"transactionId": self.tx.pop(i, None) or 0,
                                                            "meterStop": self.meter[i], "timestamp": now_iso(),
                                                            "reason": "Other"})
                    self.ends[i] = t + (20 / 60) * 3600 / self.args.speed
                    await self.set_status(i, "Faulted", "OtherError")
                    log(f"{self.name} · point {i} → Faulted ({self.summary()})")
            if t >= next_hb:
                await self.call("Heartbeat", {})
                next_hb = t + HEARTBEAT_S
                if self.args.tariff_change and random.random() < self.args.tariff_change:
                    old, self.tariff = self.tariff, max(5.0, round((self.tariff + random.choice([-1, -0.5, 0.5, 1])) * 2) / 2)
                    log(f"{self.name}: operator changed the tariff Rs {old:g} → Rs {self.tariff:g}/kWh")
                await self.send_tariff()
                for i, s in self.status.items():
                    if s == "Charging":
                        self.meter[i] += int(random.uniform(0.2, 1.5) * 1000)
                        await self.call("MeterValues", {"connectorId": i, "transactionId": self.tx.get(i) or 0,
                                                        "meterValue": [{"timestamp": now_iso(), "sampledValue": [
                                                            {"value": str(self.meter[i]), "unit": "Wh"}]}]})

    @staticmethod
    def _poisson(lam):
        if lam <= 0:
            return 0
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= random.random()
            if p <= L:
                return k
            k += 1


async def main():
    ap = argparse.ArgumentParser(description="Simulated chargers reporting live over OCPP 1.6-J")
    ap.add_argument("--location", help='e.g. "Banjara Hills, Hyderabad"')
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--radius", type=float, default=10, help="km")
    ap.add_argument("--stations", type=int, default=5)
    ap.add_argument("--demand", type=float, default=1.0)
    ap.add_argument("--speed", type=float, default=60)
    ap.add_argument("--fault-rate", type=float, default=0.02)
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--tariff", type=float, help="fixed Rs per kWh for every station (default: per station)")
    ap.add_argument("--tariff-change", type=float, default=0.0,
                    help="chance at each heartbeat (30 s) that the operator changes the tariff, e.g. 0.1")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    if not args.location and (args.lat is None or args.lon is None):
        ap.error('give --location "Area, City" or --lat and --lon')
    args.ws = args.server.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    q = {"distance_km": args.radius, "limit": args.stations}
    if args.location:
        q["q"] = args.location
    else:
        q.update(latitude=args.lat, longitude=args.lon)
    try:
        data = get_json(args.server.rstrip("/") + "/api/ocpp/candidates?" + urllib.parse.urlencode(q))
    except Exception as exc:
        sys.exit(f"Could not reach the backend at {args.server} ({exc}). Is the server running?")
    if not data["stations"]:
        sys.exit("No stations found there - try a larger --radius.")
    log(f"Simulating {len(data['stations'])} station(s) near {data['location']} · demand x{args.demand} · "
        f"speed x{args.speed:g} (1 real minute = {args.speed:g} simulated minutes)")
    cps = [ChargePoint(s, args) for s in data["stations"]]
    await asyncio.gather(*(cp.run() for cp in cps))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
