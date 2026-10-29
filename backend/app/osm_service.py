"""
OpenStreetMap charging stations - a second, free station source (no API key).

Open Charge Map is volunteer data and lists only a handful of stations in many Indian cities, so the same few
stations come back wherever you search. OpenStreetMap maps EV charging stations too (amenity=charging_station),
queried through the public Overpass API. Each OSM station is converted into the same shape as an Open Charge Map
station, so demand, the charger feed, tariffs, pricing and ranking all treat it the same way.

OSM tags used: name / brand / operator / network, capacity (vehicles at once), socket:<type>=<count>,
socket:<type>:output (kW), charge (price), addr:*.  Stations within 120 m of an Open Charge Map station are
treated as the same station (the Open Charge Map entry is kept).
Results are cached for 10 minutes. If Overpass cannot be reached, searches still work with Open Charge Map only.
"""
import math
import re
import threading
import time

import requests

ENDPOINTS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
             "https://overpass.private.coffee/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "EV-Energy-Management-Project/1.0 (student project)"}
CACHE_S = 600
DUPLICATE_M = 120
_cache, _lock = {}, threading.Lock()

# OSM socket keys -> connector names used in this app (as on Open Charge Map, so compatibility rules apply)
SOCKETS = {
    "type2": "Type 2 (Socket Only)", "type2_cable": "Type 2 (Tethered Connector)", "type2_combo": "CCS (Type 2)",
    "chademo": "CHAdeMO", "type1": "Type 1 (J1772)", "type1_combo": "CCS (Type 1)", "gb_t": "GB-T AC",
    "gb_dc": "GB-T DC - GB/T 20234.3", "schuko": "Europlug 2-Pin (CEE 7/16)", "typee": "Type E (French)",
    "bs1363": "BS1363 3 Pin 13 Amp", "cee_blue": "IEC 60309 3-pin", "cee_red_16a": "IEC 60309 5-pin",
    "type3c": "Type 3C (Scame)", "tesla_supercharger": "Tesla Supercharger", "nacs": "NACS / Tesla Supercharger",
    "as3112": "Type I (AS 3112)", "typed": "Domestic plug/socket type D", "type_d": "Domestic plug/socket type D",
    "indian": "Domestic plug/socket type D", "bharat_ac001": "Bharat AC-001", "bharat_dc001": "Bharat DC-001",
    "lecct": "LECCT (light EV)", "leccs": "LECCS (light EV)",
}


def _dist_m(a_lat, a_lon, b_lat, b_lon):
    r = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _kw(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kw|w)?", str(text or "").lower())
    if not m:
        return None
    v = float(m.group(1))
    return v / 1000 if m.group(2) == "w" else v


def _count(text):
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else None


def to_ocm(el):
    """One Overpass element -> a station dict shaped like Open Charge Map's."""
    t = el.get("tags") or {}
    lat = el.get("lat", (el.get("center") or {}).get("lat"))
    lon = el.get("lon", (el.get("center") or {}).get("lon"))
    if lat is None or lon is None:
        return None
    conns = []
    for key, val in t.items():
        if not key.startswith("socket:") or key.count(":") != 1:
            continue
        kind = key.split(":", 1)[1]
        if str(val).lower() in ("no", "0"):
            continue
        title = SOCKETS.get(kind) or ("Bharat AC-001" if "bharat" in kind and "ac" in kind else
                                      "Bharat DC-001" if "bharat" in kind else kind.replace("_", " ").title())
        conns.append({"ConnectionType": {"Title": title}, "Quantity": _count(val) or 1,
                      "PowerKW": _kw(t.get(f"socket:{kind}:output"))})
    points = _count(t.get("capacity")) or sum(c["Quantity"] for c in conns) or None
    top_kw = _kw(t.get("maxpower") or t.get("output"))
    if top_kw and conns and not any(c["PowerKW"] for c in conns):
        conns[0]["PowerKW"] = top_kw
    name = t.get("name") or t.get("brand") or t.get("operator") or t.get("network") or "EV charging station"
    disused = any(k.startswith(("disused", "abandoned")) for k in t) or t.get("access") in ("private", "no")
    charge = t.get("charge") or t.get("fee:amount")
    return {
        "ID": f"osm-{el['type'][0]}{el['id']}",
        "_source": "OpenStreetMap",
        "AddressInfo": {"Title": name, "Latitude": lat, "Longitude": lon,
                        "AddressLine1": " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x) or None,
                        "Town": t.get("addr:city") or t.get("addr:suburb"), "StateOrProvince": t.get("addr:state")},
        "Connections": conns,
        "NumberOfPoints": points,
        "UsageCost": charge or None,
        "StatusType": {"Title": "Not Operational" if disused else "Operational"},
        "OperatorInfo": {"Title": t.get("operator") or t.get("network") or t.get("brand")},
        "osm_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        "private": disused,
    }


AREA_KM = 25                 # one query covers ~25 km around the first search; later searches are filtered locally
AREA_TTL_S = 6 * 3600
FAIL_TTL_S = 180             # after a failure, skip OpenStreetMap for 3 minutes instead of waiting every search
TIMEOUT = (5, 20)            # seconds to connect / to receive
_areas = []                  # (lat, lon, radius_km, fetched_at, stations)
_fail = {"until": 0.0, "error": None}
_last = {"ok": None, "error": None, "stations": 0, "from_cache": False, "seconds": None, "at": None}


def _lat_lon(s):
    a = s["AddressInfo"]
    return a["Latitude"], a["Longitude"]


def _bbox(latitude, longitude, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(latitude)), 0.2))
    return latitude - dlat, longitude - dlon, latitude + dlat, longitude + dlon


def _nominatim_box(s_, w_, n_, e_):
    """Charging stations inside a box from OpenStreetMap's Nominatim service (max 40 per request)."""
    from app import ocm_service as G                 # share its one-request-per-second limit
    with G._geocode_lock:
        wait = 1.1 - (time.monotonic() - G._last_geocode_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(NOMINATIM, params={
                "q": "charging station", "format": "jsonv2", "limit": 40, "bounded": 1, "extratags": 1,
                "viewbox": f"{w_:.5f},{n_:.5f},{e_:.5f},{s_:.5f}", "countrycodes": "in", "accept-language": "en"},
                headers=HEADERS, timeout=(5, 15))
        finally:
            G._last_geocode_request = time.monotonic()
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from Nominatim")
    out = []
    for r in resp.json():
        if r.get("type") != "charging_station":
            continue
        tags = dict(r.get("extratags") or {})
        tags.setdefault("amenity", "charging_station")
        if r.get("name"):
            tags.setdefault("name", r["name"])
        out.append({"type": r.get("osm_type", "node"), "id": r.get("osm_id"), "lat": float(r["lat"]),
                    "lon": float(r["lon"]), "tags": tags})
    return out


def _nominatim(latitude, longitude, radius_km):
    """Fallback when the Overpass servers fail: Nominatim, splitting a full box (40 results) into quarters."""
    s_, w_, n_, e_ = _bbox(latitude, longitude, radius_km)
    boxes, els, seen = [(s_, w_, n_, e_, 0)], [], set()
    try:
        while boxes and len(seen) < 400:
            a, b, c, d, depth = boxes.pop(0)
            got = _nominatim_box(a, b, c, d)
            for e in got:
                key = (e["type"], e["id"])
                if key not in seen:
                    seen.add(key)
                    els.append(e)
            if len(got) >= 40 and depth < 2:             # box is full - there are more stations, split it
                mlat, mlon = (a + c) / 2, (b + d) / 2
                boxes += [(a, b, mlat, mlon, depth + 1), (a, mlon, mlat, d, depth + 1),
                          (mlat, b, c, mlon, depth + 1), (mlat, mlon, c, d, depth + 1)]
    except Exception as exc:
        if not els:
            return None, str(exc) if isinstance(exc, RuntimeError) else exc.__class__.__name__
    return [st for st in (to_ocm(e) for e in els) if st and not st["private"]], None


def _query(latitude, longitude, radius_km):
    """Ask both Overpass servers at once; the first good answer wins."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    s_, w_, n_, e_ = _bbox(latitude, longitude, radius_km)
    bb = f"{s_:.5f},{w_:.5f},{n_:.5f},{e_:.5f}"         # a bounding box is much cheaper for the server than "around"
    q = (f'[out:json][timeout:18];(node["amenity"="charging_station"]({bb});way["amenity"="charging_station"]({bb}););'
         f"out center tags 3000;")

    def one(url):
        resp = requests.post(url, data={"data": q}, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} from {url.split('/')[2]}")
        return resp.json().get("elements", [])

    from concurrent.futures import TimeoutError as FutureTimeout
    errors = []
    pool = ThreadPoolExecutor(max_workers=len(ENDPOINTS))
    try:
        futures = [pool.submit(one, u) for u in ENDPOINTS]
        try:                                  # give the Overpass servers 6 s; busy ones often hang much longer
            for f in as_completed(futures, timeout=6):
                try:
                    els = f.result()
                    return [st for st in (to_ocm(e) for e in els) if st and not st["private"]], None
                except Exception as exc:          # network error, bad reply
                    errors.append(str(exc) if isinstance(exc, RuntimeError) else exc.__class__.__name__)
        except FutureTimeout:
            errors.append("Overpass slow (>6 s)")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # all Overpass servers failed or were too slow - use Nominatim (the same OpenStreetMap data)
    sts, err = _nominatim(latitude, longitude, radius_km)
    if sts is not None:
        _last["via"] = "Nominatim (Overpass servers busy)"
        return sts, None
    return None, "OpenStreetMap not reachable (" + "; ".join(errors + [err]) + ")"


def fetch(latitude, longitude, radius_km):
    """-> (stations within radius_km shaped like Open Charge Map's, error or None)."""
    now = time.time()
    with _lock:
        for la, lo, R, t, sts in _areas:
            if now - t < AREA_TTL_S and _dist_m(latitude, longitude, la, lo) / 1000 + radius_km <= R:
                out = [st for st in sts if _dist_m(latitude, longitude, *_lat_lon(st)) <= radius_km * 1000]
                _last.update(ok=True, error=None, stations=len(out), from_cache=True, seconds=0.0, at=now)
                return out, None
        if now < _fail["until"]:
            return [], _fail["error"]
    R = min(max(radius_km + 10, AREA_KM), 50)
    _last["via"] = "Overpass"
    t0 = time.time()
    sts, err = _query(latitude, longitude, R)
    took = round(time.time() - t0, 1)
    with _lock:
        if err:
            _fail.update(until=time.time() + FAIL_TTL_S, error=err)
            _last.update(ok=False, error=err, stations=0, from_cache=False, seconds=took, at=now)
            return [], err
        _fail.update(until=0.0, error=None)
        _areas.append((latitude, longitude, R, now, sts))
        del _areas[:-20]
        out = [st for st in sts if _dist_m(latitude, longitude, *_lat_lon(st)) <= radius_km * 1000]
        _last.update(ok=True, error=None, stations=len(out), from_cache=False, seconds=took, at=now)
    return out, None


def status():
    with _lock:
        return {**_last, "areas_cached": len(_areas), "retry_in_s": max(0, round(_fail["until"] - time.time()))}


_GENERIC = {"ev", "charging", "charger", "charge", "station", "point", "electric", "vehicle", "the", "and", "pvt",
            "ltd", "limited", "india", "hub", "fast", "plaza", "dc", "ac", "kw", "public"}


def _words(station):
    import re as _re
    text = f"{(station.get('AddressInfo') or {}).get('Title') or ''} {(station.get('OperatorInfo') or {}).get('Title') or ''}"
    return {w for w in _re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _GENERIC}


def merge(ocm_stations, osm_stations):
    """Stations already listed + stations from another source that are not already listed.
    A station from another source counts as the same one only if it is within 120 m and shares a name / operator
    word, or within 25 m when one of them has no specific name ("EV charging station"). Stations from the same source are never merged - several networks often have chargers
    side by side at one mall or fuel station."""
    def src(st):
        return st.get("_source", "Open Charge Map")

    pts = []                                  # (lat, lon, station, source, words) of stations already listed
    for st in ocm_stations or []:
        a = st.get("AddressInfo") or {}
        if a.get("Latitude") is not None and a.get("Longitude") is not None:
            pts.append((float(a["Latitude"]), float(a["Longitude"]), st, src(st), _words(st)))
    out = list(ocm_stations or [])
    for st in osm_stations or []:
        a = st["AddressInfo"]
        w = _words(st)
        same = None
        for la, lo, other, other_src, other_w in pts:
            if other_src == src(st):
                continue
            d = _dist_m(a["Latitude"], a["Longitude"], la, lo)
            # same name / operator nearby, or a nameless entry ("EV charging station") on the same spot
            if (d <= DUPLICATE_M and w & other_w) or (d <= 25 and (not w or not other_w)):
                same = other
                break
        if same is not None:
            # the same station: keep the first entry, fill what it is missing from the other source
            if not same.get("Connections") and st["Connections"]:
                same["Connections"] = st["Connections"]
            if not same.get("NumberOfPoints") and st["NumberOfPoints"]:
                same["NumberOfPoints"] = st["NumberOfPoints"]
            if not same.get("UsageCost") and st.get("UsageCost"):
                same["UsageCost"] = st["UsageCost"]
            continue
        out.append(st)
        pts.append((a["Latitude"], a["Longitude"], st, src(st), w))
    return out
