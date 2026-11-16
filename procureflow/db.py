from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        parsed_url = make_url(url)
        options = {"pool_pre_ping": True}
        if parsed_url.get_backend_name() == "mysql" and parsed_url.get_driver_name() == "pymysql":
            # A dependency outage should not leave readiness checks waiting indefinitely.
            options["connect_args"] = {"connect_timeout": 2, "read_timeout": 2, "write_timeout": 2}
            options["pool_timeout"] = 3
        if parsed_url.get_backend_name() == "sqlite":
            options["connect_args"] = {"check_same_thread": False}
            if parsed_url.database in (None, "", ":memory:"):
                options["poolclass"] = StaticPool
        self.engine = create_engine(parsed_url, **options)
        if parsed_url.get_backend_name() == "sqlite":
            event.listen(self.engine, "connect", self._setup_sqlite)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _setup_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.sessions.begin() as session:
            yield session

    def close(self) -> None:
        self.engine.dispose()
