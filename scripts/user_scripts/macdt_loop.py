#!/usr/bin/env python3
"""
MACDT loop (F5): adjust the IT latency threshold according to transport load.

This is the executor in the MAPE-K loop. Each cycle:

  Monitor   reads rho, V_gps, and the operating-window gate from databus items
  Analyze   decides whether to apply the adjustment for degraded GPS coverage
  Plan      calculates T_warn = clamp(T_base(t) * (1 + beta*rho) * F, T_floor, T_RNF)
            with T_base(t) = b + m*N(t) + delta
  Execute   writes {$T_LATENCY_WARN} as a HOST macro on the infobus host
  Knowledge records the cycle in history.jsonl for OE5 reproduction

Why T_base is adjustable (early lab result, 2026-11-10):
  next-trips latency grows linearly with the scheduled stop times in the 5 h
  window of the queried stop (R2 = 0.9986). T_base is therefore no longer a
  constant but the schedule line:
      T_base(t) = b + m*N(t) + delta
  N(t) = infobus.api.llegadas_p95_1h: arrivals over the same window and with the
  same percentile as the latency the threshold is compared with.
  m = weight of each arrival, b = fixed cost, delta = margin (percentile
  MACDT_CALIB_DELTA_PCT of the residuals, 95 by default). beta still carries
  the weight of active buses (rho).
  Until a calibration runs, m = 0, delta = 0 and b = {$MACDT.T_BASE}: the loop
  behaves exactly as before.

Calibration (--calibrate, or daily with MACDT_CALIB_MODE=daily):
  regression latency = b + m*N + c*V on the probe's own data
  (infobus.api.lat_p95_1h, infobus.api.llegadas_p95_1h) and databus
  gtfs.v_trips over MACDT_CALIB_WINDOW_D days.
  Why p95 of 1 h and not the p50 of each cycle: the alert compares
  {$T_LATENCY_WARN} against infobus.api.lat_p95_1h, and the loop feeds N with
  infobus.api.llegadas_p95_1h. A line fitted on the p50 sits below the normal
  p95 when there are many arrivals, and the threshold ends up on top of normal
  behavior. Fitted on the p95, the line passes through the normal p95 of each
  load (b is the p95 with no arrivals) and delta is the margin above it.
  Both p95 items are calculated every minute and do not share the same second,
  so they are joined by minute.
  Consecutive p95_1h values share 59 minutes of data: R2 comes out very high.
  It is valid for drawing the line, not as a measure of precision.
  c is the cost of each active bus and is converted
  to beta = c * V_max / mean(T_base) while buses are running.
  The new line is applied only if:
    - there are at least MACDT_CALIB_MIN_POINTS points and R2 >= MACDT_CALIB_MIN_R2
    - m > 0
    - the line does not change more than MACDT_CALIB_MAX_DRIFT from the current
      one (compared at the low and high N of the window). If it changes more, it
      is not applied automatically: macdt.calib = 2 and a person decides with
      --calibrate --force. This keeps the loop from learning a degradation as
      the new normal.
  With no buses in the window, c cannot be measured and beta is kept.
  Calibrated values are written as HOST macros on infobus.

Why the macro is written at the HOST level instead of the template:
  zabbix-init reimports templates with updateExisting=true, and macros are part
  of the YAML. Writing the template macro would reset the threshold to its seed
  and erase the loop state on every redeployment. The host macro overrides the
  inherited value, and init does not modify host macros.

Why this script reads p.calculated instead of calculating rho:
  this calculated item already applies {$MACDT.RHO_CAP}. Recalculating it here
  would duplicate the definition and risk inconsistent results.

Why the GPS factor applies only while the operating-window gate is open:
  outside the operating window or when V_gps is stale (older than MACDT_MAX_AGE
  seconds), F = 1.0 and hysteresis is reset. With no buses, the probe returns
  v_gps_pct = null and Zabbix discards it, leaving the item's last value for the
  day. Using that value would tighten the threshold overnight when no bus is in
  service.

Why the critical threshold is not changed:
  {$T_LATENCY_CRIT} is the service-level requirement. A commitment that relaxes
  under load is not a commitment. The loop changes only the warning threshold.

Why there is a freshness check:
  writing a threshold from stale data is worse than not writing one. If the
  latest rho sample is older than MACDT_MAX_AGE seconds, the cycle is marked
  degraded, the macro remains unchanged, and the status is sent through trapper.

Why GPS tightening uses persistence and hysteresis:
  without persistence, one bad sample would tighten the threshold and the next
  cycle would release it, causing oscillation. Tightening starts after 3 samples
  below V_GPS.CRIT and ends only after exceeding V_GPS.WARN. Without GPS data,
  F = 1.0: missing data does not tighten the threshold.

Usage:
  macdt_loop-en.py                    # continuous loop
  macdt_loop-en.py --once             # run one cycle and exit
  macdt_loop-en.py --once --dry-run   # calculate and display without writing
  macdt_loop-en.py --calibrate --dry-run   # fit the line and display it, no writes
  macdt_loop-en.py --calibrate             # calibrate and apply if it passes the guards
  macdt_loop-en.py --calibrate --force     # apply even if it changes more than MAX_DRIFT

Environment variables (all have reasonable defaults except access credentials):
  ZBX_API_URL, ZBX_API_USER, ZBX_API_PASS
  MACDT_HOST_DATABUS, MACDT_HOST_INFOBUS
  MACDT_TPL_DATABUS, MACDT_TPL_INFOBUS
  MACDT_INTERVAL, MACDT_MAX_AGE, MACDT_STATE_DIR
  MACDT_CALIB_MODE (daily | manual), MACDT_CALIB_HOUR (HH:MM),
  MACDT_CALIB_WINDOW_D, MACDT_CALIB_MIN_R2, MACDT_CALIB_MIN_POINTS,
  MACDT_CALIB_MAX_DRIFT, MACDT_CALIB_DELTA_PCT (50-100, default 95)
  ZBX_SERVER, ZBX_SERVER_PORT         (for the macdt.status trapper item)

Dependency: requests
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
# T_base(t) and beta calibration parameters
# ---------------------------------------------------------------------------
CALIB_MODE = os.environ.get("MACDT_CALIB_MODE", "manual").strip().lower()
CALIB_HOUR = os.environ.get("MACDT_CALIB_HOUR", "03:30").strip()
CALIB_WINDOW_D = int(os.environ.get("MACDT_CALIB_WINDOW_D", "7"))
CALIB_MIN_R2 = float(os.environ.get("MACDT_CALIB_MIN_R2", "0.8"))
CALIB_MIN_POINTS = int(os.environ.get("MACDT_CALIB_MIN_POINTS", "300"))
CALIB_MAX_DRIFT = float(os.environ.get("MACDT_CALIB_MAX_DRIFT", "0.25"))
# Percentile of the residuals used as delta (margin above the normal p95).
# 95: ~5 % of normal minutes above T_base; 99: ~1 %.
CALIB_DELTA_PCT = float(os.environ.get("MACDT_CALIB_DELTA_PCT", "95"))

# Items used by the calibration: same window and percentile as the alert
ITEM_CALIB_LAT = "infobus.api.lat_p95_1h"
ITEM_CALIB_N = "infobus.api.llegadas_p95_1h"

# ---------------------------------------------------------------------------
# Define the items queried on the databus host
# ---------------------------------------------------------------------------
ITEMS_DATABUS = ["p.calculated", "gtfs.v_gps_pct", "macdt.gate",
                 "gtfs.v_trips", "macdt.v_trips_max", "gtfs.fetch_ok"]

# ---------------------------------------------------------------------------
# Define the template macros containing calculation parameters
# ---------------------------------------------------------------------------
MACROS_NEEDED = ["{$MACDT.T_BASE}", "{$MACDT.BETA}", "{$MACDT.F_CRIT}",
                 "{$MACDT.T_FLOOR}", "{$MACDT.T_RNF}",
                 "{$MACDT.V_GPS.CRIT}", "{$MACDT.V_GPS.WARN}",
                 "{$MACDT.M}", "{$MACDT.B}", "{$MACDT.DELTA}"]

# ---------------------------------------------------------------------------
# Infobus item with N(t): arrivals over the same window as latency
# ---------------------------------------------------------------------------
ITEM_N = "infobus.api.llegadas_p95_1h"

MACRO_OUT = "{$T_LATENCY_WARN}"


# ---------------------------------------------------------------------------
# Print a message with the local time and flush the output buffer
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Group the operations needed to communicate with the Zabbix API
# ---------------------------------------------------------------------------
class Zabbix:
    # -----------------------------------------------------------------------
    # Initialize the HTTP session and Zabbix access credentials
    # -----------------------------------------------------------------------
    def __init__(self, url, user, password):
        self.url, self.user, self.password = url, user, password
        self.token = None
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json-rpc"})

    # -----------------------------------------------------------------------
    # Get an access token and store it in the session
    # -----------------------------------------------------------------------
    def login(self):
        r = self._raw("user.login", {"username": self.user, "password": self.password}, auth=False)
        self.token = r
        self.s.headers.update({"Authorization": f"Bearer {self.token}"})

    # -----------------------------------------------------------------------
    # Send a JSON-RPC request and return its result
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
    # Make an API call and reauthenticate if the token has expired
    # -----------------------------------------------------------------------
    def call(self, method, params):
        """Retry once after reauthentication because the token may have expired."""
        try:
            return self._raw(method, params)
        except RuntimeError as e:
            if "Not authorised" in str(e) or "re-login" in str(e).lower():
                self.token = None
                self.login()
                return self._raw(method, params)
            raise


# ---------------------------------------------------------------------------
# Read the latest value and update time for each requested item
# ---------------------------------------------------------------------------
def leer_items(z, host, keys):
    """Return each key's latest value and clock; item.get provides lastvalue directly."""
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
        raise RuntimeError(f"missing items on '{host}': {faltan}")
    return out


# ---------------------------------------------------------------------------
# Read the macros required to calculate the threshold from the templates
# ---------------------------------------------------------------------------
def leer_macros(z, plantillas, necesarias, host=None):
    """Read constants from template macros. Infobus values override databus values.

    If a host is given, its HOST macros override the template ones: that is where
    the calibration writes, so a template reimport does not erase it."""
    vals = {}
    for nombre in plantillas:
        tpl = z.call("template.get", {"filter": {"host": [nombre]}, "output": ["templateid"]})
        if not tpl:
            raise RuntimeError(f"template '{nombre}' does not exist")
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
        raise RuntimeError(f"missing macros in {plantillas}: {faltan}")
    return vals


# ---------------------------------------------------------------------------
# Read numeric HOST macros from a host
# ---------------------------------------------------------------------------
def leer_macros_host(z, host):
    h = z.call("host.get", {"filter": {"host": [host]}, "output": ["hostid"]})
    if not h:
        raise RuntimeError(f"host '{host}' does not exist")
    out = {}
    for m in z.call("usermacro.get", {"hostids": h[0]["hostid"], "output": ["macro", "value"]}):
        try:
            out[m["macro"]] = float(m["value"])
        except (TypeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# Load the persistent GPS tightening state from disk
# ---------------------------------------------------------------------------
def cargar_estado():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"hardened": False, "below": 0}


# ---------------------------------------------------------------------------
# Atomically save the cycle's persistent state
# ---------------------------------------------------------------------------
def guardar_estado(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Apply the GPS factor with persistence and hysteresis
# ---------------------------------------------------------------------------
def factor_gps(v_gps, gps_crit, gps_warn, f_crit, st, persistencia=3):
    """Return (F, new_state). Do not tighten when GPS data is unavailable."""
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
# Calculate the warning threshold and clamp it to the allowed range
# ---------------------------------------------------------------------------
def calcular_umbral(t_base, beta, rho, factor, t_floor, t_rnf):
    crudo = t_base * (1.0 + beta * rho) * factor
    return round(max(t_floor, min(crudo, t_rnf)), 1), round(crudo, 1)


# ---------------------------------------------------------------------------
# Create or update a host macro with the calculated threshold
# ---------------------------------------------------------------------------
def escribir_macro(z, host, macro, valor):
    """Create or update the HOST macro. Return 'create' | 'update' | 'noop'."""
    h = z.call("host.get", {"filter": {"host": [host]}, "output": ["hostid"]})
    if not h:
        raise RuntimeError(f"host '{host}' does not exist")
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
# Publish a value to a trapper item through the Zabbix API
# ---------------------------------------------------------------------------
def enviar_trapper(z, host, clave, valor):
    """Send the value to the trapper item through the API (history.push, Zabbix 7.0+).

    Do not use port 10051: auto-registered hosts accept only PSK, and the server
    rejects an unencrypted trapper connection without returning an error here.
    """
    try:
        r = z.call("history.push", [{"host": host, "key": clave, "value": str(valor)}])
        errores = [d["error"] for d in r.get("data", []) if d.get("error")]
        if errores:
            log(f"  warning: Zabbix rejected {clave}: {errores[0]}")
            return False
        return True
    except Exception as e:
        log(f"  warning: could not send {clave}: {e}")
        return False


# ---------------------------------------------------------------------------
# Append a JSON record to the process's persistent history
# ---------------------------------------------------------------------------
def registrar(reg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(reg) + "\n")


# ---------------------------------------------------------------------------
# Calculate T_base(t) from the calibrated line
# ---------------------------------------------------------------------------
def t_base_t(b, m, delta, n):
    return b + m * n + delta


# ---------------------------------------------------------------------------
# Solve a small linear system using Gaussian elimination
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
# Fit least squares with an intercept: y = c0 + c1*x1 + c2*x2 ...
# ---------------------------------------------------------------------------
def ajustar(filas_x, y):
    """filas_x: list of regressor rows (without the column of 1s). Returns (coef, r2, residuals)."""
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
# Get an item's history over a time window
# ---------------------------------------------------------------------------
def historial(z, host, clave, desde, hasta):
    it = z.call("item.get", {"host": host, "filter": {"key_": clave},
                             "output": ["itemid", "value_type"]})
    if not it:
        raise RuntimeError(f"item '{clave}' does not exist on '{host}'")
    res = z.call("history.get", {"itemids": it[0]["itemid"], "history": int(it[0]["value_type"]),
                                 "time_from": desde, "time_till": hasta,
                                 "sortfield": "clock", "sortorder": "ASC",
                                 "output": ["clock", "value"]})
    return [(int(r["clock"]), float(r["value"])) for r in res]


# ---------------------------------------------------------------------------
# Load and save the daily calibration state
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
# Calibrate b, m, delta, and beta using probe data
# ---------------------------------------------------------------------------
def calibrar(z, dry_run=False, force=False):
    """Regression latency = b + m*N + c*V. Returns the calibration record.

    estado: 1 applied | 0 rejected (not enough data or poor fit) |
            2 held (changes exceed MAX_DRIFT; apply with --force)
    """
    hasta = int(time.time())
    desde = hasta - CALIB_WINDOW_D * 86400
    reg = {"ts": hasta, "tipo": "calibracion", "ventana_d": CALIB_WINDOW_D}

    # -----------------------------------------------------------------------
    # Join latency and arrivals by minute (two calculated items, each with
    # its own second) and add the databus V_trips value in force at that time
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
    # Reject if there is not enough data
    # -----------------------------------------------------------------------
    if len(filas) < CALIB_MIN_POINTS:
        reg.update({"estado": 0, "motivo": f"{len(filas)} points, minimum {CALIB_MIN_POINTS}"})
        return _cerrar_calib(z, reg, dry_run)

    # -----------------------------------------------------------------------
    # Measure c when buses are present in the window; otherwise, fit only b and m
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
        reg.update({"estado": 0, "motivo": f"regression failed: {e}"})
        return _cerrar_calib(z, reg, dry_run)

    q = min(max(CALIB_DELTA_PCT, 50.0), 100.0) / 100.0
    delta = max(0.0, sorted(res)[min(len(res) - 1, int(round(q * (len(res) - 1))))])
    reg.update({"b": round(b, 2), "m": round(m, 4), "c": round(c, 2) if c is not None else None,
                "delta": round(delta, 2), "r2": round(r2, 4), "puntos_usados": len(usadas)})

    # -----------------------------------------------------------------------
    # beta = c * V_max / mean T_base while buses are running
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
        reg["beta_nota"] = "not enough buses in the window: beta kept"

    # -----------------------------------------------------------------------
    # Quality checks
    # -----------------------------------------------------------------------
    if r2 < CALIB_MIN_R2 or m <= 0:
        reg.update({"estado": 0, "motivo": f"R2={r2:.3f} (minimum {CALIB_MIN_R2}) or m={m:.3f} <= 0"})
        return _cerrar_calib(z, reg, dry_run)

    # -----------------------------------------------------------------------
    # Drift check: compare the new line with the current one at low and high N
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
        reg.update({"estado": 2, "motivo": f"changes {max(deriva):.0%} (maximum {CALIB_MAX_DRIFT:.0%}); "
                                            "review and apply with --calibrate --force"})
        return _cerrar_calib(z, reg, dry_run)

    # -----------------------------------------------------------------------
    # Apply: HOST macros on infobus
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
# Record the calibration result and notify Zabbix
# ---------------------------------------------------------------------------
def _cerrar_calib(z, reg, dry_run):
    textos = {0: "REJECTED", 1: "APPLIED", 2: "HELD"}
    pref = "[dry-run] " if dry_run else ""
    log(f"{pref}Calibration {textos[reg['estado']]}: "
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
# Determine whether it is time for daily calibration
# ---------------------------------------------------------------------------
def toca_calibrar():
    if CALIB_MODE != "daily":
        return False
    hoy = time.strftime("%Y-%m-%d")
    if cargar_calib().get("ultimo_dia") == hoy:
        return False
    return time.strftime("%H:%M") >= CALIB_HOUR


# ---------------------------------------------------------------------------
# Read the data, calculate the threshold, and apply cycle updates
# ---------------------------------------------------------------------------
def ciclo(z, dry_run=False):
    ahora = int(time.time())
    reg = {"ts": ahora, "status": "ok"}

    items = leer_items(z, HOST_DATABUS, ITEMS_DATABUS)
    m = leer_macros(z, [TPL_DATABUS, TPL_INFOBUS], MACROS_NEEDED, host=HOST_INFOBUS)

    rho_i = items["p.calculated"]
    edad = ahora - rho_i["clock"] if rho_i["clock"] else None

    # -----------------------------------------------------------------------
    # Keep the current threshold if the load value is missing or stale
    # -----------------------------------------------------------------------
    if rho_i["v"] is None or edad is None or edad > MAX_AGE:
        reg.update({"status": "degraded",
                    "motivo": f"rho is missing or {edad} seconds old (limit {MAX_AGE})"})
        log(f"DEGRADED: {reg['motivo']}. Keeping the current threshold.")
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
    # Reset hysteresis when there is no service or the GPS measurement is stale.
    # With no buses in service, the retained GPS value does not represent the
    # passenger experience, so do not apply the tightening factor.
    # -----------------------------------------------------------------------
    if gate != 1 or ahora - v_gps_i["clock"] > MAX_AGE:
        v_gps = None
        st = {"hardened": False, "below": 0}
    factor, st_nuevo = factor_gps(v_gps, m["{$MACDT.V_GPS.CRIT}"],
                                  m["{$MACDT.V_GPS.WARN}"], m["{$MACDT.F_CRIT}"], st)

    # -----------------------------------------------------------------------
    # T_base(t) = b + m*N(t) + delta. Use the T_BASE seed when N is stale.
    # -----------------------------------------------------------------------
    try:
        n_i = leer_items(z, HOST_INFOBUS, [ITEM_N])[ITEM_N]
    except RuntimeError:
        n_i = {"v": None, "clock": 0}   # Item not in the template yet: use T_BASE
    n_edad = ahora - n_i["clock"] if n_i["clock"] else None
    if n_i["v"] is None or n_edad is None or n_edad > MAX_AGE:
        n_t, t_base = None, m["{$MACDT.T_BASE}"]
        log(f"  warning: {ITEM_N} missing or {n_edad} s old; using T_BASE = {t_base}")
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
# Validate the configuration and run one-time or continuous cycles
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one cycle")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="calculate and display without writing the macro or history")
    ap.add_argument("--calibrate", action="store_true",
                    help="calibrate b, m, delta and beta from the probe data and exit")
    ap.add_argument("--force", action="store_true",
                    help="with --calibrate: apply even if the line changes more than MACDT_CALIB_MAX_DRIFT")
    a = ap.parse_args()

    if not API_USER or not API_PASS:
        sys.exit("ZBX_API_USER and ZBX_API_PASS are required")
    if CALIB_MODE not in ("daily", "manual"):
        sys.exit("MACDT_CALIB_MODE must be 'daily' or 'manual'")

    z = Zabbix(API_URL, API_USER, API_PASS)

    if a.calibrate:
        calibrar(z, dry_run=a.dry_run, force=a.force)
        return

    while True:
        # -------------------------------------------------------------------
        # Daily calibration: once a day, starting at MACDT_CALIB_HOUR
        # -------------------------------------------------------------------
        if not a.dry_run and toca_calibrar():
            try:
                calibrar(z)
            except Exception as e:
                log(f"ERROR in calibration: {e}")
            guardar_calib({"ultimo_dia": time.strftime("%Y-%m-%d")})
        try:
            ciclo(z, dry_run=a.dry_run)
        except Exception as e:
            # -------------------------------------------------------------------
            # Record the failure, keep the current threshold, and continue the loop
            # -------------------------------------------------------------------
            log(f"ERROR in cycle: {e}")
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