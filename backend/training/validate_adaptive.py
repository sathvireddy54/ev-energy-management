"""
Validation of adaptive learning with a known answer (for the report).

Four stations are given a "true" demand that differs from the model (x2.0, x0.5, x1.0, x1.5). Two weeks of
live readings (one per station every 10 minutes, each point busy at random with the true probability) are
fed to adaptive_service. A good learner recovers the true factors and predicts held-out readings better
than the plain model. Uses a temporary database - the app's own data is not touched.

Run from the backend folder:  python -m training.validate_adaptive
"""
import json
import random
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app import adaptive_service as A
from app import station_demand_service as sds

STATIONS = {  # id: (points, power kW, true factor)
    "S1": (4, 60.0, 2.0), "S2": (2, 30.0, 0.5), "S3": (1, 7.4, 1.0), "S4": (6, 3.3, 1.5)}


def main(days=14, seed=3):
    random.seed(seed)
    A.DB = Path(tempfile.mkdtemp()) / "validate.db"
    A._cache["dirty"] = True
    A.remember_stations([{"id": k, "name": f"Station {k}", "number_of_points": p, "max_power_kw": kw}
                         for k, (p, kw, _) in STATIONS.items()])
    start = datetime(2026, 10, 5).astimezone()
    for step in range(days * 24 * 6):
        t = start + timedelta(minutes=10 * step)
        for sid, (pts, kw, f) in STATIONS.items():
            u = sds.station_demand(pts, kw, t.weekday(), t.hour, f)["utilisation_pct"] / 100
            busy = sum(random.random() < u for _ in range(pts))
            A.record(sid, "ocpp", {"connector_count": pts, "available": pts - busy, "out_of_service": 0, "queue": 0},
                     when=t)
    s = A.summary()
    rows = [{"station": x["station_id"], "true_factor": STATIONS[x["station_id"]][2], "learned_factor": x["factor"],
             "readings": x["readings"]} for x in sorted(s["stations"], key=lambda x: x["station_id"])]
    out = {"days": days, "readings": s["observations"], "stations": rows, "evaluation": s["evaluation"]}
    print(json.dumps(out, indent=2))
    path = Path(__file__).resolve().parent.parent / "models" / "metrics_adaptive.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
