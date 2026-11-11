#!/usr/bin/env python3
"""
MACDT loop (F5): adjust the IT latency threshold according to transport load.

This is the executor in the MAPE-K loop. Each cycle:

  Monitor   reads rho, V_gps, and the operating-window gate from databus items
  Analyze   decides whether to apply the adjustment for degraded GPS coverage
  Plan      calculates T_warn = clamp(T_base * (1 + beta*rho) * F, T_floor, T_RNF)
  Execute   writes {$T_LATENCY_WARN} as a HOST macro on the infobus host
  Knowledge records the cycle in history.jsonl for OE5 reproduction

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

Environment variables (all have reasonable defaults except access credentials):
  ZBX_API_URL, ZBX_API_USER, ZBX_API_PASS
  MACDT_HOST_DATABUS, MACDT_HOST_INFOBUS
  MACDT_TPL_DATABUS, MACDT_TPL_INFOBUS
  MACDT_INTERVAL, MACDT_MAX_AGE, MACDT_STATE_DIR
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

# ---------------------------------------------------------------------------
# Definir los ítems que se consultan en el host de databus
# ---------------------------------------------------------------------------
ITEMS_DATABUS = ["p.calculated", "gtfs.v_gps_pct", "macdt.gate",
                 "gtfs.v_trips", "macdt.v_trips_max", "gtfs.fetch_ok"]

# ---------------------------------------------------------------------------
# Definir las macros de plantilla que contienen los parámetros de cálculo
# ---------------------------------------------------------------------------
MACROS_NEEDED = ["{$MACDT.T_BASE}", "{$MACDT.BETA}", "{$MACDT.F_CRIT}",
                 "{$MACDT.T_FLOOR}", "{$MACDT.T_RNF}",
                 "{$MACDT.V_GPS.CRIT}", "{$MACDT.V_GPS.WARN}"]

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
    # Enviar una solicitud JSON-RPC y devolver su resultado
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
    # Ejecutar una llamada a la API y reautenticar si el token expiró
    # -----------------------------------------------------------------------
    def call(self, method, params):
        """Reintenta una vez tras reautenticar: el token puede haber expirado."""
        try:
            return self._raw(method, params)
        except RuntimeError as e:
            if "Not authorised" in str(e) or "re-login" in str(e).lower():
                self.token = None
                self.login()
                return self._raw(method, params)
            raise


# ---------------------------------------------------------------------------
# Leer el último valor y la hora de actualización de cada ítem solicitado
# ---------------------------------------------------------------------------
def leer_items(z, host, keys):
    """Ultimo valor y su reloj por clave. item.get entrega lastvalue directo."""
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
# Leer de las plantillas las macros requeridas para calcular el umbral
# ---------------------------------------------------------------------------
def leer_macros(z, plantillas, necesarias):
    """Lee las constantes de las macros de plantilla. Las de infobus pisan a las de databus."""
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
    faltan = [m for m in necesarias if m not in vals]
    if faltan:
        raise RuntimeError(f"missing macros in {plantillas}: {faltan}")
    return vals


# ---------------------------------------------------------------------------
# Cargar del disco el estado persistente del endurecimiento por GPS
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
    """Devuelve (F, estado_nuevo). Sin dato de GPS no endurece."""
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
    """Crea o actualiza la macro DE HOST. Devuelve 'create' | 'update' | 'noop'."""
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
# Publicar un valor en un ítem trapper mediante la API de Zabbix
# ---------------------------------------------------------------------------
def enviar_trapper(z, host, clave, valor):
    """Envia el valor al item trapper por el API (history.push, Zabbix 7.0+).

    No usa el puerto 10051: los hosts autorregistrados aceptan solo PSK, y una
    conexion sin cifrar al trapper la rechaza el servidor sin dar error aqui.
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
# Añadir un registro JSON al historial persistente del proceso
# ---------------------------------------------------------------------------
def registrar(reg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(reg) + "\n")


# ---------------------------------------------------------------------------
# Leer los datos, calcular el umbral y aplicar las actualizaciones del ciclo
# ---------------------------------------------------------------------------
def ciclo(z, dry_run=False):
    ahora = int(time.time())
    reg = {"ts": ahora, "status": "ok"}

    items = leer_items(z, HOST_DATABUS, ITEMS_DATABUS)
    m = leer_macros(z, [TPL_DATABUS, TPL_INFOBUS], MACROS_NEEDED)

    rho_i = items["p.calculated"]
    edad = ahora - rho_i["clock"] if rho_i["clock"] else None

    # -----------------------------------------------------------------------
    # Conservar el umbral actual si el valor de carga está ausente o vencido
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
    # Reiniciar la histéresis cuando no haya servicio o la medición GPS venza
    # Sin buses en servicio, el valor GPS retenido no representa la experiencia
    # de pasajeros; por eso, no aplicar el factor de endurecimiento.
    # -----------------------------------------------------------------------
    if gate != 1 or ahora - v_gps_i["clock"] > MAX_AGE:
        v_gps = None
        st = {"hardened": False, "below": 0}
    factor, st_nuevo = factor_gps(v_gps, m["{$MACDT.V_GPS.CRIT}"],
                                  m["{$MACDT.V_GPS.WARN}"], m["{$MACDT.F_CRIT}"], st)

    t_warn, crudo = calcular_umbral(m["{$MACDT.T_BASE}"], m["{$MACDT.BETA}"], rho,
                                    factor, m["{$MACDT.T_FLOOR}"], m["{$MACDT.T_RNF}"])

    reg.update({
        "rho": rho, "rho_age_s": edad, "v_gps_pct": v_gps, "gate": gate,
        "v_trips": items["gtfs.v_trips"]["v"],
        "v_trips_max": items["macdt.v_trips_max"]["v"],
        "fetch_ok": items["gtfs.fetch_ok"]["v"],
        "t_base": m["{$MACDT.T_BASE}"], "beta": m["{$MACDT.BETA}"],
        "factor_gps": factor, "hardened": st_nuevo["hardened"],
        "t_warn_crudo": crudo, "t_warn": t_warn,
        "clamped": crudo != t_warn,
    })

    if dry_run:
        log(f"[dry-run] rho={rho:.2f} v_gps={v_gps} F={factor} -> T_warn={t_warn} ms "
            f"(raw {crudo}, {'clamped' if crudo != t_warn else 'not clamped'})")
        reg["accion"] = "dry-run"
        return reg

    accion = escribir_macro(z, HOST_INFOBUS, MACRO_OUT, t_warn)
    guardar_estado(st_nuevo)
    reg["accion"] = accion

    enviar_trapper(z, HOST_INFOBUS, "macdt.status", 1)
    enviar_trapper(z, HOST_INFOBUS, "macdt.t_warn", t_warn)
    registrar(reg)

    log(f"rho={rho:.2f} v_gps={v_gps} F={factor} -> T_warn={t_warn} ms ({accion})")
    return reg


# ---------------------------------------------------------------------------
# Validar la configuración y ejecutar ciclos únicos o continuos
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one cycle")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="calculate and display without writing the macro or history")
    a = ap.parse_args()

    if not API_USER or not API_PASS:
        sys.exit("ZBX_API_USER and ZBX_API_PASS are required")

    z = Zabbix(API_URL, API_USER, API_PASS)

    while True:
        try:
            ciclo(z, dry_run=a.dry_run)
        except Exception as e:
            # -------------------------------------------------------------------
            # Registrar el fallo, mantener el umbral vigente y continuar el bucle
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