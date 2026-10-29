"""
Station-level demand and waiting time (workflow step 4, per station).

Model learned by training/train_station_demand.py from Palo Alto ChargePoint sessions:
    lambda_port[weekday][hour]  arrivals per charging point per hour
    occupancy_kernel G[k]       hours a car stays plugged in during the k-th hour after arriving
For a station with c points at a weekday and hour:
    expected busy points a = sum_k lambda(h-k) x c x scale x G[k]
    P(all busy on arrival) = Erlang C(c, a);  expected wait = P x S / (c - a)
DC fast chargers (> 22 kW): a session lasts energy / (0.8 x power) + 0.15 h (estimate), so G is recomputed.

Demand scenarios (the scale):
    expected  - the day's expected sessions (demand forecast) / average 2019 sessions for that weekday
    busy      - a busy normal-year day: 90th percentile of 2019 daily sessions / 2019 average
    latest    - the latest-data (post-COVID) forecast for that weekday / 2019 average
"""
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parent.parent
MODEL = BACKEND / "models" / "station_demand.json"
METRICS = BACKEND / "models" / "metrics_station_demand.json"
DAILY = BACKEND / "data" / "daily_demand.csv"
DC_KW = 22.0
SCENARIOS = {"expected": "Expected for the chosen day (normal-year pattern)",
             "busy": "Busy day (top 10% of a normal year)",
             "latest": "Latest data (post-COVID, much lower)"}


@lru_cache(maxsize=1)
def _m():
    m = json.loads(MODEL.read_text())
    m["lambda_port"] = np.array(m["lambda_port"])
    m["occupancy_kernel"] = np.array(m["occupancy_kernel"])
    return m


def info():
    return json.loads(METRICS.read_text()) if METRICS.exists() else {}


def erlang_c(c, a):
    if a <= 0:
        return 0.0
    if a >= c:
        return 1.0
    s = sum(a ** k / math.factorial(k) for k in range(c))
    top = a ** c / math.factorial(c) * c / (c - a)
    return top / (s + top)


@lru_cache(maxsize=1)
def _busy_scale():
    d = pd.read_csv(DAILY, parse_dates=["date"])
    y = d[d["date"].dt.year == _m()["fit_year"]]["demand"]
    return float(y.quantile(0.9) / y.mean())


def scale_for(scenario, forecast_day, latest_days=None):
    """forecast_day: dict with 'date' and 'predicted_sessions' (expected demand for that date).
    The scale compares the day's demand with an average day of the same weekday in the fitted year."""
    dow = pd.Timestamp(forecast_day["date"]).dayofweek
    base = _m()["daily_sessions_by_weekday"][dow]
    if scenario == "busy":
        return round(_busy_scale(), 3)
    if scenario == "latest" and latest_days:
        same = [d for d in latest_days if pd.Timestamp(d["date"]).dayofweek == dow] or latest_days
        return round(same[0]["predicted_sessions"] / base, 3)
    return round(forecast_day["predicted_sessions"] / base, 3)


def _kernel(power_kw):
    m = _m()
    if power_kw and power_kw > DC_KW:
        dur = m["energy_kwh_median"] / (m["dc_efficiency"] * power_kw) + m["dc_overhead_h"]
        end = 0.5 + dur
        return np.array([max(0.0, min(end, k + 1) - max(0.5, k)) for k in range(24)]), float(dur), "DC fast (estimated session length)"
    return m["occupancy_kernel"], float(m["mean_connected_h"]), "AC (observed plug-in times)"


def station_demand(points, power_kw, weekday, hour, scale):
    m = _m()
    assumed = not points or points < 1
    c = 1 if assumed else int(points)
    G, service_h, basis = _kernel(power_kw)
    lam = m["lambda_port"]
    a = 0.0
    for k in range(24):
        h = hour - k
        d = (weekday + (h // 24)) % 7
        a += lam[d, h % 24] * c * scale * G[k]
    a = float(a)
    rho = a / c
    p_wait = erlang_c(c, a)
    t = m["level_thresholds"]
    level = "low" if rho < t["low_below"] else "high" if rho > t["high_above"] else "medium"
    if a >= c:
        wait_min, wait_label = None, "Long – station likely full"
    else:
        wait_min = p_wait * service_h / (c - a) * 60
        wait_label = "No wait expected" if wait_min < 1 else f"~{wait_min:.0f} min"
    return {"points_used": c, "points_assumed": assumed, "expected_busy_points": round(min(a, c), 2),
            "utilisation_pct": round(min(rho, 1) * 100, 1), "level": level,
            "p_all_busy_pct": round(p_wait * 100, 1),
            "expected_wait_min": None if wait_min is None else round(wait_min, 1), "wait_label": wait_label,
            "session_hours": round(service_h, 2), "basis": basis}


def live_demand(live, power_kw, model_now, model_at_time, is_now, weekday, hour, scale):
    """Combine live availability with the model.
    live: parsed Google availability (count, available, out_of_service).
    is_now: the chosen time is the current hour today -> use live values directly.
    Otherwise the model's forecast for the chosen time is scaled by how busy the station is now compared
    with what the model expected now (clipped to x0.5-x2): "forecast adjusted to live occupancy"."""
    t = _m()["level_thresholds"]
    count, avail, oos = live["connector_count"], live["available"], live["out_of_service"]
    usable = max(count - oos, 0)
    if usable == 0:
        return {**model_at_time, "source": "live", "level": "high", "utilisation_pct": 100.0, "p_all_busy_pct": 100.0,
                "expected_wait_min": None, "wait_label": "All points out of service (live)",
                "live_available": 0, "live_count": count, "live_out_of_service": oos}
    in_use = max(usable - avail, 0)
    rho_live = in_use / usable
    if is_now:
        level = "low" if rho_live < t["low_below"] else "high" if rho_live > t["high_above"] else "medium"
        _, service_h, basis = _kernel(power_kw)
        queue = live.get("queue") or 0
        if avail > 0:
            wait, label = 0.0, "No wait – free point now (live)"
        elif queue:
            wait = (queue + 1) * service_h / usable * 60     # cars ahead of you + the first free point
            label = f"~{wait:.0f} min – {queue} waiting (live)"
        else:
            wait = service_h / usable * 60          # time until the first of the busy points frees up
            label = f"~{wait:.0f} min – all points busy (live)"
        return {"points_used": usable, "points_assumed": False, "expected_busy_points": float(in_use),
                "utilisation_pct": round(rho_live * 100, 1), "level": level,
                "p_all_busy_pct": 100.0 if avail == 0 else 0.0, "expected_wait_min": round(wait, 1),
                "wait_label": label, "session_hours": round(service_h, 2), "basis": basis, "source": "live",
                "live_available": avail, "live_count": count, "live_out_of_service": oos}
    rho_model_now = model_now["utilisation_pct"] / 100
    factor = min(max((rho_live + 0.05) / (rho_model_now + 0.05), 0.5), 2.0)
    adj = station_demand(usable, power_kw, weekday, hour, scale * factor)
    return {**adj, "source": "live-adjusted", "live_factor": round(factor, 2), "live_available": avail,
            "live_count": count, "live_out_of_service": oos}


def arrival_profile(power_kw, weekday, scale=1.0):
    """Arrivals per charging point for each hour of one weekday and the mean session length - used by the
    charger simulator so simulated chargers behave like the learned model (times an optional demand factor)."""
    m = _m()
    _, service_h, basis = _kernel(power_kw)
    return {"arrivals_per_point_per_hour": [round(float(m["lambda_port"][weekday, h]) * scale, 5) for h in range(24)],
            "session_hours": round(service_h, 3), "basis": basis}
