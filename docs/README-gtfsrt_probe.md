# `gtfsrt_probe.py` · databus GTFS-RT probe

![Static Badge](https://img.shields.io/badge/language-Python_3-white?logo=python)
![Static Badge](https://img.shields.io/badge/runs_in-Zabbix_Agent_2-white?logo=zabbix)
![Static Badge](https://img.shields.io/badge/feed-GTFS--Realtime-white)

MACDT Layer D probe. It downloads the two GTFS-RT feeds that databus publishes (vehicle positions and trip updates), computes freshness, load and completeness, and prints **one JSON**. Zabbix stores that JSON in the master item `gtfs.probe[{$MACDT.STALE_S}]` and splits it into dependent items with JSONPath.

## Overview

| Item | Value |
| --- | --- |
| **Location** | `scripts/user_scripts/gtfsrt_probe.py` |
| **Runs on** | `zabbix-agent2` on the databus server (srv-databus), inside the `databus_internal` network |
| **Run by** | Zabbix Agent 2, `UserParameter=gtfs.probe[*]` in [`agent2/zabbix_agent2.conf`](../agent2/zabbix_agent2.conf) |
| **Item key** | `gtfs.probe[{$MACDT.STALE_S}]` (template `simovilab-databus`) |
| **Queries** | `<base>/vehicle_positions.pb` and `<base>/trip_updates.pb` |
| **Dependencies** | `requests`, `gtfs-realtime-bindings` (installed in the agent image) |

```mermaid
flowchart LR
  Z["Zabbix Agent 2<br/>gtfs.probe[60]"] --> P["gtfsrt_probe.py --stale 60"]
  P -- "warm-up (discarded)" --> TU0["trip_updates.pb"]
  P -- "measured" --> VP["vehicle_positions.pb"]
  P -- "measured" --> TU["trip_updates.pb"]
  P -- "one JSON" --> M["Master item"]
  M -- "JSONPath" --> D["Dependent items<br/>V_gps · V_trips · V_age · completeness"]
```

## Usage

### Quick start

```bash
# As the agent runs it (inside the databus network)
./gtfsrt_probe.py --base http://orchestrator:8000/feed/realtime --stale 60

# With a Host header accepted by databus ALLOWED_HOSTS
./gtfsrt_probe.py --base http://orchestrator:8000/feed/realtime --host-header databus.ucr.ac.cr --stale 60

# Offline, from saved .pb files
./gtfsrt_probe.py --vp-file vp.pb --tu-file tu.pb
```

### How to

#### 1. Prerequisites

- The databus orchestrator reachable from the agent at the `--base` address.
- `requests` and `gtfs-realtime-bindings`. The agent image ([`agent2/Dockerfile`](../agent2/Dockerfile)) already installs them.

#### 2. Set the address in the agent

The line is in [`agent2/zabbix_agent2.conf`](../agent2/zabbix_agent2.conf):

```text
UserParameter=gtfs.probe[*],/var/lib/zabbix/user_scripts/gtfsrt_probe.py --base http://orchestrator:8000/feed/realtime --stale $1
```

`$1` takes the value of the `{$MACDT.STALE_S}` macro (default `60`). Restart the agent after changing this file. Changing the script needs no restart: the agent mounts `scripts/user_scripts/` from the repository read-only and runs the file on every check.

#### 3. Test it by hand

Run the item through the agent itself, with the same parameters Zabbix uses:

```bash
docker exec zabbix-agent2 zabbix_agent2 -t 'gtfs.probe[60]'
```

Inside the agent container (`docker exec -it zabbix-agent2 bash`), `zabbix_agent2 -t 'gtfs.probe[60]'` is enough. The output is the item key followed by one JSON. `"fetch_ok": 1` means both feeds were downloaded and parsed.

To print only the filler check and the vehicle ages:

```bash
docker exec zabbix-agent2 zabbix_agent2 -t 'gtfs.probe[60]' | grep -o '"v_vehicles":[^,]*\|"ts_igual_header_pct":[^,]*\|"feed_age_vp_s":[^,]*\|"data_age_max_s":[^,]*'
```

With no vehicles in the feed, the percentages are `null`: the template discards them, so the items keep their last value and record no new reading.

#### 4. What the probe does

1. Opens one HTTP session, so both downloads reuse the same TCP connection.
2. Sends a warm-up request to `trip_updates.pb` and discards it (unless `--no-warmup`).
3. Downloads `vehicle_positions.pb` and `trip_updates.pb`, timing each one.
4. Classifies each vehicle by the age of its position: `fresco` (0 to `--stale` s), `viejo` (older, or in the future) or `sin_ts` (no timestamp).
5. Marks each vehicle whose time is equal to or later than the feed header as possible filler by databus (see [Timestamp filler](#timestamp-filler)).
6. Computes freshness, load, completeness and the vehicle table.
7. Prints one JSON and exits with code `0`. If a download fails, it prints `fetch_ok = 0` with the error.

#### 5. Verify in Zabbix

**Monitoring → Latest data**, host `Databus-host`, items `gtfs.*`. The *Tabla de vehículos del feed (HTML)* item shows the per-vehicle table.

## Reference

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--base` | `https://databus.ucr.ac.cr/feed/realtime` | Feed path. Inside the network: `http://orchestrator:8000/feed/realtime` |
| `--host-header` | *(none)* | `Host` header to send. Must be a value in databus `ALLOWED_HOSTS`; otherwise Django answers `400 DisallowedHost` |
| `--stale` | `60` | Seconds a position counts as fresh. Must equal databus `TELEMETRY_GRACE_S` |
| `--timeout` | `10.0` | Timeout per request (s) |
| `--no-warmup` | off | Do not discard the first request. Latencies then include opening the connection |
| `--vp-file`, `--tu-file` | *(none)* | Read both feeds from files instead of the network. Both are required together |

### Output

| Field | Item | Description |
| --- | --- | --- |
| `fetch_ok` | `gtfs.fetch_ok` | `1` if both feeds were downloaded and parsed, `0` if not. Root of the alert tree |
| `error` | — | Error text (only when `fetch_ok = 0`) |
| `fetch_vp_ms`, `fetch_tu_ms` | `gtfs.fetch_vp_ms`, `gtfs.fetch_tu_ms` | Download time of each feed (ms) |
| `warmup` | — | `1` if the warm-up request succeeded |
| `ts_sonda` | — | Probe time (Unix) |
| `stale_s` | `gtfs.stale_s` | Fresh threshold used in this run |
| `feed_age_vp_s`, `feed_age_tu_s` | `gtfs.feed_age_vp_s`, `gtfs.feed_age_tu_s` | Age of each feed header: whether the publisher runs (V_age) |
| `v_trips` | `gtfs.v_trips` | Trips in the trip-updates feed (V_trips) |
| `v_vehicles` | `gtfs.v_vehicles` | Vehicles in the positions feed: denominator of V_gps |
| `data_age_p50_s`, `data_age_p95_s`, `data_age_max_s` | `gtfs.data_age_p95_s`, `gtfs.data_age_max_s` | Age of the vehicle positions: p50, p95 and maximum (s) |
| `v_gps_pct` | `gtfs.v_gps_pct` | % of vehicles with a fresh position (V_gps). `null` with no vehicles |
| `v_gps_frescos`, `v_gps_viejos`, `v_gps_sin_ts` | `gtfs.v_gps_frescos`, `gtfs.v_gps_viejos`, `gtfs.v_gps_sin_ts` | Vehicles by state |
| `v_trips_sin_paradas` | `gtfs.v_trips_sin_paradas` | Published trips with no stops ahead |
| `ts_igual_header_pct` | `gtfs.ts_igual_header_pct` | % of vehicles whose time is equal to or later than the header time: possible filler by databus |
| `v_completeness_vp_pct` | `gtfs.completeness_vp_pct` | % of positions with trip, latitude, longitude and timestamp |
| `v_completeness_tu_pct` | `gtfs.completeness_tu_pct` | % of trip updates with trip and at least one stop |
| `vehiculos` | — | List per vehicle: id, trip, timestamp, age, state, position, time source and stops ahead |
| `vehiculos_texto` | `gtfs.vehiculos_texto` | One-line summary for the dashboard |
| `vehiculos_html` | `gtfs.vehiculos_html` | HTML table for the *Item history* widget |

### Timestamp filler

If databus did not receive a GPS time for a vehicle, it writes the time at which it builds the feed (`backend/schedule_engine/builders.py`, `build_vehicle_position_entity`). That vehicle then looks fresh with no telemetry of its own. databus does this on purpose, because it must accept vehicles without a timestamp. The probe detects it from outside by comparing each vehicle time with the feed header time.

**Rule: a vehicle counts as possible filler when its time is equal to or later than the header time.**

| Time source | Compared with the header | Why |
| --- | --- | --- |
| Filled by databus | Equal, or 1 s later | databus writes the header time first and each filled vehicle time afterwards, in separate calls to the same clock, in whole seconds. If the second changes in between, the vehicle is 1 s later. Never earlier |
| Real GPS | Earlier, or equal | The vehicle reported before the feed was built |

A real vehicle is equal to the header only when its position arrived in the same second the feed was built. The alert *Sospecha de relleno de timestamps* therefore needs more than `{$MACDT.TSFILL.WARN}` % (50) of the vehicles in that state for 10 minutes in a row.

The previous rule (within 1 s of the header, either side) flagged real vehicles that reported 1 s before the build, and raised false alerts with vehicles that report often. Measured with the simulator, which reports every 2 s (20 readings, 2026-11-17):

| Seconds the vehicle was behind the header | Readings | Previous rule | Current rule |
| --- | --- | --- | --- |
| 2 s | 5 | 0 % | 0 % |
| 1 s | 11 | 100 % (false alert) | 0 % |
| 0 s | 4 | 100 % | 100 % |

The previous rule flagged 75 % of the readings, the current one 20 %. Ten readings in a row at that rate is very unlikely (0.2¹⁰). Real filler gives 100 % in every reading.

Requirement: the databus server and the GPS devices must keep their clocks synchronized by NTP. A device 1 s or more ahead counts as filler.

### Design decisions

| Decision | Reason |
| --- | --- |
| Two ages (`feed_age_*` and `data_age_*`) | The header time is when Celery Beat built the file (every 15 s): it shows the publisher runs, not that vehicles report. The vehicle time shows real telemetry |
| `ts_igual_header_pct` | If a vehicle omits its time, databus fills it with the build time, so a feed with no telemetry can look fresh. This field flags it with a one-sided rule (see [Timestamp filler](#timestamp-filler)) |
| `--stale 60` and not 90 | databus accepts telemetry for up to `TELEMETRY_GRACE_S = 60` s. With 90, monitoring would report better health than databus itself |
| One session and a warm-up | Without them, the first download pays DNS and the TCP handshake and the two latencies are not comparable (measured: 848 ms versus 12 ms) |
| Exit code always `0` | A failure is reported as `fetch_ok = 0`, so Zabbix stores it and can alert on it instead of marking the item unsupported |

## Documentation

- [`README.md`](../README.md): full deployment guide, items and alerts.
- [`agent2/zabbix_agent2.conf`](../agent2/zabbix_agent2.conf): `UserParameter` lines.
- [`templates/simovilab-databus.yml`](../templates/simovilab-databus.yml): master and dependent items.
- [`gtfs_window.py`](../scripts/user_scripts/gtfs_window.py): operating window probe. See [its README](README-gtfs_window.md).
