"""
Evaluation of station recommendation - does it beat "go to the nearest station"?

A day in a simulated city is replayed many times. Drivers appear over the day (hourly pattern of the charging
data, data/hourly_profile.csv), each needing a charge, and choose a station with one of three strategies:

    A  Nearest station                 - what a driver does without the system (baseline)
    B  Recommender, forecast only      - this project's balanced scoring; waiting time from the station demand
                                         model (queueing model), no live data
    C  Recommender, live availability  - the same scoring, with each station's live state at decision time
                                         (free points / queue), as the app does with the charger feed

The scoring is the app's own (plan_service.PREFERENCES["balanced"]: distance 25 %, price 20 %, wait 15 %,
power 10 %, connector / status / battery 10 % each), the waiting time the app's formulas (station_demand_service)
and the price the app's rules (station tariff x time-of-day factor x demand factor).

City: 60 stations in a 24 x 24 km area, 60 % of them clustered in 3 hotspots (as charging stations cluster at malls,
IT parks and highways), with 1-6 points, 7.4-120 kW (car chargers) and tariffs Rs 12-24/kWh by type. Drivers
(cars): 60 % start near the hotspots, need 8-25 kWh, drive at 20 km/h with a road factor of 1.3, and only consider
stations within 10 km.
At a station, a driver charges at once if a point is free, otherwise queues (first come, first served).
Two scenarios: normal day (300 drivers) and busy day (450 drivers); 20 random days each.

Run from the backend folder:  python -m training.evaluate_recommender
Writes models/metrics_recommender_eval.json and models/recommender_eval.png.
Limitations: a synthetic city, not real Indian traffic or stations; FIFO is applied in order of decision.
"""
import heapq
import json
import math
import random
import statistics
from pathlib import Path

import pandas as pd

from app import station_demand_service as sds
from app.plan_service import DEMAND_FACTOR, PREFERENCES
from app.pricing_service import time_band
from app.recommendation_engine import normalize_distance, normalize_power

BACKEND = Path(__file__).resolve().parent.parent
OUT_JSON = BACKEND / "models" / "metrics_recommender_eval.json"
OUT_PNG = BACKEND / "models" / "recommender_eval.png"
AREA_KM, N_STATIONS, MAX_KM = 24.0, 60, 10.0
SPEED_KMH, ROAD = 20.0, 1.3
STRATEGIES = {"A": "Nearest station", "B": "Recommender – forecast", "C": "Recommender – live"}
SCENARIOS = {"normal": 300, "busy": 450}
RUNS = 20
WEEKDAY = 2                       # a Wednesday


def make_city(rnd):
    hot = [(rnd.uniform(5, 19), rnd.uniform(5, 19)) for _ in range(3)]
    stations = []
    for i in range(N_STATIONS):
        if i < 0.6 * N_STATIONS:
            hx, hy = hot[i % 3]
            x, y = hx + rnd.gauss(0, 1.2), hy + rnd.gauss(0, 1.2)
        else:
            x, y = rnd.uniform(0, AREA_KM), rnd.uniform(0, AREA_KM)
        kw = rnd.choices([7.4, 22, 30, 60, 120], [0.25, 0.15, 0.25, 0.27, 0.08])[0]        # car chargers
        pts = rnd.choices([1, 2, 4, 6], [0.35, 0.35, 0.2, 0.1])[0]
        lo, hi = (18, 24) if kw > 22 else (12, 16)
        stations.append({"id": i, "x": min(max(x, 0), AREA_KM), "y": min(max(y, 0), AREA_KM), "kw": kw,
                         "points": pts, "tariff": round(rnd.uniform(lo, hi) * 2) / 2,
                         "session_h": sds._kernel(kw)[1]})
    return stations, hot


def make_drivers(rnd, n, hot, share):
    hours = rnd.choices(range(24), weights=share, k=n)
    drivers = []
    for h in hours:
        if rnd.random() < 0.6:
            hx, hy = rnd.choice(hot)
            x, y = hx + rnd.gauss(0, 2.5), hy + rnd.gauss(0, 2.5)
        else:
            x, y = rnd.uniform(0, AREA_KM), rnd.uniform(0, AREA_KM)
        drivers.append({"t": h + rnd.random(), "x": min(max(x, 0), AREA_KM), "y": min(max(y, 0), AREA_KM),
                        "kwh": rnd.uniform(8, 25)})
    return sorted(drivers, key=lambda d: d["t"])


def charge_h(st, kwh):
    kw = min(st["kw"], 60.0 if st["kw"] > 22 else 7.4)          # car limits: 60 kW DC, 7.4 kW AC
    return kwh / (0.9 * kw) + 0.1


class Station:
    def __init__(self, s):
        self.s = s
        self.free_at = [0.0] * s["points"]                        # when each point becomes free (heap)
        self.sessions = []                                        # (arrive, start, end)

    def state(self, t):
        busy = sum(1 for _, st, en in self.sessions if st <= t < en)
        queue = sum(1 for ar, st, _ in self.sessions if ar <= t < st)
        return busy, queue

    def serve(self, arrive, dur):
        first = heapq.heappop(self.free_at)
        start = max(arrive, first)
        heapq.heappush(self.free_at, start + dur)
        self.sessions.append((arrive, start, start + dur))
        return start - arrive, start


def wait_forecast_min(s, hour):
    return sds.station_demand(s["points"], s["kw"], WEEKDAY, hour, 1.0)["expected_wait_min"]


def wait_live_min(stn, t):
    busy, queue = stn.state(t)
    usable = stn.s["points"]
    if busy < usable and queue == 0:
        return 0.0
    return (queue + 1) * stn.s["session_h"] / usable * 60          # as station_demand_service.live_demand


def level_from_util(u):
    t = sds._m()["level_thresholds"]
    return "low" if u < t["low_below"] else "high" if u > t["high_above"] else "medium"


def price(s, hour, level):
    return s["tariff"] * time_band(hour)["factor"] * DEMAND_FACTOR[level]


def choose(strategy, d, stns, cands):
    if strategy == "A":
        return min(cands, key=lambda c: c[1])
    w = PREFERENCES["balanced"]["weights"]
    hour = int(d["t"]) % 24
    rows = []
    for stn, km in cands:
        s = stn.s
        if strategy == "B":
            dem = sds.station_demand(s["points"], s["kw"], WEEKDAY, hour, 1.0)
            wait, lvl = dem["expected_wait_min"], dem["level"]
        else:
            busy, queue = stn.state(d["t"])
            wait = wait_live_min(stn, d["t"])
            lvl = "high" if queue else level_from_util(busy / s["points"])
        rows.append((stn, km, wait, price(s, hour, lvl)))
    pmin = min(r[3] for r in rows)
    best, best_score = None, -1
    for stn, km, wait, p in rows:
        sc = (w["distance"] * normalize_distance(km) + w["price"] * (pmin / p) +
              w["wait"] * (0.0 if wait is None else 1 / (1 + wait / 15)) + w["power"] * normalize_power(stn.s["kw"]) +
              w["connector"] + w["status"] + w["battery"])
        if sc > best_score:
            best, best_score = (stn, km), sc
    return best


def run_day(seed, n_drivers, share):
    rnd = random.Random(seed)
    city, hot = make_city(rnd)
    drivers = make_drivers(rnd, n_drivers, hot, share)
    out = {}
    for strat in STRATEGIES:
        stns = [Station(s) for s in city]
        rec = []
        for d in drivers:
            cands = [(st, math.dist((d["x"], d["y"]), (st.s["x"], st.s["y"])) * ROAD) for st in stns]
            cands = [c for c in cands if c[1] <= MAX_KM] or [min(cands, key=lambda c: c[1])]
            stn, km = choose(strat, d, stns, cands)
            arrive = d["t"] + km / SPEED_KMH
            dur = charge_h(stn.s, d["kwh"])
            wait_h, start = stn.serve(arrive, dur)
            busy, queue = stn.state(start)
            lvl = "high" if queue else level_from_util(min(busy, stn.s["points"]) / stn.s["points"])
            rec.append({"wait": wait_h * 60, "km": km, "total": (km / SPEED_KMH + wait_h + dur) * 60,
                        "price": price(stn.s, int(start) % 24, lvl)})
        busy_h = [sum(min(en, 24) - st for _, st, en in x.sessions if st < 24) / x.s["points"] for x in stns]
        waits = sorted(r["wait"] for r in rec)
        out[strat] = {"mean_wait_min": statistics.mean(waits), "p90_wait_min": waits[int(0.9 * len(waits))],
                      "share_waited_over_15_min_pct": 100 * sum(w > 15 for w in waits) / len(waits),
                      "mean_travel_km": statistics.mean(r["km"] for r in rec),
                      "mean_total_time_min": statistics.mean(r["total"] for r in rec),
                      "mean_price_inr_per_kwh": statistics.mean(r["price"] for r in rec),
                      "utilisation_spread_h": statistics.pstdev(busy_h)}
    return out


def summarise(runs):
    keys = list(runs[0]["A"].keys())
    res = {}
    for strat, name in STRATEGIES.items():
        res[strat] = {"strategy": name}
        for k in keys:
            vals = [r[strat][k] for r in runs]
            res[strat][k] = round(statistics.mean(vals), 2)
            res[strat][k + "_sd"] = round(statistics.stdev(vals), 2)
    a = res["A"]
    for strat in ("B", "C"):
        res[strat]["wait_reduction_vs_nearest_pct"] = round(100 * (1 - res[strat]["mean_wait_min"] / a["mean_wait_min"]), 1) \
            if a["mean_wait_min"] > 0 else None
        res[strat]["extra_travel_km_vs_nearest"] = round(res[strat]["mean_travel_km"] - a["mean_travel_km"], 2)
        res[strat]["total_time_change_vs_nearest_pct"] = round(100 * (res[strat]["mean_total_time_min"] / a["mean_total_time_min"] - 1), 1)
    return res


def chart(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colours = {"A": "#898781", "B": "#3987e5", "C": "#1baf7a"}
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))
    metrics = [("mean_wait_min", "Mean waiting time (min)"), ("p90_wait_min", "90th-percentile wait (min)"),
               ("mean_total_time_min", "Mean total time: travel + wait + charge (min)")]
    for ax, (m, title) in zip(axes, metrics):
        xs = list(SCENARIOS)
        width = 0.26
        for j, strat in enumerate(STRATEGIES):
            vals = [results[sc][strat][m] for sc in xs]
            sds_ = [results[sc][strat][m + "_sd"] for sc in xs]
            ax.bar([i + (j - 1) * width for i in range(len(xs))], vals, width, yerr=sds_, capsize=3,
                   color=colours[strat], label=STRATEGIES[strat])
        ax.set_xticks(range(len(xs)), [f"{sc} day ({SCENARIOS[sc]} drivers)" for sc in xs])
        ax.set_title(title, fontsize=10)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.3)
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle(f"Station recommendation vs nearest station – {RUNS} simulated days per scenario (mean ± SD)", fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=150)


def main():
    share = pd.read_csv(BACKEND / "data" / "hourly_profile.csv").sort_values("hour")["share"].tolist()
    results = {}
    for sc, n in SCENARIOS.items():
        runs = [run_day(1000 + i, n, share) for i in range(RUNS)]
        results[sc] = summarise(runs)
    out = {"setup": {"stations": N_STATIONS, "area_km": AREA_KM, "runs_per_scenario": RUNS, "drivers": SCENARIOS,
                     "max_km": MAX_KM, "speed_kmh": SPEED_KMH, "road_factor": ROAD,
                     "weights": PREFERENCES["balanced"]["weights"]},
           "results": results,
           "note": "Synthetic city; mean ± SD over runs. B and C use the app's balanced scoring, waiting-time formulas "
                   "and price rules; A goes to the nearest station within 10 km."}
    OUT_JSON.write_text(json.dumps(out, indent=2))
    chart(results)
    for sc in SCENARIOS:
        print(f"\n{sc.upper()} DAY ({SCENARIOS[sc]} drivers, {RUNS} runs)")
        print(f"{'strategy':30} {'wait':>7} {'p90':>7} {'>15min%':>8} {'km':>6} {'total':>7} {'Rs/kWh':>7}")
        for strat in STRATEGIES:
            r = results[sc][strat]
            print(f"{r['strategy']:30} {r['mean_wait_min']:7.1f} {r['p90_wait_min']:7.1f} {r['share_waited_over_15_min_pct']:8.1f} "
                  f"{r['mean_travel_km']:6.2f} {r['mean_total_time_min']:7.1f} {r['mean_price_inr_per_kwh']:7.2f}")
        for strat in ("B", "C"):
            r = results[sc][strat]
            print(f"  {STRATEGIES[strat]}: waiting {r['wait_reduction_vs_nearest_pct']}% less than nearest, "
                  f"{r['extra_travel_km_vs_nearest']:+.2f} km travel, total time {r['total_time_change_vs_nearest_pct']:+.1f}%")
    print(f"\nSaved {OUT_JSON} and {OUT_PNG}")


if __name__ == "__main__":
    main()
