#!/bin/sh
# Conexiones TCP ESTABLISHED del servidor (IPv4 + IPv6).
# Lee /proc/1/net del host (montado en el agente). Estado 01 = ESTABLISHED.
# No necesita privileged ni nsenter.
awk 'FNR>1 && $4=="01"' /proc/1/net/tcp /proc/1/net/tcp6 | wc -l