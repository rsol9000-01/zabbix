# `simovilab servidor y sistema operativo` · host y contenedores

> [English](README-template-simovilab-templates.md) · **Español**

![Static Badge](https://img.shields.io/badge/Zabbix-7.4-white?logo=zabbix) ![Static Badge](https://img.shields.io/badge/archivo-templates%2Fsimovilab-templates.yml-white)

Capa base de los tres hosts. Mide lo que necesita cualquier servidor, corra lo que corra: alcance por ping, CPU, memoria, presión (PSI), disco, conexiones TCP y todos los contenedores Docker, también los detenidos. Es la raíz del árbol de alertas: mientras **Server DOWN!!!** está abierta, las alertas de las demás plantillas que dependen de ella quedan suprimidas.

## Resumen

| | |
| --- | --- |
| **Archivo** | [`templates/simovilab-templates.yml`](../templates/simovilab-templates.yml) |
| **Plantilla** | `simovilab servidor y sistema operativo` |
| **Se vincula a** | Los tres hosts: `Zabbix-server` (srv-gestion, por `LOCAL_TEMPLATE_*`), `srv-databus` y `srv-infobus` (por `ROLE_DATABUS_TEMPLATE_*` y `ROLE_INFOBUS_TEMPLATE_*`). |
| **Contenido** | 17 ítems, 1 regla de descubrimiento (5 prototipos), 17 alertas, 6 macros |

## Requisitos

| Requisito | Para qué | Dónde |
| --- | --- | --- |
| Timer `simovilab-host-stats` en el host | Escribe cada minuto `meminfo`, `cpu`, `cpu.pressure` e `io.pressure`. Dentro de un LXC o una VM el contenedor del agente no puede leer de `/proc` los valores reales del host | `scripts/host_stats/`, lo instala `zabbix-dev.sh` |
| `/srv/zabbix-disco` montado como `/hostfs` | El agente lee esos archivos y el espacio libre de `/` | `agent2/docker-compose-agent2.yml` |
| Socket de Docker montado en solo lectura | Descubrimiento de contenedores | `agent2/docker-compose-agent2.yml` |
| Ping desde el servidor Zabbix (`icmpping`) | **Server DOWN!!!** se mide desde afuera, no desde el agente | Chequeo simple, no usa el agente |
| `zabbix-host-tcp-established.sh` + sudoers | Cuenta las conexiones ESTABLISHED del host | [README](README-zabbix-host-tcp-established.md) |

## Árbol de alertas

Cada flecha va de la alerta que suprime a la alerta suprimida: mientras la de arriba está abierta, la de abajo no se abre. En cada par, la de menor severidad depende de la de mayor, así solo se muestra el nivel más grave de un problema.

_Las alertas de esta plantilla no dependen unas de otras._

## Alertas

Los nombres de las alertas son los que muestra Zabbix.

| Alerta | Severidad | Se dispara cuando | La suprime |
| --- | --- | --- | --- |
| Server DOWN!!! | Desastre | 3 pings seguidos (unos 30 s) sin respuesta desde srv-gestion. "Capa D: sonda sin datos" depende de esta alerta. | — |
| Too many established TCP connections | Alta | Más de 1200 conexiones TCP establecidas durante 5 min. | — |
| CPU utilization critical | Alta | CPU usada por el servidor (solo sus núcleos) > 90 % durante 5 min. | — |
| CPU pressure is critical | Alta | Procesos esperando CPU más del {$CPU.PSI.CRIT} % del tiempo durante 5 min. | — |
| Disk I/O pressure is critical | Alta | Procesos esperando disco más del {$IO.PSI.CRIT} % del tiempo durante 5 min. | — |
| Disk space critical (/) | Alta | Menos de 10 % libre en /. | — |
| Memory utilization critical | Alta | Memoria en uso (sin la caché que se puede liberar) > 90 % durante 5 min. | — |
| Contenedor {#NAME} apagado | Media | Por contenedor: existe y no está corriendo. | — |
| Too many established TCP connections | Advertencia | Más de 1200 conexiones TCP establecidas durante 5 min. | — |
| CPU utilization high | Advertencia | CPU usada por el servidor (solo sus núcleos) > 70 % durante 5 min. | — |
| CPU pressure is high | Advertencia | Procesos esperando CPU más del {$CPU.PSI.WARN} % del tiempo durante 5 min. | — |
| Disk I/O pressure is high | Advertencia | Procesos esperando disco más del {$IO.PSI.WARN} % del tiempo durante 5 min. | — |
| Server has been restarted recently | Advertencia | El servidor lleva menos de 5 min encendido. | — |
| Disk space low (/) | Advertencia | Menos de 20 % libre en /. | — |
| Memory utilization high | Advertencia | Memoria en uso (sin la caché que se puede liberar) > 75 % durante 5 min. | — |
| Datos de memoria sin actualizar | Advertencia | `/srv/zabbix-disco/meminfo` tiene más de 5 min o no se puede leer (los archivos de CPU y presión salen del mismo timer). | — |
| Contenedor {#NAME} se reinició | Advertencia | Por contenedor: arrancó hace menos de {$CONTENEDOR.REINICIO.VENTANA} s. | — |

## Macros

Para que un cambio sobreviva a la siguiente importación, sobrescribir la macro en el host: *Data collection → Hosts → host → Macros → Inherited and host macros → Change*.

| Macro | Valor por defecto | Qué controla |
| --- | --- | --- |
| `{$CPU.PSI.CRIT}` | `40` | Presión de CPU (% del tiempo esperando CPU, promedio de 1 min) para la alerta crítica. |
| `{$CPU.PSI.WARN}` | `20` | Presión de CPU para la alerta de advertencia. Debajo de 2 % en el laboratorio sin carga anómala. |
| `{$IO.PSI.CRIT}` | `40` | Presión de disco (% del tiempo esperando disco, promedio de 1 min) para la alerta crítica. |
| `{$IO.PSI.WARN}` | `20` | Presión de disco para la alerta de advertencia. Entre 0 y 5 % en el laboratorio sin carga anómala. |
| `{$CONTENEDOR.IGNORAR}` | `-init\|migrat\|-run-` | Regex de contenedores que terminan a propósito (init, migraciones, run) y no se vigilan. |
| `{$CONTENEDOR.REINICIO.VENTANA}` | `600` | Segundos que la alerta "se reinició" queda activa. |

## Dashboard

- **Servidor → Operación** (10 widgets): *CPU en uso*, *Memoria: usada + libre = total*, *Memoria RAM en uso*, *Presión de CPU (1 min)*, *Espacio libre en disco (/)*, *Tiempo encendido*, *Conexiones TCP establecidas*, *Presión de disco (1 min)*, *CPU y memoria*, *Presión de CPU y disco*
- **Servidor → Contenedores** (3 widgets): *Contenedores arriba (verde) o detenidos (rojo)*, *Último arranque o reinicio*, *Cantidad de reinicios*

## Ítems

<details>
<summary>17 ítems y 5 prototipos</summary>

| Nombre | Clave | Tipo | Unidades |
| --- | --- | --- | --- |
| TCP connections ESTABLISHED | `host.tcp.established` | Agente (activo) | — |
| Server UP/DOWN | `icmpping[,3]` | Chequeo simple | — |
| CPU: uso y núcleos del servidor (archivo) | `vfs.file.contents[/hostfs/cpu]` | Agente (activo) | — |
| CPU utilization (user + system) | `simovilab.cpu.util` | Dependiente | % |
| CPU: presión (archivo) | `vfs.file.contents[/hostfs/cpu.pressure]` | Agente (activo) | — |
| CPU pressure (1 min) | `simovilab.cpu.psi` | Dependiente | % |
| Disco: presión (archivo) | `vfs.file.contents[/hostfs/io.pressure]` | Agente (activo) | — |
| Disk I/O pressure (1 min) | `simovilab.io.psi` | Dependiente | % |
| Uptime | `system.uptime` | Agente (activo) | s |
| Disk space free (/) | `vfs.fs.size[/hostfs,pfree]` | Agente (activo) | % |
| Memoria: /proc/meminfo del servidor | `vfs.file.contents[/hostfs/meminfo]` | Agente (activo) | — |
| Memoria total | `simovilab.mem.total` | Dependiente | B |
| Memoria libre (disponible) | `simovilab.mem.available` | Dependiente | B |
| Memoria usada | `simovilab.mem.used` | Dependiente | B |
| Memory utilization (% used) | `simovilab.mem.pused` | Dependiente | % |
| Memoria: usada + libre = total | `simovilab.mem.html` | Dependiente | — |
| Memoria: última actualización del archivo | `vfs.file.time[/hostfs/meminfo,modify]` | Agente (activo) | unixtime |
| *Contenedores (todos, incluidos los detenidos)* | `docker.containers.discovery[true]` | Descubrimiento | — |
| ↳ Contenedor {#NAME}: info (short) | `docker.container_info["{#NAME}",short]` | Prototipo | — |
| ↳ Contenedor {#NAME}: código de salida | `simovilab.contenedor.exitcode["{#NAME}"]` | Prototipo | — |
| ↳ Contenedor {#NAME}: reinicios automáticos | `simovilab.contenedor.restart_count["{#NAME}"]` | Prototipo | — |
| ↳ Contenedor {#NAME}: corriendo | `simovilab.contenedor.running["{#NAME}"]` | Prototipo | — |
| ↳ Contenedor {#NAME}: hora de inicio | `simovilab.contenedor.started["{#NAME}"]` | Prototipo | unixtime |

</details>

## Notas

- **Memoria y CPU salen de archivos, no de `/proc`.** Si el timer se detiene, salta **Datos de memoria sin actualizar** y los valores de CPU, memoria y PSI se congelan. Revisar con `systemctl status simovilab-host-stats.timer` en el host.
- **Contenedores que terminan a propósito** (init, migraciones, `docker compose run`) se excluyen con `{$CONTENEDOR.IGNORAR}`. Ajustarla en cada instalación nueva.
- Junto a esta se vincula la plantilla oficial `Docker by Zabbix agent 2`; esa solo descubre contenedores en ejecución, por eso esta agrega el descubrimiento de los detenidos.
