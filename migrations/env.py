from functools import partial

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from dispute_casework.database import DatabaseSettings, connect, engine

settings = DatabaseSettings()
runner = create_engine(
    "postgresql+psycopg://", creator=partial(connect, settings), poolclass=NullPool
)
with runner.connect() as connection:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations(
            make_engine=partial(engine, settings, pool_size=1, max_overflow=0),
            connect=partial(connect, settings),
        )
