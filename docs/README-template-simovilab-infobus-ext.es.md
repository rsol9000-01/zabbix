# `simovilab-infobus-ext` · capa I de MACDT (latencia externa de la API)

> [English](README-template-simovilab-infobus-ext.md) · **Español**

![Static Badge](https://img.shields.io/badge/Zabbix-7.4-white?logo=zabbix) ![Static Badge](https://img.shields.io/badge/archivo-templates%2Fsimovilab-infobus-ext.yml-white)

Mide la misma API de infobus desde afuera: el agente de srv-gestion llama al dominio público por Traefik y HTTPS con el mismo `infobus_probe.py`. Al compararla con la sonda interna se ve cuánto agrega el camino público (red, TLS y Traefik) a lo que percibe un usuario. Su dashboard **API-INFOBUS** muestra los dos p95 lado a lado.

## Resumen

| | |
| --- | --- |
| **Archivo** | [`templates/simovilab-infobus-ext.yml`](../templates/simovilab-infobus-ext.yml) |
| **Plantilla** | `simovilab-infobus-ext` |
| **Se vincula a** | `Zabbix-server` (host local de srv-gestion), por `LOCAL_TEMPLATE_*`. |
| **Contenido** | 19 ítems, 1 alerta, 2 macros |

## Requisitos

| Requisito | Para qué | Dónde |
| --- | --- | --- |
| `infobus_probe.py` con `--base https://<dominio público>` (`infobus.api.ext`) | Maestro de todos los ítems `infobus.api.ext.*` | `agent2/zabbix_agent2.conf`, [README](README-infobus_probe.md) |
| `timeout: 30s` en el ítem maestro | Con TLS, 10 peticiones y la revisión horaria del GTFS, un ciclo tarda más que los 3 s por defecto. Cambiar el timeout de un ítem activo requiere reiniciar el agente | Plantilla |
| `simovilab-infobus` en el host de infobus | `Internal: L_api real p95` lee `infobus.api.lat_p95_1h` con `last_foreach` desde cualquier host, se llame como se llame | Plantillas del rol en `.env` |
| API publicada detrás de Traefik | Sin Traefik no hay nada externo que medir | `simovilab/traefik` |

## Árbol de alertas

Cada flecha va de la alerta que suprime a la alerta suprimida: mientras la de arriba está abierta, la de abajo no se abre. En cada par, la de menor severidad depende de la de mayor, así solo se muestra el nivel más grave de un problema.

_Las alertas de esta plantilla no dependen unas de otras._

## Alertas

Los nombres de las alertas son los que muestra Zabbix.

| Alerta | Severidad | Se dispara cuando | La suprime |
| --- | --- | --- | --- |
| External: connection setup cost out of the ordinary | Advertencia | Promedio de 10 min sobre {$INFOBUS.EXT.SETUP.FACTOR} × la mediana diaria y sobre {$INFOBUS.EXT.SETUP.MIN_MS} ms, para la conexión pública (DNS + TCP + TLS). | — |

## Macros

Para que un cambio sobreviva a la siguiente importación, sobrescribir la macro en el host: *Data collection → Hosts → host → Macros → Inherited and host macros → Change*.

| Macro | Valor por defecto | Qué controla |
| --- | --- | --- |
| `{$INFOBUS.EXT.SETUP.FACTOR}` | `2` | Lo mismo que INFOBUS.SETUP.FACTOR, para la conexión pública. |
| `{$INFOBUS.EXT.SETUP.MIN_MS}` | `5` | Lo mismo que INFOBUS.SETUP.MIN_MS, para la conexión pública. |

## Dashboard

- **API-INFOBUS → API-INFOBUS** (13 widgets): *Local time*, *External L_api p95 (last hour)*, *API responds correctly*, *External − internal difference (p95, 1 h)*, *200 · with trips*, *204 · no service*, *429 · rate limit exceeded*, *Other or no response*, *Burst requests*, *Cycle timings*, *INFOBUS API (external)*, *External L_api: cycle p50 and p90*, *Last hour L_api p95: external vs internal*

## Ítems

<details>
<summary>19 ítems</summary>

| Nombre | Clave | Tipo | Unidades |
| --- | --- | --- | --- |
| External InfoBus API probe | `infobus.api.ext` | Agente (activo) | — |
| External: API responds correctly | `infobus.api.ext.ok` | Dependiente | — |
| External: Too Many Requests (429): requests rejected by the rate limit | `infobus.api.ext.throttled` | Dependiente | — |
| External: L_api cycle p50 | `infobus.api.ext.lat_p50` | Dependiente | ms |
| External: L_api cycle p90 | `infobus.api.ext.lat_p90` | Dependiente | ms |
| External: query cost | `infobus.api.ext.query_p50` | Dependiente | ms |
| External: baseline with an open connection (network + TLS + Traefik + Daphne) | `infobus.api.ext.health_ms` | Dependiente | ms |
| External: first request (opens the connection) | `infobus.api.ext.health_cold_ms` | Dependiente | ms |
| External: connection setup cost (TCP + TLS) | `infobus.api.ext.setup_ms` | Dependiente | ms |
| External: TCP connection from srv-gestion to Traefik (network) | `infobus.api.ext.tcp_connect_ms` | Dependiente | ms |
| External: burst requests | `infobus.api.ext.n` | Dependiente | — |
| External: 200 responses in the burst | `infobus.api.ext.n_200` | Dependiente | — |
| External: 204 responses in the burst | `infobus.api.ext.n_204` | Dependiente | — |
| External: other or no responses in the burst | `infobus.api.ext.errors` | Dependiente | — |
| External: INFOBUS API table (HTML) | `infobus.api.ext.tabla_html` | Dependiente | — |
| External: cycle timings (HTML) | `infobus.api.ext.tiempos_html` | Dependiente | — |
| External: L_api real p95, 1 h window | `infobus.api.ext.lat_p95_1h` | Calculado | ms |
| Internal: L_api real p95, 1 h window | `infobus.api.ext.int_p95_1h` | Calculado | ms |
| External - internal: last hour p95 | `infobus.api.ext.delta_p95_1h` | Calculado | ms |

</details>

## Notas

- **p95 externo menor que el interno:** puede pasar mientras la ventana de una hora tiene pocas muestras o porque las dos sondas no corren en el mismo segundo. Comparar solo con una hora completa de datos.
- La sonda comparte la cuota de 30 peticiones por minuto por IP con los usuarios de la misma IP pública. Si aparece **429** en el dashboard, la medición de ese ciclo no es válida.
