"""
Feature 2 - Battery anomaly detection (owner inputs only).

Inputs: the same fields as the RUL form. Battery health needs a reference, in order of preference:
    1. the full-charge range shown when the vehicle was new (optional - many owners do not know it), or
    2. the owner's own earlier readings saved by this app (at least 30 days old).
Without a reference (first check), a second Isolation Forest on age + km per year checks the usage, the readings
are checked for consistency, and today's readings are saved as the reference for later checks.

Derived measurements
    age_years    years since registration
    km_per_year  odometer / age (actual average use)
    health_pct   with range when new: full-charge range now / range when new x 100
                 with own history:    typical health at the first reading (ageing model) x
                                      full-charge range now / full-charge range then
                 (full-charge range = range shown / SOC, averaged over the readings)
Also flagged: a sudden drop (> 15 % below the previous reading) and a decline more than 5 points faster
than the ageing model expects since the first reading.

Isolation Forest (training/train_anomaly.py, 2,724 real used cars) scores the three values together:
    anomalous - score < 0
    warning   - score below the 5th percentile of normal cars, or the readings disagree by > 15 %
    normal    - otherwise
Each measurement is also compared with the 10th-90th percentile of real cars of the same age,
so the alert shows which values are unusual.
"""
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from app.rul_service import FULL_CHARGE_KM, LABELS, MIN_SOC, _model as _rul_model, _project, vehicle_age

BACKEND = Path(__file__).resolve().parent.parent
MODEL = BACKEND / "models" / "anomaly_model.joblib"
METRICS = BACKEND / "models" / "metrics_anomaly.json"
SAMPLES = BACKEND / "data" / "anomaly_samples.json"
READING_SPREAD = 0.15


@lru_cache(maxsize=1)
def _bundle():
    return joblib.load(MODEL)


@lru_cache(maxsize=1)
def samples():
    return json.loads(SAMPLES.read_text())


def info():
    return json.loads(METRICS.read_text()) if METRICS.exists() else {}


def _typical(age):
    groups = _bundle()["typical_by_age"]
    g = min(groups, key=lambda r: abs((r["age_from"] + r["age_to"]) / 2 - age))
    return g


def _position(value, lo, hi):
    if value < lo:
        return "below_typical"
    if value > hi:
        return "above_typical"
    return "typical"


TRIP_MIN_USED = 10.0          # % SOC that must be used for a valid trip
TRIP_WARN = 20.0              # delivered range more than 20 % below promised -> warning
TRIP_ANOM = 35.0              # ... more than 35 % below -> anomalous
TRIP_OVER = 20.0              # delivered more than 20 % ABOVE promised -> estimate unreliable (warning)
SEVERITY = {"normal": 0, "baseline_saved": 0, "warning": 1, "anomalous": 2}
MIN_HISTORY_DAYS = 30        # an earlier reading must be at least this old to measure wear
SUDDEN_DROP = 0.15           # >15 % lower than a reading from the last 90 days -> warning
EXCESS_DECLINE_PTS = 5.0     # measured decline this much faster than expected -> alert


def _baseline(history, today):
    """Earliest saved readings (median of the first 30 days of history), if at least 30 days old."""
    if not history:
        return None
    h = pd.DataFrame(history)
    h["reading_date"] = pd.to_datetime(h["reading_date"])
    first = h["reading_date"].min()
    if (pd.Timestamp(today) - first).days < MIN_HISTORY_DAYS:
        return None
    win = h[h["reading_date"] <= first + pd.Timedelta(days=MIN_HISTORY_DAYS)]
    return {"date": win["reading_date"].median().date(), "odometer_km": float(win["odometer_km"].median()),
            "full_charge_km": float(win["full_charge_km"].median()), "readings": int(len(win))}


def _detect_core(vehicle_type, registration_year, odometer_km, km_per_year, readings, new_full_range_km=None,
                 vehicle_name=None, history=None, today=None):
    """readings: list of (soc_pct, range_km) taken today.
    The health reference is, in order of preference:
      1. new_full_range_km - the full-charge range shown when the vehicle was new (optional), or
      2. the owner's own earlier readings saved by this app (history), at least 30 days old.
    Without either, battery health cannot be measured yet: only the readings and the usage are checked,
    and today's readings become the reference for future checks."""
    if vehicle_type not in LABELS:
        raise ValueError("vehicle_type must be '2W', '3W' or '4W'.")
    if odometer_km <= 0 or km_per_year <= 0:
        raise ValueError("Odometer and distance per year must be greater than 0.")
    if not readings or len(readings) > 3:
        raise ValueError("Enter 1 to 3 dashboard readings.")
    for i, (soc, rng) in enumerate(readings, 1):
        if not MIN_SOC <= soc <= 100:
            raise ValueError(f"Reading {i}: battery % (SOC) must be between {MIN_SOC:.0f} and 100.")
        if rng <= 0:
            raise ValueError(f"Reading {i}: range shown must be greater than 0.")
    lo_km, hi_km = FULL_CHARGE_KM[vehicle_type]
    if new_full_range_km is not None and not lo_km <= new_full_range_km <= hi_km:
        raise ValueError(f"Range when new of {new_full_range_km:.0f} km is unusual for this vehicle type "
                         f"({lo_km}-{hi_km} km). Please re-check it.")

    today = today or date.today()
    b = _bundle()
    m = _rul_model()
    age = vehicle_age(registration_year, today)
    actual_kmpy = odometer_km / max(age, 0.25)
    per_reading = [rng / (soc / 100) for soc, rng in readings]
    full_now = float(np.mean(per_reading))
    median = float(np.median(per_reading))
    history = [r for r in (history or []) if r["odometer_km"] <= odometer_km + 1]

    alerts, status_floor = [], "normal"
    reading_rows = []
    for i, ((soc, rng), full) in enumerate(zip(readings, per_reading), 1):
        dev = (full - median) / median
        flag = abs(dev) > READING_SPREAD
        reading_rows.append({"reading": i, "soc_pct": soc, "range_km": rng,
                             "full_charge_range_km": round(full, 1), "deviation_pct": round(dev * 100, 1),
                             "flag": "inconsistent" if flag else "ok"})
    if any(r["flag"] != "ok" for r in reading_rows):
        status_floor = "warning"
        alerts.append("The dashboard readings disagree by more than 15%. The range estimate depends on recent "
                      "driving, so re-take readings on similar days before drawing conclusions.")

    # sudden drop compared with the most recent earlier reading, if it is from the last 90 days
    recent = [r for r in history if (pd.Timestamp(today) - pd.Timestamp(r["reading_date"])).days <= 90]
    if recent:
        last = recent[-1]
        drop = (last["full_charge_km"] - full_now) / last["full_charge_km"]
        if drop > SUDDEN_DROP:
            status_floor = "warning"
            alerts.append(f"Full-charge range is {drop * 100:.0f}% lower than your reading on {last['reading_date']} "
                          f"({last['full_charge_km']:.0f} km). A drop this quick is unusual – re-check on a normal day; "
                          "if it stays low, have the battery checked.")

    # ---- health reference
    reference, trend = None, None
    if new_full_range_km:
        health = full_now / new_full_range_km * 100
        reference = {"type": "range_when_new", "label": f"Full-charge range when new: {new_full_range_km:.0f} km",
                     "full_charge_km": new_full_range_km}
    else:
        base = _baseline(history, today)
        if base:
            # health at the baseline date is taken as typical for the vehicle's age and use then;
            # health now = that x (full-charge range now / full-charge range then)
            age0 = vehicle_age(registration_year, base["date"])
            _, soh0, _, _ = _project(m, age0, base["odometer_km"], base["full_charge_km"], km_per_year)
            _, soh_now_typ, _, _ = _project(m, age, odometer_km, full_now, km_per_year)
            measured_ratio = full_now / base["full_charge_km"]
            expected_ratio = soh_now_typ / soh0
            health = soh0 * measured_ratio
            months = (pd.Timestamp(today) - pd.Timestamp(base["date"])).days / 30.4
            trend = {"since": str(base["date"]), "months": round(months, 1),
                     "km_driven": int(round(odometer_km - base["odometer_km"])),
                     "range_then_km": round(base["full_charge_km"], 1), "range_now_km": round(full_now, 1),
                     "measured_change_pct": round((measured_ratio - 1) * 100, 1),
                     "expected_change_pct": round((expected_ratio - 1) * 100, 1)}
            reference = {"type": "own_history", "label": f"Your readings from {base['date']} "
                                                         f"({base['full_charge_km']:.0f} km full-charge range)",
                         "full_charge_km": base["full_charge_km"], "health_at_reference_pct": round(soh0, 1)}
            excess = (expected_ratio - measured_ratio) * 100
            if excess > EXCESS_DECLINE_PTS:
                status_floor = "warning"
                alerts.append(f"Since {base['date']} your full-charge range fell {abs(trend['measured_change_pct']):.1f}%, "
                              f"while about {abs(trend['expected_change_pct']):.1f}% is expected for this age and use.")
        else:
            health = None

    t = _typical(age)
    usage = {"field": "km_per_year", "label": "Distance per year (odometer ÷ age)", "unit": "km",
             "value": int(round(actual_kmpy, -2)),
             "typical_low": t["km_per_year_p10"], "typical_mid": t["km_per_year_p50"], "typical_high": t["km_per_year_p90"]}
    usage["position"] = _position(usage["value"], usage["typical_low"], usage["typical_high"])
    if usage["position"] == "above_typical":
        alerts.append(f"Distance per year ({usage['value']:,} km) is higher than 90% of real cars of this age.")
    if abs(actual_kmpy - km_per_year) / km_per_year > 0.5:
        alerts.append(f"Your stated {km_per_year:,.0f} km per year differs a lot from the odometer average "
                      f"({actual_kmpy:,.0f} km per year); check both values.")
    age_row = {"field": "age_years", "label": "Vehicle age", "unit": "years", "value": round(age, 1),
               "typical_low": None, "typical_mid": None, "typical_high": None, "position": "input"}
    base_out = {"vehicle": LABELS[vehicle_type] + (f" - {vehicle_name}" if vehicle_name else ""),
                "readings": reading_rows, "reference": reference, "trend": trend,
                "history_count": len(history), "compared_with": f"{t['cars']} real cars aged {t['age_from']}-{t['age_to']} years"}

    # ---- no reference yet: usage (Isolation Forest on age + km per year) and readings only
    if health is None:
        ux = pd.DataFrame([{"age_years": age, "km_per_year": actual_kmpy}])[b["usage_features"]]
        uscore = float(b["usage_model"].decision_function(ux)[0])
        status = "anomalous" if uscore < 0 else "warning" if uscore < b["usage_warn_score"] else "normal"
        if status == "normal" and status_floor == "warning":
            status = "warning"
        _, soh_typ, _, _ = _project(m, age, odometer_km, full_now, km_per_year)
        expected = {"field": "expected_health_pct", "label": "Expected battery health for this age and use (not measured)",
                    "unit": "%", "value": round(soh_typ, 1), "typical_low": t["health_pct_p10"],
                    "typical_mid": t["health_pct_p50"], "typical_high": t["health_pct_p90"], "position": "input"}
        first = history[0]["reading_date"] if history else None
        pending = (f"Battery health will be measured from your next check after {MIN_HISTORY_DAYS} days "
                   f"(today's readings are saved as the reference), or now if you enter the range when new."
                   if not first else
                   f"Your earliest saved reading ({first}) is less than {MIN_HISTORY_DAYS} days old; battery health "
                   "will be measured once it is older, or now if you enter the range when new.")
        head = {"normal": "Usage and dashboard readings are normal for a vehicle of this age.",
                "warning": "Some usage or reading values are unusual – see the alerts.",
                "anomalous": "This vehicle's usage is unusual compared with real vehicles of the same age."}[status]
        return {**base_out, "status": status, "health_checked": False, "message": f"{head} {pending}",
                "anomaly_score": round(uscore, 4), "warning_threshold": round(b["usage_warn_score"], 4),
                "score_note": "Usage Isolation Forest (age + km per year): below 0 = anomalous, below the warning "
                              "threshold = warning.",
                "measurements": [usage, age_row, expected], "alerts": alerts,
                "notes": ["Battery health needs a reference: the range when new, or your own reading from at least a "
                          "month ago. Until then the check covers usage and the dashboard readings.",
                          "Expected health comes from the ageing model for this age and use; it is not a measurement."]}

    if health > 105:
        return {**base_out, "status": "check_inputs", "measurements": [], "alerts": alerts,
                "message": "The current full-charge range is well above the reference. Please re-check the readings."}

    x = pd.DataFrame([{"age_years": age, "km_per_year": actual_kmpy, "health_pct": min(health, 100.0)}])[b["features"]]
    score = float(b["model"].decision_function(x)[0])
    status = "anomalous" if score < 0 else "warning" if score < b["warn_score"] else "normal"
    if status == "normal" and status_floor == "warning":
        status = "warning"

    ref_km = reference["full_charge_km"] / ((reference.get("health_at_reference_pct") or 100) / 100)
    hrow = {"field": "health_pct", "label": "Battery health from range", "unit": "%", "value": round(min(health, 100.0), 1),
            "typical_low": t["health_pct_p10"], "typical_mid": t["health_pct_p50"], "typical_high": t["health_pct_p90"]}
    rrow = {"field": "full_charge_range_km", "label": "Full-charge range now (range ÷ SOC)", "unit": "km",
            "value": round(full_now, 1), "typical_low": round(ref_km * t["health_pct_p10"] / 100, 1),
            "typical_mid": round(ref_km * t["health_pct_p50"] / 100, 1), "typical_high": round(ref_km * t["health_pct_p90"] / 100, 1)}
    for mm in (hrow, rrow):
        mm["position"] = _position(mm["value"], mm["typical_low"], mm["typical_high"])
    if hrow["position"] == "below_typical":
        alerts.append(f"Battery health from range ({hrow['value']}%) is lower than about 90% of real cars of this age "
                      f"(typical {hrow['typical_low']}–{hrow['typical_high']}%).")

    messages = {
        "normal": "Battery measurements are within the normal pattern for vehicles of this age and use.",
        "warning": "Some measurements are at the edge of the normal pattern. Re-check the readings; if it "
                   "persists, keep an eye on the range over the next months.",
        "anomalous": "This combination of age, use and battery health is unusual compared with real vehicles. "
                     "Consider a battery health check at a service centre.",
    }
    notes = ["Health here comes from the dashboard range, which also changes with temperature, AC use and "
             "recent driving style."]
    if reference["type"] == "own_history":
        notes.append("Health from your own history assumes the battery was typical for its age at your first reading.")
    if vehicle_type != "4W":
        notes.append("The detector was trained on cars; for 2- and 3-wheelers treat the result as indicative.")
    if age > b["train_age_max"]:
        notes.append("This vehicle is older than any car in the training data.")
    return {**base_out, "status": status, "health_checked": True, "message": messages[status],
            "anomaly_score": round(score, 4), "warning_threshold": round(b["warn_score"], 4),
            "score_note": "Isolation Forest score: below 0 = anomalous, below the warning threshold = warning.",
            "measurements": [hrow, usage, rrow, age_row], "alerts": alerts, "notes": notes}


# ------------------------------------------------------------------ trip check (discharge behaviour)
def trip_check(vehicle_type, soc_start, range_start_km, soc_end, distance_km, ride_mode=None, previous=None):
    """Compare the range the dashboard promised at the start of a ride with the range the battery delivered.
        promised full-charge range  = range shown at start / (SOC at start / 100)
        delivered full-charge range = km ridden / ((SOC start - SOC end) / 100)
        gap % = (promised - delivered) / promised x 100
    Status: gap <= 20 % normal, 20-35 % warning, > 35 % anomalous; delivered > promised by over 20 % = warning
    (the battery's estimate is unreliable). Two of the last three trips over a threshold escalate the status."""
    if not MIN_SOC <= soc_start <= 100:
        raise ValueError(f"Trip: battery % at start must be between {MIN_SOC:.0f} and 100.")
    if not 0 <= soc_end < soc_start:
        raise ValueError("Trip: battery % at the end must be lower than at the start.")
    used = soc_start - soc_end
    if used < TRIP_MIN_USED:
        raise ValueError(f"Trip: at least {TRIP_MIN_USED:.0f}% of battery must be used for a reliable check "
                         f"(this trip used {used:.0f}%). Try a longer ride.")
    if range_start_km <= 0 or distance_km <= 0:
        raise ValueError("Trip: range shown at start and distance ridden must be greater than 0.")
    promised = range_start_km / (soc_start / 100)
    delivered = distance_km / (used / 100)
    lo, hi = FULL_CHARGE_KM[vehicle_type]
    if not lo * 0.4 <= delivered <= hi * 1.5:
        raise ValueError(f"Trip: {distance_km:g} km on {used:.0f}% of battery means about {delivered:.0f} km per full "
                         f"charge, which is not possible for this vehicle type. Please re-check the numbers.")
    gap = (promised - delivered) / promised * 100
    if gap > TRIP_ANOM:
        status, msg = "anomalous", (f"The battery delivered only {delivered:.0f} km per full charge against "
                                    f"{promised:.0f} km promised by the dashboard ({gap:.0f}% less). It is draining much "
                                    "faster than its own estimate – have the battery checked.")
    elif gap > TRIP_WARN:
        status, msg = "warning", (f"The battery delivered {delivered:.0f} km per full charge against {promised:.0f} km "
                                  f"promised ({gap:.0f}% less). Repeat the check on a normal ride in the same mode.")
    elif gap < -TRIP_OVER:
        status, msg = "warning", (f"The battery delivered {delivered:.0f} km per full charge, {abs(gap):.0f}% more than the "
                                  f"{promised:.0f} km promised: the dashboard's estimate is unreliable (calibration).")
    else:
        status, msg = "normal", (f"The battery delivered {delivered:.0f} km per full charge against {promised:.0f} km "
                                 f"promised ({gap:+.0f}%) – it behaves as its estimate expects.")
    recent = [t["gap_pct"] for t in (previous or [])][-2:] + [gap]
    repeated = None
    if len(recent) >= 2:
        if sum(g > TRIP_ANOM for g in recent) >= 2:
            repeated = "anomalous"
        elif sum(g > TRIP_WARN for g in recent) >= 2:
            repeated = "warning"
        if repeated:
            if SEVERITY[repeated] > SEVERITY[status]:
                status = repeated
            msg += f" {sum(g > TRIP_WARN for g in recent)} of your last {len(recent)} trips show the same pattern."
    return {"status": status, "message": msg, "soc_start": soc_start, "range_start_km": range_start_km,
            "soc_end": soc_end, "distance_km": distance_km, "ride_mode": ride_mode, "battery_used_pct": round(used, 1),
            "promised_km": round(promised, 1), "delivered_km": round(delivered, 1), "gap_pct": round(gap, 1),
            "recent_gaps_pct": [round(g, 1) for g in recent], "thresholds": {"warning_above_pct": TRIP_WARN,
            "anomalous_above_pct": TRIP_ANOM, "estimate_unreliable_below_pct": -TRIP_OVER}}


def detect(vehicle_type, registration_year, odometer_km, km_per_year, readings, new_full_range_km=None,
           vehicle_name=None, history=None, today=None, trip=None, trip_history=None):
    """Battery behaviour check combining four checks:
        1 discharge behaviour (trip: promised vs delivered range)       - optional, works on the first use
        2 reading consistency (the 1-3 dashboard readings agree)
        3 capacity fade (health vs range when new or own history; Isolation Forest on age, use, health)
        4 usage pattern (age and km per year vs real vehicles)
    The overall status is the most severe of the checks."""
    r = _detect_core(vehicle_type, registration_year, odometer_km, km_per_year, readings, new_full_range_km,
                     vehicle_name, history, today)
    t = None
    if trip:
        t = trip_check(vehicle_type, trip["soc_start"], trip["range_start_km"], trip["soc_end"], trip["distance_km"],
                       trip.get("ride_mode"), trip_history)
    elif trip_history:
        last = trip_history[-1]
        t = {**trip_check(vehicle_type, last["soc_start"], last["range_start_km"], last["soc_end"], last["distance_km"],
                          last.get("ride_mode"), trip_history[:-1]), "from_saved_trip": last["trip_date"]}

    readings_bad = any(x["flag"] != "ok" for x in r.get("readings", []))
    checks = [{"check": "Discharge behaviour (trip)",
               "status": t["status"] if t else "not_checked",
               "detail": t["message"] if t else "Not checked – add a trip (battery % before and after a ride, and km ridden)."},
              {"check": "Reading consistency",
               "status": "warning" if readings_bad else ("normal" if len(r.get("readings", [])) > 1 else "not_checked"),
               "detail": ("The readings disagree by more than 15%." if readings_bad else
                          "The readings agree." if len(r.get("readings", [])) > 1 else "Only one reading – add more to check.")},
              {"check": "Capacity fade (battery health)",
               "status": r["status"] if r.get("health_checked") else "not_checked",
               "detail": (r["reference"]["label"] if r.get("reference") else
                          "Not checked yet – needs the range when new or a saved reading 30+ days old.")},
              {"check": "Usage pattern",
               "status": r["status"] if not r.get("health_checked") and r["status"] in SEVERITY else "normal",
               "detail": next((f"{m['value']:,} km per year (typical {m['typical_low']:,.0f}–{m['typical_high']:,.0f})"
                               for m in r.get("measurements", []) if m["field"] == "km_per_year"), "")}]
    r["checks"] = checks
    r["trip"] = t
    if t and r["status"] in SEVERITY and SEVERITY[t["status"]] > SEVERITY[r["status"]]:
        r["status"] = t["status"]
        r["message"] = t["message"]
    elif t and r["status"] in SEVERITY:
        r["message"] = t["message"] + " " + r["message"]
    if t and t["status"] != "normal":
        r["alerts"] = [t["message"]] + r.get("alerts", [])
    return r
