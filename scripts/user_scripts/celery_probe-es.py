#!/usr/bin/env python3
"""
Sonda de las colas de tareas de Celery para Zabbix Agent 2 (plantilla simovilab-celery).

Responde una pregunta: ¿se están procesando las tareas en segundo plano del proyecto?
Lee el broker que usa Celery e imprime un objeto JSON, que es el ítem maestro
`celery.probe`. Los demás ítems de la plantilla toman un campo de él.

Los dos proyectos usan brokers distintos, así que la sonda entiende ambos:

  databus  ->  RabbitMQ (servicio "message-broker" del compose), leído por la API
               de gestión con un usuario de solo lectura (etiqueta "monitoring",
               sin permiso para configurar, escribir ni leer mensajes).
  infobus  ->  Redis (servicio "state", base lógica 0). Celery lo usa a través de
               kombu: una lista de Redis por cola, el hash "unacked" con las tareas
               tomadas y no terminadas, y un conjunto "_kombu.binding.*" por exchange
               que nombra las colas.

Qué broker usar y cómo llegar a él sale de un archivo pequeño en cada servidor:
/etc/zabbix/simovilab-celery.conf (agent2/simovilab-celery.conf, fuera de git).
La contraseña de Redis no se repite ahí: la sonda la lee de la sesión de Redis
que ya está en simovilab-plugins.conf para la plantilla de base de datos.

No todas las colas son de trabajo:
  - "<worker>.celery.pidbox"  canal de control, uno por worker. No se reporta como
                              cola; sirve para contar los workers vivos (solo
                              RabbitMQ).
  - "celeryev.<uuid>"         eventos para Flower y otros monitores. El nombre
                              cambia en cada reinicio, así que nunca se reporta.
La exclusión es una expresión regular (QUEUE_EXCLUDE) para poder ajustarla.

La sonda nunca falla con un código de salida: ante cualquier error imprime
"ok": 0 y el motivo en "error", así Zabbix tiene un valor y puede saltar la
alerta "broker no alcanzable".

Solo usa la biblioteca estándar de Python: habla con Redis directamente (RESP).
"""

import argparse
import base64
import json
import re
import socket
import sys
import time
import urllib.parse
import urllib.request

DEFAULT_CONF = "/etc/zabbix/simovilab-celery.conf"
DEFAULT_PLUGINS = "/etc/zabbix/zabbix_agent2.d/plugins.d/simovilab-plugins.conf"
DEFAULT_EXCLUDE = r"\.pidbox$|^celeryev\."
PIDBOX = re.compile(r"\.pidbox$")
# kombu guarda las prioridades 3, 6 y 9 en listas extra llamadas <cola>\x06\x16<prioridad>
PRIORITY_SEP = "\x06\x16"
PRIORITY_STEPS = ("3", "6", "9")


def read_kv(path):
    """Lee líneas LLAVE=VALOR. Ignora líneas vacías y las que empiezan con #."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------
# RabbitMQ
# --------------------------------------------------------------------------
def http_json(url, user, password, timeout):
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def probe_rabbitmq(conf, exclude, timeout):
    base = conf.get("RABBITMQ_URL", "http://message-broker:15672").rstrip("/")
    user = conf.get("RABBITMQ_USER", "zabbix_mon")
    password = conf.get("RABBITMQ_PASSWORD", "")
    cols = "name,vhost,messages_ready,messages_unacknowledged,consumers"
    queues_raw = http_json(f"{base}/api/queues?columns={cols}", user, password, timeout)
    overview = http_json(f"{base}/api/overview", user, password, timeout)

    queues, workers = [], 0
    for q in queues_raw:
        name = q.get("name", "")
        if PIDBOX.search(name) and (q.get("consumers") or 0) > 0:
            workers += 1
        if exclude.search(name):
            continue
        queues.append({
            "queue": name,
            "vhost": q.get("vhost"),
            "ready": int(q.get("messages_ready") or 0),
            "unacked": int(q.get("messages_unacknowledged") or 0),
            "consumers": int(q.get("consumers") or 0),
        })
    stats = overview.get("message_stats") or {}
    unroutable = (stats.get("drop_unroutable_details") or {}).get("rate")
    return {
        "queues": queues,
        "workers": workers,
        "unacked_total": sum(q["unacked"] for q in queues),
        "unroutable_rate": unroutable,
        "per_queue_detail": True,
    }


# --------------------------------------------------------------------------
# Redis (kombu transport)
# --------------------------------------------------------------------------
class Resp:
    """Cliente mínimo de Redis: envía un comando y lee una respuesta."""

    def __init__(self, host, port, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = self.sock.makefile("rb")

    def cmd(self, *args):
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode()
            parts.append(f"${len(b)}\r\n".encode() + b + b"\r\n")
        self.sock.sendall(b"".join(parts))
        return self._reply()

    def _reply(self):
        line = self.buf.readline()
        if not line:
            raise ConnectionError("Redis closed the connection")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RuntimeError("Redis: " + rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = self.buf.read(n + 2)[:-2]
            return data
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._reply() for _ in range(n)]
        raise RuntimeError("Unexpected Redis reply")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def redis_session(plugins_path, session):
    """Uri y Password de la sesión de Redis en simovilab-plugins.conf."""
    conf = read_kv(plugins_path)
    uri = conf.get(f"Plugins.Redis.Sessions.{session}.Uri", "tcp://state:6379")
    password = conf.get(f"Plugins.Redis.Sessions.{session}.Password", "")
    parsed = urllib.parse.urlparse(uri)
    return parsed.hostname or "state", parsed.port or 6379, password


def probe_redis(conf, exclude, timeout, plugins_path):
    session = conf.get("REDIS_SESSION", "infobus")
    db = conf.get("REDIS_DB", "0")
    host, port, password = redis_session(plugins_path, session)
    r = Resp(host, port, timeout)
    try:
        if password:
            r.cmd("AUTH", password)
        r.cmd("SELECT", db)
        # Nombres de cola: los miembros de cada conjunto _kombu.binding.<exchange>
        # tienen la forma routing_key \x06\x16 patrón \x06\x16 cola
        names, cursor = set(), "0"
        while True:
            cursor, keys = r.cmd("SCAN", cursor, "MATCH", "_kombu.binding.*", "COUNT", 200)
            cursor = cursor.decode() if isinstance(cursor, bytes) else str(cursor)
            for key in keys or []:
                for member in r.cmd("SMEMBERS", key) or []:
                    queue = member.decode(errors="replace").split(PRIORITY_SEP)[-1]
                    if queue:
                        names.add(queue)
            if cursor == "0":
                break
        queues = []
        for name in sorted(names):
            if exclude.search(name):
                continue
            ready = r.cmd("LLEN", name) or 0
            for p in PRIORITY_STEPS:
                ready += r.cmd("LLEN", name + PRIORITY_SEP + p) or 0
            queues.append({"queue": name, "vhost": None, "ready": int(ready),
                           "unacked": None, "consumers": None})
        unacked = r.cmd("HLEN", conf.get("REDIS_UNACKED_KEY", "unacked")) or 0
    finally:
        r.close()
    return {
        "queues": queues,
        "workers": None,
        "unacked_total": int(unacked),
        "unroutable_rate": None,
        "per_queue_detail": False,
    }


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Sonda de colas de Celery (JSON para Zabbix)")
    ap.add_argument("--conf", default=DEFAULT_CONF, help="archivo con los datos del broker")
    ap.add_argument("--plugins", default=DEFAULT_PLUGINS,
                    help="simovilab-plugins.conf, para la contraseña de Redis")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()

    t0 = time.monotonic()
    out = {"ok": 0, "broker": None, "error": "", "queues": [], "queue_names": [],
           "detail_queues": [], "ready_total": None, "unacked_total": None,
           "workers": None, "unroutable_rate": None, "elapsed_ms": None}
    try:
        conf = read_kv(args.conf)
        broker = conf.get("BROKER", "").lower()
        out["broker"] = broker
        exclude = re.compile(conf.get("QUEUE_EXCLUDE", DEFAULT_EXCLUDE))
        if broker == "rabbitmq":
            res = probe_rabbitmq(conf, exclude, args.timeout)
        elif broker == "redis":
            res = probe_redis(conf, exclude, args.timeout, args.plugins)
        else:
            raise ValueError(f"BROKER must be rabbitmq or redis, got '{broker}'")
        out.update({k: v for k, v in res.items() if k != "per_queue_detail"})
        out["ready_total"] = sum(q["ready"] for q in res["queues"])
        out["queue_names"] = [{"queue": q["queue"]} for q in res["queues"]]
        # Segunda lista de descubrimiento: solo brokers que reportan sin confirmar y consumidores por cola
        out["detail_queues"] = out["queue_names"] if res["per_queue_detail"] else []
        out["ok"] = 1
    except FileNotFoundError as exc:
        out["error"] = f"file not found: {exc.filename}"
    except Exception as exc:  # toda falla se reporta, nunca se lanza
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    out["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
