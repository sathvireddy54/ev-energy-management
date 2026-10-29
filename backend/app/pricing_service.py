"""
Feature 4 - Adaptive dynamic pricing (indicative, rule-based).

Documented rules (all values are in RULES below and returned by the API):

    price per kWh before tax = base price x time-of-day factor x demand factor
    tax                      = GST % of that (18 % for EV charging services in India; editable)
    price per kWh            = before tax + tax
    session cost             = price per kWh x energy charged (kWh, shown on the charger / bill)

    Time-of-day factor (peak / off-peak)
        peak      17:00-20:59  x 1.20   evening peak
        normal    10:00-16:59  x 1.05
        off-peak  all other    x 0.90
    Demand factor - from the expected demand level for the chosen day
        low  x 0.90   medium x 1.00   high x 1.10
        (levels = tertiles of daily sessions in the normal year 2019)

Battery remaining life is NOT used in the price: charging owners of older batteries more
would penalise them for something they cannot control. Remaining charge cycles are shown as
advice only (see the RUL module).

This is an indicative model output, not a station's actual tariff.
"""
from app.demand_service import forecast, hour_band, level_thresholds

RULES = {
    "time_bands": [
        {"name": "peak", "label": "Peak", "hours": "17:00-20:59", "from": 17, "to": 20, "factor": 1.20},
        {"name": "normal", "label": "Normal", "hours": "10:00-16:59", "from": 10, "to": 16, "factor": 1.05},
        {"name": "off_peak", "label": "Off-peak", "hours": "21:00-09:59", "from": None, "to": None, "factor": 0.90},
    ],
    "demand_factors": {"low": 0.90, "medium": 1.00, "high": 1.10},
    "default_base_price_inr_per_kwh": 10.0,
    "default_gst_pct": 18.0,
    "formula": "price/kWh = base x time factor x demand factor, then + GST",
    "battery_rul_used_in_price": False,
}
LEVELS = ("low", "medium", "high")


def time_band(hour):
    for b in RULES["time_bands"][:2]:
        if b["from"] <= hour <= b["to"]:
            return b
    return RULES["time_bands"][2]


def quote(hour, base_price, gst_pct, demand_level, energy_kwh=None):
    if not 0 <= hour <= 23:
        raise ValueError("Hour must be between 0 and 23.")
    if base_price <= 0:
        raise ValueError("Base price must be greater than 0.")
    if not 0 <= gst_pct <= 50:
        raise ValueError("GST must be between 0 and 50 %.")
    if demand_level not in LEVELS:
        raise ValueError("Demand level must be low, medium or high.")
    band = time_band(hour)
    tf, df = band["factor"], RULES["demand_factors"][demand_level]
    after_time = base_price * tf
    before_tax = after_time * df
    tax = before_tax * gst_pct / 100
    per_kwh = before_tax + tax
    q = {
        "hour": hour, "time_band": band["label"], "time_factor": tf,
        "demand_level": demand_level, "demand_factor": df, "gst_pct": gst_pct,
        "base_price": round(base_price, 2),
        "breakdown_per_kwh": [
            {"step": "Base price", "amount": round(base_price, 2)},
            {"step": f"Time of day ({band['label']}, x{tf:.2f})", "amount": round(after_time - base_price, 2)},
            {"step": f"Demand ({demand_level}, x{df:.2f})", "amount": round(before_tax - after_time, 2)},
            {"step": f"GST ({gst_pct:g}%)", "amount": round(tax, 2)},
        ],
        "price_before_tax_per_kwh": round(before_tax, 2),
        "price_per_kwh": round(per_kwh, 2),
        "unit": "INR per kWh (indicative)",
    }
    if energy_kwh:
        if not 0 < energy_kwh <= 200:
            raise ValueError("Energy to charge must be between 0 and 200 kWh.")
        q["energy_kwh"] = energy_kwh
        q["session_cost"] = {"energy_cost_before_tax": round(before_tax * energy_kwh, 2),
                             "gst": round(tax * energy_kwh, 2),
                             "total": round(per_kwh * energy_kwh, 2)}
    return q


def pricing(hour, base_price=None, gst_pct=None, date=None, demand_level=None, energy_kwh=None):
    """Full pricing response: quote for the chosen hour/day, 24-hour profile, level comparison,
    7-day comparison and the rules."""
    base_price = base_price or RULES["default_base_price_inr_per_kwh"]
    gst_pct = RULES["default_gst_pct"] if gst_pct is None else gst_pct
    days = forecast()
    day = next((d for d in days if d["date"] == date), days[0])
    level = demand_level or day["level"]
    q = quote(hour, base_price, gst_pct, level, energy_kwh)

    by_hour = [{"hour": h, "time_band": time_band(h)["label"], "historical_busyness": hour_band(h),
                **{lv: quote(h, base_price, gst_pct, lv)["price_per_kwh"] for lv in LEVELS}}
               for h in range(24)]
    by_level = [{"demand_level": lv, **{b["label"]: quote(h, base_price, gst_pct, lv)["price_per_kwh"]
                                        for b, h in zip(RULES["time_bands"], (18, 12, 23))}} for lv in LEVELS]
    by_day = [{**d, "price_per_kwh": quote(hour, base_price, gst_pct, d["level"])["price_per_kwh"]} for d in days]
    prices = [r[lv] for r in by_hour for lv in LEVELS]
    return {
        "quote": q, "forecast_day": day, "level_source": "chosen by you" if demand_level else "demand forecast",
        "comparison_by_hour": by_hour, "comparison_by_level": by_level, "comparison_by_day": by_day,
        "cheapest_per_kwh": min(prices), "highest_per_kwh": max(prices),
        "rules": RULES, "demand_levels": level_thresholds(),
        "is_actual_station_tariff": False,
        "notes": ["Indicative price from documented rules, not a station's actual tariff.",
                  "Battery remaining life is not used in the price, so owners of older batteries are not "
                  "charged more."],
    }


def score_for_ranking(price_per_kwh, reference=25.0):
    """Lower price -> higher score (same form as the recommendation engine)."""
    return 1 / (1 + price_per_kwh / reference)
