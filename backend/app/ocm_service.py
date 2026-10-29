import os
import time
import threading
import requests

from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()

OCM_URL = "https://api.openchargemap.io/v3/poi/"
GEOCODING_URL = "https://nominatim.openstreetmap.org/search"

HEADERS = {
    "User-Agent": "EV-Energy-Management-Project/1.0"
}

# Respect the public Nominatim service's request rate.
_geocode_lock = threading.Lock()
_last_geocode_request = 0.0


class OCMServiceError(Exception):
    pass


class LocationSearchError(Exception):
    pass


def _validate_coordinates(latitude, longitude):
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        raise ValueError("Latitude and longitude must be valid numbers.")

    if not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90.")

    if not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180.")

    return latitude, longitude


def _validate_search_options(distance_km, max_results):
    try:
        distance_km = float(distance_km)
        max_results = int(max_results)
    except (TypeError, ValueError):
        raise ValueError("Invalid search radius or result limit.")

    if not 1 <= distance_km <= 100:
        raise ValueError("Search radius must be between 1 and 100 km.")

    if not 1 <= max_results <= 100:
        raise ValueError("Maximum results must be between 1 and 100.")

    return distance_km, max_results


TOMTOM_GEOCODE_URL = "https://api.tomtom.com/search/2/geocode/{q}.json"


def _tomtom_geocode(query):
    """Second geocoder (TomTom, same free key as the stations) - used when OpenStreetMap's public geocoder is busy
    or blocks the server (it limits shared cloud servers). Returns results in the same shape as Nominatim's."""
    key = (os.getenv("TOMTOM_API_KEY") or "").strip()
    if not key:
        return None
    try:
        r = requests.get(TOMTOM_GEOCODE_URL.format(q=requests.utils.quote(query, safe="")),
                         params={"key": key, "countrySet": "IN", "limit": 5, "language": "en-GB"}, timeout=10)
        r.raise_for_status()
        out = []
        for x in r.json().get("results") or []:
            pos, adr = x.get("position") or {}, x.get("address") or {}
            if pos.get("lat") is None:
                continue
            name = (adr.get("municipalitySubdivision") or adr.get("municipality") or adr.get("freeformAddress") or query)
            out.append({"lat": pos["lat"], "lon": pos["lon"], "name": name,
                        "display_name": adr.get("freeformAddress") or name, "importance": x.get("score", 0) / 10})
        return out
    except (requests.RequestException, ValueError):
        return None


def _geocode_request(query):
    try:
        result = _nominatim_request(query)
    except LocationSearchError:
        result = None
    if not result:                                   # busy, blocked or nothing found: ask TomTom
        tt = _tomtom_geocode(query)
        if tt is not None:
            return tt
        if result is None:
            raise LocationSearchError("Location search is temporarily unavailable. Please try again in a moment.")
    return result


def _nominatim_request(query):
    global _last_geocode_request

    # Serialize requests and leave at least one second between them.
    with _geocode_lock:
        wait = 1.1 - (time.monotonic() - _last_geocode_request)
        if wait > 0:
            time.sleep(wait)

        try:
            response = requests.get(
                GEOCODING_URL,
                params={
                    "q": query,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "limit": 5,
                    "countrycodes": "in",
                    "accept-language": "en",
                },
                headers=HEADERS,
                timeout=8,
            )
            _last_geocode_request = time.monotonic()
            response.raise_for_status()
            result = response.json()

        except requests.RequestException as exc:
            raise LocationSearchError(
                "Location search is temporarily unavailable. "
                "Please try again or enter coordinates manually."
            ) from exc

        except ValueError as exc:
            raise LocationSearchError(
                "The location service returned an invalid response."
            ) from exc

    if not isinstance(result, list):
        raise LocationSearchError(
            "The location service returned an unexpected response."
        )

    return result


# Indian cities, states and union territories - used to correct spelling mistakes in a search
# ("Hyderbad" -> "Hyderabad") before asking the geocoder again.
INDIAN_PLACES = [w.replace("_", " ") for w in """
Agartala Agra Ahmedabad Ahmednagar Aizawl Ajmer Akola Aligarh Allahabad Prayagraj Alappuzha Alwar Ambala Amravati
Amritsar Anand Anantapur Asansol Aurangabad Bareilly Belagavi Belgaum Bellary Ballari Bengaluru Bangalore Bhagalpur
Bharuch Bhavnagar Bhilai Bhiwandi Bhopal Bhubaneswar Bikaner Bilaspur Bokaro Chandigarh Chennai Coimbatore Cuttack
Dehradun Delhi Dhanbad Dharwad Durg Durgapur Erode Faridabad Gandhinagar Gangtok Gaya Ghaziabad Gorakhpur Gulbarga
Kalaburagi Guntur Gurugram Gurgaon Guwahati Gwalior Haridwar Hisar Hosur Howrah Hubli Hubballi Hyderabad Secunderabad
Imphal Indore Itanagar Jabalpur Jaipur Jalandhar Jalgaon Jammu Jamnagar Jamshedpur Jhansi Jodhpur Junagadh Kakinada
Kannur Kanpur Karimnagar Karnal Kochi Cochin Kohima Kolhapur Kolkata Kollam Kota Kozhikode Calicut Kurnool Lucknow
Ludhiana Madurai Malappuram Mangaluru Mangalore Mathura Meerut Mohali Moradabad Mumbai Mysuru Mysore Nagpur Nanded
Nashik Nellore Noida Panaji Panipat Patiala Patna Puducherry Pondicherry Pune Raipur Rajahmundry Rajkot Ranchi Rohtak
Rourkela Saharanpur Salem Sangli Shillong Shimla Siliguri Solapur Srinagar Surat Thane Thiruvananthapuram Trivandrum
Thrissur Tiruchirappalli Trichy Tirunelveli Tirupati Tiruppur Udaipur Ujjain Vadodara Baroda Varanasi Vellore
Vijayawada Visakhapatnam Vizag Warangal Navi_Mumbai Greater_Noida New_Delhi Pimpri_Chinchwad Kalyan Vasai Virar
Andhra_Pradesh Arunachal_Pradesh Assam Bihar Chhattisgarh Goa Gujarat Haryana Himachal_Pradesh Jharkhand Karnataka
Kerala Madhya_Pradesh Maharashtra Manipur Meghalaya Mizoram Nagaland Odisha Punjab Rajasthan Sikkim Tamil_Nadu
Telangana Tripura Uttar_Pradesh Uttarakhand West_Bengal Ladakh Lakshadweep Andaman_and_Nicobar Dadra_and_Nagar_Haveli
Daman_and_Diu India""".split()]
_PLACE_BY_LOWER = {p.lower(): p for p in INDIAN_PLACES}


def _correct_part(part):
    """Correct one comma-separated part if it is a misspelt Indian city / state (e.g. 'Hyderbad')."""
    import difflib
    low = part.lower()
    if low in _PLACE_BY_LOWER:
        return _PLACE_BY_LOWER[low]
    hit = difflib.get_close_matches(low, list(_PLACE_BY_LOWER), n=1, cutoff=0.8)
    return _PLACE_BY_LOWER[hit[0]] if hit else part


def _tidy(location):
    """'Jubilee Hills,Hyderbad' -> ['Jubilee Hills', 'Hyderbad']"""
    return [" ".join(p.split()) for p in location.replace(";", ",").split(",") if p.strip()]


def _pick(results, query):
    query_lower = query.lower().strip()

    def result_score(item):
        name = item.get("name", "").lower()
        display = item.get("display_name", "").lower()
        score = float(item.get("importance") or 0)
        if name == query_lower:
            score += 10
        elif name and name in query_lower:
            score += 5
        elif query_lower in display:
            score += 3
        return score

    return sorted(results, key=result_score, reverse=True)[0]


@lru_cache(maxsize=200)
def _geocode_cached(location):
    parts = _tidy(location)
    tidy = ", ".join(parts)
    fixed_parts = [_correct_part(p) for p in parts]
    fixed = ", ".join(fixed_parts)

    # Attempts, most specific first: as typed, with spelling corrected, then (if the area itself is not
    # known to the map) the wider place - the city or state - so the search still lands in the right region.
    attempts = [(tidy, None)]
    if fixed != tidy:
        attempts.append((fixed, f"Spelling corrected to '{fixed}'."))
    if len(fixed_parts) > 1:
        attempts.append((", ".join(fixed_parts[1:]),
                         f"'{parts[0]}' was not found on the map – showing {', '.join(fixed_parts[1:])} instead."))
    seen, results, used, note = set(), [], None, None
    for query, why in attempts:
        for q in (query, f"{query}, India") if "india" not in query.lower() else (query,):
            if q.lower() in seen:
                continue
            seen.add(q.lower())
            results = _geocode_request(q)
            if results:
                used, note = query, why
                break
        if results:
            break

    if not results:
        raise LocationSearchError(
            f"No matching place was found for '{location}'. Check the spelling, or add the city or PIN code – "
            "for example 'Jubilee Hills, Hyderabad', 'Indiranagar, Bengaluru' or '500033'."
        )

    best = _pick(results, used)
    try:
        latitude = float(best["lat"])
        longitude = float(best["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LocationSearchError(
            "The location service did not return valid coordinates."
        ) from exc

    return {
        "location_name": best.get("display_name", used),
        "latitude": latitude,
        "longitude": longitude,
        "matched_place": best.get("name") or used,
        "searched_for": used,
        "note": note,
        "source": "OpenStreetMap Nominatim",
    }


def search_location(location):
    """Resolve an Indian city, neighborhood, street, or address."""

    if not isinstance(location, str) or not location.strip():
        raise ValueError("Enter a city, neighborhood, street, or address.")

    location = " ".join(location.split())

    if len(location) > 200:
        raise ValueError("Location must be 200 characters or fewer.")

    return _geocode_cached(location)


def get_nearby_stations(
    latitude: float,
    longitude: float,
    distance_km: float = 25,
    max_results: int = 20,
):
    """Retrieve nearby charging stations from Open Charge Map."""

    latitude, longitude = _validate_coordinates(latitude, longitude)
    distance_km, max_results = _validate_search_options(
        distance_km, max_results
    )

    api_key = os.getenv("OCM_API_KEY")

    if not api_key:
        raise OCMServiceError(
            "OCM_API_KEY is missing. Check your backend .env file."
        )

    params = {
        "output": "json",
        "countrycode": "IN",
        "latitude": latitude,
        "longitude": longitude,
        "distance": distance_km,
        "distanceunit": "KM",
        "maxresults": max_results,
        "compact": "false",
        "verbose": "true",
        "includecomments": "true",
    }

    headers = {
        "X-API-Key": api_key,
        "User-Agent": HEADERS["User-Agent"],
    }

    try:
        response = requests.get(
            OCM_URL,
            params=params,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

    except requests.Timeout as exc:
        raise OCMServiceError(
            "Open Charge Map timed out. Please try again."
        ) from exc

    except requests.HTTPError as exc:
        code = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )
        raise OCMServiceError(
            f"Open Charge Map returned HTTP {code}. "
            "Check your API key and request limits."
        ) from exc

    except requests.RequestException as exc:
        raise OCMServiceError(
            "Could not connect to Open Charge Map."
        ) from exc

    except ValueError as exc:
        raise OCMServiceError(
            "Open Charge Map returned invalid JSON."
        ) from exc

    if not isinstance(data, list):
        raise OCMServiceError(
            "Open Charge Map returned an unexpected response."
        )

    return data