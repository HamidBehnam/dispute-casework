"""Row security, grants and constraints on the app tables and the bank ledger."""

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import psycopg
import pytest
from psycopg import Connection

from dispute_casework.database import LOADER, DatabaseSettings, connect

PREPARER = uuid.UUID(int=1)
APPROVER = uuid.UUID(int=2)
VIEWER = uuid.UUID(int=3)
OTHER_PREPARER = uuid.UUID(int=4)
STRANGER = uuid.UUID(int=5)

STATUSES = (
    "intake",
    "triaged",
    "in_review",
    "awaiting_approval",
    "approved",
    "returned",
    "rejected",
    "executed",
    "closed",
)
PREPARER_CASES = {status: uuid.UUID(int=110 + i) for i, status in enumerate(STATUSES)}
APPROVER_CASES = {
    PREPARER_CASES[status]
    for status in (
        "awaiting_approval",
        "approved",
        "returned",
        "rejected",
        "executed",
        "closed",
    )
}
OWN_CASE = PREPARER_CASES["in_review"]
WAITING_CASE = PREPARER_CASES["awaiting_approval"]
OTHER_CASE = uuid.UUID(int=103)
WAITING_ACTION = uuid.UUID(int=201)
SECOND_WAITING_ACTION = uuid.UUID(int=202)
OTHER_ACTION = uuid.UUID(int=203)
IN_REVIEW_ACTION = uuid.UUID(int=204)
SEEDED_KEY = "seeded-approval"
OTHER_KEY = "other-approval"

APP_TABLES = ["app.cases", "app.actions", "app.approvals", "app.audit_events"]
INSERT_COLUMNS = {
    "app.cases": {"case_id", "preparer_oid"},
    "app.actions": {"action_id", "case_id", "preparer_oid", "payload"},
    "app.approvals": {
        "approval_id",
        "action_id",
        "idempotency_key",
        "approver_oid",
        "preparer_oid",
        "decision",
    },
    "app.audit_events": {"case_id", "actor_oid"},
}
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
TABLE_PRIVILEGES = ("DELETE", "TRUNCATE", "TRIGGER", "MAINTAIN")


@pytest.fixture(scope="module", autouse=True)
def seeded(superuser: DatabaseSettings) -> Iterator[None]:
    """Cases, actions, approvals and audit events, written past row security."""
    with connect(superuser) as connection:
        connection.cursor().executemany(
            "INSERT INTO app.cases (case_id, preparer_oid, status) VALUES (%s, %s, %s)",
            [(case, PREPARER, status) for status, case in PREPARER_CASES.items()]
            + [(OTHER_CASE, OTHER_PREPARER, "intake")],
        )
        connection.cursor().executemany(
            "INSERT INTO app.actions (action_id, case_id, preparer_oid, payload)"
            " VALUES (%s, %s, %s, '{}')",
            [
                (WAITING_ACTION, WAITING_CASE, PREPARER),
                (SECOND_WAITING_ACTION, WAITING_CASE, PREPARER),
                (OTHER_ACTION, OTHER_CASE, OTHER_PREPARER),
                (IN_REVIEW_ACTION, OWN_CASE, PREPARER),
            ],
        )
        connection.execute(
            "INSERT INTO app.approvals (action_id, idempotency_key, approver_oid,"
            " preparer_oid, decision) VALUES (%s, %s, %s, %s, 'approve'),"
            " (%s, %s, %s, %s, 'approve')",
            (
                *(SECOND_WAITING_ACTION, SEEDED_KEY, APPROVER, PREPARER),
                *(OTHER_ACTION, OTHER_KEY, APPROVER, OTHER_PREPARER),
            ),
        )
        connection.execute(
            "INSERT INTO app.audit_events (case_id, actor_oid)"
            " VALUES (%s, %s), (%s, %s)",
            (WAITING_CASE, PREPARER, OTHER_CASE, OTHER_PREPARER),
        )
    yield
    with connect(superuser) as connection:
        connection.execute(
            "TRUNCATE app.approvals, app.audit_events, app.actions, app.cases"
        )


@contextmanager
def acting_as(
    database: DatabaseSettings, oid: uuid.UUID, *roles: str
) -> Iterator[Connection[Any]]:
    """An app_rw connection whose transaction carries the principal; rolled back."""
    with connect(database, options="-c role=app_rw") as connection:
        connection.execute(
            "SELECT set_config('app.oid', %s, true), set_config('app.roles', %s, true)",
            (str(oid), " ".join(roles)),
        )
        yield connection
        connection.rollback()


def visible(connection: Connection[Any], table: str, key: str) -> set[uuid.UUID]:
    return {row[0] for row in connection.execute(f"SELECT {key} FROM {table}")}


def settable_roles(connection: Connection[Any]) -> set[str]:
    rows = connection.execute(
        "WITH RECURSIVE settable(role) AS ("
        " SELECT session_user::regrole::oid"
        " UNION SELECT m.roleid FROM pg_auth_members m"
        " JOIN settable s ON m.member = s.role WHERE m.set_option)"
        " SELECT role::regrole::text FROM settable"
    ).fetchall()
    return {role for (role,) in rows}


def relation(connection: Connection[Any], table: str) -> int:
    """The table's oid from the catalog; a name would need USAGE on its schema."""
    schema, name = table.split(".")
    row = connection.execute(
        "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = %s",
        (schema, name),
    ).fetchone()
    assert row is not None
    oid: int = row[0]
    return oid


def privileges(connection: Connection[Any], role: str, table: str) -> set[str]:
    oid = relation(connection, table)
    held = set()
    for privilege in COLUMN_PRIVILEGES:
        row = connection.execute(
            "SELECT has_any_column_privilege(%s, %s::oid, %s)", (role, oid, privilege)
        ).fetchone()
        if row == (True,):
            held.add(privilege)
    for privilege in TABLE_PRIVILEGES:
        row = connection.execute(
            "SELECT has_table_privilege(%s, %s::oid, %s)", (role, oid, privilege)
        ).fetchone()
        if row == (True,):
            held.add(privilege)
    return held


def insertable_columns(connection: Connection[Any], role: str, table: str) -> set[str]:
    rows = connection.execute(
        "SELECT attname FROM pg_attribute"
        " WHERE attrelid = %s::oid AND attnum > 0 AND NOT attisdropped"
        " AND has_column_privilege(%s, attrelid, attnum, 'INSERT')",
        (relation(connection, table), role),
    ).fetchall()
    return {column for (column,) in rows}


def test_app_rw_holds_exactly_the_matrix(database: DatabaseSettings) -> None:
    with connect(database) as connection:
        assert connection.execute(
            "SELECT has_schema_privilege('app_rw', 'app', 'USAGE')"
        ).fetchone() == (True,)
        for table in APP_TABLES:
            assert privileges(connection, "app_rw", table) == {"SELECT", "INSERT"}
            assert connection.execute(
                "SELECT has_table_privilege('app_rw', %s::oid, 'INSERT')",
                (relation(connection, table),),
            ).fetchone() == (False,)
            assert (
                insertable_columns(connection, "app_rw", table) == INSERT_COLUMNS[table]
            )


def test_no_other_settable_role_reaches_the_app_or_bank_tables(
    database: DatabaseSettings,
    loader_grant: Callable[[], AbstractContextManager[None]],
) -> None:
    with loader_grant(), connect(database) as connection:
        roles = settable_roles(connection)
        assert LOADER in roles
        for role in roles:
            assert connection.execute(
                "SELECT has_schema_privilege(%s, 'bank', 'USAGE')", (role,)
            ).fetchone() == (False,)
            assert privileges(connection, role, "bank.ledger_entries") == set()
            if role == "app_rw":
                continue
            assert connection.execute(
                "SELECT has_schema_privilege(%s, 'app', 'USAGE')", (role,)
            ).fetchone() == (False,)
            for table in APP_TABLES:
                assert privileges(connection, role, table) == set()


def test_no_settable_role_can_update_delete_or_truncate_an_app_table(
    database: DatabaseSettings,
    loader_grant: Callable[[], AbstractContextManager[None]],
) -> None:
    with loader_grant(), connect(database) as connection:
        for role in settable_roles(connection):
            for table in APP_TABLES:
                held = privileges(connection, role, table)
                assert held.isdisjoint({"UPDATE", "DELETE", "TRUNCATE"})


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE app.cases SET status = 'approved'",
        "UPDATE app.actions SET payload = '{}'",
        "UPDATE app.approvals SET decision = 'approve'",
        "UPDATE app.audit_events SET actor_oid = gen_random_uuid()",
        "DELETE FROM app.cases",
        "DELETE FROM app.approvals",
        "DELETE FROM app.audit_events",
        "TRUNCATE app.audit_events",
        "TRUNCATE app.approvals",
    ],
)
def test_app_rw_is_refused_updates_deletes_and_truncates(
    database: DatabaseSettings, statement: str
) -> None:
    with (
        acting_as(database, PREPARER, "preparer", "approver") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(statement)


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        (
            "INSERT INTO app.cases (preparer_oid, status) VALUES (%s, 'approved')",
            (PREPARER,),
        ),
        (
            "INSERT INTO app.actions (case_id, preparer_oid, payload, status)"
            " VALUES (%s, %s, '{}', 'approved')",
            (OWN_CASE, PREPARER),
        ),
        (
            "INSERT INTO app.audit_events (case_id, actor_oid, occurred_at)"
            " VALUES (%s, %s, now())",
            (OWN_CASE, PREPARER),
        ),
        (
            "INSERT INTO app.audit_events (event_id, case_id, actor_oid)"
            " VALUES (gen_random_uuid(), %s, %s)",
            (OWN_CASE, PREPARER),
        ),
    ],
)
def test_server_set_columns_cannot_be_written(
    database: DatabaseSettings, statement: str, parameters: tuple[Any, ...]
) -> None:
    with (
        acting_as(database, PREPARER, "preparer") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(statement, parameters)


def test_audit_events_refuse_update_delete_and_truncate_for_every_settable_role(
    database: DatabaseSettings,
    loader_grant: Callable[[], AbstractContextManager[None]],
) -> None:
    with loader_grant(), connect(database) as connection:
        roles = settable_roles(connection)
        for role in roles:
            assert privileges(connection, role, "app.audit_events").isdisjoint(
                {"UPDATE", "DELETE", "TRUNCATE"}
            )
            insertable = insertable_columns(connection, role, "app.audit_events")
            assert insertable.isdisjoint({"event_id", "occurred_at"})
        owner = connection.execute(
            "SELECT tableowner FROM pg_tables"
            " WHERE schemaname = 'app' AND tablename = 'audit_events'"
        ).fetchone()
        assert owner == ("app_owner",)
        assert "app_owner" not in roles


def test_app_foreign_keys_carry_no_referential_action(
    database: DatabaseSettings,
) -> None:
    with connect(database) as connection:
        actions = connection.execute(
            "SELECT conrelid::regclass::text, confdeltype, confupdtype"
            " FROM pg_constraint WHERE contype = 'f'"
            " AND connamespace = 'app'::regnamespace ORDER BY 1"
        ).fetchall()
    assert actions == [
        ("app.actions", "a", "a"),
        ("app.approvals", "a", "a"),
        ("app.audit_events", "a", "a"),
    ]


def test_owners(database: DatabaseSettings) -> None:
    with connect(database) as connection:
        owners = connection.execute(
            "SELECT schemaname || '.' || tablename, tableowner FROM pg_tables"
            " WHERE schemaname IN ('app', 'bank')"
        ).fetchall()
        owned_by_app_rw = connection.execute(
            "SELECT count(*) FROM pg_class WHERE relowner = 'app_rw'::regrole"
        ).fetchone()
    assert dict(owners) == dict.fromkeys(APP_TABLES, "app_owner") | {
        "bank.ledger_entries": "bank_owner"
    }
    assert owned_by_app_rw == (0,)


@pytest.mark.parametrize("tier", ["tier_public", "tier_full", "lg_app"])
def test_the_other_settable_roles_are_refused_on_schema_app(
    database: DatabaseSettings, tier: str
) -> None:
    with (
        connect(database, options=f"-c role={tier}") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="schema app"),
    ):
        connection.execute("SELECT count(*) FROM app.cases")


@pytest.mark.parametrize(
    ("oid", "roles", "cases"),
    [
        (PREPARER, ("preparer",), set(PREPARER_CASES.values())),
        (OTHER_PREPARER, ("preparer",), {OTHER_CASE}),
        (APPROVER, ("approver",), APPROVER_CASES),
        (PREPARER, ("approver",), set()),
        (VIEWER, ("restricted_viewer",), {*PREPARER_CASES.values(), OTHER_CASE}),
        (STRANGER, (), set()),
    ],
)
def test_each_principal_sees_exactly_its_cases(
    database: DatabaseSettings,
    oid: uuid.UUID,
    roles: tuple[str, ...],
    cases: set[uuid.UUID],
) -> None:
    with acting_as(database, oid, *roles) as connection:
        assert visible(connection, "app.cases", "case_id") == cases


@pytest.mark.parametrize(
    ("oid", "roles", "actions", "approved", "audited"),
    [
        (
            PREPARER,
            ("preparer",),
            {WAITING_ACTION, SECOND_WAITING_ACTION, IN_REVIEW_ACTION},
            {SEEDED_KEY},
            {WAITING_CASE},
        ),
        (
            APPROVER,
            ("approver",),
            {WAITING_ACTION, SECOND_WAITING_ACTION},
            {SEEDED_KEY},
            {WAITING_CASE},
        ),
        (
            VIEWER,
            ("restricted_viewer",),
            {WAITING_ACTION, SECOND_WAITING_ACTION, OTHER_ACTION, IN_REVIEW_ACTION},
            {SEEDED_KEY, OTHER_KEY},
            {WAITING_CASE, OTHER_CASE},
        ),
        (STRANGER, (), set(), set(), set()),
    ],
)
def test_child_rows_follow_their_case(
    database: DatabaseSettings,
    oid: uuid.UUID,
    roles: tuple[str, ...],
    actions: set[uuid.UUID],
    approved: set[str],
    audited: set[uuid.UUID],
) -> None:
    """The subqueries of the child policies are filtered by the cases policies."""
    with acting_as(database, oid, *roles) as connection:
        assert visible(connection, "app.actions", "action_id") == actions
        assert visible(connection, "app.approvals", "idempotency_key") == approved
        assert visible(connection, "app.audit_events", "case_id") == audited


def insert(connection: Connection[Any], table: str, oid: uuid.UUID) -> None:
    statements: dict[str, tuple[str, tuple[Any, ...]]] = {
        "app.cases": ("INSERT INTO app.cases (preparer_oid) VALUES (%s)", (oid,)),
        "app.actions": (
            "INSERT INTO app.actions (case_id, preparer_oid, payload)"
            " VALUES (%s, %s, '{}')",
            (WAITING_CASE, PREPARER),
        ),
        "app.approvals": (
            "INSERT INTO app.approvals (action_id, idempotency_key, approver_oid,"
            " preparer_oid, decision) VALUES (%s, %s, %s, %s, 'approve')",
            (WAITING_ACTION, str(uuid.uuid4()), oid, PREPARER),
        ),
        "app.audit_events": (
            "INSERT INTO app.audit_events (case_id, actor_oid) VALUES (%s, %s)",
            (WAITING_CASE, oid),
        ),
    }
    statement, parameters = statements[table]
    connection.execute(statement, parameters)


@pytest.mark.parametrize("table", APP_TABLES)
def test_without_the_setting_no_app_row_is_visible_or_writable(
    database: DatabaseSettings, table: str
) -> None:
    with connect(database, options="-c role=app_rw") as connection:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"):
            insert(connection, table, APPROVER)


def test_a_setting_from_an_earlier_transaction_leaves_nothing_visible(
    database: DatabaseSettings,
) -> None:
    """A placeholder set with is_local reads back as an empty string afterwards."""
    with connect(database, options="-c role=app_rw") as connection:
        connection.execute(
            "SELECT set_config('app.oid', %s, true),"
            " set_config('app.roles', 'restricted_viewer', true)",
            (str(VIEWER),),
        )
        assert visible(connection, "app.cases", "case_id") != set()
        connection.commit()
        setting = connection.execute(
            "SELECT current_setting('app.oid', true)"
        ).fetchone()
        assert setting == ("",)
        assert visible(connection, "app.cases", "case_id") == set()


def test_the_preparer_and_the_approver_write_what_their_roles_allow(
    database: DatabaseSettings,
) -> None:
    with acting_as(database, PREPARER, "preparer") as connection:
        for table in ("app.cases", "app.actions", "app.audit_events"):
            insert(connection, table, PREPARER)
    with acting_as(database, APPROVER, "approver") as connection:
        for table in ("app.approvals", "app.audit_events"):
            insert(connection, table, APPROVER)


@pytest.mark.parametrize("table", APP_TABLES)
def test_the_restricted_viewer_writes_nothing(
    database: DatabaseSettings, table: str
) -> None:
    with (
        acting_as(database, VIEWER, "restricted_viewer") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        insert(connection, table, VIEWER)


def test_a_principal_without_the_approver_role_cannot_approve(
    database: DatabaseSettings,
) -> None:
    """The principal sees the action, so only the missing role refuses the row."""
    with acting_as(database, STRANGER, "preparer", "restricted_viewer") as connection:
        assert WAITING_ACTION in visible(connection, "app.actions", "action_id")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"):
            insert(connection, "app.approvals", STRANGER)


def test_an_approval_naming_another_approver_is_refused(
    database: DatabaseSettings,
) -> None:
    with (
        acting_as(database, APPROVER, "approver") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        insert(connection, "app.approvals", STRANGER)


def test_an_approval_of_an_action_the_approver_cannot_see_is_refused(
    database: DatabaseSettings,
) -> None:
    """The action's case is still in review, outside the approver's statuses."""
    with (
        acting_as(database, APPROVER, "approver") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        connection.execute(
            "INSERT INTO app.approvals (action_id, idempotency_key, approver_oid,"
            " preparer_oid, decision) VALUES (%s, 'unseen', %s, %s, 'approve')",
            (IN_REVIEW_ACTION, APPROVER, PREPARER),
        )


def test_an_audit_event_naming_another_actor_is_refused(
    database: DatabaseSettings,
) -> None:
    with (
        acting_as(database, PREPARER, "preparer") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        connection.execute(
            "INSERT INTO app.audit_events (case_id, actor_oid) VALUES (%s, %s)",
            (OWN_CASE, STRANGER),
        )


def test_an_audit_event_on_a_case_the_actor_cannot_see_is_refused(
    database: DatabaseSettings,
) -> None:
    with (
        acting_as(database, PREPARER, "preparer") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        connection.execute(
            "INSERT INTO app.audit_events (case_id, actor_oid) VALUES (%s, %s)",
            (OTHER_CASE, PREPARER),
        )


def test_an_action_by_a_principal_without_the_preparer_role_is_refused(
    database: DatabaseSettings,
) -> None:
    """The principal prepared the case and sees it, so only the missing role refuses."""
    with (
        acting_as(database, PREPARER, "restricted_viewer") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        insert(connection, "app.actions", PREPARER)


def test_an_action_on_another_preparers_case_is_refused(
    database: DatabaseSettings,
) -> None:
    """The principal sees the case through restricted_viewer; only ownership refuses."""
    with (
        acting_as(database, PREPARER, "preparer", "restricted_viewer") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        connection.execute(
            "INSERT INTO app.actions (case_id, preparer_oid, payload)"
            " VALUES (%s, %s, '{}')",
            (OTHER_CASE, OTHER_PREPARER),
        )


def test_a_case_naming_another_preparer_is_refused(
    database: DatabaseSettings,
) -> None:
    with (
        acting_as(database, PREPARER, "preparer") as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level"),
    ):
        insert(connection, "app.cases", OTHER_PREPARER)


def test_an_approval_by_the_preparer_is_refused(database: DatabaseSettings) -> None:
    """Holding both roles, the principal passes the policy; the CHECK refuses."""
    with (
        acting_as(database, PREPARER, "preparer", "approver") as connection,
        pytest.raises(psycopg.errors.CheckViolation, match="approver_is_not_preparer"),
    ):
        insert(connection, "app.approvals", PREPARER)


def test_an_approval_without_a_preparer_is_refused(
    database: DatabaseSettings,
) -> None:
    with (
        acting_as(database, PREPARER, "preparer", "approver") as connection,
        pytest.raises(psycopg.errors.NotNullViolation, match="preparer_oid"),
    ):
        connection.execute(
            "INSERT INTO app.approvals (action_id, idempotency_key, approver_oid,"
            " preparer_oid, decision) VALUES (%s, 'no-preparer', %s, NULL, 'approve')",
            (WAITING_ACTION, PREPARER),
        )


def test_an_action_naming_another_preparer_is_refused(
    database: DatabaseSettings,
) -> None:
    with (
        acting_as(database, PREPARER, "preparer", "approver") as connection,
        pytest.raises(psycopg.errors.ForeignKeyViolation, match="preparer_oid"),
    ):
        connection.execute(
            "INSERT INTO app.actions (case_id, preparer_oid, payload)"
            " VALUES (%s, %s, '{}')",
            (OWN_CASE, OTHER_PREPARER),
        )


def test_an_approval_naming_another_preparer_is_refused(
    database: DatabaseSettings,
) -> None:
    with (
        acting_as(database, PREPARER, "preparer", "approver") as connection,
        pytest.raises(psycopg.errors.ForeignKeyViolation, match="preparer_oid"),
    ):
        connection.execute(
            "INSERT INTO app.approvals (action_id, idempotency_key, approver_oid,"
            " preparer_oid, decision) VALUES (%s, 'forged', %s, %s, 'approve')",
            (WAITING_ACTION, PREPARER, OTHER_PREPARER),
        )


def test_a_second_approval_of_the_same_action_is_refused(
    database: DatabaseSettings,
) -> None:
    with acting_as(database, APPROVER, "approver") as connection:
        insert(connection, "app.approvals", APPROVER)
        with pytest.raises(psycopg.errors.UniqueViolation, match="action_id"):
            insert(connection, "app.approvals", APPROVER)


def test_a_repeated_approval_key_is_refused(database: DatabaseSettings) -> None:
    with (
        acting_as(database, APPROVER, "approver") as connection,
        pytest.raises(psycopg.errors.UniqueViolation, match="idempotency_key"),
    ):
        connection.execute(
            "INSERT INTO app.approvals (action_id, idempotency_key, approver_oid,"
            " preparer_oid, decision) VALUES (%s, %s, %s, %s, 'approve')",
            (WAITING_ACTION, SEEDED_KEY, APPROVER, PREPARER),
        )


def test_a_repeated_ledger_idempotency_key_is_refused(
    runner: DatabaseSettings,
) -> None:
    with connect(runner) as connection:
        connection.execute(
            "INSERT INTO bank.ledger_entries (idempotency_key) VALUES ('posted')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation, match="idempotency_key"):
            connection.execute(
                "INSERT INTO bank.ledger_entries (idempotency_key) VALUES ('posted')"
            )
        connection.rollback()
