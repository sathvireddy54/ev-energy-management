# Smart EV Energy Management

**An Adaptive Data-Driven Framework for Smart Electric Vehicle Energy Management** – one FastAPI application and
one responsive web dashboard (laptop, tablet, mobile) with six modules.

Battery modules use **only what an owner can read from the dashboard or RC**: vehicle type, registration year,
odometer, distance per year and 1–3 dashboard readings (SOC % + range shown). No SoH, voltage, temperature or
battery kWh is asked for. The anomaly check measures health against the owner's own earlier readings, which the app
saves at every check; the full-charge range when new can be added if the owner knows it, but is optional.

**Plan my charge** runs the whole project workflow in one step: input → validation → battery analysis (RUL +
anomaly) → demand forecast (city + each station) → station information → price for each station → sufficient
data? → recommendation (your preference + battery condition) → final output.

| # | Module | Method | Data |
|---|---|---|---|
| 1 | **Battery RUL** – health now, degradation chart, years / km / charge cycles left | Calendar + cycle ageing law projected to 80 % health, with a range for similar batteries | NASA cells (run to end of life) + 2,742 real used EVs (Zenodo) |
| 2 | **Battery anomaly detection** – battery behaviour: trip check (discharge vs the dashboard's own estimate), reading consistency, capacity fade, usage | Trip rule + Isolation Forests on age, distance per year and health | 2,724 real used EVs (Zenodo) |
| 3 | **Charging-demand forecast** – 7-day forecast, low/medium/high days, busy periods, evaluation | Random Forest on calendar + 1/2/7-day lags (recursive for the horizon) | City of Palo Alto charging sessions 2011–2020 |
| 3b | **Station demand and waiting time** – busy-ness, chance all points busy, expected wait per station | Learned per-point arrival rates and plug-in times + M/M/c queue (Erlang C) | Palo Alto ChargePoint sessions (per-port plug-in/out times) |
| 4 | **Adaptive dynamic pricing** – peak/off-peak, demand adjustment, cost breakdown, comparisons | Documented rules: base × time factor × demand factor + GST | Demand forecast |
| 5 | **Station recommendation** (Station finder page, and inside Plan my charge) – ranked stations, per-station price, demand and wait, explanations, map | Weighted multi-criteria scoring with your preference and battery condition | Open Charge Map, OpenStreetMap |
| 6 | **Dashboard** – overview, alerts, backend and model status | – | – |
| Live | **Live station layer** – real-time availability per connector (free / in use / out of service) for stations whose network shares it | Google Places API (New), Nearby Search `evChargeOptions` | Google Maps (e.g. Statiq, ElectricPe in India) |

## Folder structure

```
EV Energy Management/
├── backend/
│   ├── app/
│   │   ├── main.py                    FastAPI app – all /api routes, serves the dashboard
│   │   ├── plan_service.py            "Plan my charge" – the full workflow in one request
│   │   ├── station_demand_service.py  per-station demand and waiting time
│   │   ├── rul_service.py             1. vehicle RUL, health now, projection curve
│   │   ├── anomaly_service.py         2. Isolation Forest anomaly check
│   │   ├── history_service.py         the owner's saved readings (health reference)
│   │   ├── demand_service.py          3. forecast, demand levels, busy periods
│   │   ├── pricing_service.py         4. documented pricing rules
│   │   ├── recommendation_service.py  5. compatibility, explanations (uses recommendation_engine.py)
│   │   ├── recommendation_engine.py   station features + weighted scoring
│   │   ├── ocm_service.py             location search + Open Charge Map
│   │   ├── live_service.py            live availability (Google Places) matched to OCM stations
│   │   └── history_service.py         the owner's saved readings and trips (data/records.db)
│   ├── training/
│   │   ├── train_rul_model.py         ageing model (NASA + Zenodo)
│   │   ├── train_anomaly.py           Isolation Forest (Zenodo)
│   │   └── train_demand.py            demand model (needs data/raw/ChargePoint CSV)
│   ├── data/                          datasets + small files the app reads
│   ├── models/                        trained models and metrics_*.json
│   ├── requirements.txt
│   └── .env.example                   copy to .env and add your Open Charge Map API key
└── frontend/
    ├── index.html, app.css, app.js    the dashboard
    ├── vendor/chart.umd.js            Chart.js 4.4.1 (bundled, works offline)
    └── leaflet/                       Leaflet 1.9.4 map library (bundled)
```

## Setup (first time)

```bash
cd ~/Downloads/"EV Energy Management"/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp ../../"MAJOR PROJECT"/backend/.env .env      # reuses your Open Charge Map API key
```

The trained models are included, so no training is needed to run the app.

## Run

```bash
cd ~/Downloads/"EV Energy Management"/backend
source .venv/bin/activate
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000**. API documentation: **http://127.0.0.1:8000/docs**.

## Deploy online (Render, free)

The project is ready for [Render](https://render.com): `render.yaml` (settings), `.python-version`, exact package
versions in `backend/requirements.txt`, and `.gitignore` (keeps `.env`, `.venv` and the local database out of GitHub).

1. Put the project on GitHub (the `EV Energy Management` folder is the repository root):
   ```bash
   cd ~/Downloads/"EV Energy Management"
   git init && git add . && git commit -m "EV energy management"
   git branch -M main
   git remote add origin https://github.com/<your-user>/ev-energy-management.git
   git push -u origin main
   ```
   Check on GitHub that there is **no `.env` file** in the repository.
2. On render.com (sign in with GitHub): **New → Blueprint** → choose the repository → Render reads `render.yaml`.
   Enter `OCM_API_KEY` and `TOMTOM_API_KEY` when asked (same values as in `backend/.env`) → **Apply**.
3. After the build (~5 min) the app is at `https://ev-energy-management.onrender.com` (or the name Render shows).
4. Updates: `git add . && git commit -m "update" && git push` – Render redeploys automatically.

Free-plan behaviour: the service sleeps after 15 min without visits (first visit then takes ~50 s); the SQLite
database (accounts, readings, driver reports) starts empty after each restart or deploy. Demo reports against the
online app: `python -m training.demo_reports --url https://<your-app>.onrender.com "Madhapur, Hyderabad"`.

## User accounts and theme

The app opens with **Log in / Create account**. Every page and API call (except login, health/status and the
charger side `/api/ocpp/...`) needs a logged-in user.
* Passwords are stored only as salted PBKDF2-HMAC-SHA256 hashes (240,000 iterations); sessions are random tokens in
  an HttpOnly cookie (30 days), stored as SHA-256 hashes. 5 wrong passwords in 10 minutes pause logins for that email.
* Battery readings and trips are saved under the user's own account; readings saved in the browser before the
  account was created move to it on sign-up.
* Settings follow the user to any browser: theme (☀ light / ☾ dark / ◐ follow the device), vehicle profile and
  live updates. `backend/app/auth_service.py`; data in `data/records.db` (tables `users`, `sessions`).

## API

| Method | Path | Purpose |
|---|---|---|
| GET, POST, DELETE | `/api/history/{vehicle_id}`, `/api/history` | Saved readings; add an earlier reading |
| GET | `/api/recommendations?q=Madhapur, Hyderabad&vehicle_type=4W&hour=18` | Station finder: ranked stations with per-station demand, wait and price |
| POST | `/api/plan` | Whole workflow: battery, station demand and price, recommendation |
| POST | `/api/rul` | Vehicle RUL, health now, projection |
| POST | `/api/anomaly` | Anomaly status, measurements, alerts |
| GET | `/api/anomaly/examples` | Example inputs + detector evaluation |
| GET | `/api/demand` | 7-day forecast, levels, history, test predictions, busy periods, evaluation |
| GET | `/api/pricing?hour=18&base_price=10&gst_pct=18&energy_kwh=20` | Price, breakdown, comparisons, rules |
| GET | `/api/status` | Backend, model and live-data status |

Example vehicle input (RUL; anomaly adds `"new_full_range_km": 312`):

```json
{"vehicle_type": "4W", "registration_year": 2021, "odometer_km": 60000, "km_per_year": 12000,
 "readings": [{"soc_pct": 80, "range_km": 220}], "vehicle_name": "Tata Nexon EV"}
```

## Results (from the training scripts)

| Module | Evaluation | Result |
|---|---|---|
| Battery health (real cars) | 2,742 cars, 5-fold CV | MAE 2.29 points (baseline 1.8 %/year: 2.65) |
| Vehicle RUL | NASA cells held out, health ≤ 90 % | MAE 7.0 cycles |
| RUL range | share of real cars inside, by age (target 80 %) | 70 / 81 / 81 / 80 / 79 % |
| Anomaly detection | 5-fold CV on real cars, injected faults | 3.0 % false alarms; severe degradation (−15 pts) flagged 92 % (62 % anomalous) |
| Demand forecast | chronological test 2019-02 → 2020-12 | MAE 10.4 sessions/day, R² 0.917 (baselines 14.3 / 13.6); 7th day MAE 12.4 |
| Station demand | trained 2018, tested 2019 on 33 stations | busy points MAE 0.18 (flat average 0.33), correlation 0.78; chance all points busy MAE 0.09 (0.13) |

## Documented rules

**Anomaly status:** anomalous = Isolation Forest score < 0; warning = score below the 5th percentile of normal cars,
readings disagreeing by more than 15 %, the full-charge range more than 15 % below a reading from the last 90 days, or a
decline more than 5 points faster than the ageing model expects since the first saved reading; otherwise normal.
**Trip check (discharge behaviour, works on the first use):** before and after a ride the owner notes battery % and
range shown, and km ridden. promised full-charge range = range at start ÷ SOC at start; delivered = km ridden ÷ SOC
used; gap = (promised − delivered) ÷ promised. Normal ≤ 20 %, warning 20–35 % (or delivered > 20 % above promised:
unreliable estimate), anomalous > 35 %; two of the last three trips over a threshold escalate. Valid trip: ≥ 10 % of
battery used, same ride mode, no charging. Thresholds are design rules (no public dataset labels battery faults; the
EV predictive-maintenance dataset was checked and rejected because its SoC values are random).
**Health reference:** the range when new if given, otherwise the owner's earliest saved readings (at least 30 days old):
health now = typical health at that first reading (ageing model) × full-charge range now ÷ full-charge range then.
With no reference yet (first check), a second Isolation Forest on age + km per year gives the status
(false alarms 3.1 %), the readings are checked for consistency, and battery health is shown as pending.

**Upcoming dates:** the data ends in 2020, so each upcoming date is matched to the same weekday and week of the normal
year 2019; the Random Forest predicts that day from its real 1, 2 and 7-day-earlier demand. The post-COVID
"latest-data" forecast (1–7 Jan 2021) is shown separately.
**Demand levels:** low / medium / high = tertiles of daily sessions in 2019.
**Busy periods:** the top 10 % of weekday-hour slots by share of sessions (2019).

**Plan my charge – per station:** expected busy points = arrivals per point (by weekday and hour) × points × demand
scenario, carried forward by how long cars stay plugged in; chance all points busy and expected wait from Erlang C.
Station demand level: utilisation < 35 % low, > 65 % high. Station price = its reported per-kWh tariff (or your base
price + GST) × time factor × station demand factor. Battery condition is "needs care" when the anomaly check warns,
estimated health < 85 % or < 3 years are left; then charging power is no longer rewarded and gentle (≤ 22 kW) charging
is preferred. Preferences: balanced, cheapest, nearest, least waiting / fastest. Fully compatible stations are ranked
first; "check connector" stations only when none are compatible.

**Price per kWh** (Dynamic pricing page) = base price × time factor × demand factor, then + GST (default 18 %).
Time: peak 17:00–20:59 × 1.20, normal 10:00–16:59 × 1.05, off-peak × 0.90. Demand: low × 0.90, medium × 1.00,
high × 1.10. Battery remaining life is **not** used in the price, so owners of older batteries are not charged more.

**Recommendation weights:** distance 25 %, price 20 %, power 15 %, connector compatibility 15 %, status 15 %,
capacity 10 %. Price counts only when a station reports a per-kWh price. Compatibility: connector names matched with
those used by the chosen vehicle type (compatible 1.0, check 0.5, not compatible 0).

## Station sources

Stations come from **Open Charge Map** and **OpenStreetMap** (`amenity=charging_station`, via the free Overpass API,
no key). Open Charge Map alone lists only a few stations in many Indian cities (8 in Hyderabad in our searches), so
every search returned the same stations. OpenStreetMap stations are converted to the same shape (sockets →
connectors, `capacity` → points, `socket:*:output` → kW, `charge` → tariff); an OSM station within 120 m of an Open
Charge Map station is treated as the same one. `backend/app/osm_service.py`. If Overpass cannot be reached, searches
continue with Open Charge Map only. Coverage of both is community-mapped and incomplete.

**TomTom (optional, free key without card).** Both volunteer databases list very few stations in some cities (in
our searches: ~8 for west Hyderabad on Open Charge Map, ~1 on OpenStreetMap, against 90+ near Indiranagar,
Bengaluru). TomTom's Search API (Nearby Search, category 7309 "electric vehicle station") adds its commercial map's
stations across India with connector types and power. Key `TOMTOM_API_KEY` in `backend/.env`; one query per ~25 km
area, cached 6 h. `backend/app/tomtom_service.py`. Order of merging: Open Charge Map → TomTom → OpenStreetMap.
Every search is logged in `data/search_log.jsonl` (place found, stations per source, errors).

**Only stations drivers can use** (`backend/app/station_filter.py`). Each station is classified from its name,
operator and the source's access data: known charging network or a name that says charging station → shown;
car showroom → shown, labelled *Dealership*; hotel / office / store / mall → shown, labelled *Venue*;
residential (apartment, residency, society, gated community …), marked private / restricted, or a name with no
sign of a charging station → left out (the count is shown under "Stations found").

## Live station layer (real time)

Three live sources, combined per station (the most recent live source wins):

1. **Driver check-ins (free, built in):** on any station card a driver reports *Points free* (optionally x of y),
   *All busy*, *Queue* (n waiting) or *Not working*. Reports from the last 45 minutes are live; demand, waiting time
   (queue: (n + 1) × session length ÷ points) and price use them. Stored in `data/records.db`.
2. **Open Charge Map check-ins (free):** each station's user check-ins (“Charged successfully” / “Failed to charge”)
   and last-update date are shown; a failed charge in the last 30 days lowers the station-status score.
3. **Google Maps availability (optional, needs a key):** described below.


Open Charge Map gives the station directory (location, connectors, points, reported tariff, status) but no live data.
Live availability comes from **Google Places API (New)** – Nearby Search with `places.evChargeOptions`, which returns
per connector type: `count`, `availableCount`, `outOfServiceCount`, `maxChargeRateKw`, `availabilityLastUpdateTime`.
Google Maps receives this from charging networks that share it (in India e.g. Statiq and ElectricPe).

* Google stations are matched to OCM stations within 150 m (or 400 m with a shared name word); Google-only stations
  are added. Results are cached for 60 s.
* **Current hour:** demand = live occupancy (points in use ÷ usable points), waiting time = 0 if a point is free,
  otherwise about one session length ÷ busy points; status = "Live now: x of y free".
* **Later time:** the model forecast for that time × (live occupancy now ÷ model occupancy now), clipped to ×0.5–×2.
* **Price:** the station's demand factor uses the live (or live-adjusted) demand level.
* Every value is tagged **Live**, **Forecast + live** or **Estimated**. Without live data the model estimates are used.

Setup: in Google Cloud, create a project, enable **Places API (New)**, enable billing, create an API key and add
`GOOGLE_MAPS_API_KEY=...` to `backend/.env`. `evChargeOptions` is billed at the Nearby Search Enterprise + Atmosphere
SKU (1,000 free calls per month; one station search = one call).

## Real-time charger feed (OCPP), live updates and adaptive learning

**Charger feed over OCPP 1.6-J.** OCPP (Open Charge Point Protocol) is the open standard real chargers use to report
to their operator. The backend is a minimal OCPP *central system*: a charge point connects by WebSocket to
`ws://127.0.0.1:8000/ocpp/OCM-<Open Charge Map station id>` and sends BootNotification, StatusNotification
(Available / Charging / Faulted …), Start/StopTransaction, MeterValues and a Heartbeat every 30 s. The latest status of
each point becomes the station's live availability – the highest-priority live source, before Google Maps and driver
reports. `backend/app/ocpp_service.py`, endpoint `/api/ocpp/status`.

**Charger simulator.** No Indian network offers a free live feed, so `simulator/ocpp_charger_sim.py` turns the nearest
stations into simulated chargers that connect exactly like real ones. Cars arrive at random with the station model's
arrival rates for the current hour (× `--demand`) and stay for a random time around the model's session length;
`--speed 60` makes one real minute equal one simulated hour (how busy a station is on average does not change).

```bash
# terminal 1 – the app
cd ~/Downloads/"EV Energy Management"/backend && source .venv/bin/activate && uvicorn app.main:app --reload
# terminal 2 – the simulated chargers (from the project folder)
cd ~/Downloads/"EV Energy Management" && source backend/.venv/bin/activate
python simulator/ocpp_charger_sim.py --location "Banjara Hills, Hyderabad" --stations 5 --demand 2
```

**Built-in simulated charger network (on by default).** Every station that appears in any search – any place in
India – automatically gets a simulated charger that reports over the same OCPP messages (status, transactions,
heartbeat, tariff) to the central system, so all stations are live without starting anything. Shown as
"charger feed (simulated)". A real charger, or the stand-alone simulator, connected for a station takes precedence.
Settings in `backend/.env`: `SIMULATE_CHARGERS=0` to switch off, `SIM_SPEED` (default 20), `SIM_DEMAND` (default 1).
`backend/app/sim_network.py`.

**Live updates.** Station finder, Demand at stations and Prices at stations refresh every 30 s (switch on the result).

**Adaptive learning.** Every *real* live reading (a real charger's heartbeat, driver report, Google Maps) is stored in
`observations`. For each station the ratio of observed to predicted use is learned overall and per hour, with
shrinkage (prior weight 6 readings, kept within ×0.25–×4); the station's arrivals are multiplied by it, so estimates
move towards what is really observed – also for hours with no live data now (tagged **Learned**).
`backend/app/adaptive_service.py`, endpoint `/api/adaptive`, card on the Demand page. Simulated charger feeds are
not used for learning – they are generated from the demand model itself, so learning from them would be circular.
Validation with a known answer (`python -m training.validate_adaptive`): 4 stations whose true demand is ×2.0, ×0.5,
×1.0, ×1.5 the model's, two weeks of readings → learned ×1.96, ×0.51, ×1.09, ×1.51; error on later readings
7.4 % → 6.0 % (18.8 % better).

## Station tariffs vs dynamic price

* **Station tariff** – what a station actually charges (Rs per kWh incl. GST), from the best source available:
  1. **Live from the charger feed** – a charger announces its tariff over OCPP (DataTransfer, messageId `Tariff`)
     at start-up and with every heartbeat; live while connected.
  2. **Reported by drivers** – "price paid" in the status report on a station card; median of the last 30 days.
  3. **Listed on Open Charge Map** – the station's usage cost (volunteer data, may be outdated).
  4. **Simulated charger feed** – the built-in charger network's tariff; used to compute the price but not shown as a
     station tariff, because it is not the station's real price.
  5. Not known – the dynamic price starts from the base price (Rs 10 + 18 % GST).
* **Dynamic price** – this system's recommendation (the adaptive pricing module): tariff × time-of-day factor ×
  demand factor; when no tariff is known, your base price + GST is the starting point. Shown with the difference
  to the tariff. `backend/app/tariff_service.py`.

No Indian charging network publishes its prices through a public API (operators exchange tariffs through roaming
agreements, OCPI), so tariffs are only as complete as these sources.

## Evaluation – station recommendation vs nearest station

`python -m training.evaluate_recommender` (from the backend folder) replays a day in a simulated city – 60 car
charging stations in 24 x 24 km, 60 % clustered in 3 hotspots; drivers over the day following the hourly pattern of
the charging data – 20 random days per scenario. Strategies: **A** nearest station, **B** this project's balanced
recommender with forecast waiting times only, **C** the same with live availability (as the app with the charger
feed). The app's own weights, waiting-time formulas and price rules are used. Results (mean of 20 days):

| | Normal day (300 drivers) | Busy day (450 drivers) |
|---|---|---|
| Mean wait – A nearest | 55.7 min | 106.0 min |
| Mean wait – B recommender, forecast | 36.0 min (−35 %) | 90.9 min (−14 %) |
| Mean wait – C recommender, live | **14.4 min (−74 %)** | **20.2 min (−81 %)** |
| 90th-percentile wait A → C | 180 → 21 min | 370 → 56 min |
| Extra travel C vs A | +1.2 km | +1.3 km |
| Total time (travel + wait + charge) C vs A | −8 % | −35 % |

Live availability is what makes recommendation work: with the forecast alone every driver in an area gets the same
"best" station at a given hour (herding), so the gain is small and unstable. Files: `models/metrics_recommender_eval.json`,
`models/recommender_eval.png`. Limitation: a synthetic city, not measured Indian traffic or queues.

## Limitations (state these in the report)

1. Vehicle RUL is a projection of a fitted ageing law to 80 % health; no public dataset follows real EVs to end of life.
2. The RUL range is wide because batteries of the same age and mileage age at different speeds.
3. 2- and 3-wheelers: no public battery-health data exists; the car-fitted models are applied (evidence: limited).
4. Health from range depends on the dashboard's range estimate, which changes with temperature, AC use and recent driving.
5. Anomaly detection has no real fault labels; detection rates come from injected faults. Mild degradation is hard to detect.
   Health needs a reference: the first check only saves one, and health is measured once a saved reading is 30+ days old
   (or immediately if the range when new is given). Readings are linked by an id stored in the owner's browser.
6. The demand model is trained on historical data (Palo Alto, USA, to 2020); live occupancy corrects it only for stations
   with live data.
7. Live availability only exists for networks that share it with Google Maps; other stations stay estimated. No public
   source publishes live prices, so prices remain indicative rule outputs.
8. Prices are indicative rule outputs, not station tariffs. Waiting times are queue-model estimates learned from Palo
   Alto AC chargers, not live occupancy; DC fast-charger session length is estimated from energy and power.
9. Station details and reported tariffs come from Open Charge Map and may be missing or outdated.
10. The OCPP charger feed is demonstrated with simulated chargers; connecting real chargers or an operator's
    back office needs the operator's agreement. Adaptive learning is only as good as the live readings it gets;
    its validation uses simulated readings with a known answer.

## Demo driver reports

Real driver reports are what make a station "live" from crowd data and what adaptive learning learns from. For a
demonstration, `training/demo_reports.py` sends the same "Report status now" a driver would, through the app's API
(second Terminal window, app running):

```bash
python -m training.demo_reports --watch                     # every place searched in the app, refreshed every 40 min
python -m training.demo_reports "Madhapur, Hyderabad"       # named places (or latitude,longitude)
```
These are demonstration reports, not real observations - say so if asked.

## Screens

Dashboard · Battery RUL (3D vehicle, health ring, distance-per-year slider) · Anomaly detection (status, checks,
trip check) · Demand forecast (station table, hour-by-hour heatmap, actual vs predicted) · Dynamic pricing (prices per
station, price heatmap) · Station finder (map, ranked cards, driver reports) · Plan my charge (all modules in one step).
Model evaluations, rules and limitations that used to be shown on the pages are in this README and the report.

## Tests

```bash
cd backend && source .venv/bin/activate
pip install pytest
python -m pytest -q          # 25 tests, ~3 s, temporary database (app data untouched)
```
They check the project's rules: SOC does not change RUL; more driving means less life left; all vehicle types; the
pricing formula; the queueing model; connector compatibility; the station filter (residential / private / dealership /
venue); merging stations from several sources; passwords stored only as hashes, login pause after 5 wrong passwords,
readings private per user; real driver reports beating a simulated charger feed; an Open Charge Map listed price
beating a simulated feed tariff; impossible tariffs rejected;
adaptive learning recovering a known factor (x2).

## Retraining

```bash
python training/train_rul_model.py     # vehicle ageing model (seconds)
python training/train_anomaly.py       # anomaly detector (seconds)
# demand: copy "ChargePoint Data CY20Q4.csv" to backend/data/raw/ first
python training/train_demand.py
python training/train_station_demand.py
```

If the demand model file cannot be loaded (for example after a scikit-learn upgrade), the app retrains it
automatically from `data/daily_demand.csv` on first use.
