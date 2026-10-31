#!/usr/bin/env python3
"""
Sonda GTFS-RT para MACDT (resuelve H6).

Lee los feeds .pb que databus publica y calcula las variables de la Capa D de
MACDT sin depender de endpoints que no existen en el codigo.
Imprime UN objeto JSON: usenlo como item maestro (UserParameter ejecutado por el
Agent2 dentro de la red) y cuelguen items dependientes con JSONPath.

Por que dos edades y no una (hallazgo del codigo de databus, schedule_engine/builders.py):
  - header.timestamp es la hora en que Celery Beat construyo el archivo (cada 15 s).
    Mide si el PUBLICADOR esta vivo, no si los vehiculos reportan.
  - entity.vehicle.timestamp viene del ultimo dato de posicion; si el vehiculo no lo trae,
    databus lo reemplaza por la hora de construccion. Un feed sin telemetria real puede
    parecer fresco. Por eso se reporta ts_igual_header_pct como alarma heuristica.

Por que --stale 60 y no 90: databus considera vigente la telemetria de un vehiculo
hasta TELEMETRY_GRACE_S = 60 s (runs/domain/detection/thresholds.py). Con 90 s el
monitoreo reportaria mejor salud que la que el propio databus cree tener.

Por que --host-header: en el compose de produccion, Django valida ALLOWED_HOSTS.
Al llamar al contenedor por su nombre de red el Host no esta en la lista y Django
responde 400 DisallowedHost. Con --host-header se conecta al contenedor pero se
envia el Host que el despliegue ya acepta, sin pedir cambios en databus.

Uso:
  python3 gtfsrt_probe.py --base http://orchestrator:8000/feed/realtime \
                          --host-header databus.ucr.ac.cr --stale 60
  python3 gtfsrt_probe.py --vp-file vp.pb --tu-file tu.pb      # prueba offline

Dependencias: requests, gtfs-realtime-bindings
"""
import argparse
import json
import sys
import time

from google.transit import gtfs_realtime_pb2 as pb

BASE = "https://databus.ucr.ac.cr/feed/realtime"


def _pct(n, d):
    return round(100.0 * n / d, 2) if d else None


def _quantil(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def calcular(vp_bytes, tu_bytes, ahora, stale_s=60):
    vp, tu = pb.FeedMessage(), pb.FeedMessage()
    vp.ParseFromString(vp_bytes)
    tu.ParseFromString(tu_bytes)

    veh = [e.vehicle for e in vp.entity if e.HasField("vehicle")]
    tus = [e.trip_update for e in tu.entity if e.HasField("trip_update")]

    # Edad del dato por vehiculo (0 = sin timestamp)
    edades = [ahora - v.timestamp for v in veh if v.timestamp]
    frescos = sum(1 for v in veh if v.timestamp and 0 <= ahora - v.timestamp <= stale_s)
    igual_header = sum(1 for v in veh if v.timestamp and abs(v.timestamp - vp.header.timestamp) <= 1)

    def completo_vp(v):
        return bool(v.trip.trip_id) and v.position.HasField("latitude") and v.position.HasField("longitude") \
            and v.timestamp > 0

    def completo_tu(t):
        return bool(t.trip.trip_id) and len(t.stop_time_update) > 0

    return {
        "ts_sonda": int(ahora),
        "stale_s": stale_s,
        # Publicador
        "feed_age_vp_s": ahora - vp.header.timestamp if vp.header.timestamp else None,
        "feed_age_tu_s": ahora - tu.header.timestamp if tu.header.timestamp else None,
        # Carga operativa
        "v_trips": len(tus),
        "v_vehicles": len(veh),
        # Frescura del dato de telemetria
        "data_age_p50_s": _quantil(edades, 0.50),
        "data_age_p95_s": _quantil(edades, 0.95),
        "data_age_max_s": max(edades) if edades else None,
        "v_gps_pct": _pct(frescos, len(veh)),                 # % de vehiculos con posicion <= stale_s
        "ts_igual_header_pct": _pct(igual_header, len(veh)),  # heuristica de relleno (ver docstring)
        # Completitud (campos exigidos explicitos)
        "v_completeness_vp_pct": _pct(sum(completo_vp(v) for v in veh), len(veh)),
        "v_completeness_tu_pct": _pct(sum(completo_tu(t) for t in tus), len(tus)),
    }


def _sesion(host_header=None):
    """Una sola sesion HTTP para que las dos descargas reusen la conexion TCP.

    Sin esto la PRIMERA descarga paga DNS y el saludo TCP y la segunda no, con lo
    que las dos latencias no son comparables (medido: 848 ms contra 12 ms).
    """
    import requests
    s = requests.Session()
    if host_header:
        s.headers.update({"Host": host_header})
    return s


def _get(sesion, url, timeout):
    t0 = time.time()
    r = sesion.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content, round((time.time() - t0) * 1000, 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--host-header", dest="host_header",
                    help="Host a enviar; usar un valor que este en ALLOWED_HOSTS de databus")
    ap.add_argument("--stale", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--no-warmup", dest="no_warmup", action="store_true",
                    help="no descartar la primera peticion (deja las latencias sin calentar)")
    ap.add_argument("--vp-file")
    ap.add_argument("--tu-file")
    a = ap.parse_args()
    try:
        if a.vp_file and a.tu_file:
            vp, tu = open(a.vp_file, "rb").read(), open(a.tu_file, "rb").read()
            lat = {}
        else:
            ses = _sesion(a.host_header)
            calentado = 0
            if not a.no_warmup:
                # Descarta la primera peticion: abre la conexion y no se mide.
                try:
                    _get(ses, f"{a.base}/trip_updates.pb", a.timeout)
                    calentado = 1
                except Exception:
                    calentado = 0
            vp, l1 = _get(ses, f"{a.base}/vehicle_positions.pb", a.timeout)
            tu, l2 = _get(ses, f"{a.base}/trip_updates.pb", a.timeout)
            lat = {"fetch_vp_ms": l1, "fetch_tu_ms": l2, "warmup": calentado}
        out = {"fetch_ok": 1, **lat, **calcular(vp, tu, time.time(), a.stale)}
    except Exception as e:  # el fallo de descarga es en si una metrica de disponibilidad
        out = {"fetch_ok": 0, "error": str(e)[:200], "ts_sonda": int(time.time())}
    print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
