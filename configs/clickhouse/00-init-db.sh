#!/bin/bash
# Runs once on first ClickHouse startup (mounted into
# /docker-entrypoint-initdb.d/, alphabetically before 01-schema.sql so this
# user exists before that file's GRANT runs). Creates the dedicated,
# least-privilege read-only ClickHouse user that the backend's analytical
# queries (detect/drill-down pipeline, plus the /api/ask chat endpoint) run
# through, without ever exposing the admin credentials to LLM-facing code.

set -e

clickhouse-client -n <<-EOSQL
    -- ch_admin is NOT recreated here. It already exists by this point (the
    -- image's own entrypoint creates it from CLICKHOUSE_USER/CLICKHOUSE_PASSWORD
    -- before any /docker-entrypoint-initdb.d/ script runs), but it lives in
    -- that entrypoint's readonly XML-backed user storage - a subsequent
    -- `CREATE USER IF NOT EXISTS ch_admin` against the SQL-driven access
    -- storage throws ACCESS_STORAGE_READONLY (verified against a real run,
    -- not just `docker compose config`) and, under `set -e`, aborts this
    -- entire script AND every init file after it in filename order -
    -- silently skipping 01-schema.sql. Confirmed the hard way.
    CREATE USER IF NOT EXISTS ${CLICKHOUSE_READONLY_USER:-ro}
        IDENTIFIED WITH sha256_password BY '${CLICKHOUSE_READONLY_PASSWORD:-12345678}';

    -- No GRANTs here on purpose. 01-schema.sql grants SELECT on inmobi_rca.*
    -- to this user as its final statement, once that database and its
    -- tables exist (init scripts in this directory run in filename order,
    -- and statements within one file run sequentially).
    REVOKE ALL ON system.* FROM ${CLICKHOUSE_READONLY_USER:-ro};
EOSQL

echo "ClickHouse users provisioned: ch_admin (created by the image entrypoint, already full-access), ${CLICKHOUSE_READONLY_USER:-ro} (data access granted by 01-schema.sql)."
