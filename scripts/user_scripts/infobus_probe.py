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

Por que una sola sesion HTTP:
  la primera peticion paga DNS, saludo TCP y TLS. Sin reuso de conexion la
  primera muestra contamina la rafaga (medido en databus: 848 ms contra 12 ms).
  La peticion de calentamiento se descarta.

Uso:
  python3 infobus_probe.py --base https://infobus.ucr.ac.cr --stop-id bUCR_0_01

  Por dentro, sin Traefik (contenedor orchestrator de infobus, HTTP en el 8000):
  python3 infobus_probe.py --base http://orchestrator:8000 --host-header localhost --stop-id bUCR_0_01
  --host-header tiene que ser un valor de ALLOWED_HOSTS de infobus; si no, Django responde 400.
  En ese modo health_ms ya no incluye TLS ni Traefik: mide solo Daphne.
  python3 infobus_probe.py --base https://infobus.ucr.ac.cr --stop-id bUCR_0_01 \
                           --repeat 10 --timeout 5

Dependencias: requests
"""
import argparse
import json
import statistics
import sys
import time

import socket
from urllib.parse import urlsplit

BASE = "https://infobus.ucr.ac.cr"
API_PATH = "/api/next-trips/"
HEALTH_PATH = "/health/"


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


def sondear(base, stop_id, repeat, timeout, insecure, warmup=True, host_header=None):
    ses = _sesion(insecure, host_header)
    api_url = base.rstrip("/") + API_PATH
    health_url = base.rstrip("/") + HEALTH_PATH
    params = {"stop_id": stop_id}

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
    throttled = errores = 0

    for _ in range(repeat):
        status, ms, nbytes = _medir(ses, api_url, timeout, params)
        estados.append(status)
        if status == 200:
            lat.append(ms)
            bytes_tot += nbytes
        elif status == 429:
            throttled += 1
        else:
            errores += 1

    # Status representativo: el mas frecuente de la rafaga.
    status_mode = max(set(estados), key=estados.count) if estados else 0
    ok = 1 if (len(lat) == repeat and status_mode == 200) else 0

    out = {
        "ts_sonda": int(time.time()),
        "ok": ok,
        "status": status_mode,
        "n": repeat,
        "n_ok": len(lat),
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


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE, help="origen de infobus, con esquema")
    ap.add_argument("--stop-id", dest="stop_id", required=True,
                    help="stop_id real del GTFS; si no existe, la API responde 404")
    ap.add_argument("--repeat", type=int, default=10,
                    help="peticiones por ciclo; tope efectivo 30/min por la cuota realtime")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--insecure", action="store_true",
                    help="no verificar TLS (solo si el certificado aun no es valido)")
    ap.add_argument("--no-warmup", dest="no_warmup", action="store_true")
    ap.add_argument("--host-header", dest="host_header",
                    help="Host a enviar al conectar directo al orchestrator (valor de ALLOWED_HOSTS)")
    a = ap.parse_args()

    if a.repeat > 25:
        print(json.dumps({"ok": 0, "error": "repeat > 25 agota la cuota realtime de 30/min",
                          "ts_sonda": int(time.time())}))
        sys.exit(0)

    try:
        out = sondear(a.base, a.stop_id, a.repeat, a.timeout,
                      a.insecure, warmup=not a.no_warmup, host_header=a.host_header)
    except Exception as e:
        out = {"ok": 0, "error": str(e)[:200], "ts_sonda": int(time.time())}

    print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()