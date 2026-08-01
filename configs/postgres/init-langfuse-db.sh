#!/bin/bash
# Creates the Langfuse relational/metadata database on the shared PostgreSQL
# instance. (Previously also provisioned a pgvector database for LibreChat's
# RAG API - removed along with LibreChat from the architecture; Langfuse is
# the only consumer of this Postgres instance now.)

set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE ${LANGFUSE_POSTGRES_DB}'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${LANGFUSE_POSTGRES_DB}')\gexec
EOSQL

echo "Provisioned database: ${LANGFUSE_POSTGRES_DB}"
