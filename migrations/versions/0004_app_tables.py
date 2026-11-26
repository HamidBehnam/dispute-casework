"""app tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-26 12:00:00.000000

"""

from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from alembic import op
from langchain_postgres import PGEngine
from psycopg import Connection
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

CASE_STATES = (
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
ACTION_STATES = ("proposed", "approved", "returned", "rejected", "executed")
DECISIONS = ("approve", "return", "reject")
APPROVER_STATES = CASE_STATES[CASE_STATES.index("awaiting_approval") :]

# The principal and its roles come from transaction-scoped settings. A missing
# or empty setting reads as NULL: no comparison with the principal holds, and
# no role is held.
PRINCIPAL = "NULLIF(current_setting('app.oid', true), '')::uuid"


def holds(role: str) -> str:
    return (
        f"'{role}' = ANY(string_to_array("
        "NULLIF(current_setting('app.roles', true), ''), ' '))"
    )


def one_of(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def primary_key(name: str) -> sa.Column[Any]:
    return sa.Column(
        name,
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def oid(name: str) -> sa.Column[Any]:
    return sa.Column(name, UUID(as_uuid=True), nullable=False)


def upgrade(
    make_engine: Callable[[], PGEngine], connect: Callable[..., Connection[Any]]
) -> None:
    op.execute("CREATE ROLE app_rw NOLOGIN")
    op.execute("GRANT app_rw TO dcw_service WITH INHERIT FALSE")

    op.create_table(
        "cases",
        primary_key("case_id"),
        oid("preparer_oid"),
        sa.Column("status", sa.Text, nullable=False, server_default="intake"),
        sa.CheckConstraint(f"status IN ({one_of(CASE_STATES)})", name="case_status"),
        # The target of the preparer copy in actions.
        sa.UniqueConstraint("case_id", "preparer_oid"),
        schema="app",
    )
    op.create_table(
        "actions",
        primary_key("action_id"),
        sa.Column("case_id", UUID(as_uuid=True), nullable=False),
        oid("preparer_oid"),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="proposed"),
        sa.CheckConstraint(
            f"status IN ({one_of(ACTION_STATES)})", name="action_status"
        ),
        sa.ForeignKeyConstraint(
            ["case_id", "preparer_oid"], ["app.cases.case_id", "app.cases.preparer_oid"]
        ),
        # The target of the preparer copy in approvals.
        sa.UniqueConstraint("action_id", "preparer_oid"),
        schema="app",
    )
    op.create_table(
        "approvals",
        primary_key("approval_id"),
        sa.Column("action_id", UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        oid("approver_oid"),
        oid("preparer_oid"),
        sa.Column("decision", sa.Text, nullable=False),
        sa.CheckConstraint(
            f"decision IN ({one_of(DECISIONS)})", name="approval_decision"
        ),
        sa.CheckConstraint(
            "approver_oid <> preparer_oid", name="approver_is_not_preparer"
        ),
        sa.ForeignKeyConstraint(
            ["action_id", "preparer_oid"],
            ["app.actions.action_id", "app.actions.preparer_oid"],
        ),
        schema="app",
    )
    op.create_table(
        "audit_events",
        primary_key("event_id"),
        sa.Column(
            "case_id",
            UUID(as_uuid=True),
            sa.ForeignKey("app.cases.case_id"),
            nullable=False,
        ),
        oid("actor_oid"),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="app",
    )
    op.create_table(
        "ledger_entries",
        primary_key("entry_id"),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        schema="bank",
    )

    for table in ("cases", "actions", "approvals", "audit_events"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} OWNER TO app_owner")
    op.execute("ALTER TABLE bank.ledger_entries OWNER TO bank_owner")

    visible_case = "EXISTS (SELECT 1 FROM app.cases c WHERE c.case_id = {}.case_id)"
    op.execute(
        "CREATE POLICY cases_select ON app.cases FOR SELECT TO app_rw USING ("
        f"({holds('preparer')} AND preparer_oid = {PRINCIPAL})"
        f" OR ({holds('approver')} AND status IN ({one_of(APPROVER_STATES)})"
        f" AND preparer_oid <> {PRINCIPAL})"
        f" OR {holds('restricted_viewer')})"
    )
    op.execute(
        "CREATE POLICY cases_insert ON app.cases FOR INSERT TO app_rw WITH CHECK ("
        f"{holds('preparer')} AND preparer_oid = {PRINCIPAL})"
    )
    op.execute(
        "CREATE POLICY actions_select ON app.actions FOR SELECT TO app_rw"
        f" USING ({visible_case.format('actions')})"
    )
    op.execute(
        "CREATE POLICY actions_insert ON app.actions FOR INSERT TO app_rw WITH CHECK ("
        f"{holds('preparer')} AND EXISTS (SELECT 1 FROM app.cases c"
        f" WHERE c.case_id = actions.case_id AND c.preparer_oid = {PRINCIPAL}))"
    )
    op.execute(
        "CREATE POLICY approvals_select ON app.approvals FOR SELECT TO app_rw USING ("
        "EXISTS (SELECT 1 FROM app.actions a WHERE a.action_id = approvals.action_id))"
    )
    op.execute(
        "CREATE POLICY approvals_insert ON app.approvals FOR INSERT TO app_rw"
        f" WITH CHECK ({holds('approver')} AND approver_oid = {PRINCIPAL}"
        " AND EXISTS (SELECT 1 FROM app.actions a"
        " WHERE a.action_id = approvals.action_id))"
    )
    op.execute(
        "CREATE POLICY audit_events_select ON app.audit_events FOR SELECT TO app_rw"
        f" USING ({visible_case.format('audit_events')})"
    )
    op.execute(
        "CREATE POLICY audit_events_insert ON app.audit_events FOR INSERT TO app_rw"
        f" WITH CHECK (actor_oid = {PRINCIPAL}"
        f" AND ({holds('preparer')} OR {holds('approver')})"
        f" AND {visible_case.format('audit_events')})"
    )

    op.execute("GRANT USAGE ON SCHEMA app TO app_rw")
    op.execute(
        "GRANT SELECT ON app.cases, app.actions, app.approvals, app.audit_events"
        " TO app_rw"
    )
    op.execute("GRANT INSERT (case_id, preparer_oid) ON app.cases TO app_rw")
    op.execute(
        "GRANT INSERT (action_id, case_id, preparer_oid, payload)"
        " ON app.actions TO app_rw"
    )
    op.execute(
        "GRANT INSERT (approval_id, action_id, idempotency_key, approver_oid,"
        " preparer_oid, decision) ON app.approvals TO app_rw"
    )
    op.execute("GRANT INSERT (case_id, actor_oid) ON app.audit_events TO app_rw")
