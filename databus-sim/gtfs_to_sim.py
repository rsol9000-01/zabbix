#!/usr/bin/env python3
"""
Alinea el simulador databus-sim con el GTFS real de bUCR y genera su horario.

Por qué hace falta:
  databus valida cada viaje contra su GTFS vigente (route_id, trip_id,
  direction_id y shape_id deben coincidir). El simulador trae rutas y formas
  fijas (bUCR_L1, bUCR_L2, hacia_artes, ...) que no existen en el GTFS actual.
  Además, su schedule.yaml no se repite: cada viaje tiene fecha y hora fijas y
  se ejecuta una sola vez. Este script carga las formas reales y arma el
  horario del día a partir de los horarios reales del GTFS.

Qué cambia:
  - simulator_app/data/shapes.json: AGREGA la ruta, formas y paradas del GTFS.
    No borra las que ya tenía. La primera vez guarda el original en shapes.json.bak.
    Si agrega formas nuevas, hay que reconstruir el contenedor (lo avisa).
  - simulator_app/data/schedule.yaml: lo REEMPLAZA (con --dia o --schedule).
    La primera vez guarda el original en schedule.yaml.bak.

Modos:
  --dia [AAAA-MM-DD]   Horario real del día: todos los viajes que el calendario
                       GTFS activa ese día, a su hora de salida real. Reparte los
                       viajes entre los 6 buses sin que un bus tenga dos viajes
                       a la vez. Omite los viajes cuya hora ya pasó.
  --schedule N         Prueba rápida: N viajes (máx. 6) a partir de --inicio minutos.

Uso (desde la carpeta databus-sim, solo biblioteca estándar de Python):
  python3 gtfs_a_sim.py --dia --recargar http://localhost:8088      # hoy
  python3 gtfs_a_sim.py --dia 2026-11-04                             # otro día
  python3 gtfs_a_sim.py --schedule 6 --inicio 2                      # prueba rápida

Para que se repita cada día, en el crontab del servidor (crontab -e):
  5 0 * * * cd /ruta/databus-sim && python3 gtfs_a_sim.py --dia --recargar http://localhost:8088 >> gtfs_a_sim.log 2>&1
"""
import argparse
import csv
import datetime
import io
import json
import math
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

URL_GTFS = "https://feeds.simovi.org/bucr/schedule/gtfs.zip"
DATA = Path("simulator_app/data")
BUSES = ["unit-01", "unit-02", "unit-03", "unit-04", "unit-05", "unit-06"]
ZONA = datetime.timezone(datetime.timedelta(hours=-6))  # Costa Rica, sin horario de verano
DIAS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Valores del simulador (schedule.yaml y simulator_app/domain/kinematics.py)
PRE_IDLE_S = 30        # create-run se envía 30 s antes de la hora de salida
CONFIRM_S = 5          # confirmación automática
ARRANQUE_S = 10        # el bus empieza a moverse 10 s después de confirmado
POST_IDLE_S = 30       # sigue transmitiendo 30 s al llegar a la terminal
PARADA_S = 6           # 3 ticks de 2 s detenido en cada parada
VEL_ESTIMADA = 5.0     # m/s; el simulador varía entre 3 y 12 m/s (media ~7,5)
MARGEN_S = 90          # holgura para que databus cierre el viaje y libere el bus


# --------------------------------------------------------------------------- GTFS

def leer_gtfs(origen_zip, url):
    if origen_zip:
        datos = Path(origen_zip).read_bytes()
    else:
        print(f"Descargando {url} ...")
        with urllib.request.urlopen(url, timeout=60) as r:
            datos = r.read()
    z = zipfile.ZipFile(io.BytesIO(datos))

    def tabla(nombre, obligatoria=True):
        if nombre not in z.namelist():
            if obligatoria:
                sys.exit(f"ERROR: el GTFS no trae {nombre}")
            return []
        with z.open(nombre) as fh:
            return list(csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")))

    g = {t: tabla(f"{t}.txt") for t in ("routes", "trips", "shapes", "stops", "stop_times")}
    g["calendar"] = tabla("calendar.txt", False)
    g["calendar_dates"] = tabla("calendar_dates.txt", False)
    return g


def distancia_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def construir_formas(filas):
    """shape_id -> [[lat, lon, km_acumulados], ...] en el orden de shape_pt_sequence."""
    por_forma = {}
    for f in filas:
        por_forma.setdefault(f["shape_id"], []).append(
            (int(f["shape_pt_sequence"]), float(f["shape_pt_lat"]), float(f["shape_pt_lon"])))
    formas = {}
    for sid, pts in por_forma.items():
        pts.sort()
        salida, acum, previo = [], 0.0, None
        for _, lat, lon in pts:
            if previo:
                acum += distancia_km(previo[0], previo[1], lat, lon)
            salida.append([lat, lon, round(acum, 4)])
            previo = (lat, lon)
        if len(salida) >= 2:
            formas[sid] = salida
    return formas


def hora_a_s(t):
    """HH:MM:SS de GTFS a segundos desde la medianoche (admite 24:00 o más)."""
    if not t or not t.strip():
        return None
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def servicios_del_dia(g, dia):
    """service_id activos en la fecha según calendar.txt y calendar_dates.txt."""
    activos = set()
    ymd = dia.strftime("%Y%m%d")
    for c in g["calendar"]:
        if c["start_date"] <= ymd <= c["end_date"] and c.get(DIAS[dia.weekday()]) == "1":
            activos.add(c["service_id"])
    for cd in g["calendar_dates"]:
        if cd["date"] == ymd:
            if cd["exception_type"] == "1":
                activos.add(cd["service_id"])
            elif cd["exception_type"] == "2":
                activos.discard(cd["service_id"])
    return activos


# ----------------------------------------------------------------- shapes.json

def respaldar(ruta):
    """Guarda el original una sola vez, para que la copia siga siendo el original."""
    bak = ruta.with_name(ruta.name + ".bak")
    if ruta.exists() and not bak.exists():
        shutil.copy2(ruta, bak)


def fusionar_formas(sim, g, formas):
    antes = {k: v for k, v in sim["shapes"].items() if k in formas}
    sim["shapes"].update(formas)

    formas_por_ruta = {}
    for t in g["trips"]:
        if t.get("shape_id") in formas:
            formas_por_ruta.setdefault(t["route_id"], set()).add(t["shape_id"])
    for r in g["routes"]:
        sim["routes"] = [x for x in sim["routes"] if x["route_id"] != r["route_id"]]
        sim["routes"].append({
            "route_id": r["route_id"],
            "short_name": r.get("route_short_name") or r["route_id"],
            "long_name": r.get("route_long_name") or "",
            "color": r.get("route_color") or "005DA4",
            "text_color": r.get("route_text_color") or "FFFFFF",
            "shape_ids": sorted(formas_por_ruta.get(r["route_id"], [])),
        })

    ya = {s["stop_id"] for s in sim["stops"]}
    for s in g["stops"]:
        if s["stop_id"] in ya or (s.get("location_type") or "0") not in ("", "0"):
            continue
        sim["stops"].append({"stop_id": s["stop_id"], "name": s.get("stop_name", ""),
                             "lat": float(s["stop_lat"]), "lon": float(s["stop_lon"])})
    cambio = antes != formas
    return cambio, formas_por_ruta


# --------------------------------------------------------------- schedule.yaml

def entrada(eid, bus_i, t, inicio):
    q = json.dumps  # cadenas entre comillas dobles, válidas en YAML
    return [
        f"  - id: {q(eid)}",
        f"    vehicle_id: {q(BUSES[bus_i])}",
        f"    operator_id: {q(f'op-{bus_i + 1:03d}')}",  # un operador por bus
        f"    route_id: {q(t['route_id'])}",
        f"    trip_id: {q(t['trip_id'])}",
        f"    direction_id: {int(t.get('direction_id') or 0)}",
        f"    shape_id: {q(t['shape_id'])}",
        '    schedule_relationship: "SCHEDULED"',
        f"    start_time: {q(inicio.isoformat())}",
        "    auto_confirm: true",
        f"    auto_start_motion_after_s: {ARRANQUE_S}",
    ]


def encabezado(titulo):
    return [f"# {titulo}", "defaults:",
            f"  pre_run_idle_s: {PRE_IDLE_S}", f"  post_run_idle_s: {POST_IDLE_S}",
            f"  auto_confirm_delay_s: {CONFIRM_S}", "", "runs:"]


def horario_dia(g, formas, dia, ahora):
    servicios = servicios_del_dia(g, dia)
    salida, paradas = {}, {}
    for st in g["stop_times"]:
        s = hora_a_s(st.get("departure_time")) or hora_a_s(st.get("arrival_time"))
        seq = int(st["stop_sequence"])
        tid = st["trip_id"]
        paradas[tid] = paradas.get(tid, 0) + 1
        if s is not None and (tid not in salida or seq < salida[tid][0]):
            salida[tid] = (seq, s)

    base = datetime.datetime.combine(dia, datetime.time(0), ZONA)
    viajes, sin_forma = [], 0
    for t in g["trips"]:
        if t["service_id"] not in servicios or t["trip_id"] not in salida:
            continue
        if t.get("shape_id") not in formas:
            sin_forma += 1
            continue
        inicio = base + datetime.timedelta(seconds=salida[t["trip_id"]][1])
        largo_m = formas[t["shape_id"]][-1][2] * 1000
        dur = (CONFIRM_S + ARRANQUE_S + largo_m / VEL_ESTIMADA
               + paradas[t["trip_id"]] * PARADA_S + POST_IDLE_S + MARGEN_S)
        viajes.append((inicio, dur, t))
    viajes.sort(key=lambda v: v[0])

    libre = [None] * len(BUSES)          # hasta cuándo está ocupado cada bus
    lineas = encabezado(f"Horario real de bUCR para {dia.isoformat()} — gtfs_a_sim.py --dia")
    pasados = sin_bus = 0
    max_simult = 0
    for inicio, dur, t in viajes:
        pedido = inicio - datetime.timedelta(seconds=PRE_IDLE_S)
        if pedido <= ahora + datetime.timedelta(seconds=60):
            pasados += 1                 # el simulador los lanzaría todos de golpe
            continue
        libres = [i for i, h in enumerate(libre) if h is None or h <= pedido]
        ocupados = len(BUSES) - len(libres)
        max_simult = max(max_simult, ocupados + 1)
        if not libres:
            sin_bus += 1
            continue
        i = min(libres, key=lambda k: libre[k] or datetime.datetime.min.replace(tzinfo=ZONA))
        libre[i] = inicio + datetime.timedelta(seconds=dur)
        # id estable por fecha y viaje: un Reload el mismo día no repite viajes ya lanzados
        lineas += entrada(f"{dia:%Y%m%d}-{t['trip_id']}", i, t, inicio)

    n = lineas.count("    auto_confirm: true")
    if n == 0:
        lineas[-1] = "runs: []"
    resumen = {
        "servicios": sorted(servicios), "viajes_del_dia": len(viajes) + sin_forma,
        "programados": n, "ya_pasaron": pasados, "sin_bus_libre": sin_bus,
        "sin_forma": sin_forma, "max_buses_simultaneos": max_simult,
    }
    return "\n".join(lineas) + "\n", resumen


def horario_prueba(g, formas, n, inicio_min):
    candidatos, vistas = [], set()
    for t in g["trips"]:
        if t.get("shape_id") in formas and t["shape_id"] not in vistas:
            candidatos.append(t)
            vistas.add(t["shape_id"])
    for t in g["trips"]:
        if len(candidatos) >= n:
            break
        if t.get("shape_id") in formas and t not in candidatos:
            candidatos.append(t)
    if not candidatos:
        sys.exit("ERROR: ningún viaje del GTFS tiene una forma utilizable")
    base = datetime.datetime.now(ZONA).replace(second=0, microsecond=0)
    base += datetime.timedelta(minutes=inicio_min)
    lote = base.strftime("%m%d-%H%M")
    lineas = encabezado("Prueba rápida — gtfs_a_sim.py --schedule")
    # databus no acepta dos viajes activos con el mismo trip_id: nunca repetir
    for i in range(min(n, len(BUSES), len(candidatos))):
        lineas += entrada(f"sched-{lote}-{i + 1:03d}", i, candidatos[i],
                          base + datetime.timedelta(minutes=i))
    return "\n".join(lineas) + "\n"


def recargar(url):
    req = urllib.request.Request(url.rstrip("/") + "/sim/schedule/reload", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"Simulador recargado (HTTP {r.status})")
    except Exception as e:
        print(f"AVISO: no se pudo recargar el simulador en {url}: {e}")


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", help="GTFS local en vez de descargarlo")
    ap.add_argument("--url", default=URL_GTFS)
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--dia", nargs="?", const="hoy", metavar="AAAA-MM-DD",
                      help="horario real de ese día (por omisión, hoy)")
    modo.add_argument("--schedule", type=int, default=0, metavar="N",
                      help="prueba rápida con N viajes (máximo 6)")
    ap.add_argument("--inicio", type=int, default=2, metavar="MIN",
                    help="con --schedule: minutos hasta el primer viaje")
    ap.add_argument("--recargar", metavar="URL",
                    help="URL del simulador para recargar el horario, p. ej. http://localhost:8088")
    a = ap.parse_args()

    ruta_formas = DATA / "shapes.json"
    if not ruta_formas.exists():
        sys.exit("ERROR: corra este script desde la carpeta databus-sim")

    g = leer_gtfs(a.zip, a.url)
    formas = construir_formas(g["shapes"])
    sim = json.loads(ruta_formas.read_text())
    cambio, por_ruta = fusionar_formas(sim, g, formas)
    if cambio:
        respaldar(ruta_formas)
        ruta_formas.write_text(json.dumps(sim, ensure_ascii=False))
        print(f"shapes.json actualizado: {len(formas)} formas del GTFS")
        print("AVISO: hay formas nuevas. Reconstruya el simulador: docker compose up --build -d")
    else:
        print("shapes.json ya estaba al día")

    ruta_horario = DATA / "schedule.yaml"
    if a.dia:
        ahora = datetime.datetime.now(ZONA)
        dia = ahora.date() if a.dia == "hoy" else datetime.date.fromisoformat(a.dia)
        texto, r = horario_dia(g, formas, dia, ahora)
        print(f"{dia}: servicios {r['servicios'] or 'ninguno'}; "
              f"{r['viajes_del_dia']} viajes en el GTFS, {r['programados']} programados")
        if r["ya_pasaron"]:
            print(f"  {r['ya_pasaron']} omitidos porque su hora ya pasó")
        if r["sin_forma"]:
            print(f"  {r['sin_forma']} omitidos porque su forma no está en shapes.txt")
        if r["sin_bus_libre"]:
            print(f"  AVISO: {r['sin_bus_libre']} omitidos por falta de bus libre "
                  f"(el horario pide hasta {r['max_buses_simultaneos']} buses a la vez; "
                  f"el simulador tiene {len(BUSES)})")
    elif a.schedule:
        texto = horario_prueba(g, formas, a.schedule, a.inicio)
        print(f"schedule.yaml: prueba con {texto.count('auto_confirm: true')} viajes, "
              f"el primero en {a.inicio} min")
    else:
        return

    respaldar(ruta_horario)
    ruta_horario.write_text(texto)
    if a.recargar:
        recargar(a.recargar)


if __name__ == "__main__":
    main()