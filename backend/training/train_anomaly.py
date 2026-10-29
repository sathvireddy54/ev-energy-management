"""
Battery anomaly detection - Isolation Forest on owner-available fields only.

The owner enters the same fields as the RUL form:
    vehicle type, registration year, odometer (km), km per year,
    1-3 dashboard readings (SOC %, range shown km), full-charge range when new (km)
From these the app derives
    age_years       = years since registration
    km_per_year     = odometer / age
    health_pct      = full-charge range now (range shown / SOC) / full-charge range when new * 100
No voltage, current, temperature or SoH reading is needed - an EV dashboard does not show them.

Training data: real used battery-electric cars (Zenodo, Aviloo battery test):
    age, odometer and measured battery health per car -> the same three derived fields.

Model: StandardScaler + Isolation Forest (300 trees, contamination 3 %).
Status rule (documented):
    anomalous - Isolation Forest score < 0
    warning   - score below the 5th percentile of normal training scores
    normal    - otherwise
Separately, the app checks the dashboard readings agree with each other (full-charge range
from each reading within 15 % of the median) and shows each field against the typical
(10th-90th percentile) values of real cars of a similar age.

Evaluation (no real fault labels exist):
    5-fold cross-validation over cars: false-alarm rate on held-out cars
    detection rate when a fault is injected into held-out cars

Outputs: models/anomaly_model.joblib, models/metrics_anomaly.json, data/anomaly_samples.json
Run from the backend folder:  python training/train_anomaly.py
"""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.model_selection import KFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

BACKEND = Path(__file__).resolve().parent.parent
ZENODO = BACKEND / "data" / "zenodo" / "Data SO BEV,PHEV.xlsx"
MODELS = BACKEND / "models"

FEATURES = {"age_years": "Vehicle age (years)",
            "km_per_year": "Distance per year (km)",
            "health_pct": "Battery health from range (%)"}
COLS = list(FEATURES)

FAULTS = {
    "Fast degradation (health -8 points)": lambda r: r.assign(health_pct=r.health_pct - 8),
    "Severe degradation (health -15 points)": lambda r: r.assign(health_pct=r.health_pct - 15),
    "Unusually heavy use (km per year x 2.5)": lambda r: r.assign(km_per_year=r.km_per_year * 2.5),
}


def load_cars():
    raw = pd.read_excel(ZENODO)
    z = raw[raw["typ_pohonu"] == "BEV"].copy()
    age = z["stáří_vozidla_k_datu_testu_ve_dnech"] / 365.25
    df = pd.DataFrame({"age_years": age.values,
                       "km_per_year": (z["odo_km"] / age.clip(lower=0.25)).values,
                       "health_pct": z["Aviloo_SoH"].clip(upper=100).values})
    return df.replace([np.inf, -np.inf], np.nan).dropna().query("age_years >= 0.25").reset_index(drop=True)


def new_model():
    return make_pipeline(StandardScaler(),
                         IsolationForest(n_estimators=300, contamination=0.03, random_state=42))


def status_of(scores, warn):
    return np.where(scores < 0, "anomalous", np.where(scores < warn, "warning", "normal"))


def typical_by_age(df):
    """10th / 50th / 90th percentile of each field for 1-year age groups."""
    out = []
    for lo in range(0, int(np.ceil(df.age_years.max()))):
        g = df[(df.age_years >= lo) & (df.age_years < lo + 1)]
        if len(g) < 20:
            continue
        out.append({"age_from": lo, "age_to": lo + 1, "cars": int(len(g)),
                    **{f"{c}_p{q}": round(float(g[c].quantile(q / 100)), 1)
                       for c in ("km_per_year", "health_pct") for q in (10, 50, 90)}})
    return out


def main():
    MODELS.mkdir(exist_ok=True)
    df = load_cars()

    fa, warn_n, det, flag = [], [], {k: [] for k in FAULTS}, {k: [] for k in FAULTS}
    for tr, te in KFold(5, shuffle=True, random_state=42).split(df):
        tr, te = df.iloc[tr], df.iloc[te]
        m = new_model().fit(tr[COLS])
        warn = float(np.quantile(m.decision_function(tr[COLS]), 0.05))
        st = status_of(m.decision_function(te[COLS]), warn)
        fa.append(np.mean(st == "anomalous"))
        warn_n.append(np.mean(st == "warning"))
        for name, f in FAULTS.items():
            sf = status_of(m.decision_function(f(te.copy())[COLS]), warn)
            det[name].append(np.mean(sf == "anomalous"))
            flag[name].append(np.mean(sf != "normal"))
    pct = lambda v: round(float(np.mean(v)) * 100, 1)

    # usage model (age + km per year) for first checks, when no health reference exists yet
    UCOLS = ["age_years", "km_per_year"]
    ufa, uheavy = [], []
    for tr, te in KFold(5, shuffle=True, random_state=42).split(df):
        tr, te = df.iloc[tr], df.iloc[te]
        um = new_model().fit(tr[UCOLS])
        ufa.append(np.mean(um.decision_function(te[UCOLS]) < 0))
        uheavy.append(np.mean(um.decision_function(FAULTS["Unusually heavy use (km per year x 2.5)"](te.copy())[UCOLS]) < 0))
    usage_model = new_model().fit(df[UCOLS])
    usage_warn = float(np.quantile(usage_model.decision_function(df[UCOLS]), 0.05))

    model = new_model().fit(df[COLS])
    warn = float(np.quantile(model.decision_function(df[COLS]), 0.05))
    typical = typical_by_age(df)
    joblib.dump({"model": model, "features": COLS, "labels": FEATURES, "warn_score": warn,
                 "usage_model": usage_model, "usage_features": UCOLS, "usage_warn_score": usage_warn,
                 "typical_by_age": typical, "train_age_max": float(df.age_years.max())},
                MODELS / "anomaly_model.joblib", compress=3)

    samples = [
        {"id": "trip-ok", "label": "Normal trip (2-wheeler)",
         "input": {"vehicle_type": "2W", "registration_year": 2024, "odometer_km": 17500, "km_per_year": 9000,
                   "readings": [{"soc_pct": 80, "range_km": 90}]},
         "trip": {"soc_start": 80, "range_start_km": 90, "soc_end": 60, "distance_km": 22, "ride_mode": "Eco"}},
        {"id": "trip-bad", "label": "Battery draining fast (2-wheeler)",
         "input": {"vehicle_type": "2W", "registration_year": 2024, "odometer_km": 17500, "km_per_year": 9000,
                   "readings": [{"soc_pct": 80, "range_km": 90}]},
         "trip": {"soc_start": 80, "range_start_km": 90, "soc_end": 60, "distance_km": 12, "ride_mode": "Eco"}},
        {"id": "typical", "label": "Typical car: 4 years, 14,000 km/yr",
         "input": {"vehicle_type": "4W", "registration_year": 2022, "odometer_km": 56000, "km_per_year": 14000,
                   "readings": [{"soc_pct": 80, "range_km": 232}], "new_full_range_km": 312}},
        {"id": "fast", "label": "Range dropped a lot: 3 years",
         "input": {"vehicle_type": "4W", "registration_year": 2023, "odometer_km": 36000, "km_per_year": 12000,
                   "readings": [{"soc_pct": 90, "range_km": 205}], "new_full_range_km": 312}},
        {"id": "heavy", "label": "Very heavy use: 120,000 km in 2 years",
         "input": {"vehicle_type": "4W", "registration_year": 2024, "odometer_km": 120000, "km_per_year": 60000,
                   "readings": [{"soc_pct": 100, "range_km": 290}], "new_full_range_km": 312}},
        {"id": "inconsistent", "label": "Readings disagree (2-wheeler)",
         "input": {"vehicle_type": "2W", "registration_year": 2023, "odometer_km": 21000, "km_per_year": 7000,
                   "readings": [{"soc_pct": 90, "range_km": 130}, {"soc_pct": 50, "range_km": 40}],
                   "new_full_range_km": 150}},
    ]
    (BACKEND / "data" / "anomaly_samples.json").write_text(json.dumps(samples, indent=1))

    metrics = {
        "dataset": f"Zenodo real used battery-electric cars - {len(df):,} cars (age, odometer, measured health)",
        "inputs": "Owner fields only: registration year, odometer, SOC + range shown; health reference = your own "
                  "earlier readings (or the range when new, if known)",
        "model": "Isolation Forest (300 trees, contamination 3%) on age, km per year and health from range",
        "evaluation": "5-fold cross-validation over cars; faults injected into held-out cars",
        "false_alarm_pct": pct(fa),
        "warning_pct_on_normal": pct(warn_n),
        "detection_pct": {k: pct(v) for k, v in det.items()},
        "flagged_pct_warning_or_anomalous": {k: pct(v) for k, v in flag.items()},
        "usage_model": {"features": "age, km per year (used on a first check, before a health reference exists)",
                        "false_alarm_pct": pct(ufa), "heavy_use_detected_pct": pct(uheavy)},
        "status_rule": {"anomalous": "Isolation Forest score < 0",
                        "warning": "score below the 5th percentile of normal training scores, the dashboard "
                                   "readings disagree by more than 15 %, the full-charge range fell more than 15 % "
                                   "below a reading from the last 90 days, or it declined more than 5 points faster "
                                   "than the ageing model expects since your first reading",
                        "normal": "otherwise"},
        "limitations": [
            "Training health is a measured battery test; the owner's health comes from the dashboard range, "
            "which also depends on recent driving, temperature and AC use.",
            "Trained on cars; 2- and 3-wheelers are checked against the same patterns (evidence: limited).",
            "No real fault labels exist, so detection rates come from injected faults.",
        ],
        "features": FEATURES,
    }
    (MODELS / "metrics_anomaly.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in metrics.items() if k != "limitations"}, indent=2, ensure_ascii=False))
    print(json.dumps(typical[:5], indent=0))


if __name__ == "__main__":
    main()
