"""
Charging-demand forecast service.

Uses the small pre-computed daily series (data/daily_demand.csv) and the trained
Random Forest (models/demand_model.joblib), both created by training/train_demand.py.

If the saved model cannot be loaded (for example a different scikit-learn version),
it is retrained automatically from data/daily_demand.csv - this takes a few seconds
and needs no raw data.

The data is historical (City of Palo Alto, 2011-2020), so the forecast is for the day
after the last recorded date. It is a system-level forecast, not live station occupancy.
"""
import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parent.parent
DAILY = BACKEND / "data" / "daily_demand.csv"
HOURLY = BACKEND / "data" / "hourly_profile.csv"
TEST_PRED = BACKEND / "data" / "demand_test_predictions.csv"
HEATMAP = BACKEND / "data" / "busy_heatmap.csv"
LEVEL_WINDOW_DAYS = 90      # demand levels are relative to the most recent 90 days
HORIZON_DAYS = 7
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MODEL = BACKEND / "models" / "demand_model.joblib"
METRICS = BACKEND / "models" / "metrics_demand.json"

FEATURES = ["day_of_week", "month", "day", "lag_1", "lag_2", "lag_7"]


@lru_cache(maxsize=1)
def _daily():
    df = pd.read_csv(DAILY, parse_dates=["date"])
    return df.sort_values("date").reset_index(drop=True)


def _retrain():
    from sklearn.ensemble import RandomForestRegressor
    x = _daily().copy()
    for k in (1, 2, 7):
        x[f"lag_{k}"] = x["demand"].shift(k)
    x["day_of_week"] = x["date"].dt.dayofweek
    x["month"] = x["date"].dt.month
    x["day"] = x["date"].dt.day
    x = x.dropna()
    model = RandomForestRegressor(n_estimators=250, max_depth=15, random_state=42, n_jobs=-1)
    model.fit(x[FEATURES], x["demand"])
    joblib.dump({"model": model, "features": FEATURES}, MODEL, compress=3)
    return model


@lru_cache(maxsize=1)
def _model():
    try:
        return joblib.load(MODEL)["model"]
    except Exception:
        return _retrain()


@lru_cache(maxsize=1)
def hourly_profile():
    df = pd.read_csv(HOURLY)
    return [{"hour": int(h), "share": float(s)} for h, s in zip(df["hour"], df["share"])]


@lru_cache(maxsize=1)
def predict_daily_demand():
    daily = _daily()
    lookup = dict(zip(daily["date"], daily["demand"]))
    last = daily["date"].max()
    target = last + timedelta(days=1)
    lags = {k: lookup.get(target - timedelta(days=k)) for k in (1, 2, 7)}
    if any(v is None for v in lags.values()):
        raise ValueError("Not enough consecutive history to build lag features.")
    row = pd.DataFrame([{
        "day_of_week": target.dayofweek, "month": target.month, "day": target.day,
        "lag_1": lags[1], "lag_2": lags[2], "lag_7": lags[7],
    }])[FEATURES]
    pred = max(float(_model().predict(row)[0]), 0.0)

    recent = daily[daily["date"] > last - timedelta(days=30)]["demand"]
    metrics = json.loads(METRICS.read_text()) if METRICS.exists() else {}
    return {
        "latest_historical_date": last.strftime("%Y-%m-%d"),
        "prediction_date": target.strftime("%Y-%m-%d"),
        "predicted_daily_sessions": round(pred, 1),
        "recent_30_day_average": round(float(recent.mean()), 1),
        "model": "Random Forest (calendar + 1, 2 and 7-day lag features)",
        "test_mae_sessions": metrics.get("random_forest", {}).get("mae_sessions"),
        "test_r2": metrics.get("random_forest", {}).get("r2"),
        "data_source": "City of Palo Alto EV charging sessions (ChargePoint), 2011-2020",
        "is_live": False,
        "is_station_specific": False,
    }


def demand_history(days=90):
    daily = _daily().tail(days)
    return [{"date": d.strftime("%Y-%m-%d"), "sessions": int(v)}
            for d, v in zip(daily["date"], daily["demand"])]


def metrics():
    return json.loads(METRICS.read_text()) if METRICS.exists() else {}


NORMAL_YEAR = 2019            # last full year before COVID-19


@lru_cache(maxsize=1)
def level_thresholds(mode="expected"):
    """Low / medium / high = tertiles of daily sessions.
    expected: over the normal year 2019 (the pattern used for upcoming dates)
    latest:   over the most recent 90 days of data (post-COVID 2020)"""
    d = _daily()
    if mode == "latest":
        x, basis = d.tail(LEVEL_WINDOW_DAYS)["demand"], f"tertiles of daily sessions over the last {LEVEL_WINDOW_DAYS} days of data (late 2020)"
    else:
        x, basis = d[d["date"].dt.year == NORMAL_YEAR]["demand"], f"tertiles of daily sessions in {NORMAL_YEAR} (normal year)"
    return {"low_below": round(float(x.quantile(1 / 3)), 1), "high_above": round(float(x.quantile(2 / 3)), 1),
            "basis": basis}


def demand_level(sessions, mode="expected"):
    t = level_thresholds(mode)
    return "low" if sessions < t["low_below"] else "high" if sessions > t["high_above"] else "medium"


@lru_cache(maxsize=8)
def _expected(start_iso, days):
    """Expected demand for real upcoming dates.
    The data ends in 2020, so each date is matched to the same weekday of the same week in the normal year
    2019 ("analogous day"). The Random Forest predicts that day from its real 1, 2 and 7-day-earlier demand,
    exactly as in testing. The result is the expected demand for that weekday and time of year."""
    daily = _daily()
    lookup = dict(zip(daily["date"].dt.date, daily["demand"].astype(float)))
    model, out = _model(), []
    start = pd.Timestamp(start_iso)
    for i in range(days):
        d = (start + pd.Timedelta(days=i)).date()
        week = min(d.isocalendar()[1], 52)
        a = pd.Timestamp.fromisocalendar(NORMAL_YEAR, week, d.isoweekday()).date()
        lags = [lookup.get(a - timedelta(days=k)) for k in (1, 2, 7)]
        if any(v is None for v in lags):
            lags = [float(daily["demand"].mean())] * 3
        row = pd.DataFrame([{"day_of_week": a.weekday(), "month": a.month, "day": a.day,
                             "lag_1": lags[0], "lag_2": lags[1], "lag_7": lags[2]}])[FEATURES]
        v = max(float(model.predict(row)[0]), 0.0)
        out.append({"date": d.isoformat(), "weekday": DAYS[d.weekday()], "predicted_sessions": round(v, 1),
                    "level": demand_level(v, "expected"), "analogous_day": a.isoformat(),
                    "actual_on_analogous_day": lookup.get(a)})
    return out


@lru_cache(maxsize=1)
def _latest(days):
    """Recursive forecast from the last recorded date (1-7 Jan 2021, post-COVID demand)."""
    daily = _daily()
    dates, values = list(daily["date"]), list(daily["demand"].astype(float))
    model, out = _model(), []
    for _ in range(days):
        d = dates[-1] + timedelta(days=1)
        row = pd.DataFrame([{"day_of_week": d.dayofweek, "month": d.month, "day": d.day,
                             "lag_1": values[-1], "lag_2": values[-2], "lag_7": values[-7]}])[FEATURES]
        v = max(float(model.predict(row)[0]), 0.0)
        dates.append(d); values.append(v)
        out.append({"date": d.strftime("%Y-%m-%d"), "weekday": DAYS[d.dayofweek],
                    "predicted_sessions": round(v, 1), "level": demand_level(v, "latest")})
    return out


def forecast(days=HORIZON_DAYS, mode="expected", start=None):
    """mode 'expected' (default): today and the next days, normal-year pattern.
    mode 'latest': the 7 days after the data ends (post-COVID)."""
    if mode == "latest":
        return _latest(days)
    return _expected((start or date.today()).isoformat(), days)


def test_predictions(days=120):
    df = pd.read_csv(TEST_PRED).tail(days)
    return [{"date": d, "actual": int(a), "predicted": float(p)}
            for d, a, p in zip(df["date"], df["actual"], df["predicted"])]


@lru_cache(maxsize=1)
def busy_periods():
    """Weekday x hour share of sessions (last full year) and the busiest slots (top 10 %)."""
    h = pd.read_csv(HEATMAP, index_col=0)
    grid = h.to_numpy()
    cut = float(np.quantile(grid, 0.90))
    quiet = float(np.quantile(grid, 0.25))
    slots = []
    for d in range(7):
        for hr in range(24):
            if grid[d, hr] >= cut:
                slots.append({"weekday": DAYS[d], "hour": hr, "share_pct": round(float(grid[d, hr]) * 100, 2)})
    slots.sort(key=lambda s: -s["share_pct"])
    return {"weekdays": DAYS, "hours": list(range(24)),
            "share_pct": [[round(float(v) * 100, 3) for v in row] for row in grid],
            "busy_threshold_pct": round(cut * 100, 3), "quiet_threshold_pct": round(quiet * 100, 3),
            "busy_slots": slots, "year": metrics().get("hourly_profile_year"),
            "rule": "Busy = among the top 10% of weekday-hour slots by share of charging sessions."}


def hour_band(hour):
    """Peak / normal / off-peak hour from the hourly profile: top quartile of hours = peak,
    bottom quartile = off-peak."""
    shares = np.array([p["share"] for p in hourly_profile()])
    hi, lo = np.quantile(shares, 0.75), np.quantile(shares, 0.25)
    s = shares[hour]
    return "busy" if s >= hi else "quiet" if s <= lo else "moderate"


def summary(history_days=90):
    f = forecast()
    latest = forecast(mode="latest")
    m = metrics()
    return {
        "latest_historical_date": _daily()["date"].max().strftime("%Y-%m-%d"),
        "forecast": f,
        "next_day": f[0],
        "forecast_basis": "Expected demand for these dates: the Random Forest predicts the same weekday and week "
                          f"of the normal year {NORMAL_YEAR} from its real 1, 2 and 7-day-earlier demand.",
        "latest_data_forecast": latest,
        "levels": level_thresholds(),
        "history": demand_history(history_days),
        "test_predictions": test_predictions(),
        "hourly_profile": hourly_profile(),
        "busy_periods": busy_periods(),
        "evaluation": {
            "model": "Random Forest (250 trees, depth 15) on day of week, month, day and 1/2/7-day lags",
            "test_period": m.get("test_period"),
            "random_forest": m.get("random_forest"),
            "baseline_same_as_yesterday": m.get("baseline_same_as_yesterday"),
            "baseline_same_as_last_week": m.get("baseline_same_as_last_week"),
            "mae_by_horizon_day": m.get("mae_by_horizon_day"),
            "horizon_days": HORIZON_DAYS,
        },
        "data_source": "City of Palo Alto EV charging sessions (ChargePoint), 2011-2020",
        "is_live": False, "is_station_specific": False,
        "limitations": [
            "Historical public data from Palo Alto, USA (2011-2020); the forecast starts after the last recorded day.",
            "System-level demand across the city's stations, not live occupancy of one station.",
            "The data ends in 2020, so upcoming dates use the normal-year (2019) pattern for the same weekday and week; "
            "demand after March 2020 was far lower (COVID-19) – see the latest-data forecast.",
            "Errors grow with the forecast horizon because each predicted day feeds the next.",
        ],
    }
