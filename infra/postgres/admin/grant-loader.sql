-- As the Entra administrator on dcw, immediately before a corpus load by the login.
-- INHERIT FALSE: the login holds no corpus privilege until it binds the role.
GRANT corpus_loader TO :"upn" WITH INHERIT FALSE;
