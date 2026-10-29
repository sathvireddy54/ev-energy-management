"""
Automated tests for the core calculations and rules.

Run from the backend folder:   pip install pytest   then   python -m pytest -q
Each test uses a temporary database, so the app's own data (data/records.db) is never touched.
"""
import random
from datetime import datetime, timedelta

import pytest

from app import db as app_db
from app import (adaptive_service, auth_service, checkin_service, history_service, ocpp_service, osm_service,
                 station_demand_service as sds, station_filter, tariff_service)
from app.pricing_service import quote
from app.recommendation_service import compatibility
from app.rul_service import predict_rul


@pytest.fixture()
def temp_db(tmp_path):
    """Point every service that stores data at a fresh temporary database."""
    db = tmp_path / "test.db"
    mods = (adaptive_service, auth_service, checkin_service, history_service, ocpp_service, tariff_service)
    old = {m: m.DB for m in mods}
    for m in mods:
        m.DB = db
    adaptive_service._cache["dirty"] = True
    auth_service._fails.clear()
    if app_db.using_postgres():          # DATABASE_URL set: start each test from empty tables
        with app_db.connect(None) as c:
            c.execute("DROP TABLE IF EXISTS users, sessions, readings, trips, checkins, observations, station_meta, meta")
        app_db._ddl_done.clear()
    yield db
    for m, d in old.items():
        m.DB = d
    adaptive_service._cache["dirty"] = True


# ------------------------------------------------------------------ 1. battery RUL
def test_soc_does_not_change_rul():
    """Project rule: the current state of charge must not change remaining life (same full-charge range)."""
    a = predict_rul("4W", 2021, 45000, 12000, readings=[(80, 240)])
    b = predict_rul("4W", 2021, 45000, 12000, readings=[(40, 120)])
    assert a["rul_years"] == b["rul_years"]


def test_more_driving_means_less_life_left():
    light = predict_rul("4W", 2021, 30000, 8000, readings=[(80, 240)])
    heavy = predict_rul("4W", 2021, 90000, 25000, readings=[(80, 240)])
    assert heavy["rul_years"]["mid"] < light["rul_years"]["mid"]


def test_all_vehicle_types_supported():
    for vt, rng in (("2W", 90), ("3W", 110), ("4W", 250)):
        r = predict_rul(vt, 2022, 15000, 8000, readings=[(90, rng)])
        assert r["rul_years"]["mid"] is None or r["rul_years"]["mid"] >= 0


# ------------------------------------------------------------------ 2. pricing
def test_pricing_formula():
    """price = base x time factor x demand factor + GST (peak 1.20, high demand 1.10, GST 18 %)."""
    q = quote(18, 10.0, 18.0, "high")
    assert q["price_per_kwh"] == pytest.approx(10 * 1.20 * 1.10 * 1.18, abs=0.01)
    assert quote(3, 10.0, 18.0, "low")["price_per_kwh"] < q["price_per_kwh"]


# ------------------------------------------------------------------ 3. station demand (queueing model)
def test_erlang_c_bounds():
    assert sds.erlang_c(2, 0) == 0.0
    assert sds.erlang_c(2, 2.5) == 1.0
    assert 0 < sds.erlang_c(4, 2.0) < 1


def test_more_points_less_busy():
    one = sds.station_demand(1, 7.4, 2, 18, 1.0)
    six = sds.station_demand(6, 7.4, 2, 18, 1.0)
    assert six["p_all_busy_pct"] <= one["p_all_busy_pct"]


# ------------------------------------------------------------------ 4. compatibility and station filter
@pytest.mark.parametrize("connectors, vt, expected", [
    (["CCS (Type 2)"], "4W", "compatible"),
    (["CCS (Type 2)"], "2W", "check"),
    (["BS1363 3 Pin 13 Amp"], "2W", "compatible"),
    (["Unknown"], "4W", "unknown"),
    (["GB-T DC - GB/T 20234.3"], "2W", "not_compatible"),
])
def test_connector_compatibility(connectors, vt, expected):
    assert compatibility(connectors, vt)[0] == expected


@pytest.mark.parametrize("name, usage, expected", [
    ("Tata Power EZ Charge - Inorbit Mall", "Public - Membership Required", "public"),
    ("Prestige Lakeside Apartments", "", "residential"),
    ("Sais Balaji Classic", "", "unclear"),
    ("BMW Navnit Motors", "", "dealership"),
    ("Marriott Hotel Whitefield", "", "venue"),
    ("Tata Power EZ Charge", "Private - Restricted Access", "private"),
])
def test_station_filter(name, usage, expected):
    station = {"AddressInfo": {"Title": name}, "UsageType": {"Title": usage}, "Connections": []}
    assert station_filter.classify(station)[0] == expected


def test_merge_keeps_different_networks_at_one_site():
    def st(i, name, src=None):
        d = {"ID": i, "AddressInfo": {"Title": name, "Latitude": 17.4345, "Longitude": 78.3867},
             "OperatorInfo": {"Title": name}, "Connections": [], "NumberOfPoints": None}
        if src:
            d["_source"] = src
        return d
    ocm = [st(1, "Tata Power EZ Charge")]
    tomtom = [st("t1", "Tata Power", "TomTom"), st("t2", "Statiq", "TomTom")]
    ids = [x["ID"] for x in osm_service.merge(ocm, tomtom)]
    assert ids == [1, "t2"]          # same Tata Power counted once, Statiq beside it kept


# ------------------------------------------------------------------ 5. accounts and per-user data
def test_passwords_are_hashed_and_checked(temp_db):
    user = auth_service.register("a@example.com", "A", "correct-horse-1")
    with app_db.connect(temp_db) as c:
        row = c.execute("SELECT pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert "correct-horse-1" not in row[0]
    auth_service.login("a@example.com", "correct-horse-1")
    with pytest.raises(auth_service.AuthError):
        auth_service.login("a@example.com", "wrong-password")


def test_demo_account_survives_and_keeps_its_password(temp_db):
    assert auth_service.ensure_account("demo@example.com", "Demo", "demo-pass-1")
    auth_service.login("demo@example.com", "demo-pass-1")
    assert auth_service.ensure_account("demo@example.com", "Demo", "demo-pass-2")   # second start: same account
    auth_service.login("demo@example.com", "demo-pass-2")
    assert auth_service.ensure_account("not-an-email", "Demo", "demo-pass-1") is None


def test_login_paused_after_five_wrong_passwords(temp_db):
    auth_service.register("b@example.com", "B", "correct-horse-2")
    for _ in range(5):
        with pytest.raises(auth_service.AuthError):
            auth_service.login("b@example.com", "nope")
    with pytest.raises(auth_service.AuthError, match="Too many"):
        auth_service.login("b@example.com", "correct-horse-2")


def test_readings_are_private_per_user(temp_db):
    a = auth_service.register("c@example.com", "C", "password-c1")
    b = auth_service.register("d@example.com", "D", "password-d1")
    tok = auth_service.current_user.set(a)
    history_service.add("anything", [(80, 240)], 20000)
    auth_service.current_user.reset(tok)
    tok = auth_service.current_user.set(b)
    assert history_service.get(a["vehicle_id"]) == []      # B cannot read A's readings, even by A's id
    auth_service.current_user.reset(tok)
    tok = auth_service.current_user.set(a)
    assert len(history_service.get("anything")) == 1
    auth_service.current_user.reset(tok)


# ------------------------------------------------------------------ 6. live data and tariffs
def test_driver_reports_beat_a_simulated_feed_tariff(temp_db):
    tariff_service.add("s1", 21.0, "ocpp")
    for p in (14.0, 15.0, 30.0):
        tariff_service.add("s1", p, "driver_report")
    real_feed = tariff_service.known(["s1"])["s1"]
    simulated_feed = tariff_service.known(["s1"], simulated={"s1"})["s1"]
    assert real_feed["source"] == "ocpp"
    assert simulated_feed["source"] == "driver_report" and simulated_feed["price_per_kwh"] == 15.0   # median


def test_listed_price_beats_a_simulated_feed_tariff(temp_db):
    tariff_service.add("s2", 21.0, "ocpp")
    sim = tariff_service.known(["s2"], simulated={"s2"})["s2"]
    station = {"price_comparable": True, "observed_price_inr": 18.0, "usage_cost_raw": "Rs 18/kWh"}
    assert tariff_service.resolve(station, sim)["source"] == "open_charge_map"
    assert tariff_service.resolve({}, sim)["source"] == "ocpp"                 # nothing listed: the feed's tariff
    assert tariff_service.resolve(station, tariff_service.known(["s2"])["s2"])["source"] == "ocpp"   # a real feed wins


def test_impossible_tariff_rejected(temp_db):
    with pytest.raises(ValueError):
        tariff_service.add("s1", 0.5, "driver_report")


# ------------------------------------------------------------------ 7. adaptive learning
def test_adaptive_learning_recovers_a_known_factor(temp_db):
    """A station twice as busy as the model expects: learning must find a factor near 2."""
    random.seed(1)
    adaptive_service.remember_stations([{"id": "x", "name": "X", "number_of_points": 4, "max_power_kw": 60.0}])
    start = datetime(2026, 10, 5).astimezone()
    for step in range(7 * 24 * 4):
        t = start + timedelta(minutes=15 * step)
        u = sds.station_demand(4, 60.0, t.weekday(), t.hour, 2.0)["utilisation_pct"] / 100
        busy = sum(random.random() < u for _ in range(4))
        adaptive_service.record("x", "driver_report",
                                {"connector_count": 4, "available": 4 - busy, "out_of_service": 0, "queue": 0}, when=t)
    factor = adaptive_service.calibration()["x"]["R"]
    assert 1.6 < factor < 2.4
