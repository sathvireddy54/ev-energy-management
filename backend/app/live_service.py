"""
Live station layer - Google Places API (New), Nearby Search with evChargeOptions.

Google Maps receives real-time charger availability from networks that share it (in India e.g. Statiq and
ElectricPe). For each station the API returns, per connector type:
    count, availableCount, outOfServiceCount, maxChargeRateKw, availabilityLastUpdateTime
Open Charge Map (OCM) stays the main station directory; Google stations are matched to OCM stations by
location (and name), and Google-only stations are added.

Needs GOOGLE_MAPS_API_KEY in backend/.env (Places API (New) enabled, billing on). The evChargeOptions field
is billed at the Nearby Search "Enterprise + Atmosphere" SKU (1,000 free calls per month).
Without a key, or if the call fails, everything falls back to the model estimates and is labelled so.
"""
import math
import os
import re
import time
from datetime import datetime, timezone

import requests

URL = "https://places.googleapis.com/v1/places:searchNearby"
FIELDS = ",".join(["places.id", "places.displayName", "places.formattedAddress", "places.location",
                   "places.businessStatus", "places.evChargeOptions"])
CACHE_SECONDS = 60
MATCH_M = 150            # OCM and Google stations within 150 m are the same site
MATCH_NAME_M = 400       # ... or within 400 m with a shared name word
CONNECTOR_NAMES = {
    "EV_CONNECTOR_TYPE_J1772": "Type 1 (J1772)", "EV_CONNECTOR_TYPE_TYPE_2": "Type 2",
    "EV_CONNECTOR_TYPE_CHADEMO": "CHAdeMO", "EV_CONNECTOR_TYPE_CCS_COMBO_1": "CCS (Type 1)",
    "EV_CONNECTOR_TYPE_CCS_COMBO_2": "CCS (Type 2)", "EV_CONNECTOR_TYPE_TESLA": "Tesla",
    "EV_CONNECTOR_TYPE_NACS": "NACS (Tesla)", "EV_CONNECTOR_TYPE_UNSPECIFIED_GB_T": "GB-T (Bharat DC-001)",
    "EV_CONNECTOR_TYPE_UNSPECIFIED_WALL_OUTLET": "Wall socket (Type D / 15A)", "EV_CONNECTOR_TYPE_OTHER": "Other",
}
_cache = {}


def api_key():
    return os.getenv("GOOGLE_MAPS_API_KEY") or None


def status():
    return "configured" if api_key() else "missing (add GOOGLE_MAPS_API_KEY to backend/.env for live availability)"


def fetch(latitude, longitude, radius_km):
    """Raw Google places (cached for 60 s). Returns (places, error)."""
    key = api_key()
    if not key:
        return [], "no_key"
    ck = (round(latitude, 3), round(longitude, 3), round(radius_km))
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1], None
    body = {"includedTypes": ["electric_vehicle_charging_station"], "maxResultCount": 20, "rankPreference": "DISTANCE",
            "locationRestriction": {"circle": {"center": {"latitude": latitude, "longitude": longitude},
                                               "radius": float(min(radius_km * 1000, 50000))}}}
    try:
        r = requests.post(URL, json=body, timeout=10,
                          headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS})
        if r.status_code != 200:
            msg = r.json().get("error", {}).get("message", r.text[:200]) if r.headers.get("content-type", "").startswith(
                "application/json") else r.text[:200]
            return [], f"Google Places error {r.status_code}: {msg}"
        places = r.json().get("places", [])
    except requests.RequestException as exc:
        return [], f"Google Places unreachable: {exc}"
    _cache[ck] = (time.time(), places)
    return places, None


def _dist_m(a_lat, a_lon, b_lat, b_lon):
    r = 6371000
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _words(name):
    stop = {"ev", "charging", "station", "charger", "point", "the", "and", "of", "-", "–"}
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in stop and len(w) > 2}


def _age_min(ts):
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return max(0, round((datetime.now(timezone.utc) - t).total_seconds() / 60))
    except ValueError:
        return None


def parse(place):
    """One Google place -> live summary (None values when the network does not share live data)."""
    ev = place.get("evChargeOptions") or {}
    aggs = ev.get("connectorAggregation") or []
    conns, count, avail, oos, have_live, ages = [], 0, 0, 0, False, []
    for a in aggs:
        n = int(a.get("count") or 0)
        count += n
        name = CONNECTOR_NAMES.get(a.get("type"), "Other")
        row = {"type": name, "count": n, "max_kw": a.get("maxChargeRateKw")}
        if a.get("availableCount") is not None:
            have_live = True
            row["available"] = int(a.get("availableCount") or 0)
            row["out_of_service"] = int(a.get("outOfServiceCount") or 0)
            avail += row["available"]
            oos += row["out_of_service"]
            ages.append(_age_min(a.get("availabilityLastUpdateTime")))
        conns.append(row)
    loc = place.get("location") or {}
    ages = [a for a in ages if a is not None]
    return {"google_id": place.get("id"), "name": (place.get("displayName") or {}).get("text"),
            "address": place.get("formattedAddress"), "latitude": loc.get("latitude"), "longitude": loc.get("longitude"),
            "business_status": place.get("businessStatus"), "connectors": conns,
            "connector_count": int(ev.get("connectorCount") or count or 0),
            "has_live_availability": have_live,
            "available": avail if have_live else None, "out_of_service": oos if have_live else None,
            "updated_min_ago": max(ages) if ages else None}


def merge(ocm_features, places, user_lat, user_lon):
    """Attach live data to OCM stations; return (stations, google_only_places)."""
    live = [parse(p) for p in places]
    live = [p for p in live if p["latitude"] is not None]
    used = set()
    for s in ocm_features:
        best, best_d = None, None
        for i, p in enumerate(live):
            if i in used:
                continue
            d = _dist_m(s["latitude"], s["longitude"], p["latitude"], p["longitude"])
            if d <= MATCH_M or (d <= MATCH_NAME_M and _words(s["name"]) & _words(p["name"])):
                if best_d is None or d < best_d:
                    best, best_d = i, d
        if best is not None:
            used.add(best)
            s["live"] = {**live[best], "match_distance_m": round(best_d)}
    extra = [p for i, p in enumerate(live) if i not in used]
    return ocm_features, extra


def google_only_feature(p, user_lat, user_lon):
    """A station known only to Google -> the same feature shape the recommendation engine uses."""
    from app.recommendation_engine import haversine_distance, normalize_distance, normalize_power
    d = haversine_distance(user_lat, user_lon, p["latitude"], p["longitude"])
    max_kw = max([c["max_kw"] or 0 for c in p["connectors"]] or [0]) or None
    status = {"OPERATIONAL": "Operational", "CLOSED_TEMPORARILY": "Temporarily Unavailable",
              "CLOSED_PERMANENTLY": "Not Operational"}.get(p["business_status"], "Unknown")
    return {"id": f"g:{p['google_id']}", "name": p["name"] or "EV charging station", "address": p["address"],
            "city": None, "latitude": p["latitude"], "longitude": p["longitude"], "distance_km": round(d, 3),
            "distance_score": round(normalize_distance(d), 4),
            "connector_types": sorted({c["type"] for c in p["connectors"]}), "max_power_kw": max_kw,
            "power_score": round(normalize_power(max_kw), 4), "number_of_points": p["connector_count"],
            "usage_cost_raw": None, "observed_price_inr": None, "price_available": False, "price_comparable": False,
            "price_score": None, "status": status, "status_score": 0.85 if status == "Operational" else 0.2,
            "operator": None, "ocm_url": None, "source": "Google Maps", "live": p}
