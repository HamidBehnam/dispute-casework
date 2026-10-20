-- As the Entra administrator on dcw, after the loading process has exited: a
-- session bound to corpus_loader keeps the role until it closes.
REVOKE corpus_loader FROM :"upn";
