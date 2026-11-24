# `simovilab-infobus-ext` · MACDT layer I (external API latency)

![Static Badge](https://img.shields.io/badge/Zabbix-7.4-white?logo=zabbix) ![Static Badge](https://img.shields.io/badge/file-templates%2Fsimovilab-infobus-ext.yml-white)

Measures the same infobus API from outside: the agent of srv-gestion calls the public domain through Traefik and HTTPS with the same `infobus_probe.py`. Comparing it with the internal probe shows what the public path (network, TLS and Traefik) adds to what a user sees. Its dashboard **API-INFOBUS** shows both p95 values side by side.

## Overview

| | |
| --- | --- |
| **File** | [`templates/simovilab-infobus-ext.yml`](../templates/simovilab-infobus-ext.yml) |
| **Template** | `simovilab-infobus-ext` |
| **Linked to** | `Zabbix-server` (local host of srv-gestion), through `LOCAL_TEMPLATE_*`. |
| **Contents** | 19 items, 1 alert, 2 macros |

## Requirements

| Requirement | Why | Where |
| --- | --- | --- |
| `infobus_probe.py` with `--base https://<public domain>` (`infobus.api.ext`) | Master of all `infobus.api.ext.*` items | `agent2/zabbix_agent2.conf`, [README](README-infobus_probe.md) |
| `timeout: 30s` on the master item | With TLS, 10 requests and the hourly GTFS check a cycle takes more than the default 3 s. Changing a timeout of an active item needs an agent restart | Template |
| `simovilab-infobus` on the infobus host | `Internal: L_api real p95` reads `infobus.api.lat_p95_1h` with `last_foreach` from any host, whatever its name | Role templates in `.env` |
| API published behind Traefik | Without Traefik there is nothing external to measure | `simovilab/traefik` |

## Alert tree

An arrow goes from the alert that suppresses to the alert that is suppressed: while the upper one is open, the lower one does not open. The lower severity of a pair depends on the higher one, so only the most serious level of a problem is shown.

_No dependencies between the alerts of this template._

## Alerts

| Alert | Severity | Fires when | Suppressed by |
| --- | --- | --- | --- |
| External: connection setup cost out of the ordinary | Warning | Same as above, for the public connection (DNS + TCP + TLS). | — |

## Macros

Override a macro on the host to keep the change after the next import: *Data collection → Hosts → host → Macros → Inherited and host macros → Change*.

| Macro | Default | What it controls |
| --- | --- | --- |
| `{$INFOBUS.EXT.SETUP.FACTOR}` | `2` | Same as INFOBUS.SETUP.FACTOR, for the public connection. |
| `{$INFOBUS.EXT.SETUP.MIN_MS}` | `5` | Same as INFOBUS.SETUP.MIN_MS, for the public connection. |

## Dashboard

- **API-INFOBUS → API-INFOBUS** (13 widgets): *Local time*, *External L_api p95 (last hour)*, *API responds correctly*, *External − internal difference (p95, 1 h)*, *200 · with trips*, *204 · no service*, *429 · rate limit exceeded*, *Other or no response*, *Burst requests*, *Cycle timings*, *INFOBUS API (external)*, *External L_api: cycle p50 and p90*, *Last hour L_api p95: external vs internal*

## Items

<details>
<summary>19 items</summary>

| Name | Key | Type | Units |
| --- | --- | --- | --- |
| External InfoBus API probe | `infobus.api.ext` | Agent (active) | — |
| External: API responds correctly | `infobus.api.ext.ok` | Dependent | — |
| External: Too Many Requests (429): requests rejected by the rate limit | `infobus.api.ext.throttled` | Dependent | — |
| External: L_api cycle p50 | `infobus.api.ext.lat_p50` | Dependent | ms |
| External: L_api cycle p90 | `infobus.api.ext.lat_p90` | Dependent | ms |
| External: query cost | `infobus.api.ext.query_p50` | Dependent | ms |
| External: baseline with an open connection (network + TLS + Traefik + Daphne) | `infobus.api.ext.health_ms` | Dependent | ms |
| External: first request (opens the connection) | `infobus.api.ext.health_cold_ms` | Dependent | ms |
| External: connection setup cost (TCP + TLS) | `infobus.api.ext.setup_ms` | Dependent | ms |
| External: TCP connection from srv-gestion to Traefik (network) | `infobus.api.ext.tcp_connect_ms` | Dependent | ms |
| External: burst requests | `infobus.api.ext.n` | Dependent | — |
| External: 200 responses in the burst | `infobus.api.ext.n_200` | Dependent | — |
| External: 204 responses in the burst | `infobus.api.ext.n_204` | Dependent | — |
| External: other or no responses in the burst | `infobus.api.ext.errors` | Dependent | — |
| External: INFOBUS API table (HTML) | `infobus.api.ext.tabla_html` | Dependent | — |
| External: cycle timings (HTML) | `infobus.api.ext.tiempos_html` | Dependent | — |
| External: L_api real p95, 1 h window | `infobus.api.ext.lat_p95_1h` | Calculated | ms |
| Internal: L_api real p95, 1 h window | `infobus.api.ext.int_p95_1h` | Calculated | ms |
| External - internal: last hour p95 | `infobus.api.ext.delta_p95_1h` | Calculated | ms |

</details>

## Notes

- **External p95 lower than internal:** it can happen while the hour window has few samples or when both probes do not run in the same second. Compare only after a full hour of data.
- The probe shares the 30 requests per minute quota per IP with the users of the same public IP. If **429** appears in the dashboard, the measurement of that cycle is not valid.
