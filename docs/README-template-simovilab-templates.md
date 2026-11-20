# `simovilab servidor y sistema operativo` · host and containers

![Static Badge](https://img.shields.io/badge/Zabbix-7.4-white?logo=zabbix) ![Static Badge](https://img.shields.io/badge/file-templates%2Fsimovilab-templates.yml-white)

Base layer of the three hosts. It measures what any server needs, whatever runs on it: reachability, CPU, memory, pressure (PSI), disk, TCP connections and every Docker container, including the stopped ones. It is the root of the alert tree: when **Server DOWN!!!** is open, the alerts of the other templates that depend on it stay suppressed.

## Overview

| | |
| --- | --- |
| **File** | [`templates/simovilab-templates.yml`](../templates/simovilab-templates.yml) |
| **Template** | `simovilab servidor y sistema operativo` |
| **Linked to** | All three hosts: `Zabbix-server` (srv-gestion, through `LOCAL_TEMPLATE_*`), `srv-databus` and `srv-infobus` (through `ROLE_DATABUS_TEMPLATE_*` and `ROLE_INFOBUS_TEMPLATE_*`). |
| **Contents** | 17 items, 1 discovery rule (6 prototypes), 17 alerts, 6 macros |

## Requirements

| Requirement | Why | Where |
| --- | --- | --- |
| Timer `simovilab-host-stats` on the host | Writes `meminfo`, `cpu`, `cpu.pressure` and `io.pressure` every minute. Inside an LXC or a VM the agent container cannot read the real host values from `/proc` | `scripts/host_stats/`, installed by `zabbix-dev.sh` |
| `/srv/zabbix-disco` mounted as `/hostfs` | The agent reads those files and the disk space of `/` | `agent2/docker-compose-agent2.yml` |
| Docker socket mounted read-only | Container discovery | `agent2/docker-compose-agent2.yml` |
| Ping from the Zabbix server (`icmpping`) | **Server DOWN!!!** is measured from outside, not by the agent | Simple check, no agent needed |
| `zabbix-host-tcp-established.sh` + sudoers | Counts ESTABLISHED connections of the host | [README](README-zabbix-host-tcp-established.md) |

## Alert tree

An arrow goes from the alert that suppresses to the alert that is suppressed: while the upper one is open, the lower one does not open. The lower severity of a pair depends on the higher one, so only the most serious level of a problem is shown.

_No dependencies between the alerts of this template._

## Alerts

| Alert | Severity | Fires when | Suppressed by |
| --- | --- | --- | --- |
| Server DOWN!!! | Disaster | 3 consecutive pings (about 30 s) unanswered from srv-gestion. "Capa D: sonda sin datos" depends on this alert. | — |
| Too many established TCP connections | High | More than 1200 established TCP connections for 5 min. | — |
| CPU utilization critical | High | CPU used by the server (its cores only) > 90 % for 5 min. | — |
| CPU pressure is critical | High | Tasks waiting for CPU more than {$CPU.PSI.CRIT} % of the time for 5 min. | — |
| Disk I/O pressure is critical | High | Tasks waiting for disk more than {$IO.PSI.CRIT} % of the time for 5 min. | — |
| Disk space critical (/) | High | Less than 10 % free on /. | — |
| Memory utilization critical | High | Memory in use (without reclaimable cache) > 90 % for 5 min. | — |
| Contenedor {#NAME} apagado | Average | Per container: it exists and is not running. | — |
| Too many established TCP connections | Warning | More than 1200 established TCP connections for 5 min. | — |
| CPU utilization high | Warning | CPU used by the server (its cores only) > 70 % for 5 min. | — |
| CPU pressure is high | Warning | Tasks waiting for CPU more than {$CPU.PSI.WARN} % of the time for 5 min. | — |
| Disk I/O pressure is high | Warning | Tasks waiting for disk more than {$IO.PSI.WARN} % of the time for 5 min. | — |
| Server has been restarted recently | Warning | The server has been up for less than 5 min. | — |
| Disk space low (/) | Warning | Less than 20 % free on /. | — |
| Memory utilization high | Warning | Memory in use (without reclaimable cache) > 75 % for 5 min. | — |
| Datos de memoria sin actualizar | Warning | `/srv/zabbix-disco/meminfo` older than 5 min or unreadable (the CPU and pressure files come from the same timer). | — |
| Contenedor {#NAME} se reinició | Warning | Per container: it started less than {$CONTENEDOR.REINICIO.VENTANA} s ago. | — |

## Macros

Override a macro on the host to keep the change after the next import: *Data collection → Hosts → host → Macros → Inherited and host macros → Change*.

| Macro | Default | What it controls |
| --- | --- | --- |
| `{$CPU.PSI.CRIT}` | `40` | CPU pressure (% of time waiting for CPU, 1-min average) for the critical alert. |
| `{$CPU.PSI.WARN}` | `20` | CPU pressure for the warning alert. Below 2 % in the lab without anomalous load. |
| `{$IO.PSI.CRIT}` | `40` | Disk pressure (% of time waiting for disk, 1-min average) for the critical alert. |
| `{$IO.PSI.WARN}` | `20` | Disk pressure for the warning alert. Between 0 and 5 % in the lab without anomalous load. |
| `{$CONTENEDOR.IGNORAR}` | `-init\|migrat\|-run-` | Regex of containers that exit on purpose (init, migrations, run) and are not watched. |
| `{$CONTENEDOR.REINICIO.VENTANA}` | `600` | Seconds the "se reinició" alert stays active. |

## Dashboard

- **Servidor → Operación** (10 widgets): *CPU en uso*, *Memoria: usada + libre = total*, *Memoria RAM en uso*, *Presión de CPU (1 min)*, *Espacio libre en disco (/)*, *Tiempo encendido*, *Conexiones TCP establecidas*, *Presión de disco (1 min)*, *CPU y memoria*, *Presión de CPU y disco*
- **Servidor → Contenedores** (4 widgets): *Contenedores arriba (verde) o detenidos (rojo)*, *Último arranque o reinicio*, *Caídas (reinicios automáticos de Docker)*, *Arranques en los últimos 7 días (manuales, caídas y reinicios del servidor)*
  - *Caídas* shows Docker's `RestartCount`: only the times Docker brought the container back by itself after the process crashed. `docker restart`, `docker compose up` or recreating the container do not count, and a recreated container starts at 0. **0 = no crashes.**
  - *Arranques en los últimos 7 días* counts every change of the start time: manual restarts, crashes and server reboots.

## Items

<details>
<summary>17 items and 6 prototypes</summary>

| Name | Key | Type | Units |
| --- | --- | --- | --- |
| TCP connections ESTABLISHED | `host.tcp.established` | Agent (active) | — |
| Server UP/DOWN | `icmpping[,3]` | Simple check | — |
| CPU: uso y núcleos del servidor (archivo) | `vfs.file.contents[/hostfs/cpu]` | Agent (active) | — |
| CPU utilization (user + system) | `simovilab.cpu.util` | Dependent | % |
| CPU: presión (archivo) | `vfs.file.contents[/hostfs/cpu.pressure]` | Agent (active) | — |
| CPU pressure (1 min) | `simovilab.cpu.psi` | Dependent | % |
| Disco: presión (archivo) | `vfs.file.contents[/hostfs/io.pressure]` | Agent (active) | — |
| Disk I/O pressure (1 min) | `simovilab.io.psi` | Dependent | % |
| Uptime | `system.uptime` | Agent (active) | s |
| Disk space free (/) | `vfs.fs.size[/hostfs,pfree]` | Agent (active) | % |
| Memoria: /proc/meminfo del servidor | `vfs.file.contents[/hostfs/meminfo]` | Agent (active) | — |
| Memoria total | `simovilab.mem.total` | Dependent | B |
| Memoria libre (disponible) | `simovilab.mem.available` | Dependent | B |
| Memoria usada | `simovilab.mem.used` | Dependent | B |
| Memory utilization (% used) | `simovilab.mem.pused` | Dependent | % |
| Memoria: usada + libre = total | `simovilab.mem.html` | Dependent | — |
| Memoria: última actualización del archivo | `vfs.file.time[/hostfs/meminfo,modify]` | Agent (active) | unixtime |
| *Contenedores (todos, incluidos los detenidos)* | `docker.containers.discovery[true]` | Discovery | — |
| ↳ Contenedor {#NAME}: info (short) | `docker.container_info["{#NAME}",short]` | Prototype | — |
| ↳ Contenedor {#NAME}: código de salida | `simovilab.contenedor.exitcode["{#NAME}"]` | Prototype | — |
| ↳ Contenedor {#NAME}: reinicios automáticos | `simovilab.contenedor.restart_count["{#NAME}"]` | Prototype | — |
| ↳ Contenedor {#NAME}: corriendo | `simovilab.contenedor.running["{#NAME}"]` | Prototype | — |
| ↳ Contenedor {#NAME}: hora de inicio | `simovilab.contenedor.started["{#NAME}"]` | Prototype | unixtime |
| ↳ Contenedor {#NAME}: arranques en 7 días | `simovilab.contenedor.arranques_7d["{#NAME}"]` | Prototype | — |

</details>

## Notes

- **Memory and CPU come from files, not from `/proc`.** If the timer stops, **Datos de memoria sin actualizar** fires and the CPU, memory and PSI values freeze. Check with `systemctl status simovilab-host-stats.timer` on the host.
- **Containers that end on purpose** (init, migrations, `docker compose run`) are excluded with `{$CONTENEDOR.IGNORAR}`. Adjust it on every new installation.
- The official template `Docker by Zabbix agent 2` is linked next to this one; it only discovers running containers, that is why this one adds the discovery of stopped containers.
