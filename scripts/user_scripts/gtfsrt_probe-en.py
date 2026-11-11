#!/usr/bin/env python3
"""
GTFS-RT probe for MACDT (resolves H6).

Read the .pb feeds published by databus and calculate MACDT Layer D variables
without relying on endpoints that do not exist in the code.
Print one JSON object: use it as the master item (a UserParameter run by Agent2
inside the network) and attach dependent items with JSONPath.

Why report two ages instead of one (based on databus schedule_engine/builders.py):
  - header.timestamp is when Celery Beat built the file (every 15 seconds). It
    measures whether the publisher is running, not whether vehicles are reporting.
  - entity.vehicle.timestamp comes from the latest position update. If the
    vehicle omits it, databus substitutes the build time. A feed with no real
    telemetry can therefore appear fresh. ts_igual_header_pct is reported as a
    heuristic warning.

Why use --stale 60 instead of 90: databus considers vehicle telemetry valid for
up to TELEMETRY_GRACE_S = 60 seconds (runs/domain/detection/thresholds.py). With
90 seconds, monitoring would report better health than databus itself does.

Why use --host-header: in the production Compose setup, Django validates
ALLOWED_HOSTS. When connecting to the container by its network name, that Host
is not on the list and Django responds with 400 DisallowedHost. --host-header
connects to the container while sending a Host value the deployment already
accepts, without requiring changes to databus.

Usage:
  python3 gtfsrt_probe-en.py --base http://orchestrator:8000/feed/realtime \
                             --host-header databus.ucr.ac.cr --stale 60
  python3 gtfsrt_probe-en.py --vp-file vp.pb --tu-file tu.pb

Dependencies: requests, gtfs-realtime-bindings
"""
import argparse
import html
import json
import sys
import time

from google.transit import gtfs_realtime_pb2 as pb

BASE = "https://databus.ucr.ac.cr/feed/realtime"


# ---------------------------------------------------------------------------
# Calcular un porcentaje y evitar la división por cero
# ---------------------------------------------------------------------------
def _pct(n, d):
    return round(100.0 * n / d, 2) if d else None


# ---------------------------------------------------------------------------
# Obtener un cuantil mediante el rango más cercano
# ---------------------------------------------------------------------------
def _quantil(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


# ---------------------------------------------------------------------------
# Convertir una edad en segundos a una descripción legible
# ---------------------------------------------------------------------------
def _edad_txt(seg):
    """12 -> '12 s'; 754 -> '12 min'; 36012 -> '10 h 0 min'."""
    seg = int(round(seg))
    if seg < 120:
        return "%d s" % seg
    if seg < 3600:
        return "%d min" % (seg // 60)
    return "%d h %d min" % (seg // 3600, (seg % 3600) // 60)


# ---------------------------------------------------------------------------
# Clasificar y ordenar los vehículos según la antigüedad de su posición
# ---------------------------------------------------------------------------
def _vehiculos(vp, ahora, stale_s, paradas=None):
    """Edad de la posicion de cada vehiculo (ahora - timestamp): primero sin hora, luego viejos, luego frescos.

    estado: fresco (<= stale_s), viejo (> stale_s o en el futuro), sin_ts (sin hora).
    """
    lista = []
    for e in vp.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        edad = round(ahora - v.timestamp, 1) if v.timestamp else None
        if edad is None:
            estado = "sin_ts"
        elif 0 <= edad <= stale_s:
            estado = "fresco"
        else:
            estado = "viejo"
        # -------------------------------------------------------------------
        # Identificar si hay coordenadas y estimar el origen de la marca horaria
        # -------------------------------------------------------------------
        if not v.timestamp:
            origen = None
        elif vp.header.timestamp and abs(v.timestamp - vp.header.timestamp) <= 1:
            origen = "posible_databus"
        else:
            origen = "bus"
        lista.append({"id": v.vehicle.id or e.id, "viaje": v.trip.trip_id, "ts": int(v.timestamp) or None,
                      "edad_s": edad, "estado": estado,
                      "posicion": v.HasField("position") and v.position.HasField("latitude")
                                  and v.position.HasField("longitude"),
                      "hora_origen": origen,
                      "paradas_por_delante": (paradas or {}).get(v.trip.trip_id)})
    orden = {"sin_ts": 0, "viejo": 1, "fresco": 2}
    lista.sort(key=lambda x: (orden[x["estado"]], -abs(x["edad_s"] or 0)))
    return lista


# ---------------------------------------------------------------------------
# Calcular métricas de los feeds GTFS-RT y devolverlas como datos estructurados
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Resumir las edades y los estados de los vehículos en una línea
# ---------------------------------------------------------------------------
def _vehiculos_texto(lista):
    """Una linea legible para el dashboard: 'unit-02: 10 h 3 min (viejo) · unit-01: 12 s'."""
    if not lista:
        return "No vehicles in feed"
    partes = []
    for x in lista:
        if x["edad_s"] is None:
            partes.append("%s: no timestamp" % x["id"])
        elif x["edad_s"] < 0:
            partes.append("%s: timestamp in the future" % x["id"])
        else:
            partes.append("%s: %s%s" % (x["id"], _edad_txt(x["edad_s"]), "" if x["estado"] == "fresco" else " (stale)"))
        if not x["posicion"]:
            partes[-1] += " [no position]"
        if x["hora_origen"] == "posible_databus":
            partes[-1] += " [databus timestamp?]"
    return " \u00b7 ".join(partes)


# ---------------------------------------------------------------------------
# Formatear una marca temporal según la hora local del agente
# ---------------------------------------------------------------------------
def _hora_txt(ts, ahora):
    """Hora local del agente (TZ del contenedor): '9:41 p.m.'; si no es de hoy, '05/11 11:38 a.m.'."""
    t, h = time.localtime(ts), time.localtime(ahora)
    hora = "%d:%02d %s" % ((t.tm_hour % 12) or 12, t.tm_min, "a.m." if t.tm_hour < 12 else "p.m.")
    if (t.tm_year, t.tm_yday) != (h.tm_year, h.tm_yday):
        hora = "%02d/%02d %s" % (t.tm_mday, t.tm_mon, hora)
    return hora


_ESTADO = {"fresco": ("fresh", "#2E7D32"), "viejo": ("stale", "#E65100"), "sin_ts": ("no timestamp", "#C62828")}


# ---------------------------------------------------------------------------
# Construir la tabla HTML de vehículos para el widget de Zabbix
# ---------------------------------------------------------------------------
def _vehiculos_html(lista, ahora, stale_s):
    """Tabla HTML para el widget Item history de Zabbix (columna en modo HTML)."""
    esc = lambda x: html.escape(str(x))
    td = 'style="padding:3px 10px;border-bottom:1px solid #8884;overflow-wrap:anywhere"'
    tdn = 'style="padding:3px 10px;border-bottom:1px solid #8884;white-space:nowrap"'
    cab = ('<div style="margin-bottom:4px">Fresh GPS threshold: <b>%d s</b> (macro {$MACDT.STALE_S})'
           ' \u00b7 checked at %s</div>' % (stale_s, _hora_txt(ahora, ahora)))
    if not lista:
        return cab + "<div>No vehicles in feed</div>"
    filas = []
    for x in lista:
        if x["edad_s"] is None:
            edad = "\u2014"
        elif x["edad_s"] < 0:
            edad = "timestamp in the future"
        else:
            edad = _edad_txt(x["edad_s"])
        nombre, color = _ESTADO[x["estado"]]
        p = x["paradas_por_delante"]
        filas.append("<tr>" + "".join("<td %s>%s</td>" % (td if k == 1 else tdn, c) for k, c in enumerate((
            "<b>%s</b>" % esc(x["id"]), esc(x["viaje"] or "\u2014"),
            "yes" if x["posicion"] else '<span style="color:#C62828;font-weight:bold">no</span>',
            _hora_txt(x["ts"], ahora) if x["ts"] else "\u2014",
            {"bus": "vehicle", None: "\u2014"}.get(x["hora_origen"], '<span style="color:#E65100;font-weight:bold">possible databus timestamp</span>'),
            edad,
            '<span style="background:%s;color:#fff;padding:1px 8px;border-radius:8px">%s</span>' % (color, nombre),
            "\u2014" if p is None else p))) + "</tr>")
    th = 'style="text-align:left;padding:3px 10px;border-bottom:2px solid #8888;white-space:nowrap"'
    cols = ("Vehicle", "Trip", "Position", "Last reported time", "Timestamp source", "Age", "Status",
            "Upcoming stops")
    return (cab + '<table style="border-collapse:collapse;width:100%%"><tr>%s</tr>%s</table>'
            % ("".join("<th %s>%s</th>" % (th, c) for c in cols), "".join(filas)))


def calcular(vp_bytes, tu_bytes, ahora, stale_s=60):
    vp, tu = pb.FeedMessage(), pb.FeedMessage()
    vp.ParseFromString(vp_bytes)
    tu.ParseFromString(tu_bytes)

    veh = [e.vehicle for e in vp.entity if e.HasField("vehicle")]
    tus = [e.trip_update for e in tu.entity if e.HasField("trip_update")]

    # -----------------------------------------------------------------------
    # Medir la antigüedad de la telemetría y contar los vehículos por estado
    # -----------------------------------------------------------------------
    edades = [ahora - v.timestamp for v in veh if v.timestamp]
    frescos = sum(1 for v in veh if v.timestamp and 0 <= ahora - v.timestamp <= stale_s)
    # -----------------------------------------------------------------------
    # Contar como antiguos los tiempos futuros y los viajes aún abiertos
    # -----------------------------------------------------------------------
    viejos = sum(1 for v in veh if v.timestamp and not (0 <= ahora - v.timestamp <= stale_s))
    sin_ts = sum(1 for v in veh if not v.timestamp)
    # -----------------------------------------------------------------------
    # Detectar viajes que siguen publicados aunque no tengan paradas pendientes
    # -----------------------------------------------------------------------
    sin_paradas = sum(1 for t in tus if len(t.stop_time_update) == 0)
    # -----------------------------------------------------------------------
    # Asociar cada vehículo con las paradas pendientes de su viaje
    # -----------------------------------------------------------------------
    paradas = {t.trip.trip_id: len(t.stop_time_update) for t in tus if t.trip.trip_id}
    lista = _vehiculos(vp, ahora, stale_s, paradas)
    igual_header = sum(1 for v in veh if v.timestamp and abs(v.timestamp - vp.header.timestamp) <= 1)

    # -----------------------------------------------------------------------
    # Verificar los campos obligatorios de cada registro de posición
    # -----------------------------------------------------------------------
    def completo_vp(v):
        return bool(v.trip.trip_id) and v.position.HasField("latitude") and v.position.HasField("longitude") \
            and v.timestamp > 0

    # -----------------------------------------------------------------------
    # Verificar los campos obligatorios de cada actualización de viaje
    # -----------------------------------------------------------------------
    def completo_tu(t):
        return bool(t.trip.trip_id) and len(t.stop_time_update) > 0

    return {
        "ts_sonda": int(ahora),
        "stale_s": stale_s,
        # -------------------------------------------------------------------
        # Medir la antigüedad de publicación de ambos feeds
        # -------------------------------------------------------------------
        "feed_age_vp_s": ahora - vp.header.timestamp if vp.header.timestamp else None,
        "feed_age_tu_s": ahora - tu.header.timestamp if tu.header.timestamp else None,
        # -------------------------------------------------------------------
        # Resumir la carga operativa publicada en los feeds
        # -------------------------------------------------------------------
        "v_trips": len(tus),
        "v_vehicles": len(veh),
        # -------------------------------------------------------------------
        # Resumir la frescura de la telemetría de los vehículos
        # -------------------------------------------------------------------
        "data_age_p50_s": _quantil(edades, 0.50),
        "data_age_p95_s": _quantil(edades, 0.95),
        "data_age_max_s": max(edades) if edades else None,
        # -------------------------------------------------------------------
        # Calcular la proporción de vehículos con posición GPS reciente
        # -------------------------------------------------------------------
        "v_gps_pct": _pct(frescos, len(veh)),
        "v_gps_frescos": frescos,
        "v_gps_viejos": viejos,
        "v_gps_sin_ts": sin_ts,
        "v_trips_sin_paradas": sin_paradas,
        # -------------------------------------------------------------------
        # Entregar el detalle de antigüedad, posición y estado por vehículo
        # -------------------------------------------------------------------
        "vehiculos": lista,
        "vehiculos_texto": _vehiculos_texto(lista),
        "vehiculos_html": _vehiculos_html(lista, ahora, stale_s),
        # -------------------------------------------------------------------
        # Estimar el porcentaje de marcas de vehículo iguales al encabezado
        # -------------------------------------------------------------------
        "ts_igual_header_pct": _pct(igual_header, len(veh)),
        # -------------------------------------------------------------------
        # Calcular la proporción de registros con los campos obligatorios
        # -------------------------------------------------------------------
        "v_completeness_vp_pct": _pct(sum(completo_vp(v) for v in veh), len(veh)),
        "v_completeness_tu_pct": _pct(sum(completo_tu(t) for t in tus), len(tus)),
    }


# ---------------------------------------------------------------------------
# Crear una sesión HTTP para reutilizar la conexión entre descargas
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Descargar un recurso y medir su latencia
# ---------------------------------------------------------------------------
def _get(sesion, url, timeout):
    t0 = time.time()
    r = sesion.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content, round((time.time() - t0) * 1000, 1)


# ---------------------------------------------------------------------------
# Obtener los feeds, calcular sus métricas e imprimir un objeto JSON
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--host-header", dest="host_header",
                    help="Host header to send; use a value in databus ALLOWED_HOSTS")
    ap.add_argument("--stale", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--no-warmup", dest="no_warmup", action="store_true",
                    help="do not discard the first request (latencies include connection warm-up)")
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
                # -------------------------------------------------------------------
                # Abrir la conexión antes de medir y descartar la solicitud inicial
                # -------------------------------------------------------------------
                try:
                    _get(ses, f"{a.base}/trip_updates.pb", a.timeout)
                    calentado = 1
                except Exception:
                    calentado = 0
            vp, l1 = _get(ses, f"{a.base}/vehicle_positions.pb", a.timeout)
            tu, l2 = _get(ses, f"{a.base}/trip_updates.pb", a.timeout)
            lat = {"fetch_vp_ms": l1, "fetch_tu_ms": l2, "warmup": calentado}
        out = {"fetch_ok": 1, **lat, **calcular(vp, tu, time.time(), a.stale)}
    except Exception as e:
        # -------------------------------------------------------------------
        # Reportar el fallo de descarga como una métrica de disponibilidad
        # -------------------------------------------------------------------
        out = {"fetch_ok": 0, "error": str(e)[:200], "ts_sonda": int(time.time())}
    print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()