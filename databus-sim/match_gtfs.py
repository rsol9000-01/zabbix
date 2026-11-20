#!/usr/bin/env python3
"""Adapta databus-sim al GTFS que publica databus.

Uso (desde /home/databus-sim):
    python3 adaptar_gtfs.py /tmp/feed.zip

Hace 4 cambios (guarda copia .bak de cada archivo antes de tocarlo):
  1. simulator_app/data/shapes.json        -> rutas, shapes y paradas del GTFS
  2. simulator_app/domain/fleet.py         -> route_id y shape por defecto de unit-01..06
  3. simulator_app/static/modals/run_request.js    -> ruta de cada unidad en el formulario
  4. simulator_app/static/modals/schedule_entry.js -> idem para el formulario de Schedule
"""

import csv
import io
import json
import math
import re
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(".")
SHAPES_JSON = ROOT / "simulator_app/data/shapes.json"
FLEET_PY = ROOT / "simulator_app/domain/fleet.py"
JS_FILES = [
    ROOT / "simulator_app/static/modals/run_request.js",
    ROOT / "simulator_app/static/modals/schedule_entry.js",
]
UNITS = [f"unit-0{i}" for i in range(1, 7)]


def read_csv(zf, name):
    with zf.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def backup(path):
    shutil.copy2(path, path.with_name(path.name + ".bak"))


def build_shapes_json(zf):
    routes = read_csv(zf, "routes.txt")
    trips = read_csv(zf, "trips.txt")
    stops = read_csv(zf, "stops.txt")
    shape_rows = read_csv(zf, "shapes.txt")

    pts = defaultdict(list)
    for row in shape_rows:
        pts[row["shape_id"]].append(
            (int(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))
        )
    shapes = {}
    for sid, rows in pts.items():
        rows.sort()
        out, dist, prev = [], 0.0, None
        for _, lat, lon in rows:
            if prev:
                dist += haversine_km(prev[0], prev[1], lat, lon)
            out.append([lat, lon, round(dist, 3)])
            prev = (lat, lon)
        shapes[sid] = out

    shapes_by_route = defaultdict(set)
    trips_per_shape = Counter()
    for t in trips:
        if t.get("shape_id"):
            shapes_by_route[t["route_id"]].add(t["shape_id"])
            trips_per_shape[t["shape_id"]] += 1

    routes_out = [
        {
            "route_id": r["route_id"],
            "short_name": r.get("route_short_name") or r["route_id"],
            "long_name": r.get("route_long_name") or "",
            "color": r.get("route_color") or "005DA4",
            "text_color": r.get("route_text_color") or "FFFFFF",
            "shape_ids": sorted(s for s in shapes_by_route[r["route_id"]] if s in shapes),
        }
        for r in routes
    ]

    stops_out = [
        {"stop_id": s["stop_id"], "name": s.get("stop_name", ""),
         "lat": float(s["stop_lat"]), "lon": float(s["stop_lon"])}
        for s in stops
        if s.get("stop_lat") and s.get("stop_lon")
    ]

    return {"routes": routes_out, "shapes": shapes, "stops": stops_out}, trips_per_shape


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python3 adaptar_gtfs.py /ruta/feed.zip")
    for p in [SHAPES_JSON, FLEET_PY, *JS_FILES]:
        if not p.exists():
            sys.exit(f"No encuentro {p}. Corré el script desde /home/databus-sim")

    with zipfile.ZipFile(sys.argv[1]) as zf:
        data, trips_per_shape = build_shapes_json(zf)

    route_ids = [r["route_id"] for r in data["routes"] if r["shape_ids"]]
    if len(route_ids) != 1:
        sys.exit(f"El GTFS tiene {len(route_ids)} rutas con shapes ({route_ids}); este script espera 1.")
    route = route_ids[0]

    # Las 6 unidades toman los shapes con más trips (uno por unidad; se repiten si hay menos de 6).
    ranked = [s for s, _ in trips_per_shape.most_common() if s in data["shapes"]]
    unit_shape = {u: ranked[i % len(ranked)] for i, u in enumerate(UNITS)}

    # 1. shapes.json
    backup(SHAPES_JSON)
    SHAPES_JSON.write_text(json.dumps(data, ensure_ascii=False))

    # 2. fleet.py
    backup(FLEET_PY)
    src = FLEET_PY.read_text()
    pattern = re.compile(
        r'Vehicle\(\s*vehicle_id="(unit-0\d)",\s*route_id="[^"]+",\s*default_shape_id="[^"]+",?\s*\)'
    )
    src, n = pattern.subn(
        lambda m: f'Vehicle(vehicle_id="{m.group(1)}", route_id="{route}", '
                  f'default_shape_id="{unit_shape[m.group(1)]}")',
        src,
    )
    if n != 12:
        sys.exit(f"fleet.py: esperaba 12 vehículos (2 listas x 6) y encontré {n}. No se modificó nada más.")
    FLEET_PY.write_text(src)

    # 3 y 4. formularios JS
    route_shapes = json.dumps({route: next(r["shape_ids"] for r in data["routes"] if r["route_id"] == route)})
    for js in JS_FILES:
        backup(js)
        s = js.read_text()
        s = re.sub(r"const ROUTE_SHAPES = \{.*?\};", f"const ROUTE_SHAPES = {route_shapes};", s, flags=re.S)
        s = s.replace("'bUCR_L1'", f"'{route}'").replace("'bUCR_L2'", f"'{route}'")
        js.write_text(s)

    print(f"Ruta: {route}")
    print(f"Shapes: {len(data['shapes'])}  Paradas: {len(data['stops'])}")
    for u in UNITS:
        print(f"  {u} -> {unit_shape[u]}")
    print("Listo. Copias de respaldo: *.bak")


if __name__ == "__main__":
    main()