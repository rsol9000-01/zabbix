-- simovilab · custom query for the template simovilab-database
-- Item key: pgsql.custom.query["<session>",,,,simovilab_tables]
-- One row per table of the database the session connects to.
-- Needs only the pg_monitor role (zabbix_ro): statistics views and size functions, no table data.
SELECT schemaname || '.' || relname                                   AS tbl,
       n_live_tup::bigint                                             AS live,
       n_dead_tup::bigint                                             AS dead,
       COALESCE(EXTRACT(EPOCH FROM now() - GREATEST(last_autovacuum, last_vacuum)), -1)::bigint AS vacuum_age_s,
       pg_total_relation_size(relid)::bigint                          AS size_b
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC, pg_total_relation_size(relid) DESC