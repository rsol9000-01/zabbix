#!/bin/sh
# ---------------------------------------------------------------------------
# Copy the server's memory, CPU and pressure figures to the folder the Zabbix agent
# mounts as /hostfs (/srv/zabbix-disco on the server).
#
# Why: the agent runs in Docker. Inside an LXC, lxcfs answers /proc/meminfo
# according to the cgroup of the reader (the agent would see its own container),
# and /proc/loadavg and /proc/stat show the whole Proxmox host. These sources give
# the server's own values on bare metal, VM, cloud VM or LXC.
#
# Run every minute by simovilab-host-stats.timer.
# ---------------------------------------------------------------------------
set -eu

DEST="${1:-/srv/zabbix-disco}"
mkdir -p "$DEST"

# write <name> <content>: write to a temporary file, then rename
write() { printf '%s\n' "$2" > "$DEST/.$1.tmp" && mv "$DEST/.$1.tmp" "$DEST/$1"; }

# Memory: /proc/meminfo is correct in VM, bare metal and LXC (lxcfs).
write meminfo "$(cat /proc/meminfo)"

# CPU: microseconds of CPU used by this server and its core count.
# /sys/fs/cgroup/cpu.stat (cgroup v2) is the LXC's own usage inside an LXC and
# the whole system in a VM. /proc/stat inside an LXC shows the Proxmox host, so it
# is only a fallback for systems without cgroup v2 (USER_HZ = 100: 1 tick = 10 000 us).
if [ -r /sys/fs/cgroup/cpu.stat ]; then
  USAGE=$(awk '$1 == "usage_usec" {print $2}' /sys/fs/cgroup/cpu.stat)
else
  USAGE=$(awk '$1 == "cpu" {print ($2 + $3 + $4 + $7 + $8 + $9) * 10000; exit}' /proc/stat)
fi
write cpu "usage_usec $USAGE
nproc $(nproc)"

# Pressure (PSI): share of time tasks waited for CPU or disk. Inside an LXC only
# the cgroup files belong to the LXC; /proc/pressure is the whole machine.
for R in cpu io; do
  if [ -r "/sys/fs/cgroup/$R.pressure" ]; then
    write "$R.pressure" "$(cat "/sys/fs/cgroup/$R.pressure")"
  elif [ -r "/proc/pressure/$R" ]; then
    write "$R.pressure" "$(cat "/proc/pressure/$R")"
  fi
done