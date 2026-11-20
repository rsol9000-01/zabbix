-- simovilab · custom query for the template simovilab-databus
-- Item key: pgsql.custom.query["databus",,,,databus_static]
-- Age of the static GTFS version that databus holds as current.
-- http_last_modified is the Last-Modified header of the source (SIMOVI) at import time,
-- so this age is comparable with macdt.static_zip_age_s (same header read directly from SIMOVI).
-- Needs, besides pg_monitor: GRANT SELECT ON feed_feed TO zabbix_ro;
SELECT feed_id,
       ROUND(EXTRACT(EPOCH FROM now() - http_last_modified)::numeric, 1) AS age_s
FROM feed_feed
WHERE is_current
ORDER BY retrieved_at DESC
LIMIT 1
