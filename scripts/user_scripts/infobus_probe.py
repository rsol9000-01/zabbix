#!/usr/bin/env python3
"""
Sonda de latencia de la API de InfoBus para MACDT (capa I).

Se ejecuta desde dos puntos con el mismo script:
  - interno (agente de R2, clave infobus.api):      http://orchestrator:8000 -> Daphne + consulta
  - externo (agente de R4, clave infobus.api.ext):  https://<dominio>         -> red + TLS + Traefik + Daphne + consulta
La diferencia externo - interno la calcula Zabbix (plantilla simovilab-infobus-ext). Imprime UN objeto JSON: usenlo como item maestro y cuelguen items
dependientes con JSONPath, igual que gtfsrt_probe.py.

Por que --repeat 10 y no 30 ni 100 (hallazgo de infobus/backend/infobus/settings.py):
  NextTripView declara throttle_scope = "realtime", y DEFAULT_THROTTLE_RATES
  pone "realtime" en 30/min. La cuota es POR IP (AnonRateThrottle), asi que la
  sonda no le quita capacidad a los pasajeros, pero si se pasa de 30/min se
  auto-estrangula y a partir de ahi mide 429, no latencia. Con 10 por ciclo de
  60 s queda en 10/min: un tercio de la cuota, con margen para reintentos.

Por que se reporta p50 de la rafaga y no p95 (consecuencia de lo anterior):
  con n = 10 el p95 de la rafaga ES el maximo, asi que no aporta nada. El p95
  verdadero se calcula en Zabbix sobre la serie almacenada:
      percentile(/HOST/infobus.api.lat_p50,1h,95)
  Eso son ~600 muestras por hora y si tiene sentido estadistico. La rafaga
  entrega la tendencia central del ciclo, que es el regresor para calibrar beta.

Por que se mide /health/ aparte, y dos veces:
  health_check es una vista Django plana, fuera de DRF, asi que no tiene
  throttling. La primera peticion de la sesion (health_cold_ms) paga abrir la
  conexion (DNS + TCP + TLS si es https). La segunda (health_ms) ya reusa la
  conexion abierta, igual que las peticiones de la rafaga, asi que es el piso
  comparable con lat_p50_ms. Por eso:
      setup_ms     = health_cold_ms - health_ms  -> costo de abrir la conexion
      query_p50_ms = lat_p50_ms - health_ms      -> costo de la consulta, que es
                                                   lo que beta pretende modelar

Por que tcp_connect_ms:
  es el tiempo de abrir una conexion TCP al host y puerto de --base, con el DNS
  ya resuelto: un viaje de ida y vuelta de red, como un ping, pero por el mismo
  puerto de la API (el firewall no lo bloquea si la API responde). Se toma la
  mediana de 3 intentos. Desde dentro (red Docker) debe ser casi cero.

Por que se valida que la respuesta sea un 200 con cuerpo:
  NextTripView responde 400 si falta stop_id y 404 si la parada no existe. Las
  dos rutas son rapidas. Un stop_id mal configurado mediria el camino de error
  y reportaria una latencia excelente y falsa. Si el status no es 200, ok = 0.

Excepcion: 204 = dia sin servicio (no es una falla):
  get_next_trips devuelve None cuando el calendario no tiene servicio para la
  fecha (en bUCR: sabado y domingo) y NextTripView responde 204. Es una
  respuesta correcta de infobus, asi que si la rafaga completa responde 200 o
  204, ok = 1. Si todas fueron 204, sin_servicio = 1 y las latencias quedan en
  null: el tiempo de una respuesta vacia no es comparable con el de un dia
  habil y contaminaria el p95.
  El tiempo de las 204 se reporta aparte en lat_sin_servicio_p50_ms: en ese
  camino get_next_trips solo hace 4 consultas fijas (feed, parada, agencia,
  calendario) y se detiene, asi que mide el costo fijo de la peticion (T_0 de
  la tuberia de umbrales) sin el costo de las horas de paso.

Por que una sola sesion HTTP:
  la primera peticion paga DNS, saludo TCP y TLS. Sin reuso de conexion la
  primera muestra contamina la rafaga (medido en databus: 848 ms contra 12 ms).
  La peticion de calentamiento se descarta.

Por que --stop-id auto (parada del peor caso):
  en infobus 9482328, feed/services/queries.py::get_next_trips devuelve TODAS las
  horas de paso de la parada en las proximas 5 h (arrival_time entre ahora y
  ahora + 5 h, sin limite y sin filtrar por servicio) y hace 2 consultas SQL por
  cada una. La latencia de next-trips crece con cuantas horas de paso tiene la
  parada consultada en esa ventana. Con --stop-id auto la sonda mide la parada
  con el pico mas alto de horas de paso en 5 h segun el GTFS, es decir, el peor
  caso, que es el que corresponde comparar contra el RNF.

  La eleccion depende solo del horario, que no cambia de un dia a otro. Se
  calcula una vez por version del GTFS y se guarda en cache (--cache). Cada
  --gtfs-check-s segundos (3600 por omision) se pregunta al servidor si el zip
  cambio (If-Modified-Since / If-None-Match): con 304 no se descarga nada; con
  200 se recalcula. Si la revision falla se sigue usando la parada guardada
  (gtfs_check_ok = 0). Si nunca se pudo calcular, se usa --stop-id-fallback y
  stop_auto = 0. El conteo replica la consulta de infobus: solo arrival_time,
  todos los servicios, ventana [t, t + 5 h] sin cruzar medianoche.

Uso:
  python3 infobus_probe.py --base https://infobus.ucr.ac.cr --stop-id bUCR_0_01
  python3 infobus_probe.py --base http://orchestrator:8000 --host-header localhost \
                           --stop-id auto --transit-system 1

  Por dentro, sin Traefik (contenedor orchestrator de infobus, HTTP en el 8000):
  python3 infobus_probe.py --base http://orchestrator:8000 --host-header localhost --stop-id bUCR_0_01
  --host-header tiene que ser un valor de ALLOWED_HOSTS de infobus; si no, Django responde 400.
  En ese modo health_ms ya no incluye TLS ni Traefik: mide solo Daphne.
  python3 infobus_probe.py --base https://infobus.ucr.ac.cr --stop-id bUCR_0_01 \
                           --repeat 10 --timeout 5

Dependencias: requests (la eleccion de parada usa solo la biblioteca estandar)
"""
import argparse
import html
import csv
import io
import json
import os
import statistics
import sys
import tempfile
import time
import zipfile

import socket
from urllib.parse import urlsplit

BASE = "https://infobus.ucr.ac.cr"
API_PATH = "/api/next-trips/"
HEALTH_PATH = "/health/"

GTFS_URL = "https://feeds.simovi.org/bucr/schedule/gtfs.zip"
VENTANA_S = 5 * 3600   # ventana fija de get_next_trips en infobus 9482328
CACHE = os.path.join(tempfile.gettempdir(), "infobus_probe_parada.json")


def _hms(t):
    """HH:MM:SS de GTFS a segundos (admite 24:00 o mas). None si viene vacio."""
    t = (t or "").strip()
    if not t:
        return None
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _pico_por_parada(datos_zip):
    """{stop_id: (pico de horas de paso en VENTANA_S, total del dia)} desde stop_times.txt."""
    z = zipfile.ZipFile(io.BytesIO(datos_zip))
    por_parada = {}
    with z.open("stop_times.txt") as fh:
        for f in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")):
            seg = _hms(f.get("arrival_time"))
            if seg is not None:
                por_parada.setdefault(f["stop_id"], []).append(seg)
    res = {}
    for parada, ts in por_parada.items():
        ts.sort()
        pico, j = 0, 0
        for i in range(len(ts)):               # ventana [ts[i], ts[i] + VENTANA_S]
            while j < len(ts) and ts[j] <= ts[i] + VENTANA_S:
                j += 1
            pico = max(pico, j - i)
        res[parada] = (pico, len(ts))
    return res


def _leer_cache(ruta):
    try:
        with open(ruta) as fh:
            return json.load(fh)
    except Exception:
        return None


def _guardar_cache(ruta, datos):
    tmp = ruta + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(datos, fh)
    os.replace(tmp, ruta)                      # escritura atomica


def elegir_parada(gtfs_url, check_s, ruta_cache, timeout=10):
    """Parada con el pico mas alto de horas de paso en 5 h, con cache por version del GTFS."""
    import urllib.error
    import urllib.request
    cache = _leer_cache(ruta_cache)
    ahora = int(time.time())
    if cache and cache.get("url") == gtfs_url and ahora - cache.get("checked_at", 0) < check_s:
        return cache, 1
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) macdt-infobus-probe/1",
               "Accept": "application/zip,application/octet-stream,*/*"}
    if cache and cache.get("url") == gtfs_url:
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
    try:
        with urllib.request.urlopen(urllib.request.Request(gtfs_url, headers=headers),
                                    timeout=timeout) as r:
            datos, lm, etag = r.read(), r.headers.get("Last-Modified"), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache:            # sin cambios: se renueva la revision
            cache["checked_at"] = ahora
            _guardar_cache(ruta_cache, cache)
            return cache, 1
        return cache, 0
    except Exception:
        return cache, 0
    picos = _pico_por_parada(datos)
    if not picos:
        return cache, 0
    # mayor pico; empate: mas horas de paso en el dia; luego orden alfabetico
    parada = sorted(picos, key=lambda p: (-picos[p][0], -picos[p][1], p))[0]
    nuevo = {"url": gtfs_url, "stop_id": parada, "pico_5h": picos[parada][0],
             "total_dia": picos[parada][1], "last_modified": lm, "etag": etag,
             "checked_at": ahora}
    _guardar_cache(ruta_cache, nuevo)
    return nuevo, 1


def _quantil(vals, q):
    """Rango mas cercano, igual que gtfsrt_probe.py para que sean comparables."""
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def _sesion(insecure=False, host_header=None):
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "macdt-infobus-probe/1"})
    if host_header:
        # Conexion directa a Daphne: Host valido para ALLOWED_HOSTS y
        # X-Forwarded-Proto para que SECURE_SSL_REDIRECT no redirija a https
        s.headers.update({"Host": host_header, "X-Forwarded-Proto": "https"})
    if insecure:
        s.verify = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


def _tcp_connect_ms(base, timeout, intentos=3):
    """Mediana del tiempo de conexion TCP a host:puerto de base, sin DNS. None si falla."""
    try:
        u = urlsplit(base)
        puerto = u.port or (443 if u.scheme == "https" else 80)
        fam, tipo, proto, _, dir_ = socket.getaddrinfo(u.hostname, puerto, type=socket.SOCK_STREAM)[0]
        tiempos = []
        for _ in range(intentos):
            s = socket.socket(fam, tipo, proto)
            s.settimeout(timeout)
            t0 = time.perf_counter()
            try:
                s.connect(dir_)
                tiempos.append((time.perf_counter() - t0) * 1000)
            finally:
                s.close()
        return round(statistics.median(tiempos), 2)
    except Exception:
        return None


def _medir(sesion, url, timeout, params=None):
    """Devuelve (status, ms, bytes). No lanza: el fallo es en si una metrica."""
    t0 = time.perf_counter()
    try:
        r = sesion.get(url, params=params, timeout=timeout)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        return r.status_code, ms, len(r.content)
    except Exception:
        ms = round((time.perf_counter() - t0) * 1000, 2)
        return 0, ms, 0


def sondear(base, stop_id, repeat, timeout, insecure, warmup=True, host_header=None,
            transit_system=None):
    ses = _sesion(insecure, host_header)
    api_url = base.rstrip("/") + API_PATH
    health_url = base.rstrip("/") + HEALTH_PATH
    params = {"stop_id": stop_id}
    if transit_system:
        # infobus responde 500 si falta: usa "default" contra un campo numerico
        params["transit_system"] = transit_system

    # Red: conexion TCP pura, antes de cualquier peticion HTTP.
    tcp_ms = _tcp_connect_ms(base, timeout)

    # Primera peticion de la sesion: incluye abrir la conexion (DNS + TCP + TLS).
    hc_status, hc_ms, _ = _medir(ses, health_url, timeout)
    # Segunda: reusa la conexion. Es el piso comparable con la rafaga.
    h_status, h_ms, _ = _medir(ses, health_url, timeout)
    h_ok = hc_status == 200 and h_status == 200

    # Calentamiento descartado: abre la conexion y no se mide.
    calentado = 0
    if warmup:
        w_status, _, _ = _medir(ses, api_url, timeout, params)
        calentado = 1 if w_status else 0

    lat, estados, bytes_tot = [], [], 0
    lat204 = []                          # tiempos de las respuestas 204 (sin servicio)
    throttled = errores = sin_serv = 0

    for _ in range(repeat):
        status, ms, nbytes = _medir(ses, api_url, timeout, params)
        estados.append(status)
        if status == 200:
            lat.append(ms)
            bytes_tot += nbytes
        elif status == 204:
            sin_serv += 1                # dia sin servicio: respuesta correcta, sin latencia util
            lat204.append(ms)
        elif status == 429:
            throttled += 1
        else:
            errores += 1

    # Status representativo: el mas frecuente de la rafaga.
    status_mode = max(set(estados), key=estados.count) if estados else 0
    ok = 1 if (len(lat) + sin_serv == repeat and status_mode in (200, 204)) else 0

    out = {
        "ts_sonda": int(time.time()),
        "ok": ok,
        "status": status_mode,
        "n": repeat,
        "n_ok": len(lat),
        "sin_servicio": 1 if sin_serv == repeat else 0,
        "status_codes": sorted(set(estados)),        # codigos distintos de la rafaga, p. ej. [200] o [200, 204]
        # Costo fijo medido en dias sin servicio (solo respuestas 204; null si no hubo)
        "lat_sin_servicio_p50_ms": _quantil(lat204, 0.50) if lat204 else None,
        # Costo de la consulta vacia (204): capa de API + 4 consultas fijas, sin horas de paso
        "query_sin_servicio_p50_ms": (round(_quantil(lat204, 0.50) - h_ms, 2) if lat204 and h_ok else None),
        "throttled": throttled,
        "errors": errores,
        "warmup": calentado,
        "stop_id": stop_id,
        # Red
        "tcp_connect_ms": tcp_ms,
        # Piso de transporte
        "health_ok": 1 if h_ok else 0,
        "health_status": h_status,
        "health_cold_ms": hc_ms if hc_status == 200 else None,
        "health_ms": h_ms if h_status == 200 else None,
        "setup_ms": round(hc_ms - h_ms, 2) if h_ok else None,
    }

    if lat:
        out.update({
            "lat_min_ms": min(lat),
            "lat_p50_ms": _quantil(lat, 0.50),
            "lat_p90_ms": _quantil(lat, 0.90),
            "lat_max_ms": max(lat),
            "lat_mean_ms": round(statistics.fmean(lat), 2),
            "lat_stdev_ms": round(statistics.stdev(lat), 2) if len(lat) > 1 else 0.0,
            # Costo atribuible a la consulta, aislado del transporte.
            "query_p50_ms": round(_quantil(lat, 0.50) - h_ms, 2) if h_ok else None,
            "bytes_mean": round(bytes_tot / len(lat)),
        })
    else:
        out.update({
            "lat_min_ms": None, "lat_p50_ms": None, "lat_p90_ms": None,
            "lat_max_ms": None, "lat_mean_ms": None, "lat_stdev_ms": None,
            "query_p50_ms": None, "bytes_mean": 0,
        })

    return out


def _p95_1h(base, p50, ahora, ventana_s=3600, serie_tipo="200"):
    """p95 de los p50 de la ultima hora, guardados por la propia sonda.

    Replica el item calculado de Zabbix percentile(/HOST/infobus.api.lat_p50,1h,95)
    para poder mostrarlo en la tabla HTML (el HTML lo arma la sonda y no puede leer
    items de Zabbix). Un archivo por --base, para no mezclar la sonda interna con la
    publica. Puede diferir un poco del item de Zabbix: la ventana no es identica.
    """
    import hashlib
    ruta = os.path.join(tempfile.gettempdir(),
                        "infobus_probe_p50_%s_%s.json" % (serie_tipo, hashlib.md5(base.encode()).hexdigest()[:8]))
    try:
        with open(ruta) as f:
            serie = [x for x in json.load(f) if ahora - x[0] <= ventana_s]
    except Exception:
        serie = []
    if p50 is not None:
        serie.append([int(ahora), p50])
    try:
        tmp = ruta + ".tmp"
        with open(tmp, "w") as f:
            json.dump(serie, f)
        os.replace(tmp, ruta)
    except Exception:
        pass
    vals = [x[1] for x in serie]
    return _quantil(vals, 0.95), len(vals)


def _tabla_html(o, base):
    """Tabla 'API INFOBUS' para el widget Item history (columna en modo HTML).

    Zabbix la muestra dentro de un iframe que hereda la fuente y el color del tema,
    asi que no se fija color de texto: solo los puntos y el estado llevan color propio.
    """
    verde, rojo, naranja = "#2E7D32", "#C62828", "#E65100"
    def punto(vivo):
        return ('<span style="display:inline-block;width:12px;height:12px;border-radius:50%%;'
                'background:%s;vertical-align:middle"></span>' % (verde if vivo else rojo))
    def ms(v):
        return "-" if v is None else "%g" % round(v, 1)
    def codigo(c):
        return "sin conexi\u00f3n" if not c else str(c)
    def estado(txt, color=None):
        return txt if not color else '<span style="color:%s;font-weight:bold">%s</span>' % (color, txt)

    h_vivo = bool(o.get("health_ok"))
    api_vivo = bool(o.get("ok"))
    if not api_vivo:
        api_estado = estado("Falla", rojo)
    elif o.get("sin_servicio"):
        api_estado = estado("Sin servicio", naranja)
    else:
        api_estado = "Activo"
    codigos = o.get("status_codes") or ([o["status"]] if "status" in o else [])
    filas = [
        ("/health/", punto(h_vivo), "Activo" if h_vivo else estado("Sin respuesta", rojo),
         codigo(o.get("health_status")), "%s ms" % ms(o.get("health_ms")) if o.get("health_ms") is not None else "-"),
        ("API (next-trips)", punto(api_vivo), api_estado,
         "/".join(codigo(c) for c in codigos) or "-",
         "%s/%s ms" % (ms(o.get("lat_p50_ms")), ms(o.get("lat_sin_servicio_p50_ms")))),
    ]
    th = 'style="text-align:left;padding:4px 12px;border-bottom:2px solid #8888;white-space:nowrap"'
    td = 'style="padding:4px 12px;border-bottom:1px solid #8884;white-space:nowrap"'
    cols = ("Servicio", "Alive", "Estado", "C\u00f3digo", "Latencia (200/204)")
    pie = "%s \u00b7 parada %s \u00b7 %s" % (
        html.escape(base), html.escape(str(o.get("stop_id") or "-")),
        time.strftime("%H:%M:%S", time.localtime(o.get("ts_sonda") or time.time())))
    if o.get("error"):
        pie += " \u00b7 error: " + html.escape(str(o["error"]))
    def tabla(titulo, cols, filas, tdc=None):
        tdc = tdc or {}
        # titulo subrayado en azul real, un salto y la tabla
        return ('<div style="font-weight:bold;font-size:1.1em;color:#4169E1;text-decoration:underline">%s</div><br>'
                '<table style="border-collapse:collapse"><tr>%s</tr>%s</table>'
                % (titulo, "".join("<th %s>%s</th>" % (th, c) for c in cols),
                   "".join("<tr>%s</tr>" % "".join("<td %s>%s</td>" % (tdc.get(k, td), c) for k, c in enumerate(f))
                           for f in filas)))

    # Tabla 2: latencias con descripcion para lectura humana
    sin_serv = o.get("sin_servicio") == 1
    nota = (" Hoy sin servicio: el valor es de la respuesta vac\u00eda (204), sin horas de paso."
            if sin_serv else "")
    def val(v, v204=None):
        """Valor de las respuestas 200; si no hay, el de las 204 marcado como tal."""
        if v is not None:
            return "%s ms" % ms(v)
        return "-" if v204 is None else "(204: %s ms)" % ms(v204)
    lat = [
        ("Latencia piso", val(o.get("health_ms")),
         "Lo m\u00ednimo que tarda infobus en contestar por HTTP con la conexi\u00f3n ya abierta "
         "(/health/: sin API, sin cuota y sin base de datos). Si sube, el problema est\u00e1 en la red "
         "o en el servidor web, no en la consulta."),
        ("Costo de la consulta", val(o.get("query_p50_ms"), o.get("query_sin_servicio_p50_ms")),
         "Lo que tarda next-trips por encima del piso: capa de API, consultas a la base de datos y "
         "armado de la respuesta. Crece con las horas de paso de las pr\u00f3ximas 5 h en la parada." + nota),
        ("Percentil 50", val(o.get("lat_p50_ms"), o.get("lat_sin_servicio_p50_ms")),
         "Tiempo t\u00edpico de next-trips en este ciclo: la mitad de las 10 consultas tard\u00f3 menos "
         "que esto y la otra mitad m\u00e1s." + nota),
        ("Percentil 95 (\u00faltima hora)", val(o.get("lat_p95_1h_sonda_ms"), o.get("lat_sin_servicio_p95_1h_sonda_ms")),
         "De los tiempos t\u00edpicos de cada minuto de la \u00faltima hora, el 95 %% qued\u00f3 por debajo "
         "de este valor: solo 3 de cada 60 minutos fueron m\u00e1s lentos. Es el valor que se compara "
         "con el umbral T_WARN y con el RNF de 200 ms (%s minutos con dato).%s"
         % (o.get("n_p95_1h_sonda") or o.get("n_sin_servicio_p95_1h_sonda") or 0, nota)),
    ]
    td_desc = 'style="padding:4px 12px;border-bottom:1px solid #8884;white-space:normal;opacity:.85"'
    td_val = 'style="padding:4px 12px;border-bottom:1px solid #8884;white-space:nowrap;text-align:right;font-weight:bold"'
    return ('<div style="font-size:calc(1em + 6px)"><br><br>'
            + tabla("API INFOBUS", cols, filas)
            + '<div style="opacity:.7;font-size:.75em;margin-top:4px">%s</div><br>' % pie
            + tabla("LATENCIAS", ("Medida", "Valor", "Qu\u00e9 significa"), lat, {1: td_val, 2: td_desc})
            + "</div>")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE, help="origen de infobus, con esquema")
    ap.add_argument("--stop-id", dest="stop_id", required=True,
                    help="stop_id real del GTFS, o 'auto' para medir la parada del peor caso")
    ap.add_argument("--gtfs-url", dest="gtfs_url", default=GTFS_URL,
                    help="GTFS estatico para elegir la parada con --stop-id auto")
    ap.add_argument("--gtfs-check-s", dest="gtfs_check_s", type=int, default=3600,
                    help="cada cuantos segundos se revisa si el GTFS cambio")
    ap.add_argument("--stop-id-fallback", dest="stop_id_fallback", default="bUCR_0_01",
                    help="parada a usar si nunca se pudo calcular la del peor caso")
    ap.add_argument("--cache", default=CACHE, help="archivo de cache de la parada elegida")
    ap.add_argument("--repeat", type=int, default=10,
                    help="peticiones por ciclo; tope efectivo 30/min por la cuota realtime")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--insecure", action="store_true",
                    help="no verificar TLS (solo si el certificado aun no es valido)")
    ap.add_argument("--no-warmup", dest="no_warmup", action="store_true")
    ap.add_argument("--host-header", dest="host_header",
                    help="Host a enviar al conectar directo al orchestrator (valor de ALLOWED_HOSTS)")
    ap.add_argument("--transit-system", dest="transit_system",
                    help="id numerico del TransitSystem en infobus (sin el, next-trips da 500)")
    a = ap.parse_args()

    if a.repeat > 25:
        print(json.dumps({"ok": 0, "error": "repeat > 25 agota la cuota realtime de 30/min",
                          "ts_sonda": int(time.time())}))
        sys.exit(0)

    extra = {}
    if a.stop_id == "auto":
        try:
            sel, check_ok = elegir_parada(a.gtfs_url, a.gtfs_check_s, a.cache)
        except Exception:
            sel, check_ok = None, 0
        if sel and sel.get("stop_id"):
            a.stop_id = sel["stop_id"]
            extra = {"stop_auto": 1, "stop_pico_5h": sel.get("pico_5h"),
                     "gtfs_last_modified": sel.get("last_modified"), "gtfs_check_ok": check_ok}
        else:
            a.stop_id = a.stop_id_fallback
            extra = {"stop_auto": 0, "stop_pico_5h": None,
                     "gtfs_last_modified": None, "gtfs_check_ok": check_ok}

    try:
        out = sondear(a.base, a.stop_id, a.repeat, a.timeout,
                      a.insecure, warmup=not a.no_warmup, host_header=a.host_header,
                      transit_system=a.transit_system)
    except Exception as e:
        out = {"ok": 0, "error": str(e)[:200], "ts_sonda": int(time.time())}

    out.update(extra)
    out["lat_p95_1h_sonda_ms"], out["n_p95_1h_sonda"] = _p95_1h(a.base, out.get("lat_p50_ms"), time.time())
    out["lat_sin_servicio_p95_1h_sonda_ms"], out["n_sin_servicio_p95_1h_sonda"] = _p95_1h(
        a.base, out.get("lat_sin_servicio_p50_ms"), time.time(), serie_tipo="204")
    out["tabla_html"] = _tabla_html(out, a.base)
    print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()