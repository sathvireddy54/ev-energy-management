"""
Train the charging-demand forecasting model.

Data: City of Palo Alto public EV charging sessions, 2011-2020
      ("ChargePoint Data CY20Q4.csv", 259,415 sessions).
      Put the raw file at backend/data/raw/ChargePoint Data CY20Q4.csv to retrain.

Target: number of charging sessions per day.
Model : Random Forest (250 trees, max depth 15) on calendar + lag features
        (day of week, month, day, demand 1, 2 and 7 days earlier).
Split : chronological 80 / 20 (no shuffling - the test period is later in time).

Outputs (small files the app uses at runtime - the raw CSV is not needed to run the app):
    data/daily_demand.csv        sessions per day
    data/hourly_profile.csv      share of sessions starting in each hour (last full year)
    data/demand_test_predictions.csv  actual vs predicted, test period (for the chart)
    data/busy_heatmap.csv        share of sessions per day-of-week x hour (last full year)
    models/demand_model.joblib   trained model (compressed)
    models/metrics_demand.json   test metrics vs naive baselines

Run from the backend folder:  python training/train_demand.py
"""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

BACKEND = Path(__file__).resolve().parent.parent
RAW = BACKEND / "data" / "raw" / "ChargePoint Data CY20Q4.csv"
DATA = BACKEND / "data"
MODELS = BACKEND / "models"

FEATURES = ["day_of_week", "month", "day", "lag_1", "lag_2", "lag_7"]


def build_features(daily):
    x = daily.copy()
    for k in (1, 2, 7):
        x[f"lag_{k}"] = x["demand"].shift(k)
    x["day_of_week"] = x["date"].dt.dayofweek
    x["month"] = x["date"].dt.month
    x["day"] = x["date"].dt.day
    return x.dropna().reset_index(drop=True)


def scores(y, p):
    return {"mae_sessions": round(float(mean_absolute_error(y, p)), 2),
            "rmse_sessions": round(float(np.sqrt(mean_squared_error(y, p))), 2),
            "r2": round(float(r2_score(y, p)), 3)}


def recursive_forecast(model, history, n_days):
    """Forecast n_days ahead, feeding each prediction back in as the next day's lag.
    history: DataFrame with 'date' and 'demand' (at least 7 rows). Returns list of floats."""
    dates = list(history["date"])
    values = list(history["demand"].astype(float))
    out = []
    for _ in range(n_days):
        d = dates[-1] + pd.Timedelta(days=1)
        row = pd.DataFrame([{"day_of_week": d.dayofweek, "month": d.month, "day": d.day,
                             "lag_1": values[-1], "lag_2": values[-2], "lag_7": values[-7]}])
        v = float(model.predict(row[FEATURES])[0])
        dates.append(d); values.append(v); out.append(v)
    return out


def horizon_mae(model, x, start, horizon=7):
    """MAE for each horizon day (1..7) over all forecast origins in the test period.
    All origins are processed together, one batch prediction per horizon step."""
    demand = x["demand"].to_numpy(float)
    dates = x["date"]
    origins = np.arange(start, len(x) - horizon)
    hist = {i: list(demand[i - 7:i]) for i in origins}           # last 7 known values per origin
    err = []
    for h in range(horizon):
        idx = origins + h
        rows = pd.DataFrame({"day_of_week": dates.iloc[idx].dt.dayofweek.values,
                             "month": dates.iloc[idx].dt.month.values,
                             "day": dates.iloc[idx].dt.day.values,
                             "lag_1": [hist[i][-1] for i in origins],
                             "lag_2": [hist[i][-2] for i in origins],
                             "lag_7": [hist[i][-7] for i in origins]})
        p = model.predict(rows[FEATURES])
        for i, v in zip(origins, p):
            hist[i].append(v)
        err.append(round(float(np.mean(np.abs(p - demand[idx]))), 2))
    return err


def main():
    if not RAW.exists():
        raise SystemExit(f"Raw data not found: {RAW}\nCopy 'ChargePoint Data CY20Q4.csv' there to retrain.")
    MODELS.mkdir(exist_ok=True)

    df = pd.read_csv(RAW, low_memory=False, usecols=["Start Date"])
    df["Start Date"] = pd.to_datetime(df["Start Date"], errors="coerce")
    df = df.dropna()

    daily = (df.groupby(df["Start Date"].dt.normalize()).size()
               .rename("demand").rename_axis("date").reset_index())
    daily.to_csv(DATA / "daily_demand.csv", index=False)

    last_year = df["Start Date"].dt.year.max() - 1          # last complete year
    hours = df.loc[df["Start Date"].dt.year == last_year, "Start Date"].dt.hour
    profile = hours.value_counts(normalize=True).reindex(range(24), fill_value=0)
    pd.DataFrame({"hour": range(24), "share": profile.round(5).values}) \
      .to_csv(DATA / "hourly_profile.csv", index=False)

    # busy periods: share of sessions per weekday x hour, last full year
    sub = df.loc[df["Start Date"].dt.year == last_year, "Start Date"]
    heat = (pd.crosstab(sub.dt.dayofweek, sub.dt.hour, normalize=True)
              .reindex(index=range(7), columns=range(24), fill_value=0))
    heat.index.name = "day_of_week"
    heat.round(5).to_csv(DATA / "busy_heatmap.csv")

    # demand levels: tertiles of daily sessions in the last full year
    year_days = daily[daily["date"].dt.year == last_year]["demand"]
    levels = {"low_below": round(float(year_days.quantile(1 / 3)), 1),
              "high_above": round(float(year_days.quantile(2 / 3)), 1)}

    x = build_features(daily)
    split = int(len(x) * 0.8)
    train, test = x.iloc[:split], x.iloc[split:]

    model = RandomForestRegressor(n_estimators=250, max_depth=15, random_state=42, n_jobs=-1)
    model.fit(train[FEATURES], train["demand"])
    pred = model.predict(test[FEATURES])
    pd.DataFrame({"date": test["date"].dt.date, "actual": test["demand"].values,
                  "predicted": pred.round(1)}).to_csv(DATA / "demand_test_predictions.csv", index=False)
    by_horizon = horizon_mae(model, x, split)
    naive_week = round(float(np.mean(np.abs(test["demand"] - test["lag_7"]))), 2)

    metrics = {
        "dataset": "City of Palo Alto EV charging sessions (ChargePoint), 2011-2020",
        "sessions": int(len(df)), "days": int(len(daily)),
        "test_period": [str(test["date"].min().date()), str(test["date"].max().date())],
        "random_forest": scores(test["demand"], pred),
        "baseline_same_as_yesterday": scores(test["demand"], test["lag_1"]),
        "baseline_same_as_last_week": scores(test["demand"], test["lag_7"]),
        "hourly_profile_year": int(last_year),
        "horizon_days": 7,
        "mae_by_horizon_day": by_horizon,
        "horizon_note": "Recursive forecast: each predicted day becomes the lag for the next. "
                        f"Same-as-last-week baseline MAE {naive_week}.",
        "demand_levels": {**levels, "basis": f"tertiles of daily sessions in {last_year}"},
        "covid_note": "Demand fell sharply from March 2020 (COVID-19); the test period includes this drop.",
    }

    model.fit(x[FEATURES], x["demand"])                      # refit on all data for use
    joblib.dump({"model": model, "features": FEATURES}, MODELS / "demand_model.joblib", compress=3)
    (MODELS / "metrics_demand.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
