#!/usr/bin/env python3
"""
Derivacion de parametros operativos de MACDT a partir del GTFS Schedule.

Fija, sin numeros escritos a mano:
  - H_op   : ventana operativa (primera salida / ultima llegada) por tipo de dia
  - D_op   : dias de servicio de la semana
  - V_trips_max : concurrencia maxima de viajes PROGRAMADOS (normalizador de rho)
  - S(t)   : concurrencia programada por minuto (linea base de completitud)
  - huecos : minutos dentro de la ventana con 0 viajes programados

Por que se deriva y no se escribe: si el GTFS cambia (nuevo horario, nueva ruta),
los umbrales siguen al horario sin tocar Zabbix. Correr tras cada import de GTFS.

Uso:
  python3 gtfs_window.py --zip GTFS_bUCR.zip            # offline, desde el zip
  python3 gtfs_window.py --pg "host=... dbname=databus user=zbx_ro password=..."
  python3 gtfs_window.py --zip f.zip --macros           # solo macros de Zabbix

Dependencias: ninguna para --zip; psycopg[binary] para --pg
"""
import os
import argparse, collections, csv, io, json, sys, zipfile

DOW = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]


def _sec(t):
    """GTFS HH:MM:SS -> segundos. Admite >24h (no asi el TimeField de databus)."""
    if not t:
        return None
    p = t.strip().split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])


def desde_zip(path):
    """Devuelve (spans, calendarios) leyendo el GTFS como archivo."""
    with zipfile.ZipFile(path) as z:
        def rd(n):
            with z.open(n) as fh:
                return list(csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")))
        trips = {t["trip_id"]: t.get("service_id", "") for t in rd("trips.txt")}
        cal = rd("calendar.txt")
        spans = collections.defaultdict(lambda: [10**9, -1])
        for r in rd("stop_times.txt"):
            a = _sec(r.get("arrival_time")) or _sec(r.get("departure_time"))
            d = _sec(r.get("departure_time")) or _sec(r.get("arrival_time"))
            if a is None or d is None:
                continue
            s = spans[r["trip_id"]]
            s[0], s[1] = min(s[0], d), max(s[1], a)
    return {k: tuple(v) for k, v in spans.items()}, trips, cal


def desde_pg(dsn):
    """Igual, contra el feed vigente en PostgreSQL (feed_trip / feed_stoptime)."""
    import psycopg
    q_span = """
        SELECT st.trip_id, t.service_id,
               MIN(COALESCE(st.departure_time, st.arrival_time)) AS ini,
               MAX(COALESCE(st.arrival_time, st.departure_time)) AS fin
        FROM feed_stoptime st
        JOIN feed_trip t ON t.trip_id = st.trip_id AND t.feed_id = st.feed_id
        JOIN feed_feed f ON f.feed_id = st.feed_id AND f.is_current
        GROUP BY st.trip_id, t.service_id
    """
    q_cal = """
        SELECT c.service_id, c.monday, c.tuesday, c.wednesday, c.thursday,
               c.friday, c.saturday, c.sunday, c.start_date, c.end_date
        FROM feed_calendar c
        JOIN feed_feed f ON f.feed_id = c.feed_id AND f.is_current
    """
    spans, trips, cal = {}, {}, []
    with psycopg.connect(dsn) as cx, cx.cursor() as cur:
        cur.execute(q_span)
        for tid, sid, ini, fin in cur.fetchall():
            spans[tid] = (ini.hour*3600 + ini.minute*60 + ini.second,
                          fin.hour*3600 + fin.minute*60 + fin.second)
            trips[tid] = sid
        cur.execute(q_cal)
        for row in cur.fetchall():
            cal.append(dict(zip(
                ["service_id"] + DOW + ["start_date", "end_date"],
                [row[0]] + [bool(x) for x in row[1:8]] + [str(row[8]), str(row[9])])))
    return spans, trips, cal


def analizar(spans, trips, cal):
    servicios = {c["service_id"] for c in cal} or {trips.get(t, "") for t in spans}
    out = {"servicios": {}}
    for sid in sorted(servicios):
        sp = {t: v for t, v in spans.items() if trips.get(t, "") == sid} or spans
        if not sp:
            continue
        ini, fin = min(v[0] for v in sp.values()), max(v[1] for v in sp.values())
        conc = collections.Counter()
        for lo, hi in sp.values():
            for m in range(lo // 60, hi // 60 + 1):
                conc[m] += 1
        minutos = list(range(ini // 60, fin // 60 + 1))
        win = [conc.get(m, 0) for m in minutos]
        n = len(win)
        pc = lambda p: sorted(win)[min(n - 1, round(p * (n - 1)))]
        huecos, z = [], [m for m in minutos if conc.get(m, 0) == 0]
        for m in z:
            if huecos and m == huecos[-1][1] + 1:
                huecos[-1][1] = m
            else:
                huecos.append([m, m])
        c = next((x for x in cal if x["service_id"] == sid), None)
        out["servicios"][sid] = {
            "dias": [d for d in DOW
                     if c and str(c.get(d, "0")).strip() in ("1", "True", "true")] if c else None,
            "vigencia": [c["start_date"], c["end_date"]] if c else None,
            "ventana": {"inicio_min": ini // 60, "fin_min": fin // 60,
                        "inicio": "%02d:%02d" % (ini // 3600, ini % 3600 // 60),
                        "fin": "%02d:%02d" % (fin // 3600, fin % 3600 // 60),
                        "duracion_min": n, "duracion_h": round(n / 60, 2)},
            "viajes_dia": len(sp),
            "concurrencia": {"media": round(sum(win) / n, 2), "P50": pc(.50),
                             "P75": pc(.75), "P90": pc(.90), "P99": pc(.99),
                             "max": max(win)},
            "v_trips_max": pc(.99),
            "huecos_cero": [{"inicio": "%02d:%02d" % (a // 60, a % 60),
                             "fin": "%02d:%02d" % (b // 60, b % 60),
                             "minutos": b - a + 1} for a, b in huecos],
            "programado_por_minuto": {str(m): conc.get(m, 0) for m in minutos},
        }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--zip"); g.add_argument("--pg")
    g.add_argument("--url", help="URL del GTFS estatico, p.ej. "
                                 "http://orchestrator:8000/feed/schedule/feed.zip")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--macros", action="store_true", help="solo macros de Zabbix")
    ap.add_argument("--now", action="store_true",
                    help="S(t) y gate de ventana del minuto actual (item Script de Zabbix)")
    ap.add_argument("--servicio", help="service_id a usar para las macros")
    a = ap.parse_args()

    if a.url:
        import tempfile
        import urllib.request
        with urllib.request.urlopen(a.url, timeout=a.timeout) as r:
            if r.status != 200:
                raise SystemExit("HTTP %s al bajar %s" % (r.status, a.url))
            cuerpo = r.read()
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
            fh.write(cuerpo)
            tmp = fh.name
        try:
            spans, trips, cal = desde_zip(tmp)
        finally:
            os.unlink(tmp)
    else:
        spans, trips, cal = desde_zip(a.zip) if a.zip else desde_pg(a.pg)
    r = analizar(spans, trips, cal)

    if not a.macros and not a.now:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return

    sid = a.servicio or max(r["servicios"], key=lambda s: r["servicios"][s]["viajes_dia"])
    s = r["servicios"][sid]

    if a.now:
        import datetime
        ahora = datetime.datetime.now()
        m = ahora.hour * 60 + ahora.minute
        dias_sv = s["dias"] or DOW[:5]
        habil = DOW[ahora.weekday()] in dias_sv
        dentro = habil and s["ventana"]["inicio_min"] <= m <= s["ventana"]["fin_min"]
        st = s["programado_por_minuto"].get(str(m), 0) if dentro else 0
        vig = s["vigencia"]
        print(json.dumps({
            "minuto": "%02d:%02d" % (ahora.hour, ahora.minute),
            "service_id": sid,
            "gate_ventana": 1 if dentro else 0,
            "s_t": st,
            "v_trips_max": s["v_trips_max"],
            "hueco_cero_programado": 1 if (dentro and st == 0) else 0,
            "dias_a_fin_vigencia": (
                (datetime.datetime.strptime(vig[1], "%Y%m%d").date() - ahora.date()).days
                if vig else None),
        }, ensure_ascii=False))
        return
    dias = s["dias"] or DOW[:5]
    print(json.dumps({
        "{$MACDT.SERVICE_ID}": sid,
        "{$MACDT.VTRIPS_MAX}": s["v_trips_max"],
        "{$MACDT.WIN_START}": s["ventana"]["inicio"],
        "{$MACDT.WIN_END}": s["ventana"]["fin"],
        "{$MACDT.WIN_DAYS}": ",".join(str(DOW.index(d) + 1) for d in dias),
        "{$MACDT.HOP_MIN_DIA}": s["ventana"]["duracion_min"],
        "{$MACDT.HOP_HORAS_MES}": round(
            s["ventana"]["duracion_min"] / 60 * len(dias) * 52 / 12, 1),
        "{$MACDT.PERIOD_ZBX}": "%s-%s,%s-%s" % (
            DOW.index(dias[0]) + 1, DOW.index(dias[-1]) + 1,
            s["ventana"]["inicio"], s["ventana"]["fin"]),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
