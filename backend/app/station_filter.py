"""
Keep only charging stations a driver can use.

The station sources sometimes list chargers at places that are not open to the public, named after the place
that hosts them (an apartment complex, a gated community) - or entries whose name says nothing about charging.
Each station is classified from its name, operator and the source's own access information:

    public        a known charging network, or the name says it is a charging station        -> shown
    dealership    a car brand's showroom / service centre (often for that brand's customers) -> shown, labelled
    venue         at a hotel, office, store, mall, hospital ... (usually open to visitors)  -> shown, labelled
    residential   apartment, residency, society, enclave, gated community ...                 -> hidden
    private       the source marks it private / restricted / members only                       -> hidden
    unclear       the name gives no sign of a charging station or a known operator             -> hidden
"""
import re

NETWORKS = ["tata power", "ez charge", "statiq", "chargezone", "charge zone", "jio-bp", "jio bp", "jiobp", "glida",
            "fortum", "thunder plus", "thunderplus", "lioncharge", "lion charge", "plugeasy", "zeon", "ather",
            "shell", "hpcl", "bpcl", "indianoil", "indian oil", "iocl", "adani", "emobility", "voltran", "roadgrid",
            "zect", "ev dock", "evre", "evere", "joulepoint", "rebolt", "e-fill", "efill", "go ec", "goec",
            "chargemod", "kurrent", "relux", "electricpe", "bolt.earth", "bolt earth", "kazam", "charzer",
            "sun mobility", "magenta", "chargegrid", "seeyan", "reliable charge", "tsev", "tsredco", "eesl", "nhev",
            "ola electric", "revolt", "ampere", "tork", "exicom", "delta", "abb", "servotech", "volttic", "evolt",
            "pulse energy", "charge+zone", "electreefi", "yulu", "battery smart", "evify", "power grid", "ntpc",
            "ola hypercharger", "hypercharger", "hero vida", "vida", "ingo", "mobilane", "bescom", "tgredco",
            "tg redco", "amplify mobility", "goego", "blusmart", "pulse", "reva", "tsredco", "apepdcl", "kseb", "msedcl", "cesl"]
CHARGING_WORDS = re.compile(r"\b(e?v|e-?charge|charg\w*|recharg\w*|electric|plug|volt\w*|kw|supercharg\w*|"
                            r"fast\s*charg\w*|power|e-?mobility|ccs|chademo|type\s*2|bharat\s*(ac|dc))\b", re.I)
RESIDENTIAL = re.compile(r"\b(apartments?|apts?|residenc\w*|society|enclave|heights|towers?|villas?|homes|"
                         r"colony|gated|community|layout|meadows|gardens?|flats?|residential|hostel|pg)\b", re.I)
DEALERS = re.compile(r"\b(mercedes|benz|bmw|audi|hyundai|kia|mg motor|volvo|byd|jaguar|land rover|porsche|"
                     r"mahindra (?:first|showroom)|car affair|showroom|dealer\w*|service cent(?:er|re)|motors)\b", re.I)
VENUES = re.compile(r"\b(hotel|resort|inn|office|corporate|campus|tech ?park|it ?park|business ?park|mall|store|ikea|"
                    r"hospital|airport|metro|railway|station road|cafe|restaurant|park|hub|plaza|centre|center|"
                    r"club|university|college|school|parking)\b", re.I)
STATION_PHRASE = re.compile(r"(charg\w*|e?v)\s*(station|hub|point|park|zone|plaza|bay)|supercharger|hypercharger", re.I)
PRIVATE_USAGE = re.compile(r"private|restricted|residents|staff only|employees", re.I)   # "Public - Membership Required" stays


def classify(station):
    """station: a raw station shaped like Open Charge Map's -> (class, reason)."""
    name = ((station.get("AddressInfo") or {}).get("Title") or "").strip()
    operator = ((station.get("OperatorInfo") or {}).get("Title") or "").strip()
    usage = ((station.get("UsageType") or {}).get("Title") or "")
    text = f"{name} {operator}".lower()
    name_wo_street = re.sub(r"\b\w+\s+(road|rd|street|st|marg|main)\b", " ", name, flags=re.I)   # "Residency Road"
    fast = any((c.get("PowerKW") or 0) >= 50 for c in station.get("Connections") or [])
    if PRIVATE_USAGE.search(usage) or station.get("private"):
        return "private", f"marked '{usage or 'private'}' by the source"
    if any(n in text for n in NETWORKS):
        return "public", None
    if DEALERS.search(text):
        return "dealership", "at a car showroom / service centre – may be for that brand's customers"
    if RESIDENTIAL.search(name_wo_street):
        if fast:
            return "venue", "fast charger at a business / residential complex – check access on arrival"
        return "residential", "charger at a residential place – usually for residents only"
    if STATION_PHRASE.search(name):
        return "public", None
    if VENUES.search(name_wo_street):
        return "venue", "at a hotel / office / store – usually open to visitors"
    if CHARGING_WORDS.search(name):
        return "public", None
    if station.get("Connections") and any((c.get("PowerKW") or 0) >= 15 for c in station["Connections"]):
        return "public", None              # a fast charger is a charging station whatever it is called
    return "unclear", "name gives no sign of a charging station or a known network"


def keep_public(stations):
    """-> (stations to show, hidden stations with the reason). Dealerships are kept and labelled."""
    shown, hidden = [], []
    for s in stations or []:
        cls, why = classify(s)
        s["_access"], s["_access_note"] = cls, why
        (hidden if cls in ("residential", "private", "unclear") else shown).append(s)
    return shown, hidden
