-- As the Entra administrator, before the migrations:
--   psql "host=<server> dbname=postgres user=<administrator>" -v upn="$TEST_USER_UPN" -f part-a.sql
\connect postgres
SELECT pgaadauth_create_principal(:'upn', false, false);
\connect dcw
CREATE EXTENSION IF NOT EXISTS vector;
