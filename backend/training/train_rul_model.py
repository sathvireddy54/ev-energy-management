"""
Train the battery ageing model used to predict Remaining Useful Life (RUL).

Model (standard semi-empirical battery ageing law - calendar + cycle ageing):

    SoH(age, cycles) = 100 - a * age^alpha - b * cycles^beta

    age    = years since registration          (calendar ageing)
    cycles = odometer / full-charge range      (cycle ageing; full-charge range
                                                = range shown / SOC, from dashboard)

Data
  1. NASA cells B0005/B0006/B0018 (REAL, cycled to end of life)
       -> cycle-ageing exponent beta (shape of capacity fade with use)
       -> leave-one-cell-out check of RUL predictions against real RUL
  2. Zenodo used-EV tests (REAL, 2,742 cars, diagnostic SoH)
       -> calendar ageing a, alpha and cycle scale b (beta fixed from NASA)
       -> 5-fold cross-validated accuracy vs a simple baseline
       -> residual spread, used for the low / high RUL range

RUL = time until SoH reaches 80 % (end of life), found by projecting the
equation forward with the owner's own km per year:
    SoH(age + t, cycles + km_per_year * t / full_charge_range) = 80

Outputs: models/ageing_model.json, models/metrics_rul.json, models/ageing_curves.png
Run from the backend folder:  python training/train_rul_model.py
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
from sklearn.linear_model import QuantileRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import KFold

BASE = Path(__file__).resolve().parent.parent   # backend/
NASA = BASE / "data" / "nasa" / "nasa_rul_dataset.csv"
ZENODO = BASE / "data" / "zenodo" / "Data SO BEV,PHEV.xlsx"
MODELS = BASE / "models"

EOL_SOH = 80.0
# Zenodo has no range data: its cycle count uses nominal full-charge range
# = battery kWh x 6 km/kWh. This only fixes the scale of the training feature.
ZENODO_KM_PER_KWH = 6.0
ZENODO_KWH = {"do 39 kWh": 35.0, "40 - 59 kWh": 50.0, "60 - 79 kWh": 70.0, "80 kWh a více": 85.0}
BASELINE_LOSS_PER_YEAR = 1.8   # Geotab fleet average, % SoH per year


# ---------------------------------------------------------------- NASA (beta)
def nasa_cells():
    df = pd.read_csv(NASA)
    cells = {}
    for cell, g in df.groupby("battery_id"):
        g = g.sort_values("discharge_cycle").reset_index(drop=True)
        soh = (g["capacity_ah"] / g["capacity_ah"].iloc[0] * 100).values
        cyc = np.arange(1, len(g) + 1, dtype=float)
        eol_idx = int(np.flatnonzero(soh <= EOL_SOH)[0])
        cells[cell] = {"cycles": cyc, "soh": soh, "eol_cycle": eol_idx + 1}
    return cells


def fit_cycle_law(cyc, soh):
    (b, beta), _ = curve_fit(lambda x, b, be: 100 - b * x ** be, cyc, soh,
                             p0=[0.5, 0.9], bounds=([1e-6, 0.2], [50, 2.0]))
    return b, beta


def nasa_loco(cells):
    """Leave-one-cell-out: beta from 2 cells, predict RUL of the 3rd from a
    single SoH reading at each cycle (like an owner would provide)."""
    out = []
    for held in cells:
        beta = float(np.median([fit_cycle_law(c["cycles"], c["soh"])[1]
                                for k, c in cells.items() if k != held]))
        c = cells[held]
        eol = c["eol_cycle"]
        rows = []
        for i in range(int(0.1 * eol), eol):                 # skip first 10 % of life
            cyc, soh = c["cycles"][i], min(c["soh"][i], 99.9)
            b_i = (100 - soh) / cyc ** beta                  # this cell's own scale
            pred_eol = (100 - EOL_SOH) / b_i
            pred_eol = pred_eol ** (1 / beta)
            rows.append({"soh": soh, "true": eol - cyc, "pred": max(pred_eol - cyc, 0)})
        r = pd.DataFrame(rows)
        late, early = r[r["soh"] <= 90], r[r["soh"] > 90]
        out.append({
            "held_out_cell": held, "beta_from_other_cells": round(beta, 3), "eol_cycles": eol,
            "mae_rul_cycles": round(float(mean_absolute_error(r["true"], r["pred"])), 1),
            "mae_rul_cycles_soh_le_90": round(float(mean_absolute_error(late["true"], late["pred"])), 1) if len(late) else None,
            "mae_rul_cycles_soh_gt_90": round(float(mean_absolute_error(early["true"], early["pred"])), 1) if len(early) else None,
            "mae_pct_of_life": round(float(mean_absolute_error(r["true"], r["pred"]) / eol * 100), 1),
        })
    return out


# ------------------------------------------------------------ Zenodo (a, alpha, b)
def load_zenodo():
    raw = pd.read_excel(ZENODO)
    z = raw[(raw["typ_pohonu"] == "BEV") & raw["baterka_BEV"].isin(ZENODO_KWH)].copy()
    full_charge_km = z["baterka_BEV"].map(ZENODO_KWH) * ZENODO_KM_PER_KWH
    return pd.DataFrame({
        "age": (z["stáří_vozidla_k_datu_testu_ve_dnech"] / 365.25).values,
        "cycles": (z["odo_km"] / full_charge_km).values,
        "soh": z["Aviloo_SoH"].clip(upper=100).values,
    })


def soh_law(X, a, alpha, b, beta):
    age, cycles = X
    return 100 - a * np.power(age, alpha) - b * np.power(cycles, beta)


def fit_fleet(age, cycles, soh, beta):
    p, _ = curve_fit(lambda X, a, al, b: soh_law(X, a, al, b, beta),
                     np.vstack([age, cycles]), soh, p0=[1.0, 0.6, 0.05],
                     bounds=([0, 0.05, 0], [20, 2.0, 10]), maxfev=20000)
    return p


def fit_bands(age, resid):
    """10th / 90th percentile of the residual as a straight line in age:
    the spread between batteries grows as they get older."""
    out = {}
    for name, q in (("q10", 0.10), ("q90", 0.90)):
        m = QuantileRegressor(quantile=q, alpha=0, solver="highs").fit(age.reshape(-1, 1), resid)
        out[name] = {"intercept": float(m.intercept_), "slope": float(m.coef_[0])}
    return out


def band_offset(bands, name, age):
    return bands[name]["intercept"] + bands[name]["slope"] * np.asarray(age)


def band_coverage_cv(z, beta):
    """Cross-validated share of real cars inside the 10-90 band, per age group."""
    X, y, age = np.vstack([z["age"], z["cycles"]]), z["soh"].values, z["age"].values
    inside = np.zeros(len(y), dtype=bool)
    for tr, te in KFold(5, shuffle=True, random_state=1).split(y):
        p = fit_fleet(age[tr], z["cycles"].values[tr], y[tr], beta)
        b = fit_bands(age[tr], y[tr] - soh_law(X[:, tr], *p, beta))
        pt = soh_law(X[:, te], *p, beta)
        inside[te] = (y[te] >= pt + band_offset(b, "q10", age[te])) & \
                     (y[te] <= pt + band_offset(b, "q90", age[te]))
    groups = pd.cut(age, [0, 2, 4, 6, 8, 15], labels=["0-2", "2-4", "4-6", "6-8", "8+"])
    return {"overall_pct": round(float(inside.mean() * 100), 1),
            "by_age_years_pct": {str(k): round(float(v * 100), 1)
                                 for k, v in pd.Series(inside).groupby(groups, observed=True).mean().items()}}


def main():
    MODELS.mkdir(exist_ok=True)

    # 1. NASA -> beta
    cells = nasa_cells()
    betas = {k: fit_cycle_law(c["cycles"], c["soh"])[1] for k, c in cells.items()}
    beta = float(np.median(list(betas.values())))
    loco = nasa_loco(cells)

    # 2. Zenodo -> a, alpha, b  (5-fold CV, then refit on all data)
    z = load_zenodo()
    pred = np.zeros(len(z))
    for tr, te in KFold(5, shuffle=True, random_state=42).split(z):
        p = fit_fleet(z["age"].values[tr], z["cycles"].values[tr], z["soh"].values[tr], beta)
        pred[te] = soh_law(np.vstack([z["age"].values[te], z["cycles"].values[te]]), *p, beta)
    baseline = np.clip(100 - BASELINE_LOSS_PER_YEAR * z["age"], 0, 100)

    # free-beta fit, to check that NASA's beta agrees with real cars
    (a_f, al_f, b_f, beta_free), _ = curve_fit(
        soh_law, np.vstack([z["age"], z["cycles"]]), z["soh"],
        p0=[1, 0.6, 0.05, 0.9], bounds=([0, 0.05, 0, 0.2], [20, 2, 10, 2]), maxfev=20000)

    a, alpha, b = fit_fleet(z["age"].values, z["cycles"].values, z["soh"].values, beta)
    resid = z["soh"].values - soh_law(np.vstack([z["age"], z["cycles"]]), a, alpha, b, beta)
    bands = fit_bands(z["age"].values, resid)
    coverage = band_coverage_cv(z, beta)

    model = {
        "equation": "SoH = 100 - a*age^alpha - b*cycles^beta",
        "a": a, "alpha": alpha, "b": b, "beta": beta,
        "eol_soh": EOL_SOH,
        "bands": bands,          # residual 10th / 90th percentile = intercept + slope * age
        "train_age_max": float(z["age"].max()),
        "train_cycles_max": float(z["cycles"].max()),
    }
    metrics = {
        "nasa_beta_per_cell": {k: round(v, 3) for k, v in betas.items()},
        "beta_used_median_nasa": round(beta, 3),
        "beta_if_fitted_on_real_cars": round(float(beta_free), 3),
        "fleet_params": {"a": round(a, 4), "alpha": round(alpha, 4), "b": round(b, 4)},
        "soh_accuracy_real_cars_5fold_cv": {
            "ageing_model": {"mae": round(float(mean_absolute_error(z["soh"], pred)), 2),
                             "r2": round(float(r2_score(z["soh"], pred)), 3)},
            "baseline_1.8pct_per_year": {"mae": round(float(mean_absolute_error(z["soh"], baseline)), 2),
                                         "r2": round(float(r2_score(z["soh"], baseline)), 3)},
            "n_vehicles": int(len(z)),
        },
        "rul_accuracy_nasa_leave_one_cell_out": loco,
        "rul_mean_mae_cycles": round(float(np.mean([r["mae_rul_cycles"] for r in loco])), 1),
        "rul_mean_mae_cycles_soh_le_90": round(float(np.mean([r["mae_rul_cycles_soh_le_90"] for r in loco
                                                             if r["mae_rul_cycles_soh_le_90"] is not None])), 1),
        "range_bands_vs_age": {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in bands.items()},
        "range_coverage_target_80pct_cv": coverage,
    }
    (MODELS / "ageing_model.json").write_text(json.dumps(model, indent=2))
    (MODELS / "metrics_rul.json").write_text(json.dumps(metrics, indent=2))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
        for k, c in cells.items():
            bb, be = fit_cycle_law(c["cycles"], c["soh"])
            ax[0].scatter(c["cycles"], c["soh"], s=8, alpha=0.5, label=f"{k} data")
            ax[0].plot(c["cycles"], 100 - bb * c["cycles"] ** be, lw=1.5, label=f"{k} fit (β={be:.2f})")
        ax[0].axhline(EOL_SOH, color="k", ls="--", lw=1)
        ax[0].set(title="NASA cells: capacity fade with use (cycle ageing)",
                  xlabel="Charge cycles", ylabel="State of health (%)")
        ax[0].legend(fontsize=7)
        yrs = np.linspace(0, 20, 200)
        for kmyr in (5000, 12000, 25000):
            s = soh_law(np.vstack([yrs, kmyr * yrs / 300.0]), a, alpha, b, beta)
            ax[1].plot(yrs, s, label=f"{kmyr:,} km/year")
        ax[1].scatter(z["age"], z["soh"], s=3, alpha=0.15, color="grey", label="Zenodo cars")
        ax[1].axhline(EOL_SOH, color="k", ls="--", lw=1)
        ax[1].set(title="Fitted ageing model (300 km full-charge range)",
                  xlabel="Vehicle age (years)", ylabel="State of health (%)", ylim=(60, 101))
        ax[1].legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(MODELS / "ageing_curves.png", dpi=150)
    except ImportError:
        pass

    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
