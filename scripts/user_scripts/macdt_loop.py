#!/usr/bin/env python3
"""
Bucle MACDT (F5): ajusta el umbral de latencia de TI segun la carga de transporte.

Es el efector del lazo MAPE-K. Cada ciclo:

  Monitor   lee rho, V_gps y la compuerta de ventana de los items de databus
  Analyze   decide si aplicar el endurecimiento por GPS degradado
  Plan      calcula T_warn = clamp(T_base * (1 + beta*rho) * F, T_floor, T_RNF)
  Execute   escribe {$T_LATENCY_WARN} como macro DE HOST en el host de infobus
  Knowledge registra el ciclo en history.jsonl para la reproduccion de OE5

Por que la macro se escribe a nivel de HOST y no de plantilla:
  zabbix-init reimporta las plantillas con updateExisting=true, y las macros son
  parte del YAML. Escribir la de plantilla significaria que cada redespliegue
  devuelve el umbral a la semilla y borra el estado del bucle. La macro de host
  prevalece sobre la heredada y el init no toca macros de host.

Por que lee p.calculated en vez de calcular rho:
  ese item calculado ya aplica {$MACDT.RHO_CAP}. Recalcularlo aqui duplicaria la
  definicion y abriria la puerta a que divergieran.

Por que el umbral critico no se toca:
  {$T_LATENCY_CRIT} es el RNF. Un compromiso que se relaja bajo carga no es un
  compromiso. El bucle solo mueve la advertencia.

Por que hay guarda de frescura:
  escribir un umbral a partir de datos viejos es peor que no escribirlo. Si la
  ultima muestra de rho tiene mas de MACDT_MAX_AGE segundos, el ciclo se declara
  degradado, la macro queda como estaba y se emite el estado por trapper.

Por que el endurecimiento por GPS lleva persistencia e histeresis:
  sin persistencia, una sola muestra mala aprieta el umbral y lo suelta al ciclo
  siguiente, oscilando. Entra tras 3 muestras bajo V_GPS.CRIT y sale solo al
  superar V_GPS.WARN. Sin datos de GPS, F = 1.0: la falta de dato no endurece.

Uso:
  macdt_loop.py                 # bucle continuo
  macdt_loop.py --once          # un ciclo y sale
  macdt_loop.py --once --dry-run  # calcula y muestra, no escribe nada

Variables de entorno (todas con valor por omision razonable salvo las de acceso):
  ZBX_API_URL, ZBX_API_USER, ZBX_API_PASS
  MACDT_HOST_DATABUS, MACDT_HOST_INFOBUS
  MACDT_TPL_DATABUS, MACDT_TPL_INFOBUS
  MACDT_INTERVAL, MACDT_MAX_AGE, MACDT_STATE_DIR
  ZBX_SERVER, ZBX_SERVER_PORT     (para el item trapper macdt.status)

Dependencias: requests
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

# Items que el bucle necesita del host de databus
ITEMS_DATABUS = ["p.calculated", "gtfs.v_gps_pct", "macdt.gate",
                 "gtfs.v_trips", "macdt.v_trips_max", "gtfs.fetch_ok"]

# Constantes que el bucle lee de las macros de plantilla
MACROS_NEEDED = ["{$MACDT.T_BASE}", "{$MACDT.BETA}", "{$MACDT.F_CRIT}",
                 "{$MACDT.T_FLOOR}", "{$MACDT.T_RNF}",
                 "{$MACDT.V_GPS.CRIT}", "{$MACDT.V_GPS.WARN}"]

MACRO_OUT = "{$T_LATENCY_WARN}"


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────
# Cliente mínimo del API de Zabbix
# ─────────────────────────────────────────────────────────────────────
class Zabbix:
    def __init__(self, url, user, password):
        self.url, self.user, self.password = url, user, password
        self.token = None
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json-rpc"})

    def login(self):
        r = self._raw("user.login", {"username": self.user, "password": self.password}, auth=False)
        self.token = r
        self.s.headers.update({"Authorization": f"Bearer {self.token}"})

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


# ─────────────────────────────────────────────────────────────────────
# Lectura
# ─────────────────────────────────────────────────────────────────────
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
        raise RuntimeError(f"items ausentes en '{host}': {faltan}")
    return out


def leer_macros(z, plantillas, necesarias):
    """Lee las constantes de las macros de plantilla. Las de infobus pisan a las de databus."""
    vals = {}
    for nombre in plantillas:
        tpl = z.call("template.get", {"filter": {"host": [nombre]}, "output": ["templateid"]})
        if not tpl:
            raise RuntimeError(f"no existe la plantilla '{nombre}'")
        macros = z.call("usermacro.get", {"hostids": tpl[0]["templateid"],
                                          "output": ["macro", "value"]})
        for m in macros:
            if m["macro"] in necesarias:
                vals[m["macro"]] = float(m["value"])
    faltan = [m for m in necesarias if m not in vals]
    if faltan:
        raise RuntimeError(f"macros ausentes en {plantillas}: {faltan}")
    return vals


# ─────────────────────────────────────────────────────────────────────
# Estado del endurecimiento por GPS
# ─────────────────────────────────────────────────────────────────────
def cargar_estado():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"hardened": False, "below": 0}


def guardar_estado(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


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


# ─────────────────────────────────────────────────────────────────────
# Cálculo
# ─────────────────────────────────────────────────────────────────────
def calcular_umbral(t_base, beta, rho, factor, t_floor, t_rnf):
    crudo = t_base * (1.0 + beta * rho) * factor
    return round(max(t_floor, min(crudo, t_rnf)), 1), round(crudo, 1)


# ─────────────────────────────────────────────────────────────────────
# Escritura
# ─────────────────────────────────────────────────────────────────────
def escribir_macro(z, host, macro, valor):
    """Crea o actualiza la macro DE HOST. Devuelve 'create' | 'update' | 'noop'."""
    h = z.call("host.get", {"filter": {"host": [host]}, "output": ["hostid"]})
    if not h:
        raise RuntimeError(f"no existe el host '{host}'")
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


def enviar_trapper(host, clave, valor):
    """Protocolo Zabbix sender, implementado aqui para no depender de zabbix_sender."""
    payload = json.dumps({
        "request": "sender data",
        "data": [{"host": host, "key": clave, "value": str(valor)}],
    }).encode()
    paquete = b"ZBXD\x01" + struct.pack("<Q", len(payload)) + payload
    try:
        with socket.create_connection((ZBX_SERVER, ZBX_SERVER_PORT), timeout=10) as s:
            s.sendall(paquete)
            s.recv(1024)
        return True
    except Exception as e:
        log(f"  aviso: no se pudo enviar {clave} por trapper: {e}")
        return False


def registrar(reg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(reg) + "\n")


# ─────────────────────────────────────────────────────────────────────
# Un ciclo
# ─────────────────────────────────────────────────────────────────────
def ciclo(z, dry_run=False):
    ahora = int(time.time())
    reg = {"ts": ahora, "status": "ok"}

    items = leer_items(z, HOST_DATABUS, ITEMS_DATABUS)
    m = leer_macros(z, [TPL_DATABUS, TPL_INFOBUS], MACROS_NEEDED)

    rho_i = items["p.calculated"]
    edad = ahora - rho_i["clock"] if rho_i["clock"] else None

    # Guarda de frescura: sin dato reciente no se escribe nada.
    if rho_i["v"] is None or edad is None or edad > MAX_AGE:
        reg.update({"status": "degradado",
                    "motivo": f"rho sin dato o con {edad} s de antiguedad (tope {MAX_AGE})"})
        log(f"DEGRADADO: {reg['motivo']}. Se conserva el umbral vigente.")
        registrar(reg)
        if not dry_run:
            enviar_trapper(HOST_INFOBUS, "macdt.status", 0)
        return reg

    rho = rho_i["v"]
    v_gps = items["gtfs.v_gps_pct"]["v"]
    gate = items["macdt.gate"]["v"]

    st = cargar_estado()
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
            f"(crudo {crudo}, {'recortado' if crudo != t_warn else 'sin recorte'})")
        reg["accion"] = "dry-run"
        return reg

    accion = escribir_macro(z, HOST_INFOBUS, MACRO_OUT, t_warn)
    guardar_estado(st_nuevo)
    reg["accion"] = accion

    enviar_trapper(HOST_INFOBUS, "macdt.status", 1)
    enviar_trapper(HOST_INFOBUS, "macdt.t_warn", t_warn)
    registrar(reg)

    log(f"rho={rho:.2f} v_gps={v_gps} F={factor} -> T_warn={t_warn} ms ({accion})")
    return reg


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="un solo ciclo")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="calcula y muestra, no escribe la macro ni el historial")
    a = ap.parse_args()

    if not API_USER or not API_PASS:
        sys.exit("ZBX_API_USER y ZBX_API_PASS son obligatorias")

    z = Zabbix(API_URL, API_USER, API_PASS)

    while True:
        try:
            ciclo(z, dry_run=a.dry_run)
        except Exception as e:
            # Degradacion: el umbral vigente se conserva, el bucle sigue vivo.
            log(f"ERROR en el ciclo: {e}")
            try:
                registrar({"ts": int(time.time()), "status": "error", "motivo": str(e)[:300]})
                if not a.dry_run:
                    enviar_trapper(HOST_INFOBUS, "macdt.status", 0)
            except Exception:
                pass
        if a.once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()