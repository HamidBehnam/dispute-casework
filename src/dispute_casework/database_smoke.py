"""One connection per tier to the database named by DATABASE_URL, role bound at connect.

Prints, for each tier, the login, the bound role and the chunks it can see.
"""

import asyncio

from dispute_casework.database import TIERS, DatabaseSettings, async_connection


async def check(settings: DatabaseSettings, tier: str) -> str:
    connection_class, keywords = async_connection(settings)
    async with await connection_class.connect(
        settings.url.get_secret_value(), options=f"-c role={tier}", **keywords
    ) as connection:
        cursor = await connection.execute(
            "SELECT session_user, current_user, (SELECT count(*) FROM corpus.chunks)"
        )
        row = await cursor.fetchone()
    assert row is not None
    login, role, chunks = row
    return f"login {login}; role {role}; {chunks} chunks visible"


def main() -> None:
    settings = DatabaseSettings()
    for tier in TIERS:
        print(f"{tier}: {asyncio.run(check(settings, tier))}")


if __name__ == "__main__":
    main()
