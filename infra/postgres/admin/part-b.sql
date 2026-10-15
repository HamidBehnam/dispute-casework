-- As the Entra administrator, after the migrations:
--   psql "host=<server> dbname=dcw user=<administrator>" -v upn="$TEST_USER_UPN" -f part-b.sql
GRANT dcw_service TO :"upn";
ALTER ROLE :"upn" CONNECTION LIMIT 16;
