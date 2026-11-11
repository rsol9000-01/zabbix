#!/usr/bin/env python3
"""Exporta TODO el historial de un ítem de Zabbix por la API (sin el límite de la web).

Saca dos archivos CSV:
  history_<key>.csv : cada valor guardado (mientras dure la retención "history" del ítem)
  trends_<key>.csv  : promedio/mín/máx por hora (retención "trends", normalmente más larga)

Uso (en el servidor Zabbix, sin dependencias externas):
  python3 zbx_export_item.py --url http://localhost:8080/api_jsonrpc.php \
      --user tfg --host Infobus-host --key infobus.api.lat_p95_1h

  La contraseña se pide por teclado (o variable ZBX_PASS).
  --insecure  si la URL es https con certificado autofirmado.
"""
import argparse
import csv
import getpass
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

DAY = 86400


class Zabbix:
    def __init__(self, url, insecure=False):
        self.url, self.token, self.n = url, None, 0
        self.ctx = ssl._create_unverified_context() if insecure else None

    def call(self, method, params):
        self.n += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": self.n}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json-rpc"})
        if self.token and method != "user.login":
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
            resp = json.load(r)
        if "error" in resp:
            sys.exit(f"Error API en {method}: {resp['error']}")
        return resp["result"]


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).astimezone().isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--host", required=True, help="Nombre técnico del host en Zabbix")
    ap.add_argument("--key", required=True, help="Clave del ítem")
    ap.add_argument("--desde", help="Fecha inicial YYYY-MM-DD (por defecto: 400 días atrás)")
    ap.add_argument("--insecure", action="store_true")
    a = ap.parse_args()

    z = Zabbix(a.url, a.insecure)
    pwd = os.environ.get("ZBX_PASS") or getpass.getpass(f"Contraseña de {a.user}: ")
    z.token = z.call("user.login", {"username": a.user, "password": pwd})

    hosts = z.call("host.get", {"filter": {"host": [a.host]}, "output": ["hostid", "host"]})
    if not hosts:
        sys.exit(f"No existe el host '{a.host}'")
    items = z.call("item.get", {
        "hostids": hosts[0]["hostid"], "filter": {"key_": a.key},
        "output": ["itemid", "name", "key_", "value_type", "history", "trends", "units"],
    })
    if not items:
        sys.exit(f"El host '{a.host}' no tiene el ítem '{a.key}'")
    it = items[0]
    vt = int(it["value_type"])
    print(f"Ítem: {it['name']} ({it['key_']})  itemid={it['itemid']}  "
          f"tipo={vt}  history={it['history']}  trends={it['trends']}  unidades={it['units']}")

    now = int(time.time())
    start = int(datetime.strptime(a.desde, "%Y-%m-%d").timestamp()) if a.desde else now - 400 * DAY
    safe = a.key.replace("/", "_").replace("[", "_").replace("]", "_").replace(",", "_")

    # Historial crudo, por bloques de 1 día para no saturar la API
    hist_file = f"history_{safe}.csv"
    total = 0
    with open(hist_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fecha_local", "epoch", "valor"])
        t = start
        while t < now:
            rows = z.call("history.get", {
                "itemids": it["itemid"], "history": vt, "time_from": t, "time_till": min(t + DAY, now) - 1,
                "sortfield": "clock", "sortorder": "ASC", "output": ["clock", "value"],
            })
            for r in rows:
                w.writerow([iso(r["clock"]), r["clock"], r["value"]])
            total += len(rows)
            t += DAY
    print(f"history: {total} valores -> {hist_file}")

    # Tendencias horarias (solo ítems numéricos)
    if vt in (0, 3):
        trends = z.call("trend.get", {
            "itemids": it["itemid"], "time_from": start, "time_till": now,
            "output": ["clock", "num", "value_min", "value_avg", "value_max"],
        })
        trends.sort(key=lambda r: int(r["clock"]))
        trend_file = f"trends_{safe}.csv"
        with open(trend_file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["hora_local", "epoch", "muestras", "min", "avg", "max"])
            for r in trends:
                w.writerow([iso(r["clock"]), r["clock"], r["num"], r["value_min"], r["value_avg"], r["value_max"]])
        rango = f"{iso(trends[0]['clock'])} a {iso(trends[-1]['clock'])}" if trends else "sin datos"
        print(f"trends: {len(trends)} horas ({rango}) -> {trend_file}")

    z.call("user.logout", [])


if __name__ == "__main__":
    main()