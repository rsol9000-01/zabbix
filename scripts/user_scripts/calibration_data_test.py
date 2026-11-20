#!/usr/bin/env python3
"""
Revisa si los datos ya tienen suficiente variacion de buses para que la
calibracion de MACDT pueda medir el peso de cada bus (c -> beta).

Solo lee de Zabbix. No escribe macros, historial ni trappers.
Usa exactamente los mismos datos que macdt_loop-en.py --calibrate (version p95):
  latencia  = infobus.api.lat_p95_1h        (host infobus)
  llegadas  = infobus.api.llegadas_p95_1h   (host infobus)   -> N
  buses     = gtfs.v_trips                  (host databus)   -> V
  Latencia y llegadas se unen por minuto, igual que la calibracion.

Se corre dentro del contenedor macdt-loop (ya tiene credenciales y variables), desde la carpeta del proyecto:
  docker cp scripts/user_scripts/calibration_data_test.py macdt-loop:/tmp/
  docker exec macdt-loop python3 /tmp/calibration_data_test.py          # ventana de MACDT_CALIB_WINDOW_D dias
  docker exec macdt-loop python3 /tmp/calibration_data_test.py --dias 3
"""
import argparse
import sys
import time

sys.path.insert(0, "/")
import macdt_loop as m  # noqa: E402

# Correlacion maxima aceptada entre llegadas (N) y buses (V).
# Propuesta, no viene de los documentos del proyecto: ajustable con --r-max.
R_MAX = 0.7
# Puntos minimos por cantidad de buses para contar ese nivel como medido.
MIN_POR_NIVEL = 30


def pearson(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / (sxx * syy) ** 0.5 if sxx and syy else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=m.CALIB_WINDOW_D)
    ap.add_argument("--r-max", type=float, default=R_MAX)
    a = ap.parse_args()

    z = m.Zabbix(m.API_URL, m.API_USER, m.API_PASS)
    hasta = int(time.time())
    desde = hasta - a.dias * 86400

    # Mismo cruce de datos que calibrar() en macdt_loop-en.py
    lat = {c // 60: (c, v) for c, v in m.historial(z, m.HOST_INFOBUS, "infobus.api.lat_p95_1h", desde, hasta)}
    lleg = {c // 60: v for c, v in m.historial(z, m.HOST_INFOBUS, "infobus.api.llegadas_p95_1h", desde, hasta)}
    vtr = m.historial(z, m.HOST_DATABUS, "gtfs.v_trips", desde, hasta)

    filas, j = [], 0
    for minuto in sorted(set(lat) & set(lleg)):
        clock, latencia = lat[minuto]
        while j + 1 < len(vtr) and vtr[j + 1][0] <= clock:
            j += 1
        v = vtr[j][1] if vtr and vtr[j][0] <= clock and clock - vtr[j][0] <= m.MAX_AGE else None
        filas.append((lleg[minuto], v, latencia))

    con_v = [f for f in filas if f[1] is not None]
    print(f"\nVentana: {a.dias} dias  |  puntos totales: {len(filas)}  |  puntos con dato de buses: {len(con_v)}"
          f"  (minimo para calibrar: {m.CALIB_MIN_POINTS})\n")

    # Cuantos puntos hay con cada cantidad de buses
    niveles = {}
    for n_lleg, v, l in con_v:
        niveles.setdefault(int(v), []).append(l)
    print("| Buses activos | Puntos | % | Latencia media (ms) |")
    print("|---|---|---|---|")
    for v in sorted(niveles):
        ls = niveles[v]
        print(f"| {v} | {len(ls)} | {100 * len(ls) / len(con_v):.1f} | {sum(ls) / len(ls):.1f} |")
    medidos = [v for v in niveles if len(niveles[v]) >= MIN_POR_NIVEL]

    # Si N y V se mueven juntos, la regresion no separa su efecto
    r = pearson([f[0] for f in con_v], [f[1] for f in con_v]) if len(con_v) > 2 else None

    print(f"\nNiveles de buses con al menos {MIN_POR_NIVEL} puntos: {sorted(medidos)}")
    print(f"Correlacion llegadas (N) vs buses (V): {r:.2f}" if r is not None else
          "Correlacion llegadas (N) vs buses (V): no se puede calcular")

    falta = []
    if len(con_v) < m.CALIB_MIN_POINTS:
        falta.append(f"faltan puntos con buses ({len(con_v)} de {m.CALIB_MIN_POINTS})")
    if len(medidos) < 3:
        falta.append("hay menos de 3 cantidades de buses distintas con datos suficientes")
    if r is None or abs(r) >= a.r_max:
        falta.append(f"los buses suben y bajan junto con las llegadas (|r| >= {a.r_max})")

    print("\nRESULTADO: " + ("SUFICIENTE: la calibracion puede medir el peso de cada bus"
                              if not falta else "TODAVIA NO: " + "; ".join(falta)))


if __name__ == "__main__":
    main()