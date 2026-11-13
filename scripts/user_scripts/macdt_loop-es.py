#!/usr/bin/env python3
"""
Bucle MACDT (F5): ajusta el umbral de latencia de TI según la carga de transporte.

Este es el ejecutor del bucle MAPE-K. Cada ciclo:

  Monitor   lee rho, V_gps y la compuerta de la ventana operativa de los items de databus
  Analizar  decide si aplica el ajuste por cobertura GPS degradada
  Planificar calcula T_warn = clamp(T_base(t) * (1 + beta*rho) * F, T_floor, T_RNF)
            con T_base(t) = b + m*N(t) + delta
  Ejecutar  escribe {$T_LATENCY_WARN} como macro HOST en el host de infobus
  Conocimiento registra el ciclo en history.jsonl para reproducir OE5

Por qué T_base es ajustable (resultado preliminar de laboratorio, 2026-11-10):
  La latencia de las próximas llegadas crece linealmente con las paradas
  programadas en la ventana de 5 h de la parada consultada (R2 = 0.9986).
  Por tanto, T_base ya no es una constante, sino la recta del horario:
      T_base(t) = b + m*N(t) + delta
  N(t) = infobus.api.llegadas_p95_1h: llegadas en la misma ventana y percentil
  que la latencia con la que se compara el umbral.
  m = peso de cada llegada, b = costo fijo, delta = margen (percentil
  MACDT_CALIB_DELTA_PCT de los residuos; 95 por defecto). beta sigue
  representando el peso de los buses activos (rho).
  Hasta ejecutar una calibración, m = 0, delta = 0 y b = {$MACDT.T_BASE}:
  el bucle se comporta exactamente como antes.

Calibración (--calibrate o diaria con MACDT_CALIB_MODE=daily):
  Regresión latencia = b + m*N + c*V con los datos propios de la sonda
  (infobus.api.lat_p95_1h, infobus.api.llegadas_p95_1h) y
  databus gtfs.v_trips durante MACDT_CALIB_WINDOW_D días.
  Por qué se usa p95 de 1 h y no el p50 de cada ciclo: la alerta compara
  {$T_LATENCY_WARN} con infobus.api.lat_p95_1h y el bucle alimenta N con
  infobus.api.llegadas_p95_1h. Una recta ajustada al p50 queda por debajo del
  p95 normal cuando hay muchas llegadas, y el umbral termina sobre el
  comportamiento normal. Ajustada al p95, la recta pasa por el p95 normal de
  cada carga (b es el p95 sin llegadas) y delta es el margen por encima.
  Ambos items p95 se calculan cada minuto y no coinciden en el mismo segundo,
  por lo que se unen por minuto.
  Los valores p95_1h consecutivos comparten 59 minutos de datos: R2 resulta
  muy alto. Sirve para trazar la recta, no como medida de precisión.
  c es el costo de cada bus activo y se convierte a
  beta = c * V_max / media(T_base) mientras circulan buses.
  La nueva recta se aplica solo si:
    - hay al menos MACDT_CALIB_MIN_POINTS puntos y R2 >= MACDT_CALIB_MIN_R2
    - m > 0
    - la recta no cambia más de MACDT_CALIB_MAX_DRIFT respecto a la actual
      (comparada en los valores bajo y alto de N de la ventana). Si cambia más,
      no se aplica automáticamente: macdt.calib = 2 y una persona decide con
      --calibrate --force. Así se evita que el bucle aprenda una degradación
      como si fuera el nuevo comportamiento normal.
  Si no hay buses en la ventana, no se puede medir c y se conserva beta.
  Los valores calibrados se escriben como macros HOST en infobus.

Por qué la macro se escribe en el nivel HOST y no en la plantilla:
  zabbix-init vuelve a importar las plantillas con updateExisting=true y las
  macros forman parte del YAML. Escribir la macro de plantilla restablecería
  el umbral a su valor inicial y borraría el estado del bucle en cada
  redespliegue. La macro del host prevalece sobre el valor heredado y el proceso
  de inicialización no modifica las macros de host.

Por qué este script lee p.calculated en lugar de calcular rho:
  este item calculado ya aplica {$MACDT.RHO_CAP}. Recalcularlo aquí duplicaría
  la definición y podría producir resultados inconsistentes.

Por qué el factor GPS se aplica solo cuando está abierta la compuerta de la
ventana operativa:
  fuera de la ventana operativa o cuando V_gps está obsoleto (más antiguo que
  MACDT_MAX_AGE segundos), F = 1.0 y se restablece la histéresis. Sin buses, la
  sonda devuelve v_gps_pct = null y Zabbix lo descarta, dejando el último valor
  del item durante el día. Usar ese valor ajustaría el umbral durante la noche,
  cuando no hay buses en servicio.

Por qué no se modifica el umbral crítico:
  {$T_LATENCY_CRIT} es el requisito de nivel de servicio. Un compromiso que se
  relaja con la carga deja de ser un compromiso. El bucle solo cambia el umbral
  de advertencia.

Por qué se comprueba la antigüedad de los datos:
  escribir un umbral con datos obsoletos es peor que no escribirlo. Si la
  muestra más reciente de rho tiene más de MACDT_MAX_AGE segundos, el ciclo se
  marca como degradado, la macro no cambia y el estado se envía mediante trapper.

Por qué el ajuste del umbral por GPS usa persistencia e histéresis:
  sin persistencia, una sola muestra incorrecta ajustaría el umbral y el ciclo
  siguiente lo liberaría, causando oscilaciones. El ajuste comienza tras 3
  muestras por debajo de V_GPS.CRIT y termina solo al superar V_GPS.WARN. Sin
  datos GPS, F = 1.0: la falta de datos no ajusta el umbral.

Uso:
  macdt_loop-es.py                    # bucle continuo
  macdt_loop-es.py --once             # ejecuta un ciclo y termina
  macdt_loop-es.py --once --dry-run   # calcula y muestra sin escribir
  macdt_loop-es.py --calibrate --dry-run   # ajusta y muestra la recta, sin escribir
  macdt_loop-es.py --calibrate             # calibra y aplica si supera las validaciones
  macdt_loop-es.py --calibrate --force     # aplica aunque cambie más que MAX_DRIFT

Variables de entorno (todas tienen valores predeterminados razonables excepto las credenciales):
  ZBX_API_URL, ZBX_API_USER, ZBX_API_PASS
  MACDT_HOST_DATABUS, MACDT_HOST_INFOBUS
  MACDT_TPL_DATABUS, MACDT_TPL_INFOBUS
  MACDT_INTERVAL, MACDT_MAX_AGE, MACDT_STATE_DIR
  MACDT_CALIB_MODE (daily | manual), MACDT_CALIB_HOUR (HH:MM),
  MACDT_CALIB_WINDOW_D, MACDT_CALIB_MIN_R2, MACDT_CALIB_MIN_POINTS,
  MACDT_CALIB_MAX_DRIFT, MACDT_CALIB_DELTA_PCT (50-100, 95 por defecto)
  ZBX_SERVER, ZBX_SERVER_PORT         (para el item trapper macdt.status)

Dependencia: requests
"""
import argparse
import json
import os
import socket
import struct
import sys
import time

import requests

API_URL = os.environ.get("ZBX_API_URL", "http://zabbix-dashboard:8080/api_jsonrpc.php")
API_USER = os.environ.get("ZBX_API_USER", "")
API_PASS = os.environ.get("ZBX_API_PASS", "")

HOST_DATABUS = os.environ.get("MACDT_HOST_DATABUS", "Databus-host")
HOST_INFOBUS = os.environ.get("MACDT_HOST_INFOBUS", "Infobus-host")
TPL_DATABUS = os.environ.get("MACDT_TPL_DATABUS", "simovilab-databus")
TPL_INFOBUS = os.environ.get("MACDT_TPL_INFOBUS", "simovilab-infobus")

INTERVAL = int(os.environ.get("MACDT_INTERVAL", "60"))
MAX_AGE = int(os.environ.get("MACDT_MAX_AGE", "180"))
STATE_DIR = os.environ.get("MACDT_STATE_DIR", "/var/lib/macdt")

ZBX_SERVER = os.environ.get("ZBX_SERVER", "zabbix-server-pgsql")
ZBX_SERVER_PORT = int(os.environ.get("ZBX_SERVER_PORT", "10051"))

STATE_FILE = os.path.join(STATE_DIR, "state.json")
HISTORY_FILE = os.path.join(STATE_DIR, "history.jsonl")
CALIB_FILE = os.path.join(STATE_DIR, "calib.json")

# ---------------------------------------------------------------------------
# Parámetros de calibración de T_base(t) y beta
# ---------------------------------------------------------------------------
CALIB_MODE = os.environ.get("MACDT_CALIB_MODE", "manual").strip().lower()
CALIB_HOUR = os.environ.get("MACDT_CALIB_HOUR", "03:30").strip()
CALIB_WINDOW_D = int(os.environ.get("MACDT_CALIB_WINDOW_D", "7"))
CALIB_MIN_R2 = float(os.environ.get("MACDT_CALIB_MIN_R2", "0.8"))
CALIB_MIN_POINTS = int(os.environ.get("MACDT_CALIB_MIN_POINTS", "300"))
CALIB_MAX_DRIFT = float(os.environ.get("MACDT_CALIB_MAX_DRIFT", "0.25"))
# Percentil de los residuos usado como delta (margen por encima del p95 normal).
# 95: ~5 % de los minutos normales por encima de T_base; 99: ~1 %.
CALIB_DELTA_PCT = float(os.environ.get("MACDT_CALIB_DELTA_PCT", "95"))

# Items usados en la calibración: misma ventana y percentil que la alerta
ITEM_CALIB_LAT = "infobus.api.lat_p95_1h"
ITEM_CALIB_N = "infobus.api.llegadas_p95_1h"

# ---------------------------------------------------------------------------
# Definir los items consultados en el host de databus
# ---------------------------------------------------------------------------
ITEMS_DATABUS = ["p.calculated", "gtfs.v_gps_pct", "macdt.gate",
                 "gtfs.v_trips", "macdt.v_trips_max", "gtfs.fetch_ok"]

# ---------------------------------------------------------------------------
# Definir las macros de plantilla con los parámetros de cálculo
# ---------------------------------------------------------------------------
MACROS_NEEDED = ["{$MACDT.T_BASE}", "{$MACDT.BETA}", "{$MACDT.F_CRIT}",
                 "{$MACDT.T_FLOOR}", "{$MACDT.T_RNF}",
                 "{$MACDT.V_GPS.CRIT}", "{$MACDT.V_GPS.WARN}",
                 "{$MACDT.M}", "{$MACDT.B}", "{$MACDT.DELTA}"]

# ---------------------------------------------------------------------------
# Item de infobus con N(t): llegadas en la misma ventana que la latencia
# ---------------------------------------------------------------------------
ITEM_N = "infobus.api.llegadas_p95_1h"

MACRO_OUT = "{$T_LATENCY_WARN}"


# ---------------------------------------------------------------------------
# Imprimir un mensaje con la hora local y vaciar el búfer de salida
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Agrupar las operaciones necesarias para comunicarse con la API de Zabbix
# ---------------------------------------------------------------------------
class Zabbix:
    # -----------------------------------------------------------------------
    # Inicializar la sesión HTTP y las credenciales de acceso a Zabbix
    # -----------------------------------------------------------------------
    def __init__(self, url, user, password):
        self.url, self.user, self.password = url, user, password
        self.token = None
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json-rpc"})

    # -----------------------------------------------------------------------
    # Obtener un token de acceso y guardarlo en la sesión
    # -----------------------------------------------------------------------
    def login(self):
        r = self._raw("user.login", {"username": self.user, "password": self.password}, auth=False)
        self.token = r
        self.s.headers.update({"Authorization": f"Bearer {self.token}"})

    # -----------------------------------------------------------------------
    # Enviar una solicitud JSON-RPC y devolver el resultado
    # -----------------------------------------------------------------------
    def _raw(self, method, params, auth=True):
        if auth and not self.token:
            self.login()
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        resp = self.s.post(self.url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error'].get('data') or data['error'].get('message')}")
        return data["result"]

    # -----------------------------------------------------------------------
    # Llamar a la API y volver a autenticarse si el token expiró
    # -----------------------------------------------------------------------
    def call(self, method, params):
        """Reintentar una vez tras autenticarse de nuevo, ya que el token pudo expirar."""
        try:
            return self._raw(method, params)
        except RuntimeError as e:
            if "Not authorised" in str(e) or "re-login" in str(e).lower():
                self.token = None
                self.login()
                return self._raw(method, params)
            raise


# ---------------------------------------------------------------------------
# Leer el último valor y la hora de actualización de cada item solicitado
# ---------------------------------------------------------------------------
def leer_items(z, host, keys):
    """Devuelve el último valor y la hora de cada clave; item.get proporciona lastvalue directamente."""
    res = z.call("item.get", {
        "host": host,
        "filter": {"key_": keys},
        "output": ["key_", "lastvalue", "lastclock", "name"],
    })
    out = {}
    for it in res:
        try:
            val = float(it["lastvalue"]) if it["lastvalue"] not in ("", None) else None
        except (TypeError, ValueError):
            val = None
        out[it["key_"]] = {"v": val, "clock": int(it.get("lastclock") or 0)}
    faltan = [k for k in keys if k not in out]
    if faltan:
        raise RuntimeError(f"faltan items en '{host}': {faltan}")
    return out


# ---------------------------------------------------------------------------
# Leer de las plantillas las macros necesarias para calcular el umbral
# ---------------------------------------------------------------------------
def leer_macros(z, plantillas, necesarias, host=None):
    """Lee constantes de las macros de plantilla. Los valores de infobus prevalecen sobre los de databus.

    Si se indica un host, sus macros HOST prevalecen sobre las de plantilla:
    allí escribe la calibración, para que una reimportación de la plantilla no las borre."""
    vals = {}
    for nombre in plantillas:
        tpl = z.call("template.get", {"filter": {"host": [nombre]}, "output": ["templateid"]})
        if not tpl:
            raise RuntimeError(f"la plantilla '{nombre}' no existe")
        macros = z.call("usermacro.get", {"hostids": tpl[0]["templateid"],
                                          "output": ["macro", "value"]})
        for m in macros:
            if m["macro"] in necesarias:
                vals[m["macro"]] = float(m["value"])
    if host:
        for macro, valor in leer_macros_host(z, host).items():
            if macro in necesarias:
                vals[macro] = valor
    faltan = [m for m in necesarias if m not in vals]
    if faltan:
        raise RuntimeError(f"faltan macros en {plantillas}: {faltan}")
    return vals


# ---------------------------------------------------------------------------
# Leer las macros HOST numéricas de un host
# ---------------------------------------------------------------------------
def leer_macros_host(z, host):
    h = z.call("host.get", {"filter": {"host": [host]}, "output": ["hostid"]})
    if not h:
        raise RuntimeError(f"el host '{host}' no existe")
    out = {}
    for m in z.call("usermacro.get", {"hostids": h[0]["hostid"], "output": ["macro", "value"]}):
        try:
            out[m["macro"]] = float(m["value"])
        except (TypeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# Cargar del disco el estado persistente del ajuste por GPS
# ---------------------------------------------------------------------------
def cargar_estado():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"hardened": False, "below": 0}


# ---------------------------------------------------------------------------
# Guardar de forma atómica el estado persistente del ciclo
# ---------------------------------------------------------------------------
def guardar_estado(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Aplicar el factor GPS con persistencia e histéresis
# ---------------------------------------------------------------------------
def factor_gps(v_gps, gps_crit, gps_warn, f_crit, st, persistencia=3):
    """Devuelve (F, estado_nuevo). No ajusta el umbral si faltan datos GPS."""
    if v_gps is None:
        return 1.0, st

    if st["hardened"]:
        if v_gps > gps_warn:
            st = {"hardened": False, "below": 0}
    else:
        if v_gps < gps_crit:
            st["below"] += 1
            if st["below"] >= persistencia:
                st = {"hardened": True, "below": st["below"]}
        else:
            st["below"] = 0

    return (f_crit if st["hardened"] else 1.0), st


# ---------------------------------------------------------------------------
# Calcular el umbral de advertencia y limitarlo al rango permitido
# ---------------------------------------------------------------------------
def calcular_umbral(t_base, beta, rho, factor, t_floor, t_rnf):
    crudo = t_base * (1.0 + beta * rho) * factor
    return round(max(t_floor, min(crudo, t_rnf)), 1), round(crudo, 1)


# ---------------------------------------------------------------------------
# Crear o actualizar una macro de host con el umbral calculado
# ---------------------------------------------------------------------------
def escribir_macro(z, host, macro, valor):
    """Crea o actualiza la macro HOST. Devuelve 'create' | 'update' | 'noop'."""
    h = z.call("host.get", {"filter": {"host": [host]}, "output": ["hostid"]})
    if not h:
        raise RuntimeError(f"el host '{host}' no existe")
    hostid = h[0]["hostid"]

    existentes = z.call("usermacro.get", {"hostids": hostid,
                                          "filter": {"macro": macro},
                                          "output": ["hostmacroid", "value"]})
    nuevo = str(valor)
    if not existentes:
        z.call("usermacro.create", {"hostid": hostid, "macro": macro, "value": nuevo})
        return "create"
    if existentes[0]["value"] == nuevo:
        return "noop"
    z.call("usermacro.update", {"hostmacroid": existentes[0]["hostmacroid"], "value": nuevo})
    return "update"


# ---------------------------------------------------------------------------
# Publicar un valor en un item trapper mediante la API de Zabbix
# ---------------------------------------------------------------------------
def enviar_trapper(z, host, clave, valor):
    """Envía el valor al item trapper mediante la API (history.push, Zabbix 7.0+).

    No usar el puerto 10051: los hosts registrados automáticamente solo aceptan
    PSK y el servidor rechaza conexiones trapper sin cifrar sin devolver aquí un error.
    """
    try:
        r = z.call("history.push", [{"host": host, "key": clave, "value": str(valor)}])
        errores = [d["error"] for d in r.get("data", []) if d.get("error")]
        if errores:
            log(f"  advertencia: Zabbix rechazó {clave}: {errores[0]}")
            return False
        return True
    except Exception as e:
        log(f"  advertencia: no se pudo enviar {clave}: {e}")
        return False


# ---------------------------------------------------------------------------
# Añadir un registro JSON al historial persistente del proceso
# ---------------------------------------------------------------------------
def registrar(reg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(reg) + "\n")


# ---------------------------------------------------------------------------
# Calcular T_base(t) a partir de la recta calibrada
# ---------------------------------------------------------------------------
def t_base_t(b, m, delta, n):
    return b + m * n + delta


# ---------------------------------------------------------------------------
# Resolver un sistema lineal pequeño mediante eliminación de Gauss
# ---------------------------------------------------------------------------
def _resolver(a, y):
    n = len(y)
    a = [fila[:] + [y[i]] for i, fila in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("singular system")
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r != col:
                f = a[r][col] / a[col][col]
                for k in range(col, n + 1):
                    a[r][k] -= f * a[col][k]
    return [a[i][n] / a[i][i] for i in range(n)]


# ---------------------------------------------------------------------------
# Ajustar por mínimos cuadrados con intercepto: y = c0 + c1*x1 + c2*x2 ...
# ---------------------------------------------------------------------------
def ajustar(filas_x, y):
    """filas_x: lista de filas regresoras (sin la columna de 1). Devuelve (coef, r2, residuos)."""
    x = [[1.0] + list(f) for f in filas_x]
    k = len(x[0])
    xtx = [[sum(r[i] * r[j] for r in x) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * yy for r, yy in zip(x, y)) for i in range(k)]
    coef = _resolver(xtx, xty)
    pred = [sum(c * v for c, v in zip(coef, r)) for r in x]
    res = [yy - pp for yy, pp in zip(y, pred)]
    media = sum(y) / len(y)
    ss_tot = sum((yy - media) ** 2 for yy in y)
    r2 = 1 - sum(e * e for e in res) / ss_tot if ss_tot else 0.0
    return coef, r2, res


# ---------------------------------------------------------------------------
# Obtener el historial de un item durante una ventana de tiempo
# ---------------------------------------------------------------------------
def historial(z, host, clave, desde, hasta):
    it = z.call("item.get", {"host": host, "filter": {"key_": clave},
                             "output": ["itemid", "value_type"]})
    if not it:
        raise RuntimeError(f"el item '{clave}' no existe en '{host}'")
    res = z.call("history.get", {"itemids": it[0]["itemid"], "history": int(it[0]["value_type"]),
                                 "time_from": desde, "time_till": hasta,
                                 "sortfield": "clock", "sortorder": "ASC",
                                 "output": ["clock", "value"]})
    return [(int(r["clock"]), float(r["value"])) for r in res]


# ---------------------------------------------------------------------------
# Cargar y guardar el estado de calibración diaria
# ---------------------------------------------------------------------------
def cargar_calib():
    try:
        with open(CALIB_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_calib(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CALIB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, CALIB_FILE)


# ---------------------------------------------------------------------------
# Calibrar b, m, delta y beta con los datos de la sonda
# ---------------------------------------------------------------------------
def calibrar(z, dry_run=False, force=False):
    """Regresión latencia = b + m*N + c*V. Devuelve el registro de calibración.

    estado: 1 aplicado | 0 rechazado (datos insuficientes o mal ajuste) |
            2 retenido (cambios superiores a MAX_DRIFT; aplicar con --force)
    """
    hasta = int(time.time())
    desde = hasta - CALIB_WINDOW_D * 86400
    reg = {"ts": hasta, "tipo": "calibracion", "ventana_d": CALIB_WINDOW_D}

    # -----------------------------------------------------------------------
    # Unir latencia y llegadas por minuto (dos items calculados, cada uno con
    # su propio segundo) y añadir el valor de V_trips de databus vigente entonces
    # -----------------------------------------------------------------------
    lat = {c // 60: (c, v) for c, v in historial(z, HOST_INFOBUS, ITEM_CALIB_LAT, desde, hasta)}
    lleg = {c // 60: v for c, v in historial(z, HOST_INFOBUS, ITEM_CALIB_N, desde, hasta)}
    vtr = historial(z, HOST_DATABUS, "gtfs.v_trips", desde, hasta)
    reg.update({"item_latencia": ITEM_CALIB_LAT, "item_n": ITEM_CALIB_N, "delta_pct": CALIB_DELTA_PCT})

    filas, j = [], 0
    for minuto in sorted(set(lat) & set(lleg)):
        clock, latencia = lat[minuto]
        while j + 1 < len(vtr) and vtr[j + 1][0] <= clock:
            j += 1
        v = vtr[j][1] if vtr and vtr[j][0] <= clock and clock - vtr[j][0] <= MAX_AGE else None
        filas.append((lleg[minuto], v, latencia))
    reg["puntos"] = len(filas)

    # -----------------------------------------------------------------------
    # Rechazar si no hay datos suficientes
    # -----------------------------------------------------------------------
    if len(filas) < CALIB_MIN_POINTS:
        reg.update({"estado": 0, "motivo": f"{len(filas)} puntos, mínimo {CALIB_MIN_POINTS}"})
        return _cerrar_calib(z, reg, dry_run)

    # -----------------------------------------------------------------------
    # Medir c si hay buses en la ventana; de lo contrario, ajustar solo b y m
    # -----------------------------------------------------------------------
    con_v = [f for f in filas if f[1] is not None]
    hay_buses = len(con_v) >= CALIB_MIN_POINTS and len({f[1] for f in con_v}) > 1
    try:
        if hay_buses:
            (b, m, c), r2, res = ajustar([[f[0], f[1]] for f in con_v], [f[2] for f in con_v])
            usadas = con_v
        else:
            (b, m), r2, res = ajustar([[f[0]] for f in filas], [f[2] for f in filas])
            c, usadas = None, filas
    except ValueError as e:
        reg.update({"estado": 0, "motivo": f"falló la regresión: {e}"})
        return _cerrar_calib(z, reg, dry_run)

    q = min(max(CALIB_DELTA_PCT, 50.0), 100.0) / 100.0
    delta = max(0.0, sorted(res)[min(len(res) - 1, int(round(q * (len(res) - 1))))])
    reg.update({"b": round(b, 2), "m": round(m, 4), "c": round(c, 2) if c is not None else None,
                "delta": round(delta, 2), "r2": round(r2, 4), "puntos_usados": len(usadas)})

    # -----------------------------------------------------------------------
    # beta = c * V_max / media de T_base mientras circulan buses
    # -----------------------------------------------------------------------
    beta = None
    if c is not None and c > 0:
        v_max = leer_items(z, HOST_DATABUS, ["macdt.v_trips_max"])["macdt.v_trips_max"]["v"]
        con_bus = [t_base_t(b, m, delta, f[0]) for f in usadas if f[1] and f[1] > 0]
        if v_max and con_bus:
            beta = c * v_max / (sum(con_bus) / len(con_bus))
            reg.update({"v_max": v_max, "t_base_media_con_buses": round(sum(con_bus) / len(con_bus), 1)})
    reg["beta"] = round(beta, 4) if beta is not None else None
    if beta is None:
        reg["beta_nota"] = "no hay suficientes buses en la ventana: se conserva beta"

    # -----------------------------------------------------------------------
    # Comprobaciones de calidad
    # -----------------------------------------------------------------------
    if r2 < CALIB_MIN_R2 or m <= 0:
        reg.update({"estado": 0, "motivo": f"R2={r2:.3f} (mínimo {CALIB_MIN_R2}) o m={m:.3f} <= 0"})
        return _cerrar_calib(z, reg, dry_run)

    # -----------------------------------------------------------------------
    # Comprobar la deriva: comparar la recta nueva con la actual para N bajo y alto
    # -----------------------------------------------------------------------
    previas = leer_macros_host(z, HOST_INFOBUS)
    ns = sorted(f[0] for f in usadas)
    n_bajo, n_alto = ns[int(0.05 * (len(ns) - 1))], ns[int(0.95 * (len(ns) - 1))]
    deriva = []
    if all(k in previas for k in ("{$MACDT.M}", "{$MACDT.B}", "{$MACDT.DELTA}")) and previas["{$MACDT.M}"] > 0:
        for n in (n_bajo, n_alto):
            viejo = t_base_t(previas["{$MACDT.B}"], previas["{$MACDT.M}"], previas["{$MACDT.DELTA}"], n)
            nuevo = t_base_t(b, m, delta, n)
            if viejo > 0:
                deriva.append(abs(nuevo - viejo) / viejo)
        if beta is not None and previas.get("{$MACDT.BETA}", 0) > 0:
            deriva.append(abs(beta - previas["{$MACDT.BETA}"]) / previas["{$MACDT.BETA}"])
    reg["deriva_max"] = round(max(deriva), 3) if deriva else None
    reg["n_bajo_alto"] = [n_bajo, n_alto]

    if deriva and max(deriva) > CALIB_MAX_DRIFT and not force:
        reg.update({"estado": 2, "motivo": f"cambio del {max(deriva):.0%} (máximo {CALIB_MAX_DRIFT:.0%}); "
                                            "revise y aplique con --calibrate --force"})
        return _cerrar_calib(z, reg, dry_run)

    # -----------------------------------------------------------------------
    # Aplicar: macros HOST en infobus
    # -----------------------------------------------------------------------
    reg["estado"] = 1
    if not dry_run:
        escribir_macro(z, HOST_INFOBUS, "{$MACDT.M}", round(m, 4))
        escribir_macro(z, HOST_INFOBUS, "{$MACDT.B}", round(b, 2))
        escribir_macro(z, HOST_INFOBUS, "{$MACDT.DELTA}", round(delta, 2))
        if beta is not None:
            escribir_macro(z, HOST_INFOBUS, "{$MACDT.BETA}", round(beta, 4))
    return _cerrar_calib(z, reg, dry_run)


# ---------------------------------------------------------------------------
# Registrar el resultado de la calibración y notificar a Zabbix
# ---------------------------------------------------------------------------
def _cerrar_calib(z, reg, dry_run):
    textos = {0: "RECHAZADA", 1: "APLICADA", 2: "RETENIDA"}
    pref = "[dry-run] " if dry_run else ""
    log(f"{pref}Calibración {textos[reg['estado']]}: "
        + ", ".join(f"{k}={reg[k]}" for k in ("puntos", "b", "m", "c", "delta", "delta_pct", "beta", "r2", "deriva_max", "motivo")
                    if reg.get(k) is not None))
    if dry_run:
        reg["accion"] = "dry-run"
        return reg
    registrar(reg)
    enviar_trapper(z, HOST_INFOBUS, "macdt.calib", reg["estado"])
    enviar_trapper(z, HOST_INFOBUS, "macdt.calib.info", json.dumps(reg))
    return reg


# ---------------------------------------------------------------------------
# Determinar si corresponde ejecutar la calibración diaria
# ---------------------------------------------------------------------------
def toca_calibrar():
    if CALIB_MODE != "daily":
        return False
    hoy = time.strftime("%Y-%m-%d")
    if cargar_calib().get("ultimo_dia") == hoy:
        return False
    return time.strftime("%H:%M") >= CALIB_HOUR


# ---------------------------------------------------------------------------
# Leer los datos, calcular el umbral y aplicar las actualizaciones del ciclo
# ---------------------------------------------------------------------------
def ciclo(z, dry_run=False):
    ahora = int(time.time())
    reg = {"ts": ahora, "status": "ok"}

    items = leer_items(z, HOST_DATABUS, ITEMS_DATABUS)
    m = leer_macros(z, [TPL_DATABUS, TPL_INFOBUS], MACROS_NEEDED, host=HOST_INFOBUS)

    rho_i = items["p.calculated"]
    edad = ahora - rho_i["clock"] if rho_i["clock"] else None

    # -----------------------------------------------------------------------
    # Conservar el umbral actual si falta el valor de carga o está obsoleto
    # -----------------------------------------------------------------------
    if rho_i["v"] is None or edad is None or edad > MAX_AGE:
        reg.update({"status": "degraded",
                    "motivo": f"falta rho o tiene {edad} segundos de antigüedad (límite {MAX_AGE})"})
        log(f"DEGRADADO: {reg['motivo']}. Se conserva el umbral actual.")
        registrar(reg)
        if not dry_run:
            enviar_trapper(z, HOST_INFOBUS, "macdt.status", 0)
        return reg

    rho = rho_i["v"]
    v_gps_i = items["gtfs.v_gps_pct"]
    v_gps = v_gps_i["v"]
    gate = items["macdt.gate"]["v"]

    st = cargar_estado()
    # -----------------------------------------------------------------------
    # Restablecer la histéresis si no hay servicio o la medición GPS está obsoleta.
    # Sin buses en servicio, el valor GPS retenido no representa la experiencia
    # de los pasajeros; por tanto, no aplicar el factor de ajuste.
    # -----------------------------------------------------------------------
    if gate != 1 or ahora - v_gps_i["clock"] > MAX_AGE:
        v_gps = None
        st = {"hardened": False, "below": 0}
    factor, st_nuevo = factor_gps(v_gps, m["{$MACDT.V_GPS.CRIT}"],
                                  m["{$MACDT.V_GPS.WARN}"], m["{$MACDT.F_CRIT}"], st)

    # -----------------------------------------------------------------------
    # T_base(t) = b + m*N(t) + delta. Usar el valor inicial T_BASE si N está obsoleto.
    # -----------------------------------------------------------------------
    try:
        n_i = leer_items(z, HOST_INFOBUS, [ITEM_N])[ITEM_N]
    except RuntimeError:
        n_i = {"v": None, "clock": 0}   # El item aún no está en la plantilla: usar T_BASE
    n_edad = ahora - n_i["clock"] if n_i["clock"] else None
    if n_i["v"] is None or n_edad is None or n_edad > MAX_AGE:
        n_t, t_base = None, m["{$MACDT.T_BASE}"]
        log(f"  advertencia: falta {ITEM_N} o tiene {n_edad} s de antigüedad; se usa T_BASE = {t_base}")
    else:
        n_t = n_i["v"]
        t_base = t_base_t(m["{$MACDT.B}"], m["{$MACDT.M}"], m["{$MACDT.DELTA}"], n_t)

    t_warn, crudo = calcular_umbral(t_base, m["{$MACDT.BETA}"], rho,
                                    factor, m["{$MACDT.T_FLOOR}"], m["{$MACDT.T_RNF}"])

    reg.update({
        "rho": rho, "rho_age_s": edad, "v_gps_pct": v_gps, "gate": gate,
        "v_trips": items["gtfs.v_trips"]["v"],
        "v_trips_max": items["macdt.v_trips_max"]["v"],
        "fetch_ok": items["gtfs.fetch_ok"]["v"],
        "n_t": n_t, "n_age_s": n_edad,
        "m": m["{$MACDT.M}"], "b": m["{$MACDT.B}"], "delta": m["{$MACDT.DELTA}"],
        "t_base": round(t_base, 1), "beta": m["{$MACDT.BETA}"],
        "factor_gps": factor, "hardened": st_nuevo["hardened"],
        "t_warn_crudo": crudo, "t_warn": t_warn,
        "clamped": crudo != t_warn,
    })

    if dry_run:
        log(f"[dry-run] N={n_t} T_base={t_base:.1f} rho={rho:.2f} v_gps={v_gps} F={factor} -> T_warn={t_warn} ms "
            f"(raw {crudo}, {'clamped' if crudo != t_warn else 'not clamped'})")
        reg["accion"] = "dry-run"
        return reg

    accion = escribir_macro(z, HOST_INFOBUS, MACRO_OUT, t_warn)
    guardar_estado(st_nuevo)
    reg["accion"] = accion

    enviar_trapper(z, HOST_INFOBUS, "macdt.status", 1)
    enviar_trapper(z, HOST_INFOBUS, "macdt.t_warn", t_warn)
    enviar_trapper(z, HOST_INFOBUS, "macdt.t_base", round(t_base, 1))
    registrar(reg)

    log(f"N={n_t} T_base={t_base:.1f} rho={rho:.2f} v_gps={v_gps} F={factor} -> T_warn={t_warn} ms ({accion})")
    return reg


# ---------------------------------------------------------------------------
# Validar la configuración y ejecutar uno o varios ciclos
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, add_help=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-h", "--help", action="help",
                    help="mostrar este mensaje de ayuda y salir")
    ap.add_argument("--once", action="store_true", help="ejecutar un ciclo")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="calcular y mostrar sin escribir la macro ni el historial")
    ap.add_argument("--calibrate", action="store_true",
                    help="calibrar b, m, delta y beta con los datos de la sonda y salir")
    ap.add_argument("--force", action="store_true",
                    help="con --calibrate: aplicar aunque la recta cambie más que MACDT_CALIB_MAX_DRIFT")
    a = ap.parse_args()

    if not API_USER or not API_PASS:
        sys.exit("Se requieren ZBX_API_USER y ZBX_API_PASS")
    if CALIB_MODE not in ("daily", "manual"):
        sys.exit("MACDT_CALIB_MODE debe ser 'daily' o 'manual'")

    z = Zabbix(API_URL, API_USER, API_PASS)

    if a.calibrate:
        calibrar(z, dry_run=a.dry_run, force=a.force)
        return

    while True:
        # -------------------------------------------------------------------
        # Calibración diaria: una vez al día, a partir de MACDT_CALIB_HOUR
        # -------------------------------------------------------------------
        if not a.dry_run and toca_calibrar():
            try:
                calibrar(z)
            except Exception as e:
                log(f"ERROR en la calibración: {e}")
            guardar_calib({"ultimo_dia": time.strftime("%Y-%m-%d")})
        try:
            ciclo(z, dry_run=a.dry_run)
        except Exception as e:
            # -------------------------------------------------------------------
            # Registrar el fallo, conservar el umbral actual y continuar el bucle
            # -------------------------------------------------------------------
            log(f"ERROR en el ciclo: {e}")
            try:
                registrar({"ts": int(time.time()), "status": "error", "motivo": str(e)[:300]})
                if not a.dry_run:
                    enviar_trapper(z, HOST_INFOBUS, "macdt.status", 0)
            except Exception:
                pass
        if a.once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()