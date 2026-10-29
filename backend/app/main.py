"""
An Adaptive Data-Driven Framework for Smart Electric Vehicle Energy Management
Single FastAPI application: API under /api, dashboard at /.

Features
  1. Battery RUL prediction        POST /api/rul             real vehicle, owner inputs only
  2. Battery anomaly detection     POST /api/anomaly         Isolation Forest, owner inputs only
                                   /api/history ...          the owner's saved readings (health reference)
  3. Charging-demand forecasting   GET  /api/demand          Random Forest, 7-day horizon, busy periods
  4. Adaptive dynamic pricing      GET  /api/pricing         documented rules, cost breakdown, comparison
  5. Station recommendation        GET  /api/recommendations Station finder (stations only)
                                   POST /api/checkins        driver check-in (live status, free)
                                   POST /api/plan            "Plan my charge": the whole workflow -
                                                             battery, station demand + price, recommendation
  6. Dashboard                     GET  /                    dashboard
                                   GET  /api/status

Run from the backend folder:  uvicorn app.main:app --reload   then open http://127.0.0.1:8000
"""
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import date as date_type
from pathlib import Path
from typing import List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import (adaptive_service, auth_service, osm_service, station_filter, tomtom_service, anomaly_service, checkin_service, demand_service, history_service, live_service,
                 ocpp_service, sim_network, station_demand_service, tariff_service)
from app.plan_service import PREFERENCES, plan as plan_calc, recommend_stations, station_demand_day
from app.ocm_service import LocationSearchError, OCMServiceError, get_nearby_stations, search_location
from app.pricing_service import pricing as pricing_calc
from app.rul_service import predict_rul

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"
MAX_STATIONS = 100          # Open Charge Map allows up to 100 per request (nearest first)
MODELS = BACKEND / "models"
DATA = BACKEND / "data"
load_dotenv(BACKEND / ".env")

@asynccontextmanager
async def lifespan(_app):
    # Load every model once, in one thread, before requests arrive. Loading scikit-learn models
    # from several request threads at the same time can deadlock Python's import system.
    for warm in (lambda: predict_rul("4W", 2021, 50000, 12000, readings=[(80, 220)]),
                 anomaly_service._bundle, demand_service.forecast, demand_service.busy_periods,
                 station_demand_service._m):
        try:
            warm()
        except Exception as exc:          # the status endpoint reports what failed
            print(f"Warm-up failed: {exc}")
    # demo account (DEMO_EMAIL / DEMO_PASSWORD in the environment): exists after every restart, so the same login
    # works on any device even where the database is wiped on restart (free hosting)
    if os.getenv("DEMO_EMAIL") and os.getenv("DEMO_PASSWORD"):
        acct = auth_service.ensure_account(os.getenv("DEMO_EMAIL"), os.getenv("DEMO_NAME") or "Demo", os.getenv("DEMO_PASSWORD"))
        print("Demo account ready." if acct else "Demo account not created: DEMO_EMAIL must be an email, DEMO_PASSWORD 8+ characters.")
    # every charger heartbeat (OCPP) is a live reading for adaptive learning
    ocpp_service.reset_connections()
    sim_network.start()              # built-in simulated charger network (SIMULATE_CHARGERS=0 in .env to switch off)
    # learn only from real chargers: the simulated feeds are generated from the demand model itself, so learning
    # from them would only teach the model what it already predicts (circular)
    adaptive_service.purge_simulated()
    ocpp_service.on_snapshot(lambda sid, live: None if (live or {}).get("simulated")
                             else adaptive_service.record(sid, "ocpp", live))
    yield


app = FastAPI(
    title="Smart EV Energy Management",
    description="Battery RUL, anomaly detection, demand forecasting, adaptive pricing and station recommendation.",
    version="3.0.0",
    lifespan=lifespan,
)


def _bad(exc):
    raise HTTPException(status_code=400, detail=str(exc))


# ------------------------------------------------------------------ shared vehicle input
class Reading(BaseModel):
    soc_pct: float = Field(..., ge=0, le=100, examples=[80])
    range_km: float = Field(..., gt=0, examples=[215])


class VehicleInput(BaseModel):
    vehicle_type: Literal["2W", "3W", "4W"] = Field(..., examples=["4W"])
    registration_year: int = Field(..., ge=2010, le=2035, examples=[2021])
    odometer_km: float = Field(..., gt=0, examples=[60000])
    km_per_year: float = Field(..., gt=0, examples=[12000])
    readings: List[Reading] = Field(..., min_length=1, max_length=3)
    vehicle_name: Optional[str] = Field(None, max_length=120, examples=["Tata Nexon EV"])
    vehicle_id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
                                      description="Random id from the owner's browser; links readings over time")
    save: bool = Field(True, description="Save today's readings to this vehicle's reading history")


class Trip(BaseModel):
    soc_start: float = Field(..., ge=0, le=100, description="Battery % at the start of the ride")
    range_start_km: float = Field(..., gt=0, description="Range shown at the start of the ride")
    soc_end: float = Field(..., ge=0, le=100, description="Battery % at the end of the ride")
    distance_km: float = Field(..., gt=0, le=2000, description="Km ridden (trip meter)")
    ride_mode: Optional[str] = Field(None, max_length=30)
    no_charging: bool = Field(True, description="Owner confirms the vehicle was not charged during the ride")


class AnomalyInput(VehicleInput):
    trip: Optional[Trip] = None
    new_full_range_km: Optional[float] = Field(None, gt=0, examples=[312],
                                               description="Optional: full-charge range shown when new")


def _vehicle_title(v):
    return v.vehicle_name or {"2W": "2-wheeler", "3W": "3-wheeler", "4W": "Car"}[v.vehicle_type]


# ------------------------------------------------------------------ 1. RUL
@app.post("/api/rul", tags=["1. Battery RUL"])
def rul(req: VehicleInput):
    try:
        r = predict_rul(vehicle_type=req.vehicle_type, registration_year=req.registration_year,
                        odometer_km=req.odometer_km, km_per_year=req.km_per_year,
                        readings=[(x.soc_pct, x.range_km) for x in req.readings], vehicle_name=req.vehicle_name)
    except ValueError as exc:
        _bad(exc)
    if req.save:
        history_service.add(req.vehicle_id, [(x.soc_pct, x.range_km) for x in req.readings], req.odometer_km, source="rul")
    return r


# ------------------------------------------------------------------ 2. anomaly
@app.post("/api/anomaly", tags=["2. Battery anomaly"])
def anomaly(req: AnomalyInput):
    try:
        r = anomaly_service.detect(vehicle_type=req.vehicle_type, registration_year=req.registration_year,
                                   odometer_km=req.odometer_km, km_per_year=req.km_per_year,
                                   readings=[(x.soc_pct, x.range_km) for x in req.readings],
                                   new_full_range_km=req.new_full_range_km, vehicle_name=req.vehicle_name,
                                   history=history_service.get(req.vehicle_id),
                                   trip=req.trip.model_dump() if req.trip else None,
                                   trip_history=history_service.get_trips(req.vehicle_id))
    except ValueError as exc:
        _bad(exc)
    if req.trip and not req.trip.no_charging:
        _bad("Trip: the check is only valid if the vehicle was not charged during the ride.")
    if req.save and r.get("trip") and req.trip:
        history_service.add_trip(req.vehicle_id, {**req.trip.model_dump(), **{k: r["trip"][k] for k in
                                                  ("promised_km", "delivered_km", "gap_pct")}})
    if req.save:
        history_service.add(req.vehicle_id, [(x.soc_pct, x.range_km) for x in req.readings], req.odometer_km,
                            source="anomaly")
    r["history"] = history_service.get(req.vehicle_id)
    r["trips"] = history_service.get_trips(req.vehicle_id)
    return r


@app.get("/api/trips/{vehicle_id}", tags=["2. Battery anomaly"])
def trips(vehicle_id: str):
    return {"trips": history_service.get_trips(vehicle_id)}


@app.delete("/api/trips/{vehicle_id}", tags=["2. Battery anomaly"])
def clear_trips(vehicle_id: str, trip_id: Optional[int] = None):
    return {"deleted": history_service.delete_trips(vehicle_id, trip_id)}


class EarlierReading(BaseModel):
    vehicle_id: str = Field(..., max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    reading_date: date_type
    odometer_km: float = Field(..., gt=0)
    readings: List[Reading] = Field(..., min_length=1, max_length=3)


@app.get("/api/history/{vehicle_id}", tags=["2. Battery anomaly"])
def history(vehicle_id: str):
    return {"readings": history_service.get(vehicle_id)}


@app.post("/api/history", tags=["2. Battery anomaly"])
def add_history(r: EarlierReading):
    """Add an earlier reading the owner noted (e.g. from an old dashboard photo) as a health reference."""
    if r.reading_date > date_type.today():
        _bad("The reading date cannot be in the future.")
    for i, x in enumerate(r.readings, 1):
        if not 20 <= x.soc_pct <= 100:
            _bad(f"Reading {i}: battery % (SOC) must be between 20 and 100.")
    n = history_service.add(r.vehicle_id, [(x.soc_pct, x.range_km) for x in r.readings], r.odometer_km,
                            reading_date=r.reading_date, source="earlier")
    return {"added": n, "readings": history_service.get(r.vehicle_id)}


@app.delete("/api/history/{vehicle_id}", tags=["2. Battery anomaly"])
def clear_history(vehicle_id: str, reading_id: Optional[int] = None):
    return {"deleted": history_service.delete(vehicle_id, reading_id)}


@app.get("/api/anomaly/examples", tags=["2. Battery anomaly"])
def anomaly_examples():
    return {"examples": anomaly_service.samples(), "model": anomaly_service.info()}


# ------------------------------------------------------------------ 3. demand
@app.get("/api/demand", tags=["3. Demand forecast"])
def demand(history_days: int = Query(90, ge=14, le=365)):
    try:
        return demand_service.summary(history_days)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Demand forecast failed: {exc}")


# ------------------------------------------------------------------ 4. pricing
@app.get("/api/pricing", tags=["4. Pricing"])
def pricing(hour: int = Query(18, ge=0, le=23),
            base_price: float = Query(10.0, gt=0, le=200),
            gst_pct: float = Query(18.0, ge=0, le=50),
            date: Optional[str] = Query(None, description="Forecast date (YYYY-MM-DD); default next day"),
            demand_level: Optional[Literal["low", "medium", "high"]] = None,
            energy_kwh: Optional[float] = Query(None, gt=0, le=200)):
    try:
        return pricing_calc(hour, base_price, gst_pct, date, demand_level, energy_kwh)
    except ValueError as exc:
        _bad(exc)


# ------------------------------------------------------------------ 5. stations
_OCM_CACHE = {}
OCM_CACHE_S = 300           # station details change rarely; reuse them for 5 minutes (live data is not cached here)


OSM_WAIT_S = 3.0


def _raw_stations(latitude, longitude, distance_km, max_results):
    key = (round(latitude, 4), round(longitude, 4), float(distance_km), max_results)
    hit = _OCM_CACHE.get(key)
    if hit and time.time() - hit[0] < OCM_CACHE_S:
        _log("stations", lat=round(latitude, 5), lon=round(longitude, 5), radius_km=distance_km, cached=True,
             stations=len(hit[1]), ids=[str(x.get("ID")) for x in hit[1]][:12])
        return hit[1]
    # two free station sources, asked at the same time: Open Charge Map + OpenStreetMap
    # (duplicates within 120 m count once)
    # OpenStreetMap (Overpass) is often slow (5-10 s, sometimes far more) and adds only a few stations, so the search
    # waits for it at most OSM_WAIT_S; a late answer is still cached for the next search of this area.
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        t0 = time.time()
        f_ocm = pool.submit(get_nearby_stations, latitude=latitude, longitude=longitude,
                            distance_km=distance_km, max_results=max_results)
        f_osm = pool.submit(osm_service.fetch, latitude, longitude, distance_km)
        f_tt = pool.submit(tomtom_service.fetch, latitude, longitude, distance_km)
        ocm_error = None
        try:
            data = f_ocm.result()
        except ValueError as exc:
            _bad(exc)
        except OCMServiceError as exc:
            data, ocm_error = [], str(exc)
        tt, tt_error = f_tt.result()
        try:
            osm, osm_error = f_osm.result(timeout=max(0.2, OSM_WAIT_S - (time.time() - t0)))
        except FutureTimeout:
            osm, osm_error = [], None                 # still loading - used from its cache next time
    finally:
        pool.shutdown(wait=False)
    _log("stations", tomtom=len(tt), tomtom_error=tt_error, lat=round(latitude, 5), lon=round(longitude, 5), radius_km=distance_km,
         open_charge_map=len(data), open_charge_map_error=ocm_error, openstreetmap=len(osm),
         openstreetmap_error=osm_error, openstreetmap_status=osm_service.status(),
         ids=[str(x.get("ID")) for x in data][:12])
    if ocm_error and not osm and not tt:
        raise HTTPException(status_code=502, detail=ocm_error + (f" {osm_error}." if osm_error else ""))
    data = osm_service.merge(osm_service.merge(data, tt), osm)      # duplicates within 120 m count once
    shown, hidden = station_filter.keep_public(data)     # drop residential / private / unclear entries
    shown = StationList(shown)
    shown.hidden = [{"name": (h.get("AddressInfo") or {}).get("Title"), "class": h["_access"], "why": h["_access_note"]}
                    for h in hidden]
    _log("filter", shown=len(shown), hidden=shown.hidden[:20])
    _OCM_CACHE[key] = (time.time(), shown)
    return shown


SEARCH_LOG = BACKEND / "data" / "search_log.jsonl"


def _log(event, **kv):
    """One line per search step - where a place was resolved to and what each station source returned -
    so a wrong or empty result can be traced. Kept small (the oldest half is dropped above 1 MB)."""
    try:
        line = json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **kv}, ensure_ascii=False)
        if SEARCH_LOG.exists() and SEARCH_LOG.stat().st_size > 1_000_000:
            keep = SEARCH_LOG.read_text(encoding="utf-8").splitlines()[-2000:]
            SEARCH_LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
        with SEARCH_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class StationList(list):
    """Stations from the sources, with the entries that were left out (and why)."""
    hidden = ()


def _with_sources(result, raw=None):
    """Add the station sources' status (OpenStreetMap reachable or not, cached or fresh) and the stations left out
    (residential / private / unclear) to a result."""
    result["station_sources"] = {"open_charge_map": "ok", "openstreetmap": osm_service.status(),
                                 "tomtom": tomtom_service.status(),
                                 "hidden": list(getattr(raw, "hidden", []) or [])}
    return result


def _resolve_location(q, latitude, longitude):
    if latitude is not None and longitude is not None:
        _log("location", typed=q, resolved="coordinates from the browser", lat=latitude, lon=longitude)
        return latitude, longitude, q or "Your location"
    if q:
        try:
            loc = search_location(q)
        except ValueError as exc:
            _bad(exc)
        except LocationSearchError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        label = loc.get("location_name") or q
        _log("location", typed=q, resolved=label, lat=loc["latitude"], lon=loc["longitude"], note=loc.get("note"))
        if loc.get("note"):                      # spelling corrected / area not found - say so
            label += f" ({loc['note']})"
        return loc["latitude"], loc["longitude"], label
    _bad("Enter a location or use your current location.")


@app.get("/api/location", tags=["5. Stations"])
def location(q: str = Query(..., min_length=2, max_length=200, examples=["Madhapur, Hyderabad"])):
    lat, lon, label = _resolve_location(q, None, None)
    return {"location_name": label, "latitude": lat, "longitude": lon}


@app.get("/api/recommendations", tags=["5. Stations"])
def recommendations(q: Optional[str] = Query(None, max_length=200),
                    latitude: Optional[float] = Query(None, ge=-90, le=90),
                    longitude: Optional[float] = Query(None, ge=-180, le=180),
                    vehicle_type: Literal["2W", "3W", "4W"] = "4W",
                    connector: Optional[str] = Query(None, max_length=60),
                    hour: int = Query(18, ge=0, le=23),
                    date: Optional[str] = None,
                    preference: Literal["balanced", "cheapest", "nearest", "fastest"] = "balanced",
                    scenario: Literal["expected", "busy", "latest"] = "expected",
                    distance_km: float = Query(15, ge=1, le=100),
                    base_price: float = Query(10.0, gt=0, le=200),
                    gst_pct: float = Query(18.0, ge=0, le=50)):
    """Station finder: nearby stations with per-station demand, waiting time and price, ranked for your
    preference (no battery analysis - use /api/plan for the full workflow)."""
    lat, lon, label = _resolve_location(q, latitude, longitude)
    raw = _raw_stations(lat, lon, distance_km, MAX_STATIONS)
    places, err = live_service.fetch(lat, lon, distance_km)
    return _with_sources(recommend_stations(raw, lat, lon, label, vehicle_type, connector, hour, date, preference, scenario,
                              base_price, gst_pct, places, err), raw)


@app.get("/api/demand/stations", tags=["3. Demand forecast"])
def demand_at_stations(q: Optional[str] = Query(None, max_length=200),
                       latitude: Optional[float] = Query(None, ge=-90, le=90),
                       longitude: Optional[float] = Query(None, ge=-180, le=180),
                       vehicle_type: Literal["2W", "3W", "4W"] = "4W",
                       date: Optional[str] = None,
                       scenario: Literal["expected", "busy", "latest"] = "expected",
                       distance_km: float = Query(10, ge=1, le=100)):
    """Demand at each nearby station, hour by hour for the chosen day (live where available)."""
    lat, lon, label = _resolve_location(q, latitude, longitude)
    raw = _raw_stations(lat, lon, distance_km, MAX_STATIONS)
    places, err = live_service.fetch(lat, lon, distance_km)
    return _with_sources(station_demand_day(raw, lat, lon, label, vehicle_type, date, scenario, places, err), raw)


@app.get("/api/pricing/stations", tags=["4. Pricing"])
def pricing_at_stations(q: Optional[str] = Query(None, max_length=200),
                        latitude: Optional[float] = Query(None, ge=-90, le=90),
                        longitude: Optional[float] = Query(None, ge=-180, le=180),
                        vehicle_type: Literal["2W", "3W", "4W"] = "4W",
                        date: Optional[str] = None,
                        hour: Optional[int] = Query(None, ge=0, le=23),
                        base_price: float = Query(10.0, gt=0, le=200),
                        gst_pct: float = Query(18.0, ge=0, le=50),
                        scenario: Literal["expected", "busy", "latest"] = "expected",
                        distance_km: float = Query(10, ge=1, le=100)):
    """Price per kWh at each nearby station, from its own availability / demand (live where available)."""
    lat, lon, label = _resolve_location(q, latitude, longitude)
    raw = _raw_stations(lat, lon, distance_km, MAX_STATIONS)
    places, err = live_service.fetch(lat, lon, distance_km)
    return _with_sources(station_demand_day(raw, lat, lon, label, vehicle_type, date, scenario, places, err, hour, base_price,
                              gst_pct), raw)


class CheckinIn(BaseModel):
    station_id: str = Field(..., max_length=80)
    station_name: Optional[str] = Field(None, max_length=200)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    status: Literal["free", "busy", "queue", "not_working"]
    free_points: Optional[int] = Field(None, ge=0, le=100)
    total_points: Optional[int] = Field(None, ge=1, le=100)
    queue: Optional[int] = Field(None, ge=1, le=50)
    price_per_kwh: Optional[float] = Field(None, ge=1, le=100, description="Price paid, Rs per kWh incl. GST")


@app.post("/api/checkins", tags=["5. Stations"])
def add_checkin(c: CheckinIn):
    """Driver check-in: live status reported at the station (free / busy / queue / not working)."""
    try:
        cid = checkin_service.add(c.station_id, c.status, c.station_name, c.latitude, c.longitude, c.free_points,
                                  c.total_points, c.queue)
        if c.price_per_kwh is not None:
            tariff_service.add(c.station_id, c.price_per_kwh, "driver_report")
    except ValueError as exc:
        _bad(exc)
    summary = checkin_service.recent([c.station_id]).get(c.station_id)
    if summary:                      # a driver report is also a live reading for adaptive learning
        pts = c.total_points
        if pts is None:
            meta = {m["station_id"]: m for m in adaptive_service.summary_meta([c.station_id])}
            pts = (meta.get(str(c.station_id)) or {}).get("points")
        adaptive_service.record(c.station_id, "driver_report", checkin_service.to_live(summary, pts))
    return {"id": cid, "summary": summary}


# ------------------------------------------------------------------ real-time charger feed (OCPP 1.6-J)
@app.websocket("/ocpp/{cp_id}")
async def ocpp_socket(ws: WebSocket, cp_id: str):
    """Charge points connect here (OCPP 1.6-J). Id OCM-<station id> links the charger to that station."""
    if not ocpp_service.valid_id(cp_id):
        await ws.close(code=1008)
        return
    offered = [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    await ws.accept(subprotocol="ocpp1.6" if "ocpp1.6" in offered else None)
    ocpp_service.set_connected(cp_id, True)
    try:
        while True:
            reply = ocpp_service.handle(cp_id, await ws.receive_text())
            if reply:
                await ws.send_text(reply)
    except WebSocketDisconnect:
        pass
    finally:
        ocpp_service.set_connected(cp_id, False)


@app.get("/api/ocpp/status", tags=["Real time"])
def ocpp_status():
    """Chargers connected over OCPP and their last messages."""
    return {**ocpp_service.status(), "built_in_simulation": sim_network.status()}


@app.get("/api/ocpp/candidates", tags=["Real time"])
def ocpp_candidates(q: Optional[str] = Query(None, max_length=200),
                    latitude: Optional[float] = Query(None, ge=-90, le=90),
                    longitude: Optional[float] = Query(None, ge=-180, le=180),
                    distance_km: float = Query(10, ge=1, le=100),
                    limit: int = Query(5, ge=1, le=30)):
    """Nearby Open Charge Map stations for the charger simulator: points, power and the model's arrival rates."""
    from datetime import datetime as _dt
    lat, lon, label = _resolve_location(q, latitude, longitude)
    raw = _raw_stations(lat, lon, distance_km, MAX_STATIONS)
    from app.recommendation_engine import build_station_features
    feats = sorted(build_station_features(raw, lat, lon), key=lambda s: s["distance_km"])[:limit]
    adaptive_service.remember_stations(feats)
    wd = _dt.now().weekday()
    return {"location": label, "weekday": wd, "stations": [
        {"id": s["id"], "name": s["name"], "distance_km": s["distance_km"],
         "points": s["number_of_points"] or 2, "points_assumed": not s["number_of_points"],
         "power_kw": s["max_power_kw"], **station_demand_service.arrival_profile(s["max_power_kw"], wd),
         "listed_tariff": s["observed_price_inr"] if s.get("price_comparable") else None}
        for s in feats]}


@app.get("/api/adaptive", tags=["Real time"])
def adaptive_status():
    """Adaptive learning: live readings stored, what each station has learned, and the error before / after."""
    out = adaptive_service.summary()
    v = BACKEND / "models" / "metrics_adaptive.json"
    out["validation"] = json.loads(v.read_text()) if v.exists() else None
    out["chargers"] = {**{k: ocpp_service.status()[k] for k in ("connected", "known")},
                       "simulated": sim_network.status()["simulated_stations"]}
    return out


@app.delete("/api/adaptive", tags=["Real time"])
def adaptive_reset():
    """Forget all stored live readings (start learning again)."""
    return {"deleted": adaptive_service.reset()}


# ------------------------------------------------------------------ plan my charge (whole workflow)
class PlanInput(VehicleInput):
    location: Optional[str] = Field(None, max_length=200, examples=["Madhapur, Hyderabad"])
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    hour: int = Field(18, ge=0, le=23)
    date: Optional[str] = None
    preference: Literal["balanced", "cheapest", "nearest", "fastest"] = "balanced"
    connector: Optional[str] = Field(None, max_length=60)
    scenario: Literal["expected", "busy", "latest"] = "expected"
    distance_km: float = Field(15, ge=1, le=100)
    base_price: float = Field(10.0, gt=0, le=200)
    gst_pct: float = Field(18.0, ge=0, le=50)
    new_full_range_km: Optional[float] = Field(None, gt=0)


@app.post("/api/plan", tags=["Plan my charge"])
def plan(req: PlanInput):
    """Runs the whole workflow: validate -> battery -> demand -> stations -> price -> recommendation -> output."""
    lat, lon, label = _resolve_location(req.location, req.latitude, req.longitude)
    vehicle = req.model_dump(include={"vehicle_type", "registration_year", "odometer_km", "km_per_year",
                                      "readings", "vehicle_name"})
    raw = _raw_stations(lat, lon, req.distance_km, MAX_STATIONS)
    places, err = live_service.fetch(lat, lon, req.distance_km)
    try:
        r = plan_calc(vehicle, raw, lat, lon, label, req.hour, req.date, req.preference, req.connector,
                      req.scenario, req.base_price, req.gst_pct, req.new_full_range_km,
                      history=history_service.get(req.vehicle_id),
                      trip_history=history_service.get_trips(req.vehicle_id), live_places=places, live_error=err)
    except ValueError as exc:
        _bad(exc)
    if req.save:
        history_service.add(req.vehicle_id, [(x.soc_pct, x.range_km) for x in req.readings], req.odometer_km,
                            source="plan")
    return r


@app.get("/api/plan/options", tags=["Plan my charge"])
def plan_options():
    return {"preferences": {k: {"label": v["label"], "weights_pct": {c: w * 100 for c, w in v["weights"].items()}}
                            for k, v in PREFERENCES.items()},
            "scenarios": station_demand_service.SCENARIOS, "forecast_days": demand_service.forecast(),
            "station_model": station_demand_service.info()}


# ------------------------------------------------------------------ status / info
def _metrics(name):
    f = MODELS / f"{name}.json"
    return json.loads(f.read_text()) if f.exists() else None


@app.get("/api/status", tags=["Info"])
def status():
    def check(fn):
        try:
            fn()
            return "ready"
        except Exception as exc:
            return f"error: {exc}"

    rul_m, an_m, dm_m = (_metrics(k) for k in ("metrics_rul", "metrics_anomaly", "metrics_demand"))
    return {
        "api": "ok",
        "tomtom_key": "configured" if tomtom_service.api_key() else "not set (optional: TOMTOM_API_KEY in backend/.env)",
        "open_charge_map_key": "configured" if os.getenv("OCM_API_KEY") else "missing (add OCM_API_KEY to backend/.env)",
        "google_live_key": live_service.status(),
        "models": [
            {"feature": "Battery RUL (vehicles)", "model": "Calendar + cycle ageing law",
             "state": check(lambda: predict_rul("4W", 2021, 50000, 12000, readings=[(80, 220)])),
             "metric": (f"Health MAE {rul_m['soh_accuracy_real_cars_5fold_cv']['ageing_model']['mae']} pts (real cars)"
                        if rul_m else None)},
            {"feature": "Anomaly detection", "model": "Isolation Forest",
             "state": check(anomaly_service._bundle),
             "metric": f"False alarms {an_m['false_alarm_pct']}%" if an_m else None},
            {"feature": "Demand forecast", "model": "Random Forest",
             "state": check(demand_service.forecast),
             "metric": f"MAE {dm_m['random_forest']['mae_sessions']} sessions/day, R² {dm_m['random_forest']['r2']}"
             if dm_m else None},
            {"feature": "Station demand & waiting time", "model": "Queue model (Erlang C) on learned arrival rates",
             "state": check(station_demand_service._m),
             "metric": (lambda m: f"Busy points MAE {m['busy_points_mae']} (average {m['busy_points_mae_baseline_average']})"
                        if m else None)(_metrics("metrics_station_demand"))},
            {"feature": "Adaptive pricing", "model": "Documented rules", "state": "ready", "metric": None},
            {"feature": "Station recommendation (Plan my charge)", "model": "Weighted multi-criteria", "state": "ready",
             "metric": None},
        ],
    }


@app.get("/api/health", tags=["Info"])
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ dashboard
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND / "index.html")


# ------------------------------------------------------------------ user accounts
PUBLIC_API = ("/api/health", "/api/status", "/api/auth/login", "/api/auth/register", "/api/auth/logout")


@app.middleware("http")
async def require_login(request, call_next):
    """Every /api/ request needs a logged-in user, except login itself, health/status and the charger side
    (/api/ocpp/...). The user is kept for this request so data is stored and read per user."""
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_API or path.startswith("/api/ocpp/"):
        return await call_next(request)
    user = auth_service.session_user(request.cookies.get(auth_service.COOKIE))
    if not user:
        return JSONResponse({"detail": "Please log in."}, status_code=401)
    request.state.user = user
    token = auth_service.current_user.set(user)
    try:
        return await call_next(request)
    finally:
        auth_service.current_user.reset(token)


class RegisterIn(BaseModel):
    email: str = Field(..., max_length=254)
    name: str = Field(..., max_length=60)
    password: str = Field(..., max_length=200)
    legacy_vehicle_id: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class LoginIn(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=200)


def _set_session(request, response, token):
    response.set_cookie(auth_service.COOKIE, token, max_age=auth_service.SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")


@app.post("/api/auth/register", tags=["Account"])
def register(body: RegisterIn, request: Request, response: Response):
    """Create an account. Readings saved in this browser before (legacy_vehicle_id) move to the new account."""
    try:
        user = auth_service.register(body.email, body.name, body.password)
    except auth_service.AuthError as exc:
        _bad(exc)
    moved = history_service.reassign(body.legacy_vehicle_id, user["vehicle_id"])
    _set_session(request, response, auth_service.new_session(user["id"]))
    return {"user": user, "moved_readings": moved}


@app.post("/api/auth/login", tags=["Account"])
def login(body: LoginIn, request: Request, response: Response):
    try:
        user, token = auth_service.login(body.email, body.password)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    _set_session(request, response, token)
    return {"user": user}


@app.post("/api/auth/logout", tags=["Account"])
def logout(request: Request, response: Response):
    auth_service.logout(request.cookies.get(auth_service.COOKIE))
    response.delete_cookie(auth_service.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me", tags=["Account"])
def me(request: Request):
    return {"user": request.state.user}


class PrefsIn(BaseModel):
    preferences: dict


@app.put("/api/auth/preferences", tags=["Account"])
def preferences(body: PrefsIn, request: Request):
    """Save the user's settings: theme (system / light / dark), vehicle profile, live updates."""
    try:
        return {"preferences": auth_service.set_preferences(request.state.user["id"], body.preferences)}
    except auth_service.AuthError as exc:
        _bad(exc)


@app.middleware("http")
async def no_cache_for_dashboard(request, call_next):
    """The dashboard files change while the project is being developed; tell the browser not to keep an old copy,
    so a normal refresh always shows the latest page."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
