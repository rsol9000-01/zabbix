#!/bin/sh
# ---------------------------------------------------------------------------
# Copiar los datos de memoria, CPU y presión del servidor a la carpeta que el agente
# Zabbix monta como /hostfs (/srv/zabbix-disco en el servidor).
#
# Por qué: el agente corre en Docker. Dentro de un LXC, lxcfs responde
# /proc/meminfo según el cgroup de quien lo lee (el agente vería su propio
# contenedor), y /proc/loadavg y /proc/stat muestran todo el host Proxmox. Estas
# fuentes dan los valores propios del servidor en servidor físico, VM, VM en la nube o LXC.
#
# Lo corre cada minuto simovilab-host-stats.timer.
# ---------------------------------------------------------------------------
set -eu

DEST="${1:-/srv/zabbix-disco}"
mkdir -p "$DEST"

# write <nombre> <contenido>: escribir en un archivo temporal y luego renombrarlo
write() { printf '%s\n' "$2" > "$DEST/.$1.tmp" && mv "$DEST/.$1.tmp" "$DEST/$1"; }

# Memoria: /proc/meminfo es correcto en VM, servidor físico y LXC (lxcfs).
write meminfo "$(cat /proc/meminfo)"

# CPU: microsegundos de CPU que usó este servidor y su cantidad de núcleos.
# /sys/fs/cgroup/cpu.stat (cgroup v2) es el uso propio del LXC dentro de un LXC y
# el del sistema completo en una VM. /proc/stat dentro de un LXC muestra el host
# Proxmox, así que solo se usa en sistemas sin cgroup v2 (USER_HZ = 100: 1 tick = 10 000 us).
if [ -r /sys/fs/cgroup/cpu.stat ]; then
  USAGE=$(awk '$1 == "usage_usec" {print $2}' /sys/fs/cgroup/cpu.stat)
else
  USAGE=$(awk '$1 == "cpu" {print ($2 + $3 + $4 + $7 + $8 + $9) * 10000; exit}' /proc/stat)
fi
write cpu "usage_usec $USAGE
nproc $(nproc)"

# Presión (PSI): parte del tiempo en que los procesos esperaron CPU o disco. Dentro
# de un LXC solo los archivos del cgroup son del LXC; /proc/pressure es de toda la máquina.
for R in cpu io; do
  if [ -r "/sys/fs/cgroup/$R.pressure" ]; then
    write "$R.pressure" "$(cat "/sys/fs/cgroup/$R.pressure")"
  elif [ -r "/proc/pressure/$R" ]; then
    write "$R.pressure" "$(cat "/proc/pressure/$R")"
  fi
done
