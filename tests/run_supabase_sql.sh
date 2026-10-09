#!/usr/bin/env bash
# Run the Supabase migration + seed + RLS behaviour test on any plain Postgres
# (no Supabase stack needed). Set PGHOST/PGPORT/PGUSER as usual.
set -euo pipefail
cd "$(dirname "$0")/.."
DB=${DB:-frame_sync_test}
dropdb --if-exists "$DB"; createdb "$DB"
psql -d "$DB" -v ON_ERROR_STOP=1 -q -f tests/supabase_shim.sql
psql -d "$DB" -v ON_ERROR_STOP=1 -q -f supabase/migrations/0001_frame_sync.sql
psql -d "$DB" -v ON_ERROR_STOP=1 -q -f supabase/seed.sql 2>&1 | grep -v NOTICE || true
psql -d "$DB" -v ON_ERROR_STOP=1 -f tests/test_supabase_rls.sql 2>&1 | grep -E "ok:|FAILED|ERROR" || true
