"""roles and schemas

Revision ID: 0001
Revises:
Create Date: 2026-10-15 01:19:41.358773

"""

from collections.abc import Callable
from typing import Any

from alembic import op
from langchain_postgres import PGEngine
from psycopg import Connection

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade(
    make_engine: Callable[[], PGEngine], connect: Callable[..., Connection[Any]]
) -> None:
    op.execute("CREATE ROLE corpus_loader NOLOGIN")
    op.execute("CREATE ROLE tier_public NOLOGIN")
    op.execute("CREATE ROLE tier_full NOLOGIN")
    op.execute("CREATE ROLE lg_app NOLOGIN")
    op.execute("CREATE ROLE corpus_owner NOLOGIN")
    op.execute("CREATE ROLE lg_owner NOLOGIN")
    op.execute("CREATE ROLE bank_owner NOLOGIN")
    op.execute("CREATE ROLE app_owner NOLOGIN")
    op.execute("CREATE ROLE dcw_service NOLOGIN")
    # CREATE ROLE leaves its creator with ADMIN OPTION only; the owners are
    # granted again so that the runner inherits them and can set them.
    op.execute("GRANT corpus_owner, lg_owner, bank_owner, app_owner TO CURRENT_USER")
    # A login holding dcw_service has no privilege until it sets one of these
    # roles at connect, and can set no other.
    op.execute("GRANT tier_public, tier_full, lg_app TO dcw_service WITH INHERIT FALSE")
    op.execute("CREATE SCHEMA corpus AUTHORIZATION corpus_owner")
    op.execute("CREATE SCHEMA lg AUTHORIZATION lg_owner")
    op.execute("CREATE SCHEMA bank AUTHORIZATION bank_owner")
    op.execute("CREATE SCHEMA app AUTHORIZATION app_owner")
    op.execute("GRANT USAGE ON SCHEMA lg TO lg_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE lg_owner IN SCHEMA lg"
        " GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lg_app"
    )
