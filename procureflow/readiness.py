"""Dependency checks for the manual procurement workflow."""

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .db import Base, Database


def database_available(database: Database) -> bool:
    """Check the connection and mapped tables without returning database details."""
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            # LIMIT 0 checks the mapped tables and columns without reading business rows.
            for table in Base.metadata.sorted_tables:
                connection.execute(select(table).limit(0))
    except SQLAlchemyError:
        return False
    return True
