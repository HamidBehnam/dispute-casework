"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from collections.abc import Callable
from typing import Any

from alembic import op
from langchain_postgres import PGEngine
from psycopg import Connection
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade(
    make_engine: Callable[[], PGEngine], connect: Callable[..., Connection[Any]]
) -> None:
    ${upgrades if upgrades else "pass"}
