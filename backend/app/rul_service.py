"""
Feature 1 - Battery RUL prediction for real vehicles (owner inputs only).

Inputs - only what an owner can read from the dashboard or RC:
    vehicle_type        2W / 3W / 4W
    registration_year   from the RC
    odometer_km         dashboard
    km_per_year         owner's estimate of future driving
    readings            1-3 dashboard readings of (SOC %, range shown km),
                        ideally taken on different days at SOC >= 50 %

Method
    full_charge_range = average of range / SOC over the readings
    cycles_used       = odometer / full-charge range when new
    Ageing law (train_rul_model.py, NASA + Zenodo real data):
        SoH(age, cycles) = 100 - a*age^alpha - b*cycles^beta
    RUL: project forward with the owner's km per year until SoH = 80 %.
    Low / high: the battery stays at the 10th / 90th percentile of real cars
    of the same age; that spread widens with age (fitted on Zenodo, ~80 %
    coverage in every age group).

RUL = battery life left until it holds 80 % of its original capacity.
It does not change with the current SOC (SOC is the charge level right now).

Also returned: estimated state of health now (low / mid / high) and the projected
health curve to 80 % for the degradation chart.

Run directly for a quick test:  python rul_service.py
"""
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.optimize import brentq

BASE = Path(__file__).resolve().parent.parent   # backend/
MODELS = BASE / "models"

MIN_SOC = 20.0              # below this the displayed range is too uncertain
RECOMMENDED_SOC = 50.0      # above this the displayed range is more stable
MAX_YEARS = 40.0            # projection horizon
READING_SPREAD_WARN = 0.15  # readings disagreeing by more than 15 % trigger a note
SENSITIVITY = 0.10          # +/- 10 % error in the range reading
WHAT_IF_FACTOR = 0.75       # "if you drove 25 % less per year"
EARLY_LIFE_SOH = 90.0       # above this, life stage is less certain (NASA check)

FULL_CHARGE_KM = {"2W": (30, 250), "3W": (40, 300), "4W": (80, 900)}
LABELS = {"2W": "Electric 2-wheeler", "3W": "Electric 3-wheeler", "4W": "Electric car"}


@lru_cache(maxsize=1)
def _model():
    return json.loads((MODELS / "ageing_model.json").read_text())


def _band(m, name, age):
    if name == "mid":
        return 0.0
    b = m["bands"][name]
    return b["intercept"] + b["slope"] * age


def _soh(m, age, cycles, band="mid"):
    return 100 - m["a"] * age ** m["alpha"] - m["b"] * cycles ** m["beta"] + _band(m, band, age)


def _years_to_eol(m, age, cycles, cycles_per_year, band):
    f = lambda t: _soh(m, age + t, cycles + cycles_per_year * t, band) - m["eol_soh"]
    if f(0) <= 0:
        return 0.0
    if f(MAX_YEARS) > 0:
        return None
    return brentq(f, 0, MAX_YEARS)


def _full_charge_range(readings, vehicle_type, notes):
    if not readings:
        raise ValueError("Enter at least one dashboard reading (SOC % and range shown).")
    if len(readings) > 3:
        raise ValueError("Enter at most 3 readings.")
    values = []
    for i, (soc, rng) in enumerate(readings, 1):
        if not MIN_SOC <= soc <= 100:
            raise ValueError(f"Reading {i}: battery % (SOC) must be between {MIN_SOC:.0f} and 100.")
        if rng <= 0:
            raise ValueError(f"Reading {i}: range shown must be greater than 0.")
        values.append(rng / (soc / 100))
    lo, hi = FULL_CHARGE_KM[vehicle_type]
    full = float(np.mean(values))
    if not lo <= full <= hi:
        raise ValueError(f"Your readings mean about {full:.0f} km on a full charge, which is unusual for "
                         f"this vehicle type ({lo}-{hi} km). Please re-check the SOC and range.")
    if len(values) > 1 and (max(values) - min(values)) / full > READING_SPREAD_WARN:
        notes.append("Your readings differ by more than 15%; the dashboard range estimate varies with "
                     "recent driving and AC use. Readings on similar days give a steadier result.")
    if min(soc for soc, _ in readings) < RECOMMENDED_SOC:
        notes.append(f"For a steadier result, take readings when the battery is above {RECOMMENDED_SOC:.0f}%.")
    return full, len(values)


def _project(m, age, odometer_km, full_charge_now, km_per_year):
    """Returns cycles used, SoH now, and years to end of life per band."""
    soh = 100.0
    for _ in range(2):                          # correct the current range for wear
        full_new = full_charge_now / (soh / 100)
        cycles = odometer_km / full_new
        soh = float(np.clip(_soh(m, age, cycles), 1, 100))
    cpy = km_per_year / full_new
    years = {b: _years_to_eol(m, age, cycles, cpy, b) for b in ("q10", "mid", "q90")}
    return cycles, soh, years, cpy


def _curve(m, age, cycles, cpy, years):
    """Projected state of health (low / mid / high) from now until all three reach 80 %
    (or 40 years). Used for the degradation chart."""
    ends = [y for y in years.values() if y is not None]
    horizon = min(MAX_YEARS, max(ends + [5.0]) * 1.15 + 1) if ends else MAX_YEARS
    step = 0.25 if horizon <= 15 else 0.5 if horizon <= 30 else 1.0
    pts = []
    for t in np.arange(0, horizon + 1e-9, step):
        a, c = age + t, cycles + cpy * t
        pts.append({"years_from_now": round(float(t), 2), "age_years": round(float(a), 2),
                    **{k: round(float(np.clip(_soh(m, a, c, b), 0, 100)), 2)
                       for k, b in (("low", "q10"), ("mid", "mid"), ("high", "q90"))}})
    return pts


def _round_years(y):
    return None if y is None else round(y, 1)


def vehicle_age(registration_year, today=None):
    today = today or date.today()
    return max(today.year - registration_year + today.timetuple().tm_yday / 365.25 - 0.5, 0.1)


def predict_rul(vehicle_type, registration_year, odometer_km, km_per_year,
                readings=None, soc_pct=None, range_km=None, vehicle_name=None, today=None):
    """readings: list of (soc_pct, range_km). A single soc_pct/range_km pair is also accepted."""
    if vehicle_type not in LABELS:
        raise ValueError("vehicle_type must be '2W', '3W' or '4W'.")
    if odometer_km <= 0 or km_per_year <= 0:
        raise ValueError("Odometer and distance per year must be greater than 0.")
    today = today or date.today()
    if registration_year > today.year:
        raise ValueError("Registration year cannot be in the future.")
    if not readings and soc_pct is not None and range_km is not None:
        readings = [(soc_pct, range_km)]

    m = _model()
    notes = []
    full_charge_now, n_readings = _full_charge_range(readings, vehicle_type, notes)
    min_soc = min(soc for soc, _ in readings)
    age = vehicle_age(registration_year, today)

    cycles, soh, years, cpy = _project(m, age, odometer_km, full_charge_now, km_per_year)
    out_of_range = age > m["train_age_max"] or cycles > m["train_cycles_max"]
    if out_of_range:
        notes.append("This vehicle is older or more used than any car in the training data; "
                     "the estimate is less reliable.")
    if vehicle_type != "4W":
        notes.append("No public battery-health data exists for 2- and 3-wheelers; the ageing model "
                     "fitted on cars is applied using your vehicle's own charge cycles.")

    # low = battery ageing like the worse 10 % of real cars, high = better 10 %
    yrs = {"low": years["q10"], "mid": years["mid"], "high": years["q90"]}
    rul_years = {k: _round_years(v) for k, v in yrs.items()}
    rul_km = {k: None if v is None else int(round(v * km_per_year, -2)) for k, v in yrs.items()}
    rul_cycles = {k: None if v is None else int(round(v / full_charge_now)) for k, v in rul_km.items()}

    # Wear so far: calendar vs use
    cal = m["a"] * age ** m["alpha"]
    use = m["b"] * cycles ** m["beta"]
    wear = {"age_pct": int(round(100 * cal / (cal + use))), "driving_pct": int(round(100 * use / (cal + use)))}

    name = LABELS[vehicle_type] + (f" - {vehicle_name}" if vehicle_name else "")
    soh_now = {k: round(float(np.clip(_soh(m, age, cycles, b), 0, 100)), 1)
               for k, b in (("low", "q10"), ("mid", "mid"), ("high", "q90"))}
    base = {"vehicle": name, "vehicle_type": vehicle_type, "eol_threshold_pct": m["eol_soh"],
            "age_years": round(age, 1), "km_per_year": km_per_year, "odometer_km": odometer_km,
            "soh_now_pct": soh_now,
            "soh_note": "Estimated from age and charge cycles (not measured); low/high = range of "
                        "similar real cars.",
            "projection": _curve(m, age, cycles, cpy, years),
            "cycles_used": int(round(cycles)), "full_charge_range_km": int(round(full_charge_now)),
            "readings_used": n_readings, "wear_so_far": wear, "notes": notes}

    if yrs["mid"] == 0:
        zero = {"low": 0, "mid": 0, "high": 0}
        return {**base, "rul_years": zero, "rul_km": zero, "rul_cycles": zero,
                "evidence": "moderate", "what_if": None, "sensitivity": None,
                "message": "The battery has likely reached its end-of-life threshold (80% of original capacity)."}

    # What-if: drive 25 % less per year
    _, _, y_less, _ = _project(m, age, odometer_km, full_charge_now, km_per_year * WHAT_IF_FACTOR)
    what_if = None
    if y_less["mid"] is not None and yrs["mid"] is not None:
        what_if = {"km_per_year": int(round(km_per_year * WHAT_IF_FACTOR, -2)),
                   "rul_years_mid": round(y_less["mid"], 1),
                   "change_years": round(y_less["mid"] - yrs["mid"], 1)}

    # Sensitivity: how much a +/-10 % different range reading changes the answer
    sens = []
    for f in (1 - SENSITIVITY, 1 + SENSITIVITY):
        _, _, ys, _ = _project(m, age, odometer_km, full_charge_now * f, km_per_year)
        if ys["mid"] is not None and yrs["mid"] is not None:
            sens.append(abs(ys["mid"] - yrs["mid"]))
    sensitivity = {"range_error_pct": int(SENSITIVITY * 100),
                   "rul_years_change": round(max(sens), 1)} if sens else None

    # Evidence = how well the data supports this prediction (not how narrow the range is):
    #   limited  - 2W/3W (no real health data) or outside the real-car training range
    #   moderate - car inside the training range
    #   strong   - additionally: later life (SoH <= 90 %, where the NASA check showed
    #              ~7-cycle accuracy) and readings taken at SOC >= 50 %
    # (Reading noise moves RUL by well under a year, far less than the spread between
    #  batteries, so one good reading is enough.)
    if out_of_range or vehicle_type != "4W":
        evidence = "limited"
    elif soh <= EARLY_LIFE_SOH and min_soc >= RECOMMENDED_SOC:
        evidence = "strong"
    else:
        evidence = "moderate"
    notes.append("The range shows where 80% of similar batteries would fall: batteries of the same "
                 "age and mileage age at different speeds.")

    if yrs["mid"] is None:
        msg = f"More than {MAX_YEARS:.0f} years of battery life left at your current driving."
    else:
        msg = (f"About {rul_years['mid']} years (~{rul_km['mid']:,} km, ~{rul_cycles['mid']:,} charge cycles) "
               f"left before the battery reaches {m['eol_soh']:.0f}% of its original capacity.")

    return {**base, "rul_years": rul_years, "rul_km": rul_km, "rul_cycles": rul_cycles,
            "evidence": evidence, "what_if": what_if, "sensitivity": sensitivity, "message": msg}


if __name__ == "__main__":
    cases = [
        ("Car 2021, 60k km, 1 reading", dict(vehicle_type="4W", registration_year=2021,
         odometer_km=60000, km_per_year=12000, readings=[(60, 170)])),
        ("Car 2021, 60k km, 3 readings", dict(vehicle_type="4W", registration_year=2021,
         odometer_km=60000, km_per_year=12000, readings=[(60, 170), (85, 245), (70, 196)])),
        ("Car 2018, only 5k km", dict(vehicle_type="4W", registration_year=2018, odometer_km=5000,
         km_per_year=3000, readings=[(80, 240)])),
        ("Car 2017, 150k km", dict(vehicle_type="4W", registration_year=2017, odometer_km=150000,
         km_per_year=15000, readings=[(80, 210), (90, 230)])),
        ("Taxi 2024, 120k km", dict(vehicle_type="4W", registration_year=2024, odometer_km=120000,
         km_per_year=50000, readings=[(70, 200)])),
        ("2W 2022, 25k km", dict(vehicle_type="2W", registration_year=2022, odometer_km=25000,
         km_per_year=9000, readings=[(70, 65)], vehicle_name="Ather 450X")),
        ("3W 2021, 110k km", dict(vehicle_type="3W", registration_year=2021, odometer_km=110000,
         km_per_year=28000, readings=[(80, 85)])),
    ]
    for label, c in cases:
        r = predict_rul(**c)
        print(f"\n{label}\n  {r['message']}\n  years {r['rul_years']} | evidence {r['evidence']} | "
              f"wear {r['wear_so_far']} | what-if {r['what_if']} | sens {r['sensitivity']}")
