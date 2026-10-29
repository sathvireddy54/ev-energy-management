"""
"Plan my charge" - the full project workflow in one request.

    1 User input        location, vehicle and dashboard readings, time, preference
    2 Input processing  validate (errors stop here)
    3 Battery analysis  RUL + anomaly check -> battery condition
    4 Demand            day forecast (Random Forest) + per-station demand and waiting time (queue model)
    5 Station info      Open Charge Map
    6 Adaptive pricing  price for each station
    7 Sufficient data?  no compatible station -> show available information only
    8 Recommendation    weighted multi-criteria with the user's preference and battery condition
    9 Final output      battery status, demand at stations, prices, recommended station

Battery condition affects the recommendation, not the price:
    "needs care" when the anomaly check is warning/anomalous, estimated health < 85 %, or < 3 years left.
    Then gentler charging is preferred: <= 22 kW scores 1.0, <= 60 kW 0.6, faster 0.3
    (frequent high-power DC charging speeds up battery wear), and the charging-power weight moves to
    this criterion so speed is no longer rewarded. A healthy battery scores 1.0 everywhere.

Per-station price (documented rule):
    base = the station's reported per-kWh tariff when it gives one (assumed to include tax),
           otherwise the base price you set, plus GST
    price = base x time-of-day factor x station demand factor (low 0.90, medium 1.00, high 1.10)
"""
import pandas as pd

from datetime import datetime

from app import adaptive_service, anomaly_service, checkin_service, live_service, ocpp_service, \
    sim_network, station_demand_service as sds, tariff_service
from app.demand_service import forecast, hour_band
from app.pricing_service import RULES, time_band
from app.recommendation_engine import build_station_features
from app.recommendation_service import compatibility
from app.rul_service import predict_rul

PREFERENCES = {
    "balanced": {"label": "Balanced", "weights": {"distance": .25, "price": .20, "wait": .15, "power": .10,
                                                   "connector": .10, "status": .10, "battery": .10}},
    "cheapest": {"label": "Cheapest", "weights": {"distance": .15, "price": .40, "wait": .10, "power": .05,
                                                   "connector": .10, "status": .10, "battery": .10}},
    "nearest": {"label": "Nearest", "weights": {"distance": .45, "price": .10, "wait": .10, "power": .05,
                                                 "connector": .10, "status": .10, "battery": .10}},
    "fastest": {"label": "Least waiting / fastest", "weights": {"distance": .15, "price": .05, "wait": .30,
                                                                "power": .25, "connector": .10, "status": .10,
                                                                "battery": .05}},
}
CRITERIA = {"distance": "Distance", "price": "Price", "wait": "Waiting time", "power": "Charging power",
            "connector": "Connector fit", "status": "Station status", "battery": "Battery-friendly"}
DEMAND_FACTOR = {"low": 0.90, "medium": 1.00, "high": 1.10}


def battery_condition(rul, anomaly):
    reasons = []
    if anomaly and anomaly.get("health_checked") and anomaly.get("status") in ("warning", "anomalous"):
        reasons.append(f"anomaly check: {anomaly['status']}")
    elif anomaly and (anomaly.get("trip") or {}).get("status") in ("warning", "anomalous"):
        reasons.append(f"trip check: {anomaly['trip']['status']} (battery drains faster than its estimate)")
    if rul["soh_now_pct"]["mid"] < 85:
        reasons.append(f"estimated health {rul['soh_now_pct']['mid']}%")
    y = rul["rul_years"]["mid"]
    if y is not None and y < 3:
        reasons.append(f"about {y} years of life left")
    return {"status": "needs_care" if reasons else "healthy", "reasons": reasons,
            "advice": ("Prefer slower (AC or up to ~22 kW) charging where you can; frequent fast DC charging "
                       "speeds up wear on a battery in this condition.") if reasons else
                      "Battery is in normal condition; any compatible charger is fine."}


def battery_fit(power_kw, condition):
    if condition == "healthy":
        return 1.0
    if not power_kw:
        return 0.7
    return 1.0 if power_kw <= 22 else 0.6 if power_kw <= 60 else 0.3


def _price(station, hour, level, base_price, gst_pct):
    """Dynamic price (this system's recommendation) = station tariff x time factor x demand factor.
    The station tariff comes from tariff_service (charger feed, driver reports or Open Charge Map, incl. GST);
    when it is not known, the user's base price + GST is used."""
    band = time_band(hour)
    t = station.get("tariff")
    if t:
        base, src, tax = t["price_per_kwh"], f"station tariff ({t['label'].lower()}, incl. GST)", 0.0
    else:
        base, src, tax = base_price, "your base price + GST (station tariff not known)", gst_pct
    df = DEMAND_FACTOR[level]
    before = base * band["factor"] * df
    final = before * (1 + tax / 100)
    return {"base": round(base, 2), "base_source": src, "time_band": band["label"], "time_factor": band["factor"],
            "demand_factor": df, "gst_pct": tax, "price_per_kwh": round(final, 2), "station_tariff": t,
            "vs_tariff_pct": round((final / t["price_per_kwh"] - 1) * 100, 1) if t else None}


HEALTHY = {"status": "healthy", "reasons": [], "advice": ""}


def rank_stations_for(raw_stations, latitude, longitude, vehicle_type, connector=None, hour=18, date=None,
                      preference="balanced", scenario="expected", base_price=10.0, gst_pct=18.0, cond=None,
                      live_places=None, live_error=None):
    """Steps 4-8 for a set of stations: expected demand for the day, per-station demand / wait / price,
    compatibility check and weighted ranking. Used by Plan my charge and by the Station finder."""
    cond = cond or HEALTHY
    days = forecast()
    day = next((d for d in days if d["date"] == date), days[0])
    weekday = pd.Timestamp(day["date"]).dayofweek
    scale = sds.scale_for(scenario, day, forecast(mode="latest"))

    feats = build_station_features(raw_stations, latitude, longitude, connector)
    # live layer: Google Places availability matched to OCM stations; Google-only stations added
    feats, extra = live_service.merge(feats, live_places or [], latitude, longitude)
    feats += [live_service.google_only_feature(p, latitude, longitude) for p in extra]
    now = datetime.now()
    is_now = day["date"] == now.date().isoformat() and abs(hour - now.hour) <= 1
    today = days[0]
    scale_now = sds.scale_for(scenario, today, forecast(mode="latest"))
    raw_by_id = {r.get("ID"): r for r in raw_stations or []}
    reports = checkin_service.recent([s["id"] for s in feats])
    sim_network.ensure(feats)              # every searched station gets a (simulated) charger feed - see sim_network
    feeds = ocpp_service.live_for([s["id"] for s in feats])       # chargers reporting over OCPP
    tariffs = tariff_service.known([s["id"] for s in feats], {k: v["connected"] for k, v in feeds.items()},
                                   simulated={k for k, v in feeds.items() if v.get("simulated")})
    adaptive_service.remember_stations(feats)
    for s in feats:
        s["compatibility"], s["connector_score"], s["compatibility_reason"] = \
            compatibility(s["connector_types"], vehicle_type, connector)
        s["tariff"] = tariff_service.resolve(s, tariffs.get(str(s["id"])))
        google = s.get("live") if s.get("live") and s["live"]["has_live_availability"] else None
        rep = reports.get(str(s["id"]))
        s["driver_reports"] = rep
        s["ocm_checkins"] = checkin_service.ocm_checkins(raw_by_id[s["id"]]) if s["id"] in raw_by_id else None
        drivers = checkin_service.to_live(rep, s["number_of_points"]) if rep and rep["is_live"] else None
        feed = feeds.get(str(s["id"]))
        # real data first: a real charger's own feed (OCPP); then the most recent of Google availability / driver
        # check-in; the simulated charger feed only when there is no real live data
        if feed and not feed.get("simulated"):
            live, s["live_source"] = feed, "ocpp"
        elif drivers and (not google or drivers["updated_min_ago"] <= (google.get("updated_min_ago") or 10 ** 6)):
            live, s["live_source"] = drivers, "driver_checkins"
        elif google:
            live, s["live_source"] = google, "google"
        elif feed:
            live, s["live_source"] = feed, "ocpp"
        else:
            live, s["live_source"] = None, None
        s["live_used"] = live
        if s["live_source"] == "google":
            adaptive_service.record(s["id"], "google", google)
        if live and not s["number_of_points"]:
            s["number_of_points"] = live["connector_count"]
        # adaptive learning: arrivals x what live readings at this station have shown (1.0 when nothing learned)
        f_h, n_h = adaptive_service.factor(s["id"], hour)
        f_now, _ = adaptive_service.factor(s["id"], now.hour)
        s["learned"] = adaptive_service.learned(s["id"])
        model = sds.station_demand(s["number_of_points"], s["max_power_kw"], weekday, hour, scale * f_h)
        if live:
            model_now = sds.station_demand(s["number_of_points"], s["max_power_kw"],
                                           pd.Timestamp(today["date"]).dayofweek, now.hour, scale_now * f_now)
            s["demand"] = sds.live_demand(live, s["max_power_kw"], model_now, model, is_now, weekday, hour,
                                          scale * f_h)
            usable = live["connector_count"] - live["out_of_service"]
            who = (f"{live['reports']} driver report{'s' if live['reports'] != 1 else ''}, {live['updated_min_ago']} min ago"
                   if s["live_source"] == "driver_checkins" else
                   f"charger feed{' (simulated)' if live.get('simulated') else ''}, {live['updated_s_ago']} s ago"
                   if s["live_source"] == "ocpp" else "Google Maps")
            if usable <= 0:
                s["status"], s["status_score"] = f"Live now ({who}): not working", 0.0
            elif live["available"] > 0:
                free = (f"{live['available']} of {usable} free" if s["live_source"] in ("google", "ocpp") or
                        live.get("free_points_reported") else "points free")
                s["status"], s["status_score"] = f"Live now ({who}): {free}", 1.0
            elif live.get("queue"):
                s["status"], s["status_score"] = f"Live now ({who}): all busy, {live['queue']} waiting", 0.15
            else:
                s["status"], s["status_score"] = f"Live now ({who}): all points busy", 0.35
        else:
            s["demand"] = {**model, "source": "calibrated" if s["learned"] else "estimated",
                           "learned_factor": f_h, "learned_readings": n_h}
            oc = s["ocm_checkins"]
            if oc and oc["latest"] and not oc["latest"]["positive"] and oc["latest"]["days_ago"] <= 30:
                s["status_score"] = min(s.get("status_score", 0.5), 0.3)
                s["status"] = f"{s.get('status', 'Unknown')} · failed charge reported {oc['latest']['days_ago']} days ago"
        src = [s.get("source") or "Open Charge Map"]
        if google:
            src.append("Google live")
        if drivers:
            src.append("driver check-ins")
        if feed:
            src.append("charger feed (OCPP)")
        s["data_source"] = " + ".join(src)
        s["price"] = _price(s, hour, s["demand"]["level"], base_price, gst_pct)
        s["price"]["demand_source"] = s["demand"]["source"]

    compatible = [s for s in feats if s["compatibility"] == "compatible"]
    check = [s for s in feats if s["compatibility"] in ("check", "unknown")]
    # fully compatible stations are recommended first; "check connector" ones only when nothing else fits
    candidates = compatible or check
    excluded = [s for s in feats if s not in candidates]
    if compatible:
        detail = f"{len(compatible)} stations suit this vehicle ({len(excluded)} others listed but not ranked)."
    elif check:
        detail = (f"No station is confirmed compatible; ranking {len(check)} stations whose connector you should "
                  "check with your vehicle.")
    else:
        detail = ("No station nearby suits this vehicle or has enough information – showing the available "
                  "information only.")

    weights = dict(PREFERENCES[preference]["weights"])
    if cond["status"] == "needs_care":          # do not reward high power for a battery that needs care
        weights["battery"] += weights["power"]
        weights["power"] = 0.0
    ranked = []
    if candidates:
        pmin = min(s["price"]["price_per_kwh"] for s in candidates)
        for s in candidates:
            w = s["demand"]["expected_wait_min"]
            p = s["price"]["price_per_kwh"]
            scores = {
                "distance": s["distance_score"],
                "price": 1.0 if p <= 0 else (pmin / p if pmin > 0 else 0.0),
                "wait": 0.0 if w is None else 1 / (1 + w / 15),
                "power": s["power_score"],
                "connector": s["connector_score"],
                "status": s["status_score"],
                "battery": battery_fit(s["max_power_kw"], cond["status"]),
            }
            s["score_breakdown"] = [{"criterion": k, "label": CRITERIA[k], "score": round(float(v), 3),
                                     "weight_pct": round(weights[k] * 100, 1),
                                     "contribution": round(weights[k] * float(v) * 100, 1)} for k, v in scores.items()]
            s["recommendation_score"] = round(sum(weights[k] * float(v) for k, v in scores.items()), 4)
        ranked = sorted(candidates, key=lambda s: -s["recommendation_score"])
        for i, s in enumerate(ranked, 1):
            s["recommendation_rank"] = i
            s["why"] = _why(s, ranked, cond)
    n_live = sum(1 for s in feats if s.get("live_source"))
    n_reports = sum(1 for s in feats if s.get("live_source") == "driver_checkins")
    n_feed = sum(1 for s in feats if s.get("live_source") == "ocpp")
    n_learned = sum(1 for s in feats if s.get("learned"))
    live_info = {"used_for": "current hour" if is_now else "adjusting the forecast for the chosen time",
                 "stations_with_live": n_live, "stations_from_checkins": n_reports, "stations_from_ocpp": n_feed,
                 "stations_learned": n_learned, "stations_total": len(feats),
                 "is_now": is_now, "status": "live" if n_live else "no_live_data",
                 "google": "not_configured" if live_error == "no_key" else ("error" if live_error else "on"),
                 "error": None if live_error in (None, "no_key") else live_error}
    return {"day": day, "scale": scale, "feats": feats, "ranked": ranked, "excluded": excluded, "live": live_info,
            "sufficient": bool(candidates), "compat_detail": detail, "weights": weights,
            "stations": ranked + [{**s, "recommendation_rank": None, "recommendation_score": None} for s in excluded]}


def recommend_stations(raw_stations, latitude, longitude, location_label, vehicle_type, connector=None, hour=18,
                       date=None, preference="balanced", scenario="expected", base_price=10.0, gst_pct=18.0,
                       live_places=None, live_error=None):
    """Station finder: ranking without the battery analysis (battery treated as healthy)."""
    r = rank_stations_for(raw_stations, latitude, longitude, vehicle_type, connector, hour, date, preference,
                          scenario, base_price, gst_pct, live_places=live_places, live_error=live_error)
    day = r["day"]
    return {
        "inputs": {"location": location_label, "latitude": latitude, "longitude": longitude, "hour": hour,
                   "date": day["date"], "vehicle_type": vehicle_type, "connector": connector, "preference": preference,
                   "preference_label": PREFERENCES[preference]["label"], "scenario": scenario,
                   "scenario_label": sds.SCENARIOS[scenario], "base_price": base_price, "gst_pct": gst_pct},
        "demand": {"date": day["date"], "weekday": day["weekday"], "city_forecast_sessions": day["predicted_sessions"],
                   "city_level": day["level"], "hour_busyness": hour_band(hour), "scale": r["scale"]},
        "sufficient_station_data": r["sufficient"], "compatibility_summary": r["compat_detail"],
        "weights_pct": {k: round(v * 100, 1) for k, v in r["weights"].items()},
        "station_count": len(r["feats"]), "stations": r["stations"], "live": r["live"],
        "notes": [
            "Battery condition is not considered here – use Plan my charge to include your battery.",
            "Live data – the charger's own feed (OCPP), Google Maps availability (with a key) or driver check-ins "
            "from the last 45 minutes – is used "
            "where available and tagged Live; other "
            "stations use the queueing model learned on Palo Alto charging sessions – tagged Estimated.",
            "Station tariff = what the station charges (live from its charger feed, reported by drivers, or listed on "
            "Open Charge Map). Dynamic price = this system's recommendation: the tariff (or your base price) adjusted "
            "for time of day and the station's demand.",
            "Station details come from Open Charge Map (plus Google Maps); reported tariffs may be outdated.",
        ],
    }


def plan(vehicle, raw_stations, latitude, longitude, location_label, hour, date=None, preference="balanced",
         connector=None, scenario="expected", base_price=10.0, gst_pct=18.0, new_full_range_km=None, history=None,
         trip_history=None, live_places=None, live_error=None):
    steps = [{"step": 1, "name": "User input", "status": "done",
              "detail": f"{vehicle['vehicle_type']} registered {vehicle['registration_year']}, "
                        f"{vehicle['odometer_km']:,.0f} km; near {location_label}; {hour:02d}:00; "
                        f"preference {PREFERENCES[preference]['label'].lower()}."},
             {"step": 2, "name": "Input processing", "status": "valid", "detail": "All inputs are valid."}]
    # ---- 3. battery analysis
    readings = [(r["soc_pct"], r["range_km"]) for r in vehicle["readings"]]
    rul = predict_rul(vehicle["vehicle_type"], vehicle["registration_year"], vehicle["odometer_km"],
                      vehicle["km_per_year"], readings=readings, vehicle_name=vehicle.get("vehicle_name"))
    anomaly = anomaly_service.detect(vehicle["vehicle_type"], vehicle["registration_year"], vehicle["odometer_km"],
                                     vehicle["km_per_year"], readings, new_full_range_km, history=history,
                                     trip_history=trip_history)
    cond = battery_condition(rul, anomaly)
    steps.append({"step": 3, "name": "Battery analysis", "status": "done",
                  "detail": rul["message"] + (f" Anomaly check: {anomaly['status']}." if anomaly.get("health_checked")
                                              else f" Anomaly check (usage and readings): {anomaly['status']}; "
                                                   "battery health is measured once a saved reading is 30+ days old.")})

    # ---- 4-8. demand, stations, prices, sufficient data, recommendation
    r = rank_stations_for(raw_stations, latitude, longitude, vehicle["vehicle_type"], connector, hour, date,
                          preference, scenario, base_price, gst_pct, cond, live_places, live_error)
    day, scale, feats, ranked, excluded, sufficient, weights = (r["day"], r["scale"], r["feats"], r["ranked"],
                                                               r["excluded"], r["sufficient"], r["weights"])
    steps.append({"step": 4, "name": "Demand forecasting", "status": "done",
                  "detail": f"{day['weekday']} {day['date']}: {day['predicted_sessions']:.0f} sessions expected "
                            f"({day['level']}). Scenario: {sds.SCENARIOS[scenario]} (demand × {scale})."})
    steps.append({"step": 5, "name": "Station information", "status": "done" if feats else "empty",
                  "detail": f"{len(feats)} stations (Open Charge Map + Google Maps); live availability for "
                            f"{r['live']['stations_with_live']}."})
    steps.append({"step": 6, "name": "Adaptive pricing", "status": "done" if feats else "empty",
                  "detail": "Price estimated for each station from its tariff (or your base price), the hour "
                            "and the station's predicted demand."})
    steps.append({"step": 7, "name": "Sufficient station data?", "status": "yes" if sufficient else "no",
                  "detail": r["compat_detail"]})
    best = ranked[0] if ranked else None
    steps.append({"step": 8, "name": "Station recommendation", "status": "done" if best else "skipped",
                  "detail": (f"Best for '{PREFERENCES[preference]['label']}': {best['name']}." if best
                             else "No recommendation – not enough suitable station data.")})

    # ---- 9. final output
    steps.append({"step": 9, "name": "Final output", "status": "done",
                  "detail": "Battery status, demand at stations, estimated prices and the recommended station."})
    final = {
        "battery": {"rul_years": rul["rul_years"], "rul_km": rul["rul_km"], "rul_cycles": rul["rul_cycles"],
                    "soh_now_pct": rul["soh_now_pct"], "evidence": rul["evidence"], "message": rul["message"],
                    "anomaly_status": anomaly["status"], "anomaly_message": anomaly["message"],
                    "anomaly_health_checked": anomaly.get("health_checked", False),
                    "anomaly_reference": (anomaly.get("reference") or {}).get("label"), "condition": cond},
        "demand": {"date": day["date"], "weekday": day["weekday"], "city_forecast_sessions": day["predicted_sessions"],
                   "city_level": day["level"], "hour_busyness": hour_band(hour), "scenario": scenario,
                   "scenario_label": sds.SCENARIOS[scenario], "scale": scale},
        "recommended": None if not best else {
            "name": best["name"], "distance_km": best["distance_km"], "price_per_kwh": best["price"]["price_per_kwh"],
            "wait": best["demand"]["wait_label"], "demand_level": best["demand"]["level"],
            "max_power_kw": best["max_power_kw"], "score": round(best["recommendation_score"] * 100),
            "why": best["why"], "latitude": best["latitude"], "longitude": best["longitude"]},
    }
    return {
        "inputs": {"location": location_label, "latitude": latitude, "longitude": longitude, "hour": hour,
                   "date": day["date"], "preference": preference, "preference_label": PREFERENCES[preference]["label"],
                   "connector": connector, "scenario": scenario, "base_price": base_price, "gst_pct": gst_pct},
        "steps": steps, "sufficient_station_data": sufficient, "final": final, "live": r["live"],
        "rul": rul, "anomaly": anomaly,
        "weights_pct": {k: round(v * 100, 1) for k, v in weights.items()},
        "weights_note": ("Battery needs care: charging power is not rewarded; its weight moved to battery-friendly."
                         if cond["status"] == "needs_care" else None),
        "stations": r["stations"],
        "notes": [
            "Live data – the charger's own feed (OCPP), Google Maps availability (with a key) or driver check-ins "
            "from the last 45 minutes – is used "
            "where available and tagged Live; other "
            "stations use the queueing model learned on Palo Alto charging sessions (validated on 2019 data).",
            "Station tariff = what the station charges (live from its charger feed, reported by drivers, or listed on "
            "Open Charge Map). Dynamic price = this system's recommendation: the tariff (or your base price) adjusted "
            "for time of day and the station's demand.",
            "Battery condition changes which station is recommended, never the price.",
        ],
    }


def _why(s, ranked, cond):
    parts = [f"{s['distance_km']:.1f} km away", f"₹{s['price']['price_per_kwh']:.2f}/kWh",
             s["demand"]["wait_label"].lower() if s["demand"]["expected_wait_min"] is not None else "may be full",
             f"{s['demand']['level']} demand"]
    if s.get("max_power_kw"):
        parts.append(f"{s['max_power_kw']:g} kW")
    if cond["status"] == "needs_care" and s.get("max_power_kw") and s["max_power_kw"] <= 22:
        parts.append("gentle charging suits your battery")
    top = sorted(s["score_breakdown"], key=lambda b: -b["contribution"])[:2]
    return f"{', '.join(parts)}. Strongest points: {top[0]['label'].lower()} and {top[1]['label'].lower()}."


LIVE_AHEAD_H = 3        # a live snapshot informs the next few hours only; later hours use the model


def station_demand_day(raw_stations, latitude, longitude, location_label, vehicle_type, date=None,
                       scenario="expected", live_places=None, live_error=None, hour=None, base_price=10.0,
                       gst_pct=18.0):
    """Demand at each nearby station, hour by hour, for one day.
    Each station's demand comes from its own number of charging points and power (queueing model learned on
    the ChargePoint sessions, scaled by the day's expected demand). Where live data exists (Google Maps or a
    driver report), the current hour uses it directly and the next few hours are adjusted to it."""
    days = forecast()
    day = next((d for d in days if d["date"] == date), days[0])
    now = datetime.now()
    today = day["date"] == now.date().isoformat()
    hour = hour if hour is not None else (now.hour if today else 18)
    r = rank_stations_for(raw_stations, latitude, longitude, vehicle_type, None, hour, day["date"], "balanced",
                          scenario, base_price, gst_pct, live_places=live_places, live_error=live_error)
    weekday = pd.Timestamp(day["date"]).dayofweek
    scale = r["scale"]
    out = []
    for s in sorted(r["feats"], key=lambda x: x["distance_km"]):      # nearest first (fit is shown per station)
        pts, kw, live = s["number_of_points"], s["max_power_kw"], s.get("live_used")
        learned = s.get("learned")
        model_now = (sds.station_demand(pts, kw, weekday, now.hour, scale * adaptive_service.factor(s["id"], now.hour)[0])
                     if today else None)
        prof = []
        for h in range(24):
            f_h = adaptive_service.factor(s["id"], h)[0]
            m = sds.station_demand(pts, kw, weekday, h, scale * f_h)
            if live and today and now.hour - 1 <= h <= now.hour + LIVE_AHEAD_H:
                d = sds.live_demand(live, kw, model_now, m, abs(h - now.hour) <= 1, weekday, h, scale * f_h)
            else:
                d = {**m, "source": "calibrated" if learned else "estimated"}
            prof.append({"hour": h, "level": d["level"], "utilisation_pct": d["utilisation_pct"],
                         "wait_label": d["wait_label"], "expected_wait_min": d["expected_wait_min"],
                         "source": d["source"],
                         "price_per_kwh": _price(s, h, d["level"], base_price, gst_pct)["price_per_kwh"]})
        open_h = [x for x in prof if 8 <= x["hour"] <= 21 and (not today or x["hour"] >= now.hour)] or [x for x in prof if 8 <= x["hour"] <= 21]
        peak = sorted(prof, key=lambda x: -x["utilisation_pct"])[:3]
        quiet = min(open_h, key=lambda x: (x["utilisation_pct"], x["hour"]))
        cheap = min(open_h, key=lambda x: (x["price_per_kwh"], x["utilisation_pct"], x["hour"]))
        focus = prof[hour]
        if s.get("live_source"):
            availability = (s.get("status") or "").replace("Live now ", "Live ")
            availability_note = (None if focus["source"] == "live" else
                                 "Chosen hour is later – forecast adjusted to this report" if focus["source"] == "live-adjusted"
                                 else "Not the current hour – forecast used")
        else:
            availability = "No live data"
            availability_note = (f"Estimated from {pts or 1} point{'s' if (pts or 1) != 1 else ''}"
                                 f"{' (assumed)' if not pts else ''} · {s.get('status') or 'status unknown'}")
        out.append({"id": s["id"], "name": s["name"], "distance_km": s["distance_km"], "address": s.get("address"),
                    "number_of_points": pts, "points_assumed": s["demand"].get("points_assumed", False),
                    "max_power_kw": kw, "compatibility": s["compatibility"], "live_source": s.get("live_source"), "live_used": live,
                    "source": s.get("source") or "Open Charge Map",
                    "access": s.get("access", "public"), "access_note": s.get("access_note"),
                    "status": s.get("status"), "data_source": s.get("data_source"),
                    "driver_reports": s.get("driver_reports"), "latitude": s.get("latitude"),
                    "longitude": s.get("longitude"), "focus": focus, "profile": prof,
                    "availability": availability, "availability_note": availability_note, "tariff": s.get("tariff"),
                    "price": _price(s, hour, focus["level"], base_price, gst_pct), "cheapest_hour": cheap,
                    "peak_hours": sorted(x["hour"] for x in peak), "quietest": quiet,
                    "day_average_pct": round(sum(x["utilisation_pct"] for x in prof) / 24, 1)})
    levels = {lv: sum(1 for x in out if x["focus"]["level"] == lv) for lv in ("high", "medium", "low")}
    return {"inputs": {"location": location_label, "latitude": latitude, "longitude": longitude,
                       "vehicle_type": vehicle_type, "date": day["date"], "weekday": day["weekday"],
                       "scenario": scenario, "scenario_label": sds.SCENARIOS[scenario], "hour": hour,
                       "base_price": base_price, "gst_pct": gst_pct},
            "time_band": time_band(hour), "demand_factors": DEMAND_FACTOR,
            "price_rule": ("Dynamic price per kWh = station tariff × time-of-day factor × demand factor. The station "
                           "tariff is what the station charges, incl. GST: live from its charger feed (OCPP), the median "
                           "of driver reports from the last 30 days, or the price listed on Open Charge Map; when none "
                           "is known, your base price + GST is used. The demand factor comes from the station's own "
                           "availability – live when available, otherwise estimated from its points and power: "
                           "low ×0.90, medium ×1.00, high ×1.10."),
            "is_today": today, "focus_hour": hour, "now_hour": now.hour if today else None,
            "city": {"sessions": day["predicted_sessions"], "level": day["level"], "scale": scale},
            "station_count": len(out), "levels_at_focus_hour": levels, "stations": out, "live": r["live"],
            "method": ("Each station's demand is calculated from its own number of charging points and charging "
                       "power with a queueing model (arrivals per point per hour and plug-in times learned from "
                       "the ChargePoint sessions), scaled by the day's expected demand. Live data – the charger's "
                       "own feed (OCPP), Google Maps availability or a driver report from the last 45 minutes – "
                       f"replaces the estimate for the current hour and adjusts the next {LIVE_AHEAD_H} hours. "
                       "Stations with live readings in the past are recalibrated to them (adaptive learning) – "
                       "tagged 'Learned'."),
            "level_rule": "Level by share of the station's points in use: low < 35 %, medium 35–65 %, high > 65 %."}
