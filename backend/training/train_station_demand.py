"""
Station-level demand and waiting time (used by "Plan my charge").

Data: City of Palo Alto ChargePoint sessions ("ChargePoint Data CY20Q4.csv"): every session's station,
port, plug-in time, plug-out time and energy. 47 stations with 1-2 ports each (Level 1 / Level 2 AC).

What is learned (training year 2018):
    arrivals per port per hour       lambda_port[weekday][hour]   (sessions starting / port / hour)
    time a car stays plugged in      mean connected hours by arrival hour  (blocks the port for others)
    energy per session               median kWh (used to estimate DC fast-charger session length)

Queueing model for a station with c charging points at a given weekday and hour:
    lambda = lambda_port[weekday][hour] x c x demand scale (cars arriving per hour)
    service time S = mean connected hours (AC); for DC fast chargers S = energy / (0.8 x power) + 0.15 h
    offered load a(h) = sum over earlier hours k of lambda(h-k) x c x G(k), where G(k) = average time a car
                        that arrived k hours ago is still plugged in during this hour (from real sessions)
    utilisation rho = a / c
    P(all points busy on arrival) = Erlang C(c, a)        (M/M/c queue)
    expected wait Wq = P(wait) x S / (c - a)              (if rho < 1; otherwise "long")
    station demand level: rho < 0.35 low, < 0.65 medium, otherwise high

Validation (test year 2019, never used for fitting): for every Palo Alto station x weekday x hour,
compare predicted busy points and P(all points busy) with what actually happened (port occupancy
sampled every 10 minutes from real plug-in / plug-out times).

Outputs: models/station_demand.json, models/metrics_station_demand.json
Run from the backend folder:  python training/train_station_demand.py
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parent.parent
RAW = BACKEND / "data" / "raw" / "ChargePoint Data CY20Q4.csv"
MODELS = BACKEND / "models"
TRAIN_YEAR, TEST_YEAR = 2018, 2019
SAMPLE_MIN = 10


def erlang_c(c, a):
    """Probability an arrival must wait in an M/M/c queue (c servers, offered load a)."""
    if a <= 0:
        return 0.0
    if a >= c:
        return 1.0
    s, term = 0.0, 1.0
    for k in range(c):
        if k > 0:
            term *= a / k
        s += term
    top = (a ** c / math.factorial(c)) * (c / (c - a))
    return top / (s + top)


def load():
    cols = ["Station Name", "Port Number", "Start Date", "End Date", "Energy (kWh)", "Port Type"]
    d = pd.read_csv(RAW, low_memory=False, usecols=cols)
    d["start"] = pd.to_datetime(d["Start Date"], errors="coerce")
    d["end"] = pd.to_datetime(d["End Date"], errors="coerce")
    d = d.dropna(subset=["start", "end"])
    d["hours"] = (d["end"] - d["start"]).dt.total_seconds() / 3600
    d = d[(d["hours"] > 0.05) & (d["hours"] < 24)]
    d["port"] = d["Station Name"] + "#" + d["Port Number"].astype(str)
    return d


def fit(d, year):
    y = d[d["start"].dt.year == year]
    ports = y.groupby("Station Name")["port"].nunique()
    n_ports = int(ports.sum())
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31")
    n_dow = days.dayofweek.value_counts().sort_index().to_numpy()
    counts = pd.crosstab(y["start"].dt.dayofweek, y["start"].dt.hour).reindex(index=range(7), columns=range(24), fill_value=0)
    lam = counts.to_numpy() / (n_ports * n_dow[:, None])                 # arrivals / port / hour
    dur = y.groupby(y["start"].dt.hour)["hours"].mean().reindex(range(24)).interpolate().bfill().ffill()
    return {"lambda_port": lam, "duration_h": dur.to_numpy(), "occupancy_kernel": kernel(y["hours"].to_numpy()), "ports": ports.to_dict(), "n_ports": n_ports,
            "energy_kwh_median": float(y["Energy (kWh)"].median()),
            "daily_sessions_by_weekday": (y.groupby(y["start"].dt.normalize()).size()
                                          .groupby(lambda t: t.dayofweek).mean().reindex(range(7)).to_numpy())}


def kernel(hours):
    """G[k]: expected hours a car occupies a port during the k-th hour after its arrival hour
    (arrival assumed mid-hour). Sum of G = mean connected time."""
    end = 0.5 + hours
    return np.array([float(np.mean(np.clip(np.minimum(end, k + 1) - max(0.5, k), 0, None))) for k in range(24)])


def offered_load(lam, G, c, dow, hour, scale=1.0):
    """Expected busy points at (dow, hour): arrivals in the previous 24 hours still plugged in."""
    a = 0.0
    for k in range(24):
        h = hour - k
        d = (dow + (h // 24)) % 7
        a += lam[d, h % 24] * c * scale * G[k]
    return a


def observed(d, year, ports):
    """Busy ports per station sampled every 10 minutes -> mean busy and P(all busy) by station x dow x hour."""
    y = d[d["start"].dt.year == year]
    t = pd.date_range(f"{year}-01-01", f"{year}-12-31 23:50", freq=f"{SAMPLE_MIN}min")
    tv = t.values.astype("datetime64[m]").astype(np.int64)
    rows = []
    for st, g in y.groupby("Station Name"):
        c = ports.get(st)
        if not c:
            continue
        s = g["start"].values.astype("datetime64[m]").astype(np.int64)
        e = g["end"].values.astype("datetime64[m]").astype(np.int64)
        busy = np.searchsorted(np.sort(s), tv, side="right") - np.searchsorted(np.sort(e), tv, side="right")
        busy = np.clip(busy, 0, c)
        df = pd.DataFrame({"dow": t.dayofweek, "hour": t.hour, "busy": busy, "full": busy >= c})
        agg = df.groupby(["dow", "hour"]).agg(busy=("busy", "mean"), full=("full", "mean")).reset_index()
        agg["station"], agg["c"] = st, c
        rows.append(agg)
    return pd.concat(rows, ignore_index=True)


def predict(m, c, dow, hour):
    a = offered_load(m["lambda_port"], m["occupancy_kernel"], c, dow, hour)
    return min(a, c), erlang_c(c, a)


def main():
    if not RAW.exists():
        raise SystemExit(f"Raw data not found: {RAW}")
    d = load()
    m = fit(d, TRAIN_YEAR)
    obs = observed(d, TEST_YEAR, d[d["start"].dt.year == TEST_YEAR].groupby("Station Name")["port"].nunique().to_dict())
    pb, pf = zip(*[predict(m, int(r.c), int(r.dow), int(r.hour)) for r in obs.itertuples()])
    obs["pred_busy"], obs["pred_full"] = pb, pf
    base_busy = obs["busy"].mean()                                   # baseline: one average for every slot
    mae = lambda a, b: round(float(np.mean(np.abs(np.asarray(a) - np.asarray(b)))), 3)
    corr = round(float(np.corrcoef(obs["pred_busy"], obs["busy"])[0, 1]), 3)
    by_slot = obs.groupby(["dow", "hour"])[["busy", "pred_busy", "full", "pred_full"]].mean()

    final = fit(d, TEST_YEAR)                                        # refit on the latest normal year for use
    out = {
        "fit_year": TEST_YEAR,
        "lambda_port": np.round(final["lambda_port"], 5).tolist(),
        "duration_h": np.round(final["duration_h"], 3).tolist(),
        "occupancy_kernel": np.round(final["occupancy_kernel"], 4).tolist(),
        "mean_connected_h": round(float(np.sum(final["occupancy_kernel"])), 3),
        "energy_kwh_median": round(final["energy_kwh_median"], 2),
        "daily_sessions_by_weekday": np.round(final["daily_sessions_by_weekday"], 1).tolist(),
        "dc_efficiency": 0.8, "dc_overhead_h": 0.15,
        "level_thresholds": {"low_below": 0.35, "high_above": 0.65},
    }
    MODELS.mkdir(exist_ok=True)
    (MODELS / "station_demand.json").write_text(json.dumps(out))

    metrics = {
        "dataset": "City of Palo Alto ChargePoint sessions - per-port plug-in / plug-out times",
        "method": "Per-port arrival rate (weekday x hour) and how long cars stay plugged in, learned from data; "
                  "occupancy carried over from earlier arrivals; M/M/c queue (Erlang C) per station",
        "train_year": TRAIN_YEAR, "test_year": TEST_YEAR,
        "stations_tested": int(obs["station"].nunique()), "station_hour_slots": int(len(obs)),
        "busy_points_mae": mae(obs["pred_busy"], obs["busy"]),
        "busy_points_mae_baseline_average": mae(base_busy, obs["busy"]),
        "busy_points_correlation": corr,
        "p_all_busy_mae": mae(obs["pred_full"], obs["full"]),
        "p_all_busy_mae_baseline_average": mae(obs["full"].mean(), obs["full"]),
        "mean_observed_p_all_busy": round(float(obs["full"].mean()), 3),
        "mean_predicted_p_all_busy": round(float(obs["pred_full"].mean()), 3),
        "busiest_slot_observed": {"weekday": int(by_slot["busy"].idxmax()[0]), "hour": int(by_slot["busy"].idxmax()[1])},
        "busiest_slot_predicted": {"weekday": int(by_slot["pred_busy"].idxmax()[0]), "hour": int(by_slot["pred_busy"].idxmax()[1])},
        "mean_connected_hours": round(float(np.sum(final["occupancy_kernel"])), 2),
        "energy_kwh_median": round(final["energy_kwh_median"], 2),
        "limitations": [
            "Learned from Palo Alto Level 1/2 AC chargers; applied to other cities and to DC fast chargers by assumption.",
            "DC fast-charger session length is estimated from energy per session and charger power, not observed.",
            "No live occupancy: predictions describe a typical day at that weekday and hour.",
            "Observed data only contains drivers who found a free port, so real queues may be under-counted.",
        ],
    }
    (MODELS / "metrics_station_demand.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
