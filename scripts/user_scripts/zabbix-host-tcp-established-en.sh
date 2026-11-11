#!/bin/sh
# ---------------------------------------------------------------------------
# Contar las conexiones TCP establecidas del host
# Leer las tablas IPv4 e IPv6 del host, montadas en el agente, y contar las
# conexiones cuyo estado TCP sea 01 (ESTABLISHED), sin requerir privilegios
# adicionales ni nsenter.
# ---------------------------------------------------------------------------
awk 'FNR>1 && $4=="01"' /proc/1/net/tcp /proc/1/net/tcp6 | wc -l