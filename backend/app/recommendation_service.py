"""
Connector compatibility for station recommendation (used by plan_service.py, "Plan my charge").

The station's connector names from Open Charge Map are matched with the connectors used by the chosen
vehicle type in India (or the connector the owner picks):
    compatible 1.0  |  check with your vehicle / unknown 0.5  |  not compatible 0.0
"""

# Keywords in Open Charge Map connector names used by each vehicle type in India.
# Household sockets (Indian 3-pin / Type D and M, BS1363 13 A, Europlug, IEC 60309 industrial) are how most
# 2-wheelers and 3-wheelers charge, with the vehicle's own portable charger. Cars can also use them with
# their portable charger, but slowly, so for a car they are "check".
HOUSEHOLD = ["domestic", "type d", "type m", "type g", "type i ", "bs1363", "bs 1363", "13 amp", "13a", "15a", "16a",
             "3-pin", "3 pin", "europlug", "cee 7", "schuko", "nema 5", "nema 14", "as 3112"]
VEHICLE_CONNECTORS = {
    "4W": {"compatible": ["ccs", "type 2", "mennekes", "chademo", "gb-t", "gb/t", "bharat", "iec 62196", "nacs"],
           "check": ["type 1", "j1772", "iec 60309", "industrial", "tesla"] + HOUSEHOLD},
    "3W": {"compatible": ["iec 60309", "industrial", "bharat", "gb-t", "gb/t", "lev", "lecc", "type 6", "type 7"]
                         + HOUSEHOLD,
           "check": ["type 2", "ccs", "type 1", "j1772"]},
    "2W": {"compatible": ["iec 60309", "industrial", "lev", "lecc", "type 6", "type 7", "bharat ac", "ather"]
                         + HOUSEHOLD,
           "check": ["type 2", "ccs", "type 1", "j1772", "bharat"]},
}
# Connector names that carry no information (Open Charge Map uses these when the type was not recorded).
GENERIC = ["unknown", "not specified", "other", "unspecified"]


LABELS = {"distance": "Distance", "price": "Reported price", "power": "Charging power",
          "connector": "Connector compatibility", "status": "Station status", "capacity": "Charging points"}


def compatibility(connector_types, vehicle_type=None, preferred=None):
    named = [c for c in connector_types or [] if c and str(c).strip()]
    known = [c for c in named if not any(g == c.lower().strip() or c.lower().startswith(g) for g in GENERIC)]
    if not known:
        return "unknown", 0.5, ("Connector type not recorded on Open Charge Map – check at the station"
                                if named else "No connector information")
    if preferred:
        p = preferred.lower().strip()
        hit = [c for c in known if p in c.lower()]
        return ("compatible", 1.0, f"Has your connector ({hit[0]})") if hit else \
               ("not_compatible", 0.0, f"No {preferred} connector listed")
    if not vehicle_type:
        return "unknown", 0.5, "Choose a vehicle type to check compatibility"
    rules = VEHICLE_CONNECTORS[vehicle_type]
    for c in known:
        if any(k in c.lower() + " " for k in rules["compatible"]):
            return "compatible", 1.0, f"{c} suits this vehicle type"
    for c in known:
        if any(k in c.lower() + " " for k in rules["check"]):
            return "check", 0.5, f"{c} - check your vehicle supports it"
    if len(known) < len(named):          # some connectors are unrecorded - they may still suit the vehicle
        return "unknown", 0.5, "Listed connectors do not match, others are not recorded – check at the station"
    return "not_compatible", 0.0, "Listed connectors do not match this vehicle type"
