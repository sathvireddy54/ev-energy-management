"""
Demo driver reports - fills in what drivers would report at stations, for a live demonstration.

Sends the same "Report status now" a driver sends from Station finder, through the running app's own API, for the
stations nearest a place. Those stations then show as Live (for 45 minutes) and their readings feed adaptive learning.
These are demonstration reports, not real observations - say so if asked.

Run while the app is running (second Terminal window), from the backend folder, after `source .venv/bin/activate`:

    python -m training.demo_reports --watch
        Recommended for the demo. Keeps running: every place searched in the app - any place in India, typed or
        "Your current location" - gets driver reports within about 15 seconds, refreshed every 40 minutes so it stays
        Live. Stop with Ctrl + C.

    python -m training.demo_reports
        One go: the last 5 different places searched in the app (current location included).

    python -m training.demo_reports "Madhapur, Hyderabad" "Andheri, Mumbai" 17.4401,78.3489
        One go: the places named (a place name, or latitude,longitude).

Options: --stations 5 (per place)   --url http://127.0.0.1:8000
"""
import argparse
import json
import random
import re
import secrets
import sys
import time
from pathlib import Path

import requests

LOG = Path(__file__).resolve().parent.parent / "data" / "search_log.jsonl"
REFRESH_S = 40 * 60          # driver reports count as live for 45 minutes
# what drivers report at each station, nearest first: (status, share of points free, queue, price Rs/kWh or None)
PATTERN = [("busy", 0.0, None, 21.0), ("free", 0.5, None, None), ("queue", 0.0, 2, 24.0),
           ("free", 1.0, None, None), ("busy", 0.0, None, 19.5), ("free", 0.5, None, None),
           ("busy", 0.0, None, None), ("free", 1.0, None, 22.0)]
COORD = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def session(url):
    """A throw-away account for the demo driver (random e-mail and password, not stored anywhere)."""
    s = requests.Session()
    try:
        r = s.post(f"{url}/api/auth/register", json={"email": f"demo-driver-{secrets.token_hex(4)}@example.com",
                                                     "name": "Demo driver", "password": secrets.token_urlsafe(16)})
    except requests.ConnectionError:
        sys.exit(f"The app is not running at {url}. Start it first (uvicorn app.main:app --reload).")
    r.raise_for_status()
    return s


def seed(s, url, target, n, radius=10, quiet=False):
    """target: a place name or (lat, lon). Sends reports for the n nearest stations."""
    if isinstance(target, tuple):
        params, label = {"q": "Your current location", "latitude": target[0], "longitude": target[1]}, f"{target[0]:.4f}, {target[1]:.4f}"
    else:
        params, label = {"q": target}, target
    params.update(vehicle_type="4W", distance_km=radius)
    d = s.get(f"{url}/api/demand/stations", params=params, timeout=180)
    if d.status_code != 200:
        print(f"{label}: station search failed – {d.text[:120]}")
        return 0
    stations = d.json().get("stations", [])[:n]
    if not stations:
        print(f"{label}: no stations found")
        return 0
    print(f"\n{label}")
    sent = 0
    for st, (status, free_share, queue, price) in zip(stations, PATTERN):
        total = int(st.get("number_of_points") or 2)
        free = max(1, round(total * free_share)) if status == "free" else 0
        body = {"station_id": str(st["id"]), "station_name": st.get("name"), "latitude": st.get("latitude"),
                "longitude": st.get("longitude"), "status": status, "free_points": free, "total_points": total}
        if queue:
            body["queue"] = queue
        if price:
            body["price_per_kwh"] = round(price + random.choice([-0.5, 0, 0.5]), 1)
        r = s.post(f"{url}/api/checkins", json=body, timeout=30)
        sent += r.status_code == 200
        if not quiet:
            print(f"  {str(st.get('name', st['id']))[:40]:40s}  {status:6s} {free}/{total} free"
                  f"{f', queue {queue}' if queue else ''}{f', Rs {body.get('price_per_kwh')}/kWh' if price else ''}"
                  f"  {'ok' if r.status_code == 200 else f'failed ({r.status_code})'}")
    return sent


def searches(after_line=0):
    """Station searches made in the app: [(line no, lat, lon, radius)] from data/search_log.jsonl."""
    out = []
    if not LOG.exists():
        return out
    with LOG.open() as f:
        for i, line in enumerate(f):
            if i < after_line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "stations" and e.get("lat") is not None:
                out.append((i, float(e["lat"]), float(e["lon"]), float(e.get("radius_km") or 10)))
    return out


def key(lat, lon):
    return (round(lat, 2), round(lon, 2))          # about 1 km


def main():
    ap = argparse.ArgumentParser(description="Send demo driver reports for the stations nearest a place.")
    ap.add_argument("places", nargs="*", help='place names or latitude,longitude, e.g. "Madhapur, Hyderabad" 17.44,78.39')
    ap.add_argument("--watch", action="store_true", help="keep running and cover every place searched in the app")
    ap.add_argument("--stations", type=int, default=5)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    a = ap.parse_args()
    s = session(a.url)

    if a.places:
        for p in a.places:
            m = COORD.match(p)
            seed(s, a.url, (float(m.group(1)), float(m.group(2))) if m else p, a.stations)
        print("\nDone - refresh the app. These stations show as Live for 45 minutes and count for adaptive learning.")
        return

    if not a.watch:
        recent, seen = [], set()
        for _, lat, lon, rad in reversed(searches()):
            if key(lat, lon) not in seen:
                seen.add(key(lat, lon)); recent.append((lat, lon, rad))
            if len(recent) == 5:
                break
        if not recent:
            sys.exit("No searches in the app yet - search a place first, or name places: demo_reports \"Madhapur, Hyderabad\"")
        for lat, lon, rad in recent:
            seed(s, a.url, (lat, lon), a.stations, radius=rad)
        print("\nDone - refresh the app. These stations show as Live for 45 minutes and count for adaptive learning.")
        return

    # --watch: follow the app's searches
    done = {}                                   # area -> time last seeded
    line = len(open(LOG).readlines()) if LOG.exists() else 0
    for _, lat, lon, rad in searches()[-3:]:    # the last few searches before starting
        if time.time() - done.get(key(lat, lon), 0) > REFRESH_S:
            seed(s, a.url, (lat, lon), a.stations, radius=rad); done[key(lat, lon)] = time.time()
    print("\nWatching the app - every place searched gets driver reports within ~15 s. Ctrl + C to stop.")
    try:
        while True:
            time.sleep(15)
            new = searches(line)
            if new:
                line = new[-1][0] + 1
            for _, lat, lon, rad in new:
                k = key(lat, lon)
                if time.time() - done.get(k, 0) > REFRESH_S:
                    done[k] = time.time()
                    seed(s, a.url, (lat, lon), a.stations, radius=rad)
            for k, t in list(done.items()):     # keep searched places live
                if time.time() - t > REFRESH_S:
                    done[k] = time.time()
                    seed(s, a.url, k, a.stations, quiet=True)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
