-- simovilab · custom query for the template simovilab-databus
-- Item key: pgsql.custom.query["databus",,,,databus_static]
-- Static GTFS version that databus holds as current (is_current).
-- http_last_modified is the Last-Modified header of the source (SIMOVI) saved at import time,
-- so lm_epoch equals macdt.static_zip_lm (same header read directly from SIMOVI) when both
-- sides hold the same version.
--   lm_epoch       version date as Unix time (s)
--   age_s          age of that version (s)
--   version_texto  'YYYY-MM-DD HH:MM UTC · N días', for the dashboard
-- Needs, besides pg_monitor: GRANT SELECT ON feed_feed TO zabbix_ro;
SELECT feed_id,
       EXTRACT(EPOCH FROM http_last_modified)::bigint AS lm_epoch,
       ROUND(EXTRACT(EPOCH FROM now() - http_last_modified)::numeric, 1) AS age_s,
       to_char(http_last_modified AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC · '
         || floor(EXTRACT(EPOCH FROM now() - http_last_modified) / 86400)::int || ' días' AS version_texto
FROM feed_feed
WHERE is_current
ORDER BY retrieved_at DESC
LIMIT 1
