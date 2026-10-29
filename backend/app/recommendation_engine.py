
import math
import re


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two latitude/longitude points in km."""
    earth_radius_km = 6371.0

    lat1 = math.radians(float(lat1))
    lon1 = math.radians(float(lon1))
    lat2 = math.radians(float(lat2))
    lon2 = math.radians(float(lon2))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    # Protect against floating-point rounding errors.
    a = max(0.0, min(1.0, a))
    return earth_radius_km * 2 * math.atan2(
        math.sqrt(a), math.sqrt(1 - a)
    )


def normalize_distance(distance_km):
    """Smaller distance produces a higher score."""
    return 1 / (1 + max(float(distance_km), 0.0))


def normalize_power(power_kw):
    """Normalize charging power using 250 kW as the reference maximum."""
    if power_kw is None:
        return 0.0

    try:
        power_kw = float(power_kw)
    except (TypeError, ValueError):
        return 0.0

    if not math.isfinite(power_kw) or power_kw <= 0:
        return 0.0

    return min(power_kw / 250.0, 1.0)


def calculate_connector_score(connector_types, preferred_connector=None):
    """
    Score connector compatibility.

    1.0 = preferred connector found
    0.5 = connector information exists, but no match was found
    0.0 = no connector information
    """
    if not connector_types:
        return 0.0

    if not preferred_connector:
        return 0.5

    preferred = str(preferred_connector).lower().strip()

    for connector in connector_types:
        if preferred in str(connector).lower():
            return 1.0

    return 0.5



def parse_price_from_usage_cost(usage_cost):
    """
    Extract a price only when the source text gives an unambiguous amount.

    If multiple numbers occur in the text, return None rather than
    accidentally interpreting charging power as the price.
    """
    if usage_cost is None:
        return None

    text = str(usage_cost).strip().lower()

    if not text or text in {
        "unknown", "not specified", "n/a", "none"
    }:
        return None

    if re.search(r"\bfree\b", text):
        return 0.0

    # Multiple numbers may represent power and price together.
    # Do not guess which number is the tariff.
    numbers = re.findall(r"\d+(?:\.\d+)?", text)

    if len(numbers) != 1:
        return None

    try:
        amount = float(numbers[0])
        return amount if math.isfinite(amount) and amount >= 0 else None
    except (ValueError, TypeError):
        return None


def is_comparable_price(usage_cost):
    """
    Only accept explicitly free charging or an explicit per-kWh price.

    Prices with unclear units are excluded from price-based ranking.
    """
    if usage_cost is None:
        return False

    text = str(usage_cost).strip().lower()

    if not text:
        return False

    if re.search(r"\bfree\b", text):
        return True

    return bool(
        re.search(r"/\s*kwh\b", text)
        or re.search(r"\bper\s+kwh\b", text)
        or re.search(
            r"\bkwh\s*[-:]?\s*(?:rate|price)\b", text
        )
    )


def calculate_price_score(price, reference_price=25.0):
    """Lower comparable prices receive higher scores."""
    if price is None:
        return None

    try:
        price = float(price)
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        return None

    if (
        not math.isfinite(price)
        or not math.isfinite(reference_price)
        or price < 0
        or reference_price <= 0
    ):
        return None

    return 1 / (1 + price / reference_price)


def get_status_score(station):
    """
    Convert Open Charge Map status into a recommendation score.

    Status is not a guarantee of real-time charger availability.
    """
    status = station.get("StatusType") or {}
    title = str(status.get("Title", "")).lower()

    if "currently available" in title:
        return 1.0

    if "currently in use" in title:
        return 0.35

    if "partly operational" in title:
        return 0.60

    if "temporarily unavailable" in title:
        return 0.20

    if "not operational" in title:
        return 0.0

    if "operational" in title:
        return 0.85

    return 0.50


def extract_station_features(
    station,
    user_latitude,
    user_longitude,
    preferred_connector=None,
):
    """Convert one raw Open Charge Map station into recommendation features."""
    address_info = station.get("AddressInfo") or {}

    station_lat = address_info.get("Latitude")
    station_lon = address_info.get("Longitude")

    if station_lat is None or station_lon is None:
        return None

    try:
        station_lat = float(station_lat)
        station_lon = float(station_lon)
        user_latitude = float(user_latitude)
        user_longitude = float(user_longitude)
    except (TypeError, ValueError):
        return None

    if not all(
        math.isfinite(value)
        for value in (
            station_lat,
            station_lon,
            user_latitude,
            user_longitude,
        )
    ):
        return None

    if not (
        -90 <= station_lat <= 90
        and -180 <= station_lon <= 180
        and -90 <= user_latitude <= 90
        and -180 <= user_longitude <= 180
    ):
        return None

    distance_km = haversine_distance(
        user_latitude,
        user_longitude,
        station_lat,
        station_lon,
    )

    connections = station.get("Connections") or []
    connector_types = []
    power_values = []

    for connection in connections:
        connector = connection.get("ConnectionType") or {}
        title = connector.get("Title")

        if title:
            connector_types.append(str(title))

        power = connection.get("PowerKW")

        if power is not None:
            try:
                power = float(power)
                if math.isfinite(power) and power > 0:
                    power_values.append(power)
            except (TypeError, ValueError):
                pass

    max_power_kw = max(power_values) if power_values else None

    usage_cost_raw = station.get("UsageCost")
    observed_price = parse_price_from_usage_cost(usage_cost_raw)
    price_comparable = is_comparable_price(usage_cost_raw)

    # Only comparable prices receive a price score.
    price_score = (
        calculate_price_score(observed_price)
        if price_comparable and observed_price is not None
        else None
    )

    number_of_points = station.get("NumberOfPoints")

    try:
        number_of_points = max(int(number_of_points or 0), 0)
    except (TypeError, ValueError):
        number_of_points = 0

    status_score = get_status_score(station)

    station_id = station.get("ID")

    return {
        "id": station_id,
        "name": address_info.get("Title") or "Unknown Station",
        "address": address_info.get("AddressLine1"),
        "city": address_info.get("Town"),
        "state": address_info.get("StateOrProvince"),
        "latitude": station_lat,
        "longitude": station_lon,
        "distance_km": round(distance_km, 3),
        "distance_score": round(normalize_distance(distance_km), 4),
        "connector_types": sorted(set(connector_types)),
        "preferred_connector": preferred_connector,
        "connector_score": round(
            calculate_connector_score(
                connector_types, preferred_connector
            ),
            4,
        ),
        "max_power_kw": max_power_kw,
        "power_score": round(normalize_power(max_power_kw), 4),
        "number_of_points": number_of_points,
        "usage_cost_raw": usage_cost_raw,
        "observed_price_inr": observed_price,
        "price_available": observed_price is not None,
        "price_comparable": price_comparable,
        "price_unit_status": (
            "Comparable per-kWh price"
            if price_comparable and observed_price is not None
            else (
                "Unit unclear"
                if observed_price is not None
                else "Price not supplied"
            )
        ),
        "price_score": (
            round(price_score, 4)
            if price_score is not None
            else None
        ),
        "status": (
            station.get("StatusType") or {}
        ).get("Title", "Unknown"),
        "status_score": round(status_score, 4),
        "operator": (
            station.get("OperatorInfo") or {}
        ).get("Title"),
        "data_quality_level": station.get("DataQualityLevel"),
        "is_recently_verified": station.get("IsRecentlyVerified"),
        "source": station.get("_source", "Open Charge Map"),
        "access": station.get("_access", "public"),
        "access_note": station.get("_access_note"),
        "osm_url": station.get("osm_url"),
        "ocm_url": (
            f"https://openchargemap.org/site/poi/details/{station_id}"
            if station_id and not station.get("_source")
            else None
        ),
    }


def build_station_features(
    stations,
    user_latitude,
    user_longitude,
    preferred_connector=None,
):
    """Build feature dictionaries for all valid stations."""
    features = []

    for station in stations:
        station_features = extract_station_features(
            station,
            user_latitude,
            user_longitude,
            preferred_connector,
        )

        if station_features is not None:
            features.append(station_features)

    return features


def calculate_station_score(station, weights=None):
    """
    Calculate a weighted multi-criteria recommendation score.

    Default design weights:
        distance:  25%
        price:     20%
        power:     15%
        connector: 15%
        status:    15%
        capacity:  10%

    If price is missing or incomparable, its weight is excluded and
    the remaining applicable weights are renormalized.

    These are design weights, not learned model parameters.
    """
    if weights is None:
        weights = {
            "distance": 0.25,
            "price": 0.20,
            "power": 0.15,
            "connector": 0.15,
            "status": 0.15,
            "capacity": 0.10,
        }

    capacity_score = min(
        max(float(station.get("number_of_points", 0) or 0), 0)
        / 8.0,
        1.0,
    )

    criteria = {
        "distance": station.get("distance_score", 0.0),
        "power": station.get("power_score", 0.0),
        "connector": station.get("connector_score", 0.0),
        "status": station.get("status_score", 0.5),
        "capacity": capacity_score,
    }

    # Include price only when it is explicitly comparable.
    price_score = station.get("price_score")
    if (
        station.get("price_comparable", False)
        and price_score is not None
    ):
        criteria["price"] = price_score

    weighted_total = 0.0
    applicable_weight_total = 0.0

    for criterion, value in criteria.items():
        weight = weights.get(criterion, 0.0)

        try:
            weight = float(weight)
            value = float(value)
        except (TypeError, ValueError):
            continue

        if (
            not math.isfinite(weight)
            or not math.isfinite(value)
            or weight <= 0
        ):
            continue

        value = max(0.0, min(value, 1.0))
        weighted_total += weight * value
        applicable_weight_total += weight

    if applicable_weight_total <= 0:
        return 0.0

    return round(weighted_total / applicable_weight_total, 4)


def rank_stations(stations, weights=None):
    """Rank stations from highest to lowest recommendation score."""
    for station in stations:
        station["capacity_score"] = round(
            min(
                max(float(station.get("number_of_points", 0) or 0), 0)
                / 8.0,
                1.0,
            ),
            4,
        )

        station["recommendation_score"] = calculate_station_score(
            station,
            weights,
        )

    stations.sort(
        key=lambda station: station["recommendation_score"],
        reverse=True,
    )

    for index, station in enumerate(stations, start=1):
        station["recommendation_rank"] = index

    return stations
