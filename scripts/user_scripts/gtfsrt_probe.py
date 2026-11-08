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
import html
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


def _edad_txt(seg):
    """12 -> '12 s'; 754 -> '12 min'; 36012 -> '10 h 0 min'."""
    seg = int(round(seg))
    if seg < 120:
        return "%d s" % seg
    if seg < 3600:
        return "%d min" % (seg // 60)
    return "%d h %d min" % (seg // 3600, (seg % 3600) // 60)


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
        # posicion: exacto (databus omite el bloque position si el bus no mando coordenadas).
        # hora_origen: sospecha. databus rellena con la hora de armado del archivo cuando el bus
        # no manda timestamp, asi que coincidir con el encabezado (+-1 s) sugiere hora puesta por databus.
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


def _vehiculos_texto(lista):
    """Una linea legible para el dashboard: 'unit-02: 10 h 3 min (viejo) · unit-01: 12 s'."""
    if not lista:
        return "Sin veh\u00edculos en el feed"
    partes = []
    for x in lista:
        if x["edad_s"] is None:
            partes.append("%s: sin hora" % x["id"])
        elif x["edad_s"] < 0:
            partes.append("%s: hora en el futuro" % x["id"])
        else:
            partes.append("%s: %s%s" % (x["id"], _edad_txt(x["edad_s"]), "" if x["estado"] == "fresco" else " (viejo)"))
        if not x["posicion"]:
            partes[-1] += " [sin posici\u00f3n]"
        if x["hora_origen"] == "posible_databus":
            partes[-1] += " [hora de databus?]"
    return " \u00b7 ".join(partes)


def _hora_txt(ts, ahora):
    """Hora local del agente (TZ del contenedor): '9:41 p.m.'; si no es de hoy, '05/11 11:38 a.m.'."""
    t, h = time.localtime(ts), time.localtime(ahora)
    hora = "%d:%02d %s" % ((t.tm_hour % 12) or 12, t.tm_min, "a.m." if t.tm_hour < 12 else "p.m.")
    if (t.tm_year, t.tm_yday) != (h.tm_year, h.tm_yday):
        hora = "%02d/%02d %s" % (t.tm_mday, t.tm_mon, hora)
    return hora


_ESTADO = {"fresco": ("fresco", "#2E7D32"), "viejo": ("viejo", "#E65100"), "sin_ts": ("sin hora", "#C62828")}


def _vehiculos_html(lista, ahora, stale_s):
    """Tabla HTML para el widget Item history de Zabbix (columna en modo HTML)."""
    esc = lambda x: html.escape(str(x))
    td = 'style="padding:3px 10px;border-bottom:1px solid #8884;overflow-wrap:anywhere"'
    tdn = 'style="padding:3px 10px;border-bottom:1px solid #8884;white-space:nowrap"'
    cab = ('<div style="margin-bottom:4px">Umbral de GPS fresco: <b>%d s</b> (macro {$MACDT.STALE_S})'
           ' \u00b7 consultado a las %s</div>' % (stale_s, _hora_txt(ahora, ahora)))
    if not lista:
        return cab + "<div>Sin veh\u00edculos en el feed</div>"
    filas = []
    for x in lista:
        if x["edad_s"] is None:
            edad = "\u2014"
        elif x["edad_s"] < 0:
            edad = "hora en el futuro"
        else:
            edad = _edad_txt(x["edad_s"])
        nombre, color = _ESTADO[x["estado"]]
        p = x["paradas_por_delante"]
        filas.append("<tr>" + "".join("<td %s>%s</td>" % (td if k == 1 else tdn, c) for k, c in enumerate((
            "<b>%s</b>" % esc(x["id"]), esc(x["viaje"] or "\u2014"),
            "s\u00ed" if x["posicion"] else '<span style="color:#C62828;font-weight:bold">no</span>',
            _hora_txt(x["ts"], ahora) if x["ts"] else "\u2014",
            {"bus": "del bus", None: "\u2014"}.get(x["hora_origen"], '<span style="color:#E65100;font-weight:bold">posible hora de databus</span>'),
            edad,
            '<span style="background:%s;color:#fff;padding:1px 8px;border-radius:8px">%s</span>' % (color, nombre),
            "\u2014" if p is None else p))) + "</tr>")
    th = 'style="text-align:left;padding:3px 10px;border-bottom:2px solid #8888;white-space:nowrap"'
    cols = ("Veh\u00edculo", "Viaje", "Posici\u00f3n", "\u00daltima hora reportada", "Origen de la hora", "Edad", "Estado",
            "Paradas por delante")
    return (cab + '<table style="border-collapse:collapse;width:100%%"><tr>%s</tr>%s</table>'
            % ("".join("<th %s>%s</th>" % (th, c) for c in cols), "".join(filas)))


def calcular(vp_bytes, tu_bytes, ahora, stale_s=60):
    vp, tu = pb.FeedMessage(), pb.FeedMessage()
    vp.ParseFromString(vp_bytes)
    tu.ParseFromString(tu_bytes)

    veh = [e.vehicle for e in vp.entity if e.HasField("vehicle")]
    tus = [e.trip_update for e in tu.entity if e.HasField("trip_update")]

    # Edad del dato por vehiculo (0 = sin timestamp)
    edades = [ahora - v.timestamp for v in veh if v.timestamp]
    frescos = sum(1 for v in veh if v.timestamp and 0 <= ahora - v.timestamp <= stale_s)
    # Conteos por estado del GPS (para el dashboard): frescos + viejos + sin_ts = v_vehicles.
    # "viejos" incluye horas en el futuro. Un viaje que termino y databus no cerro cae aqui.
    viejos = sum(1 for v in veh if v.timestamp and not (0 <= ahora - v.timestamp <= stale_s))
    sin_ts = sum(1 for v in veh if not v.timestamp)
    # Viajes sin ninguna parada pendiente: si persisten, el viaje termino y no se cerro.
    sin_paradas = sum(1 for t in tus if len(t.stop_time_update) == 0)
    # Por vehiculo: edad del GPS y paradas pendientes de su viaje (del feed de predicciones)
    paradas = {t.trip.trip_id: len(t.stop_time_update) for t in tus if t.trip.trip_id}
    lista = _vehiculos(vp, ahora, stale_s, paradas)
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
        "v_gps_frescos": frescos,
        "v_gps_viejos": viejos,
        "v_gps_sin_ts": sin_ts,
        "v_trips_sin_paradas": sin_paradas,
        # Edad de la posicion por vehiculo (ahora - timestamp del GPS)
        "vehiculos": lista,
        "vehiculos_texto": _vehiculos_texto(lista),
        "vehiculos_html": _vehiculos_html(lista, ahora, stale_s),
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