#!/usr/bin/env python3
"""
InfoBus API latency probe for MACDT (Layer I).

Run the same script from two locations:
  - Internal (R2 agent, infobus.api key):      http://orchestrator:8000 -> Daphne + query
  - External (R4 agent, infobus.api.ext key):  https://<domain>          -> network + TLS + Traefik + Daphne + query
Zabbix calculates the external-to-internal difference (simovilab-infobus-ext template).
Prints one JSON object: use it as the master item and attach dependent items with
JSONPath, as with gtfsrt_probe-en.py.

Why use --repeat 10 instead of 30 or 100 (based on infobus/backend/infobus/settings.py):
  NextTripView declares throttle_scope = "realtime", and DEFAULT_THROTTLE_RATES
  sets "realtime" to 30/min. The limit is per IP (AnonRateThrottle), so the probe
  does not take capacity from passengers. However, exceeding 30/min triggers
  throttling and measures 429 responses instead of latency. Ten requests per
  60-second cycle means 10/min: one third of the limit, leaving room for retries.

Why report the burst p50 instead of p95:
  With n = 10, the burst p95 is the maximum and adds no useful information.
  Zabbix calculates the actual p95 over the stored series:
      percentile(/HOST/infobus.api.lat_p50,1h,95)
  This provides about 600 samples per hour and is statistically meaningful.
  The burst provides the cycle's central tendency, used as the regressor for beta.

Why measure /health/ separately and twice:
  health_check is a plain Django view outside DRF, so it is not throttled. The
  first request in the session (health_cold_ms) pays to open the connection
  (DNS + TCP + TLS for HTTPS). The second (health_ms) reuses the open connection,
  like the burst requests, and is therefore a baseline comparable to lat_p50_ms:
      setup_ms     = health_cold_ms - health_ms  -> connection setup cost
      query_p50_ms = lat_p50_ms - health_ms      -> query cost, which beta models

Why report tcp_connect_ms:
  It is the time to open a TCP connection to the host and port in --base after
  DNS resolution: a network round trip, like ping, but over the API port (the
  firewall does not block it when the API responds). The probe takes the median
  of three attempts. From inside the Docker network, it should be close to zero.

Why require a 200 response with a body:
  NextTripView responds with 400 when stop_id is missing and 404 when the stop
  does not exist. Both paths are fast. A misconfigured stop_id would measure the
  error path and report misleadingly low latency. If the status is not 200, ok = 0.

Exception: 204 means no service that day, not a failure:
  get_next_trips returns None when the calendar has no service for the date
  (Saturday and Sunday in bUCR), and NextTripView responds with 204. This is a
  valid InfoBus response, so a burst of only 200 or 204 responses has ok = 1.
  If all responses are 204, sin_servicio = 1 and latency values are null: an
  empty response is not comparable to a service-day response and would distort
  the p95. The 204 timings are reported separately in lat_sin_servicio_p50_ms:
  this path performs only four fixed queries (feed, stop, agency, calendar), so
  it measures the fixed request cost (T_0 in the threshold pipeline), excluding
  the cost of retrieving stop times.

Why use one HTTP session:
  The first request pays for DNS, TCP handshake, and TLS. Without connection
  reuse, the first sample contaminates the burst (measured in databus: 848 ms
  versus 12 ms). The warm-up request is discarded.

Why use --stop-id auto (worst-case stop):
  In infobus 9482328, feed/services/queries.py::get_next_trips returns all stop
  times in the next five hours (arrival_time between now and now + 5 h, without
  a limit or service filter) and makes two SQL queries per stop time. The
  next-trips latency increases with the number of stop times in that window.
  With --stop-id auto, the probe measures the stop with the highest number of
  stop times in five hours according to GTFS: the worst case to compare with
  the RNF.

  Stop selection depends only on the schedule, which does not change daily. It
  is calculated once per GTFS version and saved in the cache (--cache). Every
  --gtfs-check-s seconds (3600 by default), the server is checked for a new ZIP
  (If-Modified-Since / If-None-Match): a 304 downloads nothing; a 200 triggers
  recalculation. If the check fails, the cached stop remains in use
  (gtfs_check_ok = 0). If no stop could be calculated, --stop-id-fallback is
  used and stop_auto = 0. The count mirrors the InfoBus query: arrival_time
  only, all services, window [t, t + 5 h], without crossing midnight.

Usage:
  python3 infobus_probe-en.py --base https://infobus.ucr.ac.cr --stop-id bUCR_0_01
  python3 infobus_probe-en.py --base http://orchestrator:8000 --host-header localhost \
                              --stop-id auto --transit-system 1

  Internally, without Traefik (InfoBus orchestrator container, HTTP on port 8000):
  python3 infobus_probe-en.py --base http://orchestrator:8000 --host-header localhost --stop-id bUCR_0_01
  --host-header must be a value in InfoBus ALLOWED_HOSTS; otherwise Django responds with 400.
  In this mode, health_ms excludes TLS and Traefik and measures Daphne only.
  python3 infobus_probe-en.py --base https://infobus.ucr.ac.cr --stop-id bUCR_0_01 \
                              --repeat 10 --timeout 5

Dependencies: requests (stop selection uses only the standard library)
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
# ---------------------------------------------------------------------------
# Definir la duración fija de la ventana de análisis
# ---------------------------------------------------------------------------
VENTANA_S = 5 * 3600
CACHE = os.path.join(tempfile.gettempdir(), "infobus_probe_parada.json")


# ---------------------------------------------------------------------------
# Convertir una hora GTFS a segundos desde el inicio del servicio
# ---------------------------------------------------------------------------
def _hms(t):
    """HH:MM:SS de GTFS a segundos (admite 24:00 o mas). None si viene vacio."""
    t = (t or "").strip()
    if not t:
        return None
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


# ---------------------------------------------------------------------------
# Calcular el pico de horas de paso por parada dentro de la ventana móvil
# ---------------------------------------------------------------------------
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
        # -------------------------------------------------------------------
        # Recorrer las horas de llegada y contar las que caben en cada ventana
        # -------------------------------------------------------------------
        for i in range(len(ts)):
            while j < len(ts) and ts[j] <= ts[i] + VENTANA_S:
                j += 1
            pico = max(pico, j - i)
        res[parada] = (pico, len(ts))
    return res


# ---------------------------------------------------------------------------
# Leer del archivo de caché la parada calculada anteriormente
# ---------------------------------------------------------------------------
def _leer_cache(ruta):
    try:
        with open(ruta) as fh:
            return json.load(fh)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Guardar los datos de caché mediante un reemplazo atómico
# ---------------------------------------------------------------------------
def _guardar_cache(ruta, datos):
    tmp = ruta + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(datos, fh)
    os.replace(tmp, ruta)


# ---------------------------------------------------------------------------
# Elegir la parada con mayor carga y actualizar la caché según la versión GTFS
# ---------------------------------------------------------------------------
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
        if e.code == 304 and cache:
            # -------------------------------------------------------------------
            # Renovar la fecha de comprobación si el servidor confirma que no cambió
            # -------------------------------------------------------------------
            cache["checked_at"] = ahora
            _guardar_cache(ruta_cache, cache)
            return cache, 1
        return cache, 0
    except Exception:
        return cache, 0
    picos = _pico_por_parada(datos)
    if not picos:
        return cache, 0
    # -----------------------------------------------------------------------
    # Desempatar por total diario y luego por identificador de parada
    # -----------------------------------------------------------------------
    parada = sorted(picos, key=lambda p: (-picos[p][0], -picos[p][1], p))[0]
    nuevo = {"url": gtfs_url, "stop_id": parada, "pico_5h": picos[parada][0],
             "total_dia": picos[parada][1], "last_modified": lm, "etag": etag,
             "checked_at": ahora}
    _guardar_cache(ruta_cache, nuevo)
    return nuevo, 1


# ---------------------------------------------------------------------------
# Obtener un cuantil mediante el rango más cercano
# ---------------------------------------------------------------------------
def _quantil(vals, q):
    """Rango mas cercano, igual que gtfsrt_probe.py para que sean comparables."""
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


# ---------------------------------------------------------------------------
# Crear una sesión HTTP y configurar encabezados y verificación TLS
# ---------------------------------------------------------------------------
def _sesion(insecure=False, host_header=None):
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "macdt-infobus-probe/1"})
    if host_header:
        # -------------------------------------------------------------------
        # Enviar los encabezados que acepta Daphne en una conexión directa
        # -------------------------------------------------------------------
        s.headers.update({"Host": host_header, "X-Forwarded-Proto": "https"})
    if insecure:
        s.verify = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


# ---------------------------------------------------------------------------
# Medir la mediana de conexión TCP al host y puerto, sin incluir DNS
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Medir una solicitud HTTP y devolver su estado, duración y bytes recibidos
# ---------------------------------------------------------------------------
def _medir(sesion, url, timeout, params=None, cuerpo=False):
    """Return (status, ms, bytes), or (status, ms, bytes, content) with cuerpo=True.
    Never raises: a failure is itself a metric. The body is read after timing."""
    t0 = time.perf_counter()
    try:
        r = sesion.get(url, params=params, timeout=timeout)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        return (r.status_code, ms, len(r.content), r.content) if cuerpo else (r.status_code, ms, len(r.content))
    except Exception:
        ms = round((time.perf_counter() - t0) * 1000, 2)
        return (0, ms, 0, b"") if cuerpo else (0, ms, 0)


# ---------------------------------------------------------------------------
# Contar las llegadas programadas y los buses en camino de una respuesta 200
# ---------------------------------------------------------------------------
def _contar_llegadas(contenido):
    """(arrivals, buses) from next-trips: N(t) and buses on their way. (None, None) if unreadable.

    arrivals = scheduled stop times in the 5 h window (in_progress = false): this is N(t),
               the variable of T_base(t) = b + m*N(t) + delta.
    buses    = trips in progress that still have the stop ahead (in_progress = true).
    """
    try:
        arr = json.loads(contenido).get("next_arrivals") or []
        buses = sum(1 for a in arr if a.get("in_progress"))
        return len(arr) - buses, buses
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Medir la API, el servicio de salud y la conexión para una parada
# ---------------------------------------------------------------------------
def sondear(base, stop_id, repeat, timeout, insecure, warmup=True, host_header=None,
            transit_system=None):
    ses = _sesion(insecure, host_header)
    api_url = base.rstrip("/") + API_PATH
    health_url = base.rstrip("/") + HEALTH_PATH
    params = {"stop_id": stop_id}
    if transit_system:
        # -------------------------------------------------------------------
        # Incluir el sistema de transporte para evitar una consulta inválida
        # -------------------------------------------------------------------
        params["transit_system"] = transit_system

    # -----------------------------------------------------------------------
    # Medir la conexión TCP antes de enviar solicitudes HTTP
    # -----------------------------------------------------------------------
    tcp_ms = _tcp_connect_ms(base, timeout)

    # -----------------------------------------------------------------------
    # Medir el servicio de salud con una conexión nueva y luego reutilizada
    # -----------------------------------------------------------------------
    hc_status, hc_ms, _ = _medir(ses, health_url, timeout)
    h_status, h_ms, _ = _medir(ses, health_url, timeout)
    h_ok = hc_status == 200 and h_status == 200

    # -----------------------------------------------------------------------
    # Calentar la conexión con una solicitud que no se incluye en las métricas
    # -----------------------------------------------------------------------
    calentado = 0
    if warmup:
        w_status, _, _ = _medir(ses, api_url, timeout, params)
        calentado = 1 if w_status else 0

    lat, estados, bytes_tot = [], [], 0
    # -----------------------------------------------------------------------
    # Guardar por separado las duraciones de las respuestas sin servicio
    # -----------------------------------------------------------------------
    lat204 = []
    throttled = errores = sin_serv = 0

    llegadas = buses = None
    for _ in range(repeat):
        status, ms, nbytes, contenido = _medir(ses, api_url, timeout, params, cuerpo=True)
        estados.append(status)
        if status == 200:
            lat.append(ms)
            bytes_tot += nbytes
            # -------------------------------------------------------------------
            # Contar N(t) y los buses de la última respuesta válida de la ráfaga
            # -------------------------------------------------------------------
            n_l, n_b = _contar_llegadas(contenido)
            if n_l is not None:
                llegadas, buses = n_l, n_b
        elif status == 204:
            # -------------------------------------------------------------------
            # Contabilizar por separado una respuesta correcta sin servicio
            # -------------------------------------------------------------------
            sin_serv += 1
            lat204.append(ms)
        elif status == 429:
            throttled += 1
        else:
            errores += 1

    # -----------------------------------------------------------------------
    # Usar como estado representativo el código más frecuente de la ráfaga
    # -----------------------------------------------------------------------
    status_mode = max(set(estados), key=estados.count) if estados else 0
    ok = 1 if (len(lat) + sin_serv == repeat and status_mode in (200, 204)) else 0

    out = {
        "ts_sonda": int(time.time()),
        "ok": ok,
        "status": status_mode,
        "n": repeat,
        "n_ok": len(lat),
        "sin_servicio": 1 if sin_serv == repeat else 0,
        # -------------------------------------------------------------------
        # Enumerar los códigos HTTP distintos recibidos durante la ráfaga
        # -------------------------------------------------------------------
        "status_codes": sorted(set(estados)),
        # -------------------------------------------------------------------
        # Informar por separado las latencias de días sin servicio
        # -------------------------------------------------------------------
        "lat_sin_servicio_p50_ms": _quantil(lat204, 0.50) if lat204 else None,
        # -------------------------------------------------------------------
        # Separar el costo de la respuesta vacía del costo de transporte HTTP
        # -------------------------------------------------------------------
        "query_sin_servicio_p50_ms": (round(_quantil(lat204, 0.50) - h_ms, 2) if lat204 and h_ok else None),
        "throttled": throttled,
        "errors": errores,
        "warmup": calentado,
        "stop_id": stop_id,
        # -------------------------------------------------------------------
        # Exponer N(t) y los buses en camino: regresores de la calibración
        # -------------------------------------------------------------------
        "llegadas": llegadas,
        "buses_parada": buses,
        # -------------------------------------------------------------------
        # Exponer las métricas de conexión de red
        # -------------------------------------------------------------------
        "tcp_connect_ms": tcp_ms,
        # -------------------------------------------------------------------
        # Exponer las métricas base del transporte HTTP
        # -------------------------------------------------------------------
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
            # -------------------------------------------------------------------
            # Aislar el tiempo de consulta del tiempo base del transporte
            # -------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Calcular y guardar el percentil 95 de los valores de la última hora
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Construir las tablas HTML de estado y latencia para el widget de Zabbix
# ---------------------------------------------------------------------------
def _tabla_html(o, base):
    """Tabla 'API INFOBUS' para el widget Item history (columna en modo HTML).

    Zabbix la muestra dentro de un iframe que hereda la fuente y el color del tema,
    asi que no se fija color de texto: solo los puntos y el estado llevan color propio.
    """
    verde, rojo, naranja = "#2E7D32", "#C62828", "#E65100"
    # -----------------------------------------------------------------------
    # Representar el estado de disponibilidad con un punto de color
    # -----------------------------------------------------------------------
    def punto(vivo):
        return ('<span style="display:inline-block;width:12px;height:12px;border-radius:50%%;'
                'background:%s;vertical-align:middle"></span>' % (verde if vivo else rojo))
    # -----------------------------------------------------------------------
    # Formatear una latencia en milisegundos o indicar que no está disponible
    # -----------------------------------------------------------------------
    def ms(v):
        return "-" if v is None else "%g" % round(v, 1)
    # -----------------------------------------------------------------------
    # Formatear un código HTTP o indicar que no hubo conexión
    # -----------------------------------------------------------------------
    def codigo(c):
        return "no connection" if not c else str(c)
    # -----------------------------------------------------------------------
    # Aplicar color al texto del estado cuando se indique
    # -----------------------------------------------------------------------
    def estado(txt, color=None):
        return txt if not color else '<span style="color:%s;font-weight:bold">%s</span>' % (color, txt)

    h_vivo = bool(o.get("health_ok"))
    api_vivo = bool(o.get("ok"))
    if not api_vivo:
        api_estado = estado("Failure", rojo)
    elif o.get("sin_servicio"):
        api_estado = estado("No service", naranja)
    else:
        api_estado = "Active"
    codigos = o.get("status_codes") or ([o["status"]] if "status" in o else [])
    filas = [
        ("/health/", punto(h_vivo), "Active" if h_vivo else estado("No response", rojo),
         codigo(o.get("health_status")), "%s ms" % ms(o.get("health_ms")) if o.get("health_ms") is not None else "-"),
        ("API (next-trips)", punto(api_vivo), api_estado,
         "/".join(codigo(c) for c in codigos) or "-",
         "%s/%s ms" % (ms(o.get("lat_p50_ms")), ms(o.get("lat_sin_servicio_p50_ms")))),
    ]
    th = 'style="text-align:left;padding:4px 12px;border-bottom:2px solid #8888;white-space:nowrap"'
    td = 'style="padding:4px 12px;border-bottom:1px solid #8884;white-space:nowrap"'
    cols = ("Service", "Alive", "Status", "Code", "Latency (200/204)")
    pie = "%s \u00b7 stop %s \u00b7 %s" % (
        html.escape(base), html.escape(str(o.get("stop_id") or "-")),
        time.strftime("%H:%M:%S", time.localtime(o.get("ts_sonda") or time.time())))
    if o.get("error"):
        pie += " \u00b7 error: " + html.escape(str(o["error"]))
    # -----------------------------------------------------------------------
    # Crear una tabla HTML con encabezados, filas y estilos opcionales
    # -----------------------------------------------------------------------
    def tabla(titulo, cols, filas, tdc=None):
        tdc = tdc or {}
        # -------------------------------------------------------------------
        # Resaltar el título y renderizar el contenido tabular
        # -------------------------------------------------------------------
        return ('<div style="font-weight:bold;font-size:1.1em;color:#4169E1;text-decoration:underline">%s</div><br>'
                '<table style="border-collapse:collapse"><tr>%s</tr>%s</table>'
                % (titulo, "".join("<th %s>%s</th>" % (th, c) for c in cols),
                   "".join("<tr>%s</tr>" % "".join("<td %s>%s</td>" % (tdc.get(k, td), c) for k, c in enumerate(f))
                           for f in filas)))

    # -----------------------------------------------------------------------
    # Explicar cada métrica de latencia con una descripción legible
    # -----------------------------------------------------------------------
    sin_serv = o.get("sin_servicio") == 1
    nota = (" No service today: the value is from the empty response (204), with no stop times."
            if sin_serv else "")
    # -----------------------------------------------------------------------
    # Seleccionar la latencia disponible e identificar las respuestas HTTP 204
    # -----------------------------------------------------------------------
    def val(v, v204=None):
        """Valor de las respuestas 200; si no hay, el de las 204 marcado como tal."""
        if v is not None:
            return "%s ms" % ms(v)
        return "-" if v204 is None else "(204: %s ms)" % ms(v204)
    lat = [
        ("Baseline latency", val(o.get("health_ms")),
         "The minimum time InfoBus takes to respond over HTTP with the connection already open "
         "(/health/: no API, rate limit, or database). If it increases, the issue is in the network "
         "or web server, not the query."),
        ("Query cost", val(o.get("query_p50_ms"), o.get("query_sin_servicio_p50_ms")),
         "The time next-trips takes above the baseline: API layer, database queries, and response "
         "construction. It increases with the number of stop times at the stop in the next 5 hours." + nota),
        ("50th percentile", val(o.get("lat_p50_ms"), o.get("lat_sin_servicio_p50_ms")),
         "Typical next-trips time in this cycle: half of the 10 requests took less than this, "
         "and half took longer." + nota),
        ("95th percentile (last hour)", val(o.get("lat_p95_1h_sonda_ms"), o.get("lat_sin_servicio_p95_1h_sonda_ms")),
         "Of the typical times for each minute in the last hour, 95%% were below this value: "
         "only 3 of 60 minutes were slower. This is the value compared with the T_WARN threshold "
         "and the 200 ms RNF (%s minutes with data).%s"
         % (o.get("n_p95_1h_sonda") or o.get("n_sin_servicio_p95_1h_sonda") or 0, nota)),
    ]
    td_desc = 'style="padding:4px 12px;border-bottom:1px solid #8884;white-space:normal;opacity:.85"'
    td_val = 'style="padding:4px 12px;border-bottom:1px solid #8884;white-space:nowrap;text-align:right;font-weight:bold"'
    return ('<div style="font-size:calc(1em + 6px)"><br><br>'
            + tabla("INFOBUS API", cols, filas)
            + '<div style="opacity:.7;font-size:.75em;margin-top:4px">%s</div><br>' % pie
            + tabla("LATENCIES", ("Metric", "Value", "Meaning"), lat, {1: td_val, 2: td_desc})
            + "</div>")


# ---------------------------------------------------------------------------
# Leer argumentos, ejecutar la sonda y emitir las métricas en formato JSON
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE, help="InfoBus origin, including the scheme")
    ap.add_argument("--stop-id", dest="stop_id", required=True,
                    help="GTFS stop_id, or 'auto' to measure the worst-case stop")
    ap.add_argument("--gtfs-url", dest="gtfs_url", default=GTFS_URL,
                    help="Static GTFS feed used to select a stop with --stop-id auto")
    ap.add_argument("--gtfs-check-s", dest="gtfs_check_s", type=int, default=3600,
                    help="seconds between checks for GTFS updates")
    ap.add_argument("--stop-id-fallback", dest="stop_id_fallback", default="bUCR_0_01",
                    help="stop to use if the worst-case stop cannot be calculated")
    ap.add_argument("--cache", default=CACHE, help="cache file for the selected stop")
    ap.add_argument("--repeat", type=int, default=10,
                    help="requests per cycle; the effective realtime limit is 30/min")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--insecure", action="store_true",
                    help="disable TLS verification (only if the certificate is not yet valid)")
    ap.add_argument("--no-warmup", dest="no_warmup", action="store_true")
    ap.add_argument("--host-header", dest="host_header",
                    help="Host header to send when connecting directly to the orchestrator (ALLOWED_HOSTS value)")
    ap.add_argument("--transit-system", dest="transit_system",
                    help="numeric TransitSystem ID in InfoBus (without it, next-trips returns 500)")
    a = ap.parse_args()

    if a.repeat > 25:
        print(json.dumps({"ok": 0, "error": "repeat > 25 exhausts the realtime limit of 30/min",
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