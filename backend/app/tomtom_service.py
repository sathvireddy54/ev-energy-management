"""
TomTom charging stations - a third station source (free developer key, no card).

Open Charge Map and OpenStreetMap are volunteer data and list only a few stations in many Indian cities. TomTom
maintains a commercial map of India, including EV charging stations (POI category 7309) with each connector's
type and power, built from its own mapping, charging-network partners and licensed data.

Search API, Nearby Search:
    GET https://api.tomtom.com/search/2/nearbySearch/.json?lat=..&lon=..&radius=<m>&categorySet=7309&limit=100&ofs=..
Results are converted to the same shape as an Open Charge Map station; a TomTom station within 120 m of one already
listed counts as the same station (osm_service.merge). TomTom returns at most the 100 stations nearest the
searched point, so each place is searched around itself (cached 6 hours per place); a cached result is reused for a
nearby search only if it held fewer than 100 stations, i.e. everything in its circle. The free daily allowance
(2,500 requests) is plenty.
Key: TOMTOM_API_KEY in backend/.env. Without a key this source is skipped.
TomTom has station details, not live availability or prices for India.
"""
import math
import os
import threading
import time

import requests

URL = "https://api.tomtom.com/search/2/nearbySearch/.json"
CATEGORY_EV = 7309
PAGE, MAX_PAGES = 100, 8
AREA_KM, AREA_TTL_S, FAIL_TTL_S = 25, 6 * 3600, 180
TIMEOUT = (5, 15)
_areas, _lock = [], threading.Lock()
_fail = {"until": 0.0, "error": None}
_last = {"ok": None, "error": None, "stations": 0, "from_cache": False, "seconds": None, "configured": False}

# TomTom connector types -> connector names used in this app (as on Open Charge Map, so compatibility rules apply)
CONNECTORS = {
    "StandardHouseholdCountrySpecific": "Domestic plug/socket type D", "IEC62196Type1": "Type 1 (J1772)",
    "IEC62196Type1CCS": "CCS (Type 1)", "IEC62196Type2CableAttached": "Type 2 (Tethered Connector)",
    "IEC62196Type2Outlet": "Type 2 (Socket Only)", "IEC62196Type2CCS": "CCS (Type 2)",
    "IEC62196Type3": "Type 3C (Scame)", "Chademo": "CHAdeMO", "GBT20234Part2": "GB-T AC",
    "GBT20234Part3": "GB-T DC - GB/T 20234.3", "IEC60309AC1PhaseBlue": "IEC 60309 3-pin",
    "IEC60309AC3PhaseRed": "IEC 60309 5-pin", "IEC60309DCWhite": "IEC 60309 DC", "Tesla": "Tesla (Model S/X)",
}


def api_key():
    return (os.getenv("TOMTOM_API_KEY") or "").strip()


def _dist_m(a_lat, a_lon, b_lat, b_lon):
    r = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lon - a_lon) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def to_ocm(r):
    """One TomTom result -> a station dict shaped like Open Charge Map's."""
    pos, poi, adr = r.get("position") or {}, r.get("poi") or {}, r.get("address") or {}
    if pos.get("lat") is None or pos.get("lon") is None:
        return None
    conns = []
    for c in (r.get("chargingPark") or {}).get("connectors") or []:
        t = c.get("connectorType") or ""
        conns.append({"ConnectionType": {"Title": CONNECTORS.get(t, t or "Unknown")}, "Quantity": 1,
                      "PowerKW": c.get("ratedPowerKW")})
    brand = ((poi.get("brands") or [{}])[0] or {}).get("name")
    return {
        "ID": f"tt-{r.get('id')}",
        "_source": "TomTom",
        "AddressInfo": {"Title": poi.get("name") or brand or "EV charging station", "Latitude": pos["lat"],
                        "Longitude": pos["lon"], "AddressLine1": adr.get("streetName") or adr.get("freeformAddress"),
                        "Town": adr.get("municipalitySubdivision") or adr.get("municipality"),
                        "StateOrProvince": adr.get("countrySubdivision")},
        "Connections": conns,
        "NumberOfPoints": len(conns) or None,
        "UsageCost": None,
        "StatusType": {"Title": "Operational"},
        "OperatorInfo": {"Title": brand},
        "private": False,
    }


def _query(latitude, longitude, radius_km, key):
    out, ofs = [], 0
    for _ in range(MAX_PAGES):
        resp = requests.get(URL, params={"key": key, "lat": latitude, "lon": longitude,
                                         "radius": int(min(radius_km, 50) * 1000), "categorySet": CATEGORY_EV,
                                         "limit": PAGE, "ofs": ofs, "language": "en-GB"}, timeout=TIMEOUT)
        if resp.status_code != 200:
            hint = {400: "request not accepted", 401: "key not valid", 403: "key not valid or Search API not enabled",
                    429: "daily free limit reached"}.get(resp.status_code, "")
            raise RuntimeError(f"HTTP {resp.status_code}" + (f" – {hint}" if hint else ""))
        data = resp.json()
        res = data.get("results") or []
        out += [s for s in (to_ocm(x) for x in res) if s]
        total = (data.get("summary") or {}).get("totalResults") or 0
        ofs += PAGE
        if len(res) < PAGE or ofs >= min(total, 2000):
            break
    return out


def fetch(latitude, longitude, radius_km):
    """-> (stations within radius_km shaped like Open Charge Map's, error or None). No key: ([], None)."""
    key = api_key()
    _last["configured"] = bool(key)
    if not key:
        return [], None
    now = time.time()
    with _lock:
        for la, lo, R, t, sts, complete in _areas:
            if now - t >= AREA_TTL_S:
                continue
            same_query = _dist_m(latitude, longitude, la, lo) < 50 and abs(R - radius_km) < 1e-6
            covered = complete and _dist_m(latitude, longitude, la, lo) / 1000 + radius_km <= R
            if same_query or covered:
                out = [s for s in sts if _dist_m(latitude, longitude, s["AddressInfo"]["Latitude"],
                                                  s["AddressInfo"]["Longitude"]) <= radius_km * 1000]
                _last.update(ok=True, error=None, stations=len(out), from_cache=True, seconds=0.0)
                return out, None
        if now < _fail["until"]:
            return [], _fail["error"]
    R = min(radius_km, 50)                      # around the searched place itself
    t0 = time.time()
    try:
        sts = _query(latitude, longitude, R, key)
    except Exception as exc:
        err = "TomTom: " + (str(exc) if isinstance(exc, RuntimeError) else f"not reachable ({exc.__class__.__name__})")
        with _lock:
            _fail.update(until=time.time() + FAIL_TTL_S, error=err)
            _last.update(ok=False, error=err, stations=0, from_cache=False, seconds=round(time.time() - t0, 1))
        return [], err
    with _lock:
        _fail.update(until=0.0, error=None)
        _areas.append((latitude, longitude, R, now, sts, len(sts) < PAGE))   # < 100 results = complete circle
        del _areas[:-20]
        out = [s for s in sts if _dist_m(latitude, longitude, s["AddressInfo"]["Latitude"],
                                          s["AddressInfo"]["Longitude"]) <= radius_km * 1000]
        _last.update(ok=True, error=None, stations=len(out), from_cache=False, seconds=round(time.time() - t0, 1))
    return out, None


def status():
    with _lock:
        return {**_last, "configured": bool(api_key()), "areas_cached": len(_areas),
                "retry_in_s": max(0, round(_fail["until"] - time.time()))}
